#Requires -Version 5.1
<#
.SYNOPSIS
  Zero-to-one installer for Odicto (Windows).

.DESCRIPTION
  Safe for humans and coding agents. Idempotent where possible.
  Installs Python deps into .\.venv, prepares .env, optionally installs
  Ollama + pulls the default LLM, and warms the Whisper model.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipOllama
#>
param(
    [switch]$SkipOllama,
    [string]$OllamaModel = "qwen2.5:1.5b-instruct",
    [string]$WhisperModel = "tiny.en"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }

Write-Host "========================================" -ForegroundColor White
Write-Host "  Odicto — Windows Installer" -ForegroundColor White
Write-Host "========================================" -ForegroundColor White

if ($env:OS -notmatch "Windows") {
    throw "This project currently supports Windows only."
}

# --- Python ---
Write-Step "Locating Python 3.10+"
$py = $null
foreach ($candidate in @("py", "python", "python3")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -or $ver -match "Python 3\.") {
            $py = $candidate
            Write-Ok "$candidate → $ver"
            break
        }
    } catch { }
}
if (-not $py) {
    Write-Warn "Python not found. Attempting winget install of Python 3.12..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $py = "py"
    & $py --version | Out-Host
}

# --- venv ---
Write-Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    if ($py -eq "py") {
        & py -3 -m venv .venv
    } else {
        & $py -m venv .venv
    }
    Write-Ok "Created .venv"
} else {
    Write-Ok ".venv already exists"
}

$venvPy = ".\.venv\Scripts\python.exe"
$venvPip = ".\.venv\Scripts\python.exe"

Write-Step "Upgrading pip / wheel"
& $venvPy -m pip install --upgrade pip wheel setuptools | Out-Host

Write-Step "Installing Python requirements"
& $venvPy -m pip install -r requirements.txt | Out-Host
Write-Ok "requirements.txt installed"

# --- .env ---
Write-Step "Preparing .env"
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Ok "Copied .env.example → .env"
} else {
    Write-Ok ".env already present (left unchanged)"
}

# Ensure model names match installer choices when still on example defaults
$envText = Get-Content ".\.env" -Raw
if ($envText -match "WHISPER_MODEL_SIZE=") {
    # only set if user hasn't customized heavily — leave as-is if file existed
}

# --- Optional: Ollama ---
if (-not $SkipOllama) {
    Write-Step "Checking Ollama"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Warn "Ollama not on PATH. Attempting winget install..."
        try {
            winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
            Write-Ok "Ollama installed (you may need a new terminal for PATH)"
        } catch {
            Write-Warn "Could not install Ollama automatically. Install from https://ollama.com/download then re-run."
        }
        $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    } else {
        Write-Ok "ollama found: $($ollama.Source)"
    }

    if ($ollama) {
        Write-Step "Ensuring Ollama server is running"
        try {
            $null = & ollama list 2>&1
        } catch { }
        # Start serve in background if needed (Windows)
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2

        Write-Step "Pulling LLM model: $OllamaModel"
        & ollama pull $OllamaModel | Out-Host
        Write-Ok "Model ready: $OllamaModel"
    }
} else {
    Write-Warn "Skipping Ollama (-SkipOllama). Raw dictation still works; AI mode will not."
}

# --- Warm Whisper weights ---
Write-Step "Pre-downloading Whisper model ($WhisperModel)"
& $venvPy -c @"
from faster_whisper import WhisperModel
print('Downloading / loading $WhisperModel ...')
m = WhisperModel('$WhisperModel', device='cpu', compute_type='int8')
print('Whisper model ready.')
"@
Write-Ok "Whisper model cached"

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  Install complete" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host @"

Next steps:
  1. Edit .env if needed (hotkeys, LLM provider, models)
  2. Double-click  start_dictation.bat
     (or run_debug.bat for a console log)
  3. Click into any text field
  4. Hold your hotkey (default: Alt+X), speak, release
  5. Optional AI mode: hold Alt+X+Z

  Switch AI backend in .env (restart after changes):
    LLM_PROVIDER=ollama      # local; Odicto may start Ollama
    LLM_PROVIDER=openrouter  # cloud; needs OPENROUTER_API_KEY + OPENROUTER_MODEL
    LLM_PROVIDER=none        # raw dictation only

  OpenRouter does not start Ollama. Quit the Ollama tray app separately
  if you want to free local LLM RAM/VRAM.

Stop with stop_dictation.bat

"@
