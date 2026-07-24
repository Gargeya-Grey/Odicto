@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo Stopping all Odicto instances...

REM 1) PID file (fast path).
if exist dictation.pid (
  set /p PID=<dictation.pid
  echo   PID file: %PID%
  taskkill /F /T /PID %PID% >nul 2>&1
  del dictation.pid >nul 2>&1
)

REM 2) Kill every python/pythonw running THIS install's main.py.
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
