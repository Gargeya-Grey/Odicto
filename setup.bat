@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found.
  echo Run install.ps1 first.
  pause
  exit /b 1
)

echo Opening the Odicto setup page in your browser...
echo Close the window or press Ctrl+C here when you are done.
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0odicto.py" setup
exit /b %ERRORLEVEL%
