# CorridorIQ — Database Operations Runbook

Covers where the operational database lives, how it is backed up, how to verify
it, and how to restore it.

---

## 1. Where the database lives

```
C:\CorridorIQData\
├── db\
│   └── corridoriq.db          <- operational database (WAL mode)
└── backups\
    ├── corridoriq_<UTC>.db    <- automatic, rotated
    └── pre_migration_*.db     <- manual snapshots, NEVER auto-deleted
```

It deliberately sits **outside OneDrive and outside the repo**. A
multi-hundred-MB SQLite file under continuous cloud sync, with a scheduled
writer and a long-lived server process, risks partial-file sync and WAL
divergence. WAL in particular is unsafe on synced or network filesystems.

### Changing the location

Resolution order, highest priority first:

| Variable | Meaning |
| --- | --- |
| `CORRIDORIQ_DB_PATH` | Full path to the `.db` file |
| `CORRIDORIQ_DATA_DIR` | Data root; database at `<root>/db/corridoriq.db` |
| *(default)* | `C:\CorridorIQData` on Windows, `~/.local/share/corridoriq` elsewhere |

Set persistently for the account that runs the scheduled tasks:

```powershell
setx CORRIDORIQ_DATA_DIR "D:\CorridorIQData"
```

> If the configured database is missing but an old in-repo one still exists,
> the pipeline **refuses to start** rather than silently creating an empty
> database. That failure is a safety feature; read the message it prints.

---

## 2. Health check

```powershell
python -m pipeline.db.doctor
```

Verifies location, cloud-sync exposure, WAL, `foreign_keys`, `busy_timeout`,
`integrity_check`, `foreign_key_check`, row counts, RAW capture state, and the
newest backup. It also **exercises the writer** with a real transaction — a
database that reports WAL but cannot commit is still broken.

Exit code `0` = all checks passed. `1` = at least one FAIL.

Expected healthy output ends with:

```
 All checks passed (0 warning(s))
```

---

## 3. Backups

### Manual

```powershell
python -m pipeline.db.backup             # create + verify + prune
python -m pipeline.db.backup --list
python -m pipeline.db.backup --verify-latest
```

Backups use `VACUUM INTO`, which produces a consistent, defragmented copy while
the database stays online — safe to run alongside the API server or a pipeline
run, unlike a plain file copy which can capture a torn page mid-write.

Every backup is integrity-checked and row-counted immediately after it is
written. **A copy that fails verification is deleted rather than kept**, because
an unverified backup that only fails during a restore is worse than no backup.

### Retention

Configured in `pipeline/config/settings.py`:

- `DB_BACKUP_KEEP_DAILY = 14` — newest backup of each of the last 14 days
- `DB_BACKUP_KEEP_WEEKLY = 8` — newest backup of each of the last 8 ISO weeks

Files named `pre_migration_*` are **never** touched by retention.

### Scheduling (not yet registered)

Run daily, at a time that does not overlap the morning refresh (05:00) or the
daily pipeline (06:00). 03:45 gives a known-good pre-run snapshot:

```powershell
$Root   = "C:\Users\dezna\OneDrive\Desktop\CorridorIQ"
$Script = Join-Path $Root "scripts\backup_database.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At 3:45AM

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 20) -RestartCount 2 `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "CorridorIQ Database Backup" `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Verified daily SQLite backup with daily/weekly retention."
```

Logs land in `logs\backup\backup_<timestamp>.log`.

> **A backup you have never restored is a hypothesis.** Do a restore drill
> (section 5) once, now, and then once a quarter.

---

## 4. Relocating the database

```powershell
python scripts\migrate_database_location.py --dry-run   # inspect first
python scripts\migrate_database_location.py
```

The move is verified before anything is given up: integrity-check the source,
record a manifest of every table and row count, copy via `VACUUM INTO`,
integrity-check the copy, compare the manifest row for row, and only then enable
WAL and retire the original. The original is **moved into `backups\`, never
deleted**.

If the original cannot be retired because another process holds it open, the new
database is already live and verified; free the file and run:

```powershell
python scripts\migrate_database_location.py --retire-only
```

---

## 5. Restore drill

1. Stop the API server and disable the scheduled tasks.
2. Pick a backup and verify it **before** trusting it:

```powershell
python -m pipeline.db.backup --list
python -m pipeline.db.backup --verify-latest
```

3. Move the current database aside — do not delete it:

```powershell
Move-Item C:\CorridorIQData\db\corridoriq.db `
          C:\CorridorIQData\backups\before_restore_corridoriq.db
```

4. Copy the chosen backup into place:

```powershell
Copy-Item C:\CorridorIQData\backups\corridoriq_<UTC>.db `
          C:\CorridorIQData\db\corridoriq.db
```

5. Verify, then re-enable the schedules:

```powershell
python -m pipeline.db.doctor
```

WAL is re-applied automatically on the first connection, so a backup restored
from a non-WAL copy self-heals.

---

## 6. Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `RuntimeError` about a legacy in-repo database | `CORRIDORIQ_DB_PATH` points somewhere with no database | Fix the variable, or run the relocation script |
| `database is locked` | A long write overlapping another writer | Check for overlapping scheduled tasks; `busy_timeout` is 10s |
| Doctor reports `cloud sync: FAIL` | Database is inside a synced folder | Relocate it (section 4) |
| Doctor reports `journal_mode: delete` | Restored from a non-WAL copy | Reconnect once; WAL is re-applied automatically |
| `WinError 32` while relocating | Another process holds the file open | Close the API server, then `--retire-only` |
