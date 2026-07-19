@echo off
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo ERROR: .venv\Scripts\pythonw.exe not found.
  echo Create the venv and install requirements first.
  pause
  exit /b 1
)

REM Stop a previous instance if still running
if exist dictation.pid (
  set /p OLD_PID=<dictation.pid
  taskkill /F /T /PID %OLD_PID% >nul 2>&1
  del dictation.pid >nul 2>&1
)

start "Odicto" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
exit
