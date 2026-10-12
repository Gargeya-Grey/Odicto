@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

REM Opt-in always-admin install (.odicto-admin-enabled): the owner must run elevated, and an
REM elevated process cannot be started with a console attached to this unelevated window. So
REM debug mode does one helper Start (UAC prompt, stop, elevated background start, wait ready)
REM and returns; the startup warnings and crash output of a console run are not shown. This is
REM decided BEFORE anything is stopped, so declining UAC leaves the running owner untouched.
if not exist "%ODICTO_ROOT%\.odicto-admin-enabled" goto :normal_debug
echo This install runs Odicto elevated, so it cannot run in this console.
echo Starting the elevated background instance instead; no console output will be shown.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%ODICTO_ROOT%\scripts\windows\start_dictation_admin.ps1" -Action Start
set "ODICTO_EXIT=%ERRORLEVEL%"
echo Exit code: %ODICTO_EXIT%
pause
exit /b %ODICTO_EXIT%

:normal_debug
echo Stopping any previous Odicto instances...
call "%~dp0stop_dictation.bat" /nopause
echo.
echo Running Odicto in DEBUG mode...
echo This console window will print any startup warnings or crash logs.
echo Keep this window open to test. Press Ctrl+C in this window to stop.
echo.
echo Python: %ODICTO_ROOT%\.venv\Scripts\python.exe
echo Expected hotkeys: Ctrl+` (dictation), Ctrl+Shift+` (AI)
echo.
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\main.py"
pause
