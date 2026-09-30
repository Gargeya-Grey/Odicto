@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

if not exist "%ODICTO_ROOT%\.venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found.
  echo Run install.ps1 first.
  pause
  exit /b 1
)

echo Opening the Odicto setup page in your browser...
echo Close the window or press Ctrl+C here when you are done.
echo.
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\odicto.py" setup
exit /b %ERRORLEVEL%
