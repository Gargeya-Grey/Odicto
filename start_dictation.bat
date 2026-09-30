@echo off
call "%~dp0scripts\windows\start_dictation.bat" %*
exit /b %ERRORLEVEL%
