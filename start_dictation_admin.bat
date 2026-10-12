@echo off
setlocal EnableExtensions
REM Elevated Odicto lifecycle: start_dictation_admin.bat [-Action Start|Stop|WaitReady|Configure]
REM Windows asks for approval itself; nothing is stopped or changed until it is granted.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start_dictation_admin.ps1" %*
set "ODICTO_EXIT=%ERRORLEVEL%"
if "%~1"=="" (
  echo.
  echo Exit code: %ODICTO_EXIT%
  pause
)
exit /b %ODICTO_EXIT%
