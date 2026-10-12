#Requires -Version 5.1
<#
.SYNOPSIS
  Zero-to-one installer for Odicto (Windows).

.DESCRIPTION
  Safe for humans and coding agents. Idempotent where possible.
  Installs Python deps into .\.venv, prepares .env, optionally installs
  Ollama + pulls the default LLM, and warms the Whisper model.

  A .venv is tied to the machine and Python that made it. A .venv that cannot
  run (for example its base Python was uninstalled) is renamed to
  .venv-backup-<timestamp> and rebuilt. Nothing is deleted; .env and prompt.txt
  are never touched.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Ollama
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Python 3.11
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Python D:\Python311\python.exe
#>
param(
    [switch]$Ollama,
    [switch]$SkipOllama,  # deprecated no-op: Ollama is now skipped by default
    [string]$OllamaModel = "qwen2.5:1.5b-instruct",
    [string]$WhisperModel = "tiny.en",
    [string]$Python,      # an installed Python to use: path to python.exe, or 3.10 / 3.11 / 3.12
    [switch]$NoUv,        # use pip + venv instead of uv
    [switch]$VenvOnly     # create or repair .venv, then stop (no packages, no Whisper)
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }
# $ErrorActionPreference does not catch native exit codes; check them by hand.
function Assert-Native($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "$what failed (exit code $LASTEXITCODE). Fix the error above and re-run install.ps1."
    }
}
. (Join-Path $PSScriptRoot "scripts\windows\install_helpers.ps1")

Write-Host "========================================" -ForegroundColor White
Write-Host "  Odicto - Installer" -ForegroundColor White
Write-Host "========================================" -ForegroundColor White

# --- uv (fast, hash-verified package manager) ---
Write-Step "Locating uv"
function Get-UvPath {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $links = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"
    if (Test-Path $links) { return $links }
    $standalone = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $standalone) { return $standalone }
    return $null
}

$venvDir = Join-Path $PSScriptRoot ".venv"
$uvPath = $null
if ($NoUv) {
    Write-Ok "uv skipped (-NoUv); using pip"
} else {
    $uvPath = Get-UvPath
}

# --- An explicit -Python is checked first, before anything is installed.
# A bad choice stops here; it never falls back to another interpreter.
$chosen = $null
if ($Python) {
    Write-Step "Checking -Python $Python"
    $chosen = Resolve-OdictoPythonChoice -Spec $Python -UvPath $uvPath -ExcludeUnder $venvDir
    Write-Ok "Python $($chosen.Version): $($chosen.Path)"
}

if (-not $uvPath -and -not $NoUv) {
    Write-Warn "uv not found. Attempting winget install..."
    try {
        winget install -e --id astral-sh.uv --accept-package-agreements --accept-source-agreements
        $uvPath = Get-UvPath
    } catch {
        $uvPath = $null
    }
    if ($uvPath) {
        Write-Ok "uv installed: $uvPath"
    } else {
        Write-Warn "Could not install uv. Falling back to pip (slower)."
    }
} elseif ($uvPath) {
    Write-Ok "uv found: $uvPath"
}

# --- venv: judged by running it, not by whether the file exists. A venv that works needs
# no base Python found or installed, so this is checked before any Python discovery.
$venvState = Get-OdictoVenvState -VenvDir $venvDir

# --- Python: reuse one that is already installed (numpy<2 has no wheels for 3.13+,
# so only 3.10 to 3.12 are accepted). Nothing is downloaded when one exists.
if (-not $chosen -and $venvState.State -ne "Healthy") {
    Write-Step "Locating an installed Python 3.10-3.12"
    $chosen = Find-OdictoPython -UvPath $uvPath -ExcludeUnder $venvDir
    if ($chosen) {
        Write-Ok "Python $($chosen.Version): $($chosen.Path)"
    } elseif ($uvPath) {
        Write-Ok "None installed; uv will fetch Python 3.12"
    } else {
        Write-Warn "Python 3.10-3.12 not found. Attempting winget install of Python 3.12..."
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        Assert-Native "winget install Python 3.12"
        $chosen = Find-OdictoPython -UvPath $uvPath -ExcludeUnder $venvDir
        if (-not $chosen) {
            throw "No Python 3.10-3.12 found after the winget install. Open a new terminal and re-run, or pass -Python with the path to python.exe."
        }
        Write-Ok "Python $($chosen.Version): $($chosen.Path)"
    }
}

