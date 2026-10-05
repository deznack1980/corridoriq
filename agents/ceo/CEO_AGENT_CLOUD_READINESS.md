# CEO agent cloud readiness

Status: architecture can move. This assignment does not provision a host.

## Current architecture

The agent is a Python package, `agents.ceo`, run as `python -m agents.ceo`. Logic, packaged state, and runtime output are separate:

| Layer | Where it lives now | Cloud note |
| --- | --- | --- |
| Agent logic | `agents/ceo/*.py` | Portable. No Windows path in this package |
| Persistent company state | `agents/ceo/company/*.json` | Override with `CEO_STATE_DIR` |
| Runtime memory | `reports/generated/ceo/` via `CEO_OUTPUT_DIR` | Put this on a volume, not the laptop disk |
| Scheduler | None | Morning and brief runs are manual CLI |
| Tools | Read-only SQLite, local report files | Point `CORRIDORIQ_DB_PATH` at a replica |
| Data access | `mode=ro` and `PRAGMA query_only` | Do not give the agent the write connection |
| Notifications | None | A future notifier must pass the approval gate |
| Approval queue | `approval.py` returns `execute: false` | No external action is performed |
| UI / voice | None | Jarvis calls `agents.ceo.jarvis` later |

## What still assumes a laptop

- Someone has to start the process. Nothing runs while the machine is off.
- The intelligence database default in pipeline settings is a local data directory, often `C:\CorridorIQData` on the founder's PC. The CEO package does not hard-code that path; the pipeline setting does.
- Generated reports the snapshot reads live in the repo's `reports/generated/`, which is gitignored and local.
- The contractor pilot root `CORRIDORIQ_PILOT_ROOT` is an operator path, documented with a Windows example. The CEO agent does not open it.
- There is no mail, Slack, or SMS path, so a cloud run would still be silent until a gated notifier exists.
- `python -m agents.ceo morning` uses the Phoenix calendar. That uses the standard library and is portable. The command itself is interactive only in the sense that a human runs it.

## Required persistent storage

- Company JSON in git, or a copy addressed by `CEO_STATE_DIR`
- Append-only decision log and briefs under `CEO_OUTPUT_DIR`
- A read-only copy or replica of the intelligence SQLite file
- Pilot databases stay out of the CEO process until a later read-only adapter exists

## Required secrets

None are required for a deterministic brief. Optional narration uses `CEO_MODEL_API_KEY` from the environment only. Do not bake keys into images. The agent redacts key-shaped strings in briefs.

## Scheduled jobs

None inside the agent. A future host can cron `python -m agents.ceo brief` and `python -m agents.ceo morning`. Those commands must keep `execute` false.

## Network access

A default run needs no network. Optional model narration needs outbound HTTPS to the configured provider. Production CorridorIQ and DNS are not called.

## Production data dependencies

Read-only intelligence database when `connect_db` is true. Generated sales-trial, lane, and ROC reports. Company JSON. The agent must not receive a writable production DSN.

## Notification and approval requirements

Any customer outreach, email, SMS, call, social post, price, contract, payment, DNS, Cloudflare, or deploy must remain a founder approval. The cloud process may write a proposal file. It may not send.

## Recommended deployment

A small always-on Linux VM or container. One vCPU, 1 GB RAM, and a few GB of disk are enough for SQLite reads and JSON briefs. No GPU.

1. Install the repo at a release commit.
2. Mount `CEO_OUTPUT_DIR` on persistent disk.
3. Mount a read-only snapshot of the intelligence database and set `CORRIDORIQ_DB_PATH`.
4. Run cron for `brief` and `morning`.
5. Leave narration off.
6. Deliver the brief file to the founder through a channel that cannot itself email customers.
7. Keep production write credentials off the box.

## Migration steps

1. Confirm `CEO_STATE_DIR` and `CEO_OUTPUT_DIR` overrides in a temp directory (covered by tests).
2. Copy company JSON with the repo. Do not fork a second agent.
3. Publish a read-only database snapshot on a schedule from the existing pipeline host. Do not point the agent at the live writable file.
4. Run `python -m agents.ceo eval` on the host.
5. Add cron only after one manual brief looks right.
6. Add notifications only behind the approval gate.

## Security

- Read-only database URI
- No production writes, DNS, or customer contact from this process
- Secrets in the environment, not in git
- Briefs are internal. `customer_safe_output()` raises
- Do not expose `reports/generated/ceo/` publicly; it can contain operating detail

## Not done here

No host was purchased or provisioned. The laptop can still be the only place the agent runs until the steps above are approved.
