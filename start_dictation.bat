@echo off
if "%~1"=="" goto :manual_restart
call "%~dp0scripts\windows\start_dictation.bat" %*
exit /b %ERRORLEVEL%

:manual_restart
call "%~dp0scripts\windows\start_dictation.bat" /restart
set "ODICTO_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %ODICTO_EXIT%
