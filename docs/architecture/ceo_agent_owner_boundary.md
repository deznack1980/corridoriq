# CEO Agent: owner-only boundary and path to a conversational CEO in HQ

Status: the authorization boundary is implemented. The conversational
experience is a design only; none of it is built.

## 1. The boundary (implemented)

| Piece | Where | Rule |
|---|---|---|
| Permission | `pipeline/auth/rbac.py` `owner.ceo_agent` | Member of `OWNER_ONLY_PERMISSIONS`. `admin.system` never implies it: it is excluded from the `admin` role, never added by `load_user_permissions`, and `has_permission` requires an exact grant. |
| Role | `owner` (Business Owner) | Grants only owner permissions. Additive to `admin`. |
| Grant | `python -m pipeline.auth.grant_owner --email … [--dry-run] [--revoke]` | Server shell only. The target must be an active `admin` in the `corridoriq` organization. Audited. The admin API can neither grant nor remove it. |
| Service check | `rbac.require_ceo_owner(conn, user)` | Exact grant **and** membership of CorridorIQ's own organization. |
| API | `GET /api/admin/ceo-morning-brief` | Calls `require_ceo_owner`. Returns 401 without a session and 403 for everyone else (audited). |
| Page | `ceo-morning-brief.html` / `.js` | Served only after the same check (`server._ceo_page_allowed`), decided on the resolved file. Hiding the menu item is a convenience, not the control. |
| Account protection | `crm/admin._guard_owner_account` | Only the owner can change the owner's account through the API (email, active flag, roles, names). |

**Rule for every future CEO surface:** each route calls `require_ceo_owner`
before it reads anything, each new page file is added to `_CEO_PAGES`, and a
test proves that `admin`, `sales_manager`, `sales_representative`,
`estimator`, `read_only`, `fulfillment_user` and `contractor_owner` all get
403, and that an unauthenticated request gets 401 or a redirect to login.
`pipeline/tests/test_ceo_owner_access.py` is the template for that test.

## 2. Current agent (what exists)

- `agents/ceo/` is a deterministic, read-only package. `decision_engine` makes
  one recommendation with `execute: false`. `approval.propose()` records that
  founder approval is needed, and `approval.execute()` always raises.
- `agents/ceo/analytics/` reads the intelligence database through
  `guard.open_analytics` (`mode=ro` plus `PRAGMA query_only`). Its tools
  (`tools.py`) are named, bounded SELECT functions. The owner brief is published
  after each successful refresh (`publish.py`) and served by `serve.py`.
- `agents/framework/provider.py` is the only model boundary. Narration is off by
  default and cannot change a locked recommendation.
- `agents/ceo/jarvis.py` is a stable intent contract (`morning_brief`,
  `blocking_revenue`, …). It is the natural entry point for a conversation layer.

## 3. Future conversational CEO in HQ (design)

### 3.1 Shape

```
HQ page (owner only) ──HTTPS──> /api/owner/ceo/* (require_ceo_owner on every call)
        │                               │
        │  text / SSE stream            ├─ conversation store (owner-only SQLite, separate file)
        │  later: audio in/out          ├─ CEO orchestrator (agents/ceo/conversation/)
        │                               │     ├─ model via agents/framework/provider (streaming)
        │                               │     └─ tool registry = read-only analytics + trust readers
        │                               └─ proposal queue (approval.propose; never executes)
```

### 3.2 Persistent text conversation

- Use new tables in a **separate owner-only database file** under `CEO_OUTPUT_DIR`,
  not the intelligence database, so the analytics connection stays read-only.
  The tables are `ceo_conversations` (id, owner_user_id, title, created_at),
  `ceo_messages` (conversation_id, role, text, claim_class, provenance_json,
  created_at), `ceo_tool_calls` (message_id, tool, args_json, row_count,
  as_of) and `ceo_proposals` (see 3.6).
- Every assistant message stores its provenance: tools called, `as_of`, and the
  refresh run id. Answers keep the existing claim classes
  (FACT/CALCULATION/INFERENCE/RECOMMENDATION/UNKNOWN).