Write-Step "Checking virtual environment (.venv)"
switch ($venvState.State) {
    "Healthy" {
        Write-Ok ".venv works (Python $($venvState.Version)); keeping it"
        if ($chosen -and $Python -and $venvState.Version -notlike "3.$($chosen.Minor).*") {
            Write-Warn "-Python selects $($chosen.Version) but the existing .venv is $($venvState.Version). To switch, move .venv aside and re-run."
        }
    }
    "Missing" {
        $null = New-OdictoVenv -VenvDir $venvDir -Python $chosen -UvPath $uvPath
        Write-Ok "Created .venv"
    }
    default {
        Write-Warn ".venv cannot be used: $($venvState.Reason)"
        $backup = Move-OdictoVenvAside -Root $PSScriptRoot -VenvDir $venvDir
        Write-Warn "Moved the old .venv to $backup (kept, not deleted; .env and prompt.txt were not touched)."
        $null = New-OdictoVenv -VenvDir $venvDir -Python $chosen -UvPath $uvPath
        Write-Ok "Rebuilt .venv"
    }
}

$venvPy = Join-Path $venvDir "Scripts\python.exe"

if ($VenvOnly) {
    Write-Ok "-VenvOnly: stopping after the virtual environment"
    return
}

Write-Step "Installing Python requirements"
if ($uvPath) {
    & $uvPath pip install --python $venvPy -r requirements.txt | Out-Host
    Assert-Native "uv pip install -r requirements.txt"
} else {
    & $venvPy -m pip install --upgrade pip wheel setuptools | Out-Host
    Assert-Native "pip install --upgrade pip wheel setuptools"
    & $venvPy -m pip install -r requirements.txt | Out-Host
    Assert-Native "pip install -r requirements.txt"
}
Write-Ok "requirements.txt installed"

# --- .env ---
Write-Step "Preparing .env"
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Ok "Copied .env.example -> .env"
} else {
    Write-Ok ".env already present (left unchanged)"
}

# --- Optional: Ollama (opt-in with -Ollama) ---
if ($Ollama) {
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
        Assert-Native "ollama pull $OllamaModel"
        Write-Ok "Model ready: $OllamaModel"
    }
} else {
    Write-Ok "Ollama skipped. Pick Ollama in the setup page later if you want a local LLM; the model downloads on demand."
}

# --- Warm Whisper weights ---
Write-Step "Pre-downloading Whisper model ($WhisperModel)"
& $venvPy -c @"
from faster_whisper import WhisperModel
print('Downloading / loading $WhisperModel ...')
m = WhisperModel('$WhisperModel', device='cpu', compute_type='int8')
print('Whisper model ready.')
"@
Assert-Native "Whisper model download"
Write-Ok "Whisper model cached"

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  Install complete" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host @"

Next steps:
  1. Open the setup page to pick a provider and paste its key:
     .\scripts\windows\setup.bat
  2. Start the app:
     .\start_dictation.bat
     (or scripts\windows\run_debug.bat for a console log)
  3. Click into any text field
  4. Hold your hotkey (default: Ctrl+`), speak, release
  5. Optional AI mode: hold Ctrl+Shift+`

  Switch AI backend in .env (restart after changes). API keys are per
  provider: paste each provider's key once, it is saved and remembered --
  switching providers never asks for a key you already gave:
    LLM_PROVIDER=none        # default; raw dictation only, no AI calls
    LLM_PROVIDER=meta        # cloud Meta API; needs META_API_KEY
    LLM_PROVIDER=ollama      # local; uses OLLAMA_MODEL; no API key
    LLM_PROVIDER=openrouter  # cloud; needs OPENROUTER_API_KEY + OPENROUTER_MODEL (default openai/gpt-5.6-luna)
    LLM_PROVIDER=gemini      # cloud; needs GEMINI_API_KEY
  See resolved values anytime: .\.venv\Scripts\python.exe odicto.py config

  Meta/OpenRouter/Gemini do not start Ollama. Quit the Ollama tray app
  separately if you want to free local LLM RAM/VRAM.

Stop with scripts\windows\stop_dictation.bat

"@
