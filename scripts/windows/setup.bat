@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

if exist "%ODICTO_ROOT%\.odicto-migration.json" goto :managed_forward

if not exist "%ODICTO_ROOT%\.venv\Scripts\pythonw.exe" (
  echo ERROR: .venv\Scripts\pythonw.exe not found.
  echo Run install.ps1 first.
  pause
  exit /b 1
)

REM The setup page opens in the browser and its server ends by itself when the page is
REM closed. pythonw has no console, so this window closes at once.
start "" "%ODICTO_ROOT%\.venv\Scripts\pythonw.exe" "%ODICTO_ROOT%\odicto.py" setup
exit /b 0

:managed_forward
if not exist "%ODICTO_ROOT%\.venv\Scripts\python.exe" (
  echo This source installation was migrated. Use the installed odicto command. Source Python is missing.
  exit /b 1
)
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\odicto.py" setup
exit /b %ERRORLEVEL%
