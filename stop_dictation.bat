@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo Stopping all Odicto instances...

REM 1) PID file (fast path). Safe if the process is already gone.
if exist dictation.pid (
  set /p PID=<dictation.pid
  echo   PID file points to: %PID%
  REM Only kill if the PID is actually a python/pythonw running THIS install's main.py.
  REM A stale pid file can point at an unrelated process after Windows reuses the PID.
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$p = Get-Process -Id %PID% -ErrorAction SilentlyContinue; " ^
    "if ($p -and ($p.ProcessName -eq 'python' -or $p.ProcessName -eq 'pythonw') -and " ^
    "($p.Path -like '%~dp0*')) { taskkill /F /T /PID %PID% | Out-Null; '  Stopped PID %PID%' } " ^
    "else { '  (PID %PID% is not an Odicto process - left alone)' }"
  del dictation.pid >nul 2>&1
)

REM 2) Kill any remaining python/pythonw running THIS install's main.py.
REM    Seeing a second PID here means a real orphan was still running (good that we kill it).
REM    Seeing only step 1 is normal for a clean single instance.
REM
REM    BUG HISTORY: the previous loop was:
REM      for /f "tokens=2 delims== " %%P in ('wmic ... /format:csv ^| findstr main.py')
REM    WMIC CSV looks like:  Node,CommandLine,ProcessId
REM    With delims== and space, token 2 is a fragment of CommandLine — never the PID.
REM    So orphan kill was a no-op. Starting again left the old process alive → two
REM    system-wide keyboard hooks → every letter typed twice.
REM
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$rootN = [System.IO.Path]::GetFullPath('%~dp0').TrimEnd('\').ToLowerInvariant(); " ^
  "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | " ^
  "Where-Object { " ^
  "  ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and " ^
  "  $_.CommandLine -and " ^
  "  ($_.CommandLine -match 'main\.py') -and " ^
  "  ($_.CommandLine.ToLowerInvariant().Contains($rootN)) " ^
  "} | ForEach-Object { " ^
  "  Write-Host ('   Killing orphan PID ' + $_.ProcessId); " ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue " ^
  "}"

if exist dictation.pid del dictation.pid >nul 2>&1
REM Lock file is released when the process dies; remove leftover name for clarity.
if exist dictation.lock del dictation.lock >nul 2>&1
echo Done.

if /I "%~1"=="/nopause" goto :eof
timeout /t 2 >nul
