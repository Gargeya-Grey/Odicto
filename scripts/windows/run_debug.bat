@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"
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
