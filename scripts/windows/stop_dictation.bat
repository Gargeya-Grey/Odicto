@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"
set "PY=%ODICTO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
REM The shared CLI verifies the executed script before killing any PID.
REM Never delete a lock file: an existing owner must retain the same lock inode.
"%PY%" "%ODICTO_ROOT%\odicto.py" stop
exit /b %ERRORLEVEL%
