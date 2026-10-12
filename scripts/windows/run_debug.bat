@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

REM Admin mode (.odicto-admin-enabled): the owner must run elevated, and an elevated process
REM cannot be started with a console attached to this normal window. So debug mode restarts
REM the background instance (no UAC prompt; odicto.py quits it through its quit event and runs
REM its logon task) and returns. The startup warnings and crash output of a console run are
REM not shown. This is decided BEFORE anything is stopped.
if not exist "%ODICTO_ROOT%\.odicto-admin-enabled" goto :normal_debug
echo Admin mode is on, so Odicto cannot run in this console.
echo Restarting the background instance instead; no console output will be shown.
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\odicto.py" restart
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
