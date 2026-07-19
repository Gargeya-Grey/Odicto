@echo off
cd /d "%~dp0"
if not exist dictation.pid goto :not_running

set /p PID=<dictation.pid
echo Stopping Odicto (PID %PID%)...
taskkill /F /T /PID %PID% >nul 2>&1
del dictation.pid
echo App stopped successfully.
goto :end

:not_running
echo Odicto is not running (no dictation.pid file found).

:end
timeout /t 3 >nul
