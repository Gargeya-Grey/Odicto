@echo off
REM Odicto setup: opens the setup page in your browser. Windows root entry point.
setlocal EnableExtensions
call "%~dp0scripts\windows\setup.bat" %*
exit /b %ERRORLEVEL%
