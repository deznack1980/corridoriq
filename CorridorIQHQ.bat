@echo off
cd /d "%~dp0"
set PORT=8780
set URL=http://127.0.0.1:%PORT%/login.html

REM Prefer the Windows py launcher, then python
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python not found on PATH. Install Python 3, then try again.
  pause
  exit /b 1
)

REM Local HQ is an intentional development process, not public production.
set "CORRIDORIQ_ENV=development"
REM Need the project root on PYTHONPATH so `pipeline.api.server` imports.
set "PYTHONPATH=%CD%"

REM If the secure portal API is already up, just open login.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/login.html' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -ge 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo Starting CorridorIQ secure portal on port %PORT%...
  start "CorridorIQ Portal" /min cmd /c "cd /d ""%~dp0"" && set PYTHONPATH=%CD% && set CORRIDORIQ_ENV=development && %PY% -m pipeline.api.server"
  REM Wait until login page responds (up to ~15s) so sign-in works on first open.
  powershell -NoProfile -Command "$ok=$false; for ($i=0; $i -lt 30; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/login.html' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -ge 200) { $ok=$true; break } } catch {} Start-Sleep -Milliseconds 500 }; if (-not $ok) { exit 1 }"
  if errorlevel 1 (
    echo.
    echo Could not start the portal on port %PORT%.
    echo Try manually:  %PY% -m pipeline.api.server
    echo Then open:     %URL%
    pause
    exit /b 1
  )
)

start "" "%URL%"
