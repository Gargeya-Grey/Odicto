@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..\..") do set "ODICTO_ROOT=%%~fI"
cd /d "%ODICTO_ROOT%"

if not exist "%ODICTO_ROOT%\.venv\Scripts\pythonw.exe" (
  echo ERROR: .venv\Scripts\pythonw.exe not found.
  echo Create the venv and install requirements first.
  if "%~1"=="/nostartup" exit /b 1
  pause
  exit /b 1
)

if not exist "%ODICTO_ROOT%\.env" (
  echo ERROR: .env not found - copy .env.example to .env and set a provider API key.
  if "%~1"=="/nostartup" exit /b 1
  pause
  exit /b 1
)

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
  pause
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
