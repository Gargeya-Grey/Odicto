@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo ERROR: .venv\Scripts\pythonw.exe not found.
  echo Create the venv and install requirements first.
  if "%~1"=="/nostartup" exit /b 1
  pause
  exit /b 1
)

if not exist "%~dp0.env" (
  echo ERROR: .env not found - copy .env.example to .env and set META_API_KEY.
  if "%~1"=="/nostartup" exit /b 1
  pause
  exit /b 1
)

REM Startup host has PS -Command length limits and no visible console;
REM keep this file minimal and delegate cold-boot work to the dedicated
REM restart helper which handles longer PowerShell safely.
if "%~1"=="/nostartup" (
  "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
  exit /b 0
)

REM Direct double-click / manual start - full-featured path.
if "%~1"=="" goto :fullstart
if /I "%~1"=="/min" goto :fullstart
goto :eof

:fullstart
set "PY=%~dp0.venv\Scripts\python.exe"
REM Config validation. IMPORTANT: do not use Python percent-formatting in this
REM one-liner. cmd.exe expands percent-sequences before Python runs, which broke
REM older starts with TypeError: str object is not callable. Use an f-string.
"%PY%" -c "from config import Config; print(f'LLM_PROVIDER={Config.LLM_PROVIDER} META_MODEL={Config.META_MODEL}')" 2>&1
if errorlevel 1 (
  echo Config validation failed - fix .env then rerun.
  pause
  exit /b 1
)

REM Stop every previous instance (PID file + any leftover main.py for this folder).
call "%~dp0stop_dictation.bat" /nopause

REM Lightweight orphan re-check via PowerShell only. Do not import main.py here -
REM that would load keyboard/Whisper/Qt just for a PID scan and slow login start.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$rootN = [System.IO.Path]::GetFullPath('%~dp0').TrimEnd('\').ToLowerInvariant(); " ^
  "$left = @(Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" -ErrorAction SilentlyContinue | " ^
  "  Where-Object { $_.CommandLine -and ($_.CommandLine -match 'main\.py') -and ($_.CommandLine.ToLowerInvariant().Contains($rootN)) }); " ^
  "if ($left.Count -gt 0) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo WARNING: stale Odicto python processes still running - trying one more stop...
  call "%~dp0stop_dictation.bat" /nopause
  REM ~2s settle - ping works when stdin is redirected; timeout.exe often does not.
  ping -n 3 127.0.0.1 >nul
)

REM Brief settle so Windows releases low-level keyboard hooks from the killed process
REM before the new instance installs its own (avoids a brief double-hook window).
ping -n 2 127.0.0.1 >nul

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
REM Launch fresh (pythonw = no console).
start "" /MIN "%PYW%" "%~dp0main.py"

REM Wait up to 10s for dictation.pid (written immediately after single-instance lock).
REM PowerShell Start-Sleep works under redirected stdin / non-console hosts.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pidFile = Join-Path '%~dp0' 'dictation.pid'; " ^
  "$deadline = (Get-Date).AddSeconds(10); " ^
  "while ((Get-Date) -lt $deadline) { " ^
  "  if (Test-Path -LiteralPath $pidFile) { exit 0 }; " ^
  "  Start-Sleep -Milliseconds 250 " ^
  "}; exit 1"
if errorlevel 1 (
  echo FAILED to start - no dictation.pid appeared. Check dictation.log and .env.
  pause
  exit /b 1
)
echo Started (dictation.pid present)
exit /b 0
