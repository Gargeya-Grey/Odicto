@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

REM A .venv belongs to the machine and Python that made it. Judge it by running it,
REM before anything else (config, .env, launch), so every start mode reports it.
set "ODICTO_VENV_PROBLEM="
if not exist "%ODICTO_ROOT%\.venv\Scripts\pythonw.exe" set "ODICTO_VENV_PROBLEM=.venv\Scripts\pythonw.exe not found."
if not defined ODICTO_VENV_PROBLEM if not exist "%ODICTO_ROOT%\.venv\Scripts\python.exe" set "ODICTO_VENV_PROBLEM=.venv\Scripts\python.exe not found."
if not defined ODICTO_VENV_PROBLEM (
  "%ODICTO_ROOT%\.venv\Scripts\python.exe" -c "import sys" >nul 2>nul
  if errorlevel 1 set "ODICTO_VENV_PROBLEM=.venv\Scripts\python.exe cannot run - it was made on another machine or its base Python was removed."
)
if defined ODICTO_VENV_PROBLEM goto :venv_problem

if not exist "%ODICTO_ROOT%\.env" (
  echo ERROR: .env not found - copy .env.example to .env and set a provider API key.
  if "%~1"=="/nostartup" exit /b 1
  call :pause_if_interactive
  exit /b 1
)

REM Admin mode (marker .odicto-admin-enabled, switched on the setup page). The elevated
REM Odicto starts from its logon task and quits through its quit event, so none of this
REM shows a UAC prompt. odicto.py leaves a running owner alone on start, and quits it
REM before the new start on /restart. It falls back to the admin helper only when it must,
REM and says why. An install without the marker skips this and behaves as before.
if not exist "%ODICTO_ROOT%\.odicto-admin-enabled" goto :unelevated_install
if "%~1"=="" goto :admin_start
if /I "%~1"=="/min" goto :admin_start
if /I "%~1"=="/nostartup" goto :admin_start
if /I "%~1"=="/restart" goto :admin_restart
:unelevated_install

REM Startup host has PS -Command length limits and no visible console;
REM keep this file minimal and delegate cold-boot work to the dedicated
REM restart helper which handles longer PowerShell safely.
if "%~1"=="/nostartup" (
  "%ODICTO_ROOT%\.venv\Scripts\pythonw.exe" "%ODICTO_ROOT%\main.py"
  exit /b 0
)

REM Direct double-click / manual start - full-featured path.
if "%~1"=="" goto :fullstart
if /I "%~1"=="/min" goto :fullstart
if /I "%~1"=="/restart" goto :fullstart
goto :eof

:fullstart
set "PY=%ODICTO_ROOT%\.venv\Scripts\python.exe"
REM Config validation. IMPORTANT: do not use Python percent-formatting in this
REM one-liner. cmd.exe expands percent-sequences before Python runs, which broke
REM older starts with TypeError: str object is not callable. Use an f-string.
"%PY%" -c "import sys; sys.path.insert(0, 'app'); from config import Config; print(f'LLM_PROVIDER={Config.LLM_PROVIDER} model={Config.effective_llm_model()}')" 2>&1
if errorlevel 1 (
  echo Config validation failed - fix .env then rerun.
  call :pause_if_interactive
  exit /b 1
)

REM Ordinary start leaves a live owner alone. The app takes both locks before
REM sweeping orphans. For an intentional restart, run stop_dictation.bat first.
if /I "%~1"=="/restart" (
  echo Restarting Odicto...
  call "%~dp0stop_dictation.bat" /nopause
  if errorlevel 1 (
    echo FAILED to stop the existing instance. Restart cancelled.
    exit /b 1
  )
)

set "PYW=%ODICTO_ROOT%\.venv\Scripts\pythonw.exe"
REM Launch fresh (pythonw = no console).
start "" /MIN "%PYW%" "%ODICTO_ROOT%\main.py"

REM Confirm a fresh owner heartbeat and microphone callbacks, not just a PID file.
"%PY%" "%ODICTO_ROOT%\odicto.py" wait-ready --timeout 30
exit /b %errorlevel%

REM Kept outside any parenthesized block: inside one, "exit /b 1" can end the batch with code 0.
:admin_start
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\odicto.py" start
exit /b %errorlevel%

:admin_restart
"%ODICTO_ROOT%\.venv\Scripts\python.exe" "%ODICTO_ROOT%\odicto.py" restart
exit /b %errorlevel%

:venv_problem
echo ERROR: %ODICTO_VENV_PROBLEM%
echo Run: powershell -ExecutionPolicy Bypass -File "%ODICTO_ROOT%\install.ps1"
echo It keeps the old .venv as .venv-backup-* and keeps .env and prompt.txt.
echo If an old Odicto is still running, Quit Odicto from its tray icon first.
if "%~1"=="/nostartup" exit /b 1
call :pause_if_interactive
exit /b 1

REM The root start_dictation.bat shows the result and pauses once itself.
:pause_if_interactive
if defined ODICTO_NO_PAUSE exit /b 0
pause
exit /b 0
