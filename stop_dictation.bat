@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
echo Stopping all Odicto instances...

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM 1) PID file (fast path). Safe if the process is already gone.
if exist dictation.pid (
  set /p PID=<dictation.pid
  echo   PID file points to: !PID!
  REM Only kill if the PID is actually a python/pythonw running THIS install.
  REM A stale pid file can point at an unrelated process after Windows reuses the PID.
  REM Do not name the PowerShell variable $pid - that is reserved (current process).
  REM taskkill /T kills venv launcher stub + real interpreter child together.
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$targetPid = [int]'!PID!'; " ^
    "$root = [System.IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); " ^
    "$p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue; " ^
    "if ($null -eq $p) { Write-Output '  (PID !PID! already gone)'; exit 0 }; " ^
    "if ($p.ProcessName -ne 'python' -and $p.ProcessName -ne 'pythonw') { Write-Output '  (PID !PID! is not python/pythonw - left alone)'; exit 0 }; " ^
    "$okPath = $false; " ^
    "try { if ($p.Path -and $p.Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $okPath = $true } } catch {}; " ^
    "if (-not $okPath) { " ^
    "  $c = (Get-CimInstance Win32_Process -Filter \"ProcessId = $targetPid\" -ErrorAction SilentlyContinue).CommandLine; " ^
    "  if ($c -and $c.ToLowerInvariant().Contains($root.ToLowerInvariant()) -and ($c -match 'main\.py')) { $okPath = $true } " ^
    "}; " ^
    "if ($okPath) { " ^
    "  Start-Process -FilePath taskkill.exe -ArgumentList @('/F','/T','/PID',\"$targetPid\") -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue; " ^
    "  Write-Output '  Stopped PID !PID! (process tree)' " ^
    "} else { Write-Output '  (PID !PID! is not an Odicto process - left alone)' }"
  del dictation.pid >nul 2>&1
)

REM 2) Kill any remaining python/pythonw running THIS install's main.py.
REM    Filter at WMI (python* only). Conda/venv often shows TWO processes with the
REM    same command line (Scripts\pythonw launcher + base pythonw). Use taskkill /T.
REM
REM    BUG HISTORY: wmic CSV + "tokens=2 delims== " never extracted ProcessId, so
REM    orphan kill was a no-op and stacked hooks doubled every typed character.
REM
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$rootN = [System.IO.Path]::GetFullPath('%~dp0').TrimEnd('\').ToLowerInvariant(); " ^
  "$pids = @(Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" -ErrorAction SilentlyContinue | " ^
  "  Where-Object { $_.CommandLine -and ($_.CommandLine -match 'main\.py') -and ($_.CommandLine.ToLowerInvariant().Contains($rootN)) } | " ^
  "  ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique); " ^
  "foreach ($procId in $pids) { " ^
  "  Write-Host ('   Killing orphan PID ' + $procId + ' (tree)'); " ^
  "  Start-Process -FilePath taskkill.exe -ArgumentList @('/F','/T','/PID',\"$procId\") -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue " ^
  "}"

if exist dictation.pid del dictation.pid >nul 2>&1
if exist dictation.lock del dictation.lock >nul 2>&1
echo Done.

if /I "%~1"=="/nopause" exit /b 0
ping -n 2 127.0.0.1 >nul
exit /b 0
