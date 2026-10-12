@echo off
REM Start Odicto. Windows root entry point (the Start Menu "Odicto" shortcut runs this).
REM With no argument: show the result, close after about 3 seconds on success, and
REM stay open on failure. With an argument (/min, /nostartup, /restart): pass it on.
setlocal EnableExtensions
if not "%~1"=="" goto :passthrough

set "ODICTO_NO_PAUSE=1"
call "%~dp0scripts\windows\start_dictation.bat"
set "ODICTO_EXIT=%ERRORLEVEL%"
echo.
if "%ODICTO_EXIT%"=="0" goto :started
echo Odicto did not start ^(exit code %ODICTO_EXIT%^). Read the message above.
pause
exit /b %ODICTO_EXIT%

:started
echo Odicto is running. This window closes in 3 seconds.
ping -n 4 127.0.0.1 >nul
exit /b 0

REM Not inside a parenthesized block: %ERRORLEVEL% there is read before the call runs.
:passthrough
call "%~dp0scripts\windows\start_dictation.bat" %*
exit /b %ERRORLEVEL%
