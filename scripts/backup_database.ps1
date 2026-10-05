<#
.SYNOPSIS
    CorridorIQ verified database backup (data platform Phase 0).

.DESCRIPTION
    Creates a consistent backup of the operational database using SQLite's
    VACUUM INTO, integrity-checks the copy, and prunes expired backups to the
    configured daily/weekly retention. Safe to run while the API server or a
    pipeline run is active - VACUUM INTO does not require exclusive access, and
    the database is in WAL mode.

    Schedule this DAILY, and at a time that does not overlap the morning
    refresh, so a backup and a heavy write pass are not competing.

    SECURITY: no credentials are stored here. The database location comes from
    CORRIDORIQ_DB_PATH / CORRIDORIQ_DATA_DIR, or the platform default.

.NOTES
    Exit code 0 = backup written and verified. Non-zero = failure.
#>

$ErrorActionPreference = "Stop"

# 1. Resolve the project root (this script lives in <root>\scripts).
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# Python 3.14 via the launcher. Do not activate a project venv: the scheduled
# job used to pick up Python 3.12 from `python` on PATH.
$env:PYTHONUTF8 = "1"

# 3. Timestamped log file.
$LogDir = Join-Path $Root "logs\backup"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "backup_$Stamp.log"

"[$(Get-Date -Format o)] Starting CorridorIQ database backup" | Tee-Object -FilePath $LogFile
"[$(Get-Date -Format o)] Root: $Root" | Tee-Object -FilePath $LogFile -Append
$pyver = & py -3 -c "import sys; print(sys.version)"
"[$(Get-Date -Format o)] Python: $pyver" | Tee-Object -FilePath $LogFile -Append

# 4. Create + verify + prune.
& py -3 -m pipeline.db.backup *>&1 | Tee-Object -FilePath $LogFile -Append
$code = $LASTEXITCODE

"[$(Get-Date -Format o)] Finished with exit code $code" | Tee-Object -FilePath $LogFile -Append

exit $code
