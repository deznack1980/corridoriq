# CorridorIQ CEO agent v0.2

The v0.2 agent is the existing read-only CEO agent, plus durable company state. It was not rebuilt.

## What it answers

From evidence in `company/` and the operating snapshot:

- What should CorridorIQ do today, and this week?
- What is blocking revenue?
- What did we learn from customers, and who needs follow-up?
- What is live, early access, or planned?
- What should the founder approve next?
- What changed since the previous snapshot?
- Are we about to spend engineering on something that does not advance revenue or validation?

It does not answer those with generic startup advice. Missing measurements stay `UNKNOWN`.

## Layout

| Piece | Role |
| --- | --- |
| `company/` | Versioned constitution, maturity, stakeholders, KPIs, pricing hypotheses, launch, priorities, approval gates |
| `decision_engine/` | One recommendation. `execute` is always false |
| `morning/` | Read-only call queue. Still manual |
| `briefs/` | Executive brief plus the decision detail |
| `jarvis.py` | Future control-plane contract. Jarvis is not coupled |
| `approval.py` | Gate check. `execute()` raises |
| `operating_state/` | Read-only SQLite and generated reports |
| `reports/generated/ceo/` | The only runtime writes |

## Commands

```text
python -m agents.ceo status
python -m agents.ceo brief
python -m agents.ceo ask "What is blocking revenue?"
python -m agents.ceo morning
python -m agents.ceo eval
```

`CEO_OUTPUT_DIR` and `CEO_STATE_DIR` override locations. See `CEO_AGENT_CLOUD_READINESS.md`.

## Safety

No production writes, no customer contact, no DNS, no spend, no public price. A Cursor brief is not permission to implement it.
