@echo off
cd /d "%~dp0"
echo Stopping all Odicto instances...

REM Kill the process from the PID file first.
if exist dictation.pid (
  set /p PID=<dictation.pid
  echo   PID file: %PID%
  taskkill /F /T /PID %PID% >nul 2>&1
  del dictation.pid >nul 2>&1
)

REM Find and kill any orphan python/pythonw processes running main.py from this folder.
REM Uses wmic (available on every Windows since Vista) — more reliable than PowerShell piping.
for /f "skip=1 tokens=2 delims== " %%P in (
  'wmic process where ^(name^="python.exe" or name^="pythonw.exe"^) get ProcessId^,CommandLine /format:csv 2^>nul ^| findstr "main.py"'
) do (
  echo   Killing zombie PID %%P
  taskkill /F /T /PID %%P >nul 2>&1
)

REM Cleanup any leftover PID file.
if exist dictation.pid del dictation.pid >nul 2>&1
echo Done.

if /I "%~1"=="/nopause" goto :eof
timeout /t 2 >nul
