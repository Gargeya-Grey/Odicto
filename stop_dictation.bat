@echo off
REM Stop Odicto. Windows root entry point (the Start Menu "Stop Odicto" shortcut runs this).
REM With no argument: show the result, close after about 3 seconds on success, and
REM stay open on failure. With an argument: pass it on and return its exit code.
setlocal EnableExtensions
if not "%~1"=="" goto :passthrough

call "%~dp0scripts\windows\stop_dictation.bat"
set "ODICTO_EXIT=%ERRORLEVEL%"
echo.
if "%ODICTO_EXIT%"=="0" goto :stopped
echo Odicto could not be stopped ^(exit code %ODICTO_EXIT%^). Read the message above.
pause
exit /b %ODICTO_EXIT%

:stopped
echo This window closes in 3 seconds.
ping -n 4 127.0.0.1 >nul
exit /b 0

REM Not inside a parenthesized block: %ERRORLEVEL% there is read before the call runs.
:passthrough
call "%~dp0scripts\windows\stop_dictation.bat" %*
exit /b %ERRORLEVEL%
