@echo off
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo ERROR: .venv\Scripts\pythonw.exe not found.
  echo Create the venv and install requirements first.
  pause
  exit /b 1
)

REM Stop every previous instance (PID file + any leftover main.py for this folder).
call "%~dp0stop_dictation.bat" /nopause

REM Brief settle so Windows releases low-level keyboard hooks from the killed process
REM before the new instance installs its own (avoids a brief double-hook window).
timeout /t 1 /nobreak >nul

start "Odicto" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
exit
