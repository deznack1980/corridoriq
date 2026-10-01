# Authority boundaries (v0.1)

The running CEO agent is read-only against CorridorIQ operations.

## Forbidden

- Modify production database records
- Modify `opportunity_score`
- Modify `customer_relevance_score`
- Modify `account_priority_score`
- Modify sales-lane assignments
- Create, update, or delete CRM relationships
- Merge companies
- Rewrite project foreign keys
- Modify permit or project data
- Enable ROC ranking consumption
- Send email
- Make phone calls
- Contact customers
- Make purchases
- Change Cloudflare or R2
- Deploy
- Merge branches
- Push git changes
- Run destructive shell commands
- Execute a Cursor brief on its own

The agent does not call `pipeline.db.database.get_connection`. Database reads, when they happen, use a read-only SQLite connection with `query_only`.

## Allowed writes

- `reports/generated/ceo/` snapshots, briefs, cursor briefs, and the append-only decision log
- Test and temp directories created for the agent

Source files under `agents/ceo/` are maintained by engineers in Cursor. The running agent does not rewrite its charter to match a new opinion.

## Output classes

- INTERNAL CEO OUTPUT: the only output authorized in v0.1
- CUSTOMER-SAFE OUTPUT: not authorized. The generator refuses.

## Control point

A Cursor brief is an instruction for a later, founder-approved Cursor session. Producing the brief is not permission to implement it.

## Future authority

A later version may request a specific action. That request needs its own approval phase. v0.1 does not grant it.