- Retention: messages are owner data. Pass them through `security.redact`
  before storing, and never include them in customer-facing output.

### 3.3 Streaming answers

- `POST /api/owner/ceo/conversations/<id>/messages` creates the user message.
  `GET …/stream` answers with `text/event-stream` (SSE). SSE works through the
  current `ThreadingHTTPServer` with chunked writes; WebSockets are not needed.
- Add streaming to `provider.ModelProvider` behind the existing `CallLimiter`
  and clip limits. Use one active stream per owner, with a server-side timeout
  and an explicit "stopped" event.
- Tool calls run on the server before or during the stream. The model only
  receives tool results, marked as data (permit text is untrusted input).

### 3.4 Voice (later)

- Voice is an I/O adapter in front of the same text pipeline: browser
  `MediaRecorder`, then `POST /api/owner/ceo/voice` (speech-to-text), then the
  same message path. Speech output streams from the final text.
- It uses the same `require_ceo_owner` check. Audio is not kept by default;
  only the transcript is stored. Voice adds no new tool or permission.

### 3.5 Access to operating data: read-only by default

- Tools are the existing `agents/ceo/analytics/tools.py` functions, plus
  trust-layer readers (`trust.health.collect_health`, the daily queue,
  `trust.recency`). Every tool is a named function with bounded output. The
  model never writes raw SQL.
- Where possible, use a read-only replica or snapshot of the intelligence
  database (`CEO_AGENT_CLOUD_READINESS.md`). The process never receives a
  writable DSN.
- Answers about freshness follow the same rules as the portal. A stale feed or
  refresh is stated, never smoothed over.
- Organization customer books are only read with an explicit organization
  scope (as `answer_question` already requires).

### 3.6 Approval gates for production changes

- The model can only **propose**. `approval.propose(action)` writes a
  `ceo_proposals` row (action, rationale, evidence refs, `status=pending`,
  `execute=false`).
- Approving a proposal is a separate owner action:
  `POST /api/owner/ceo/proposals/<id>/approve`. It requires step-up
  re-authentication (password re-entry), is audited, and in the first version
  **still executes nothing**. It records the decision and creates a human task.
- Before anything executes automatically, each action type gets its own
  reviewed executor, its own test, and its own entry in
  `company/approval_gates.json`. `approval.execute()` keeps raising for every
  other action. Deploys, database writes, DNS, outreach, pricing and payments
  stay founder-approved actions performed by people or existing pipelines.

### 3.7 Kept separate from any customer-facing assistant

| | CEO Agent | Future customer assistant |
|---|---|---|
| Users | Business owner only | Supplier or contractor users, within their own organization |
| Permission | `owner.ceo_agent` (owner-only, exact grant) | A tenant permission such as `assistant.use`, never `owner.*` |
| Routes | `/api/owner/ceo/*` | `/api/assistant/*` (or `/api/pilot/assistant/*` for contractors) |
| Data | Cross-tenant operating data, company strategy, CEO memory | Only that tenant's records, through tenant-scoped services |
| Tools | `agents/ceo/analytics` and the trust readers | A separate registry; never imports `agents.ceo` |
| Storage | Owner conversation database | A tenant-scoped store |
| Output | Internal only (`security.customer_safe_output()` raises) | Customer-safe serializers only |

Enforcement once the customer assistant exists:

- An import-boundary test fails if the customer assistant package imports
  `agents.ceo`.
- `require_ceo_owner` is never used on customer routes, and tenant permissions
  are never accepted on `/api/owner/*`.
- The two have separate model budgets and prompts.

### 3.8 Rollout order

1. Owner boundary (this change). Run the owner grant in production.
2. Read-only conversation over `jarvis.INTENTS` and the analytics tools, as
   plain request/response with no model narration.
3. Streaming, with the model given tool results only.
4. A proposal queue that records decisions without executing them.
5. Voice adapter.
6. Per-action executors, one at a time, each separately approved.
