# Scheduler cutover (prepared, not enabled)

Status: the failing daily task is **disabled**. The local backup task is still
the one that runs. No replacement task is registered. Do not enable a
replacement until `C:\CorridorIQ-prod` is the deployed release and the owner
has approved production cutover.

## What runs today

| Task | State | Interpreter | Code |
|---|---|---|---|
| CorridorIQ Daily Pipeline | Disabled 2026-10-05 | `python` = Python 3.12.10 | `C:\Users\dezna\OneDrive\Desktop\CorridorIQ\automation\run_daily.ps1` |
| CorridorIQ Database Backup | Ready, daily 03:45 local | `python` = Python 3.12.10 | `C:\Users\dezna\OneDrive\Desktop\CorridorIQ\scripts\backup_database.ps1` |

Both run as the interactive user `dezna`, `MultipleInstances = IgnoreNew`.
The daily task's last run (2026-10-05 06:00) exited 1 in about two seconds:
Python 3.12 evaluates the `KnowledgeEngine` annotation in
`pipeline/analysis/scoring.py` and raises `NameError`. Python 3.14.3 does not.
The backup task's last run succeeded locally and then skipped R2 because
Python 3.12 has no boto3. It does not migrate the database. `VACUUM INTO` is
a read of `C:\CorridorIQData\db\corridoriq.db`.

Do not point these tasks at `CorridorIQ-launch-integration` or the OneDrive
checkout. Those trees move. After cutover they should run from
`C:\CorridorIQ-prod` with `py -3` (Python 3.14.3).

## Replacement, leave disabled until cutover

Only one scheduler may write the production database. Disable the old task
before enabling the replacement. Do not enable both.

Daily pipeline, after the production checkout is the approved SHA:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\CorridorIQ-prod\automation\run_daily.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At 6:00am
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable
# Do not register this until production cutover is approved.
# Register-ScheduledTask -TaskName "CorridorIQ Daily Pipeline" -Action $action -Trigger $trigger -Settings $settings
```

The script must invoke `py -3`, not `python`, so the job stays on 3.14.3.

Database backup, same rule. Keep the current task until cutover so local
backups continue. At cutover, change its action to
`C:\CorridorIQ-prod\scripts\backup_database.ps1` and make that script call
`py -3 -m pipeline.db.backup`. Do not register a second backup task.

Off-machine replication still requires the R2 TLS gate in
`offsite_backup_r2.md`. Changing the interpreter does not fix the handshake.
