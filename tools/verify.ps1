# One-command gate runner for Odicto.
#
#   1. test_units.py must match the recorded hash (its assertions cannot be quietly weakened)
#   2. the whole test suite via tools/run_tests.py (the same call CI makes): discovery of every
#      tests/test_*.py, a test-count floor and a per-platform skip allow-list. Floors and the
#      allow-list live in tools/run_tests.py only.
#   3. import smoke test across all top-level modules
#   4. syntax check for platforms/macos.py and platforms/linux.py, which cannot be
#      imported on Windows (CI is the real gate for those; see .github/workflows/ci.yml)
#   5. an isolated source copy runs the same suite without live configuration
#   6. LOC accounting against origin/main (skipped with a note if origin/main is not fetched)
#
# Usage:  .\tools\verify.ps1 [-SkipCleanEnv] [-Quiet]

[CmdletBinding()]
param(
    [switch]$SkipCleanEnv,
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Hash of test_units.py. It protects the assertions in that file from being weakened to make a
# change pass. It does NOT pin test counts (tools/run_tests.py has a rising floor instead).
# A re-base needs a numbered reason line below stating what changed and that no assertion was
# weakened. History:
# (1) test_openrouter_glm53_keeps_explicit_high pinned _PRESENT_AT_IMPORT inside the test.
# (2) F7 live polish: two live-stop tests updated to the new behavior, five added.
# (3) LIVE_POLISH pass removed; five polish tests shrank to two.
# (4) Speech providers Groq/Grok/OpenRouter and a CUDA warmup; AI-chord tests replaced.
# (5) Direct Grok speech API removed; its endpoint test went with it.
# (6) Gemini Live stop drains queued PCM; two stop tests updated, one added.
# (7) F7 previews in the HUD and inserts once; three tests removed with the caret editor.
# (8) Folder layout: only the two fixture paths resolve from tests/ to the install root.
# (9) October review fixes. TestOdicto.setUp adds patches only: a text-only fake clipboard
#     snapshot, no-op rich restore and change token, IDE host off, inline deferred restore.
#     Three expected values follow intended changes, each still an exact assertion:
#     Groq upload clip.wav -> clip.flac; Gemini upload audio/wav -> audio/flac; the Gemini
#     client is built with retries off and keep-alive (http_options). No assertion removed
#     or loosened; still 153 tests. test_whisper_transcriber_loading_fallback skips on macOS
#     only: macOS forces CPU under auto, so its CUDA->CPU path cannot run there (it failed
#     on macOS CI since 4d8302d). It still runs on Windows and Linux.
$ExpectedTestUnitsHash = '776D6B6E71F7899EE3B9553F270487AC57DF276F309D68B44703A1CCC9B12D3B'

$Script:Failures = @()

function Write-Step {
    param([string]$Text)
    if (-not $Quiet) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
}

function Add-Failure {
    param([string]$Text)
    $Script:Failures += $Text
    Write-Host "FAIL: $Text" -ForegroundColor Red
}

function Add-Pass {
    param([string]$Text)
    Write-Host "  ok: $Text" -ForegroundColor DarkGray
}

$Python = if (Test-Path (Join-Path $RepoRoot '.venv\Scripts\python.exe')) {
    Join-Path $RepoRoot '.venv\Scripts\python.exe'
} else {
    Join-Path $RepoRoot '.venv/bin/python'
}

if (-not (Test-Path $Python)) {
    Write-Host "No virtualenv found at .venv - run install.ps1 / install.sh first." -ForegroundColor Red
    exit 2
}

$env:QT_QPA_PLATFORM = 'offscreen'
$OnWindows = ($env:OS -eq 'Windows_NT')

$Scratch = if ($env:COMMANDCODE_SCRATCHPAD) { $env:COMMANDCODE_SCRATCHPAD } else { Join-Path $env:TEMP 'odicto-verify' }
if (-not (Test-Path $Scratch)) { New-Item -ItemType Directory -Path $Scratch -Force | Out-Null }

# Self-heal first: if a previous run was killed mid-way, .env / prompt.txt may still be
# sitting in the backup directory. Put them back before doing anything else, so this
# script can never leave the app running without its configuration.
$backupDir = Join-Path $Scratch 'clean-env-backup'
foreach ($name in @('.env', 'prompt.txt')) {
    $live = Join-Path $RepoRoot $name
    $stashed = Join-Path $backupDir $name
    if ((Test-Path -LiteralPath $stashed) -and -not (Test-Path -LiteralPath $live)) {
        Move-Item -LiteralPath $stashed -Destination $live -Force
        Write-Host "Recovered $name from an interrupted previous run." -ForegroundColor Yellow
    }
}

# A successful test summary alone is insufficient: process exit, timeout, count
# and skips must also pass, including failures during interpreter teardown.
$GateTimeoutSeconds = 180

function Invoke-Python {
    param([string[]]$PyArgs, [int]$TimeoutSeconds = $GateTimeoutSeconds)
    $outFile = Join-Path $Scratch 'py-out.txt'
    if (Test-Path $outFile) { Remove-Item $outFile -Force -ErrorAction SilentlyContinue }
    $job = Start-Job -ScriptBlock {
        param($py, $pyArgs, $repo, $target)
        Set-Location $repo
        $env:QT_QPA_PLATFORM = 'offscreen'
        & $py @pyArgs *>&1 > $target
        ('EXITCODE={0}' -f $LASTEXITCODE) | Add-Content -Path $target
    } -ArgumentList $Python, $PyArgs, $RepoRoot, $outFile
    Wait-Job $job -Timeout $TimeoutSeconds | Out-Null
    $timedOut = ($job.State -eq 'Running')
    if ($timedOut) { Stop-Job $job }
    Remove-Job $job -Force
    Start-Sleep -Milliseconds 200
    $text = if (Test-Path $outFile) { [string](Get-Content $outFile -Raw -ErrorAction SilentlyContinue) } else { '' }
    $match = [regex]::Match($text, 'EXITCODE=(\d+)')
    $code = if ($match.Success) { [int]$match.Groups[1].Value } else { 124 }
    return [pscustomobject]@{ Text = $text; Code = $code; TimedOut = $timedOut }
}

# ---------------------------------------------------------------- 1. test file frozen
Write-Step 'Gate 1: test_units.py is unchanged'
# Normalize Git's checkout line endings so this same checkpoint works on all OSes.
$unitSource = [IO.File]::ReadAllText((Join-Path $RepoRoot 'tests/test_units.py')).Replace("`r`n", "`n")
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $actualHash = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($unitSource))).Replace('-', '')
} finally { $hasher.Dispose() }
if ($actualHash -ne $ExpectedTestUnitsHash) {
    Add-Failure "test_units.py changed (expected $ExpectedTestUnitsHash, got $actualHash). Re-base only with a numbered reason line in verify.ps1."
} else {
    Add-Pass 'test_units.py matches the checkpoint hash'
}

# ------------------------------------------------------------- 2. the test suite
Write-Step 'Gate 2: test suite (tools/run_tests.py: discovery, floor, skip allow-list)'
$suite = Invoke-Python @('tools/run_tests.py') 600
if ($suite.Code -ne 0 -or $suite.TimedOut -or $suite.Text -notmatch '(?m)^GATE OK\s*$') {
    Add-Failure "test gate failed (exit $($suite.Code), timed out: $($suite.TimedOut)); last lines:`n$(($suite.Text -split "`n" | Select-Object -Last 25) -join "`n")"
} else {
    Add-Pass ([regex]::Match($suite.Text, '(?m)^GATE: .*$').Value)
}

# --------------------------------------------------------------- 3. import smoke test
Write-Step 'Gate 3: import smoke test'
$smokeModules = 'main, refiner, indicator, setup_web, config, transcriber, typer, recorder, odicto, openrouter_catalog'
$smoke = Invoke-Python @('-c', "import sys; sys.path.insert(0, 'app'); import $smokeModules")
if ($smoke.Code -ne 0) {
    Add-Failure "import smoke test failed:`n$($smoke.Text)"
} else {
    Add-Pass 'all top-level modules import'
}

# ------------------------------------------------- 4. macOS/Linux backends (syntax)
Write-Step 'Gate 4: cross-platform backend syntax (macOS/Linux are not importable here)'
# One file per invocation: PowerShell 5.1 splits a multi-line -c script into separate
# arguments, so a here-string would arrive at python truncated.
$syntaxFailed = $false
foreach ($target in @('app/platforms/macos.py', 'app/platforms/linux.py')) {
    $check = Invoke-Python @('-c', "compile(open('$target', encoding='utf-8').read(), '$target', 'exec')")
    if ($check.Code -ne 0) {
        $syntaxFailed = $true
        Add-Failure "syntax check failed for ${target}:`n$($check.Text)"
    }
}
if (-not $syntaxFailed) {
    Add-Pass 'platforms/macos.py and platforms/linux.py compile (CI is the real gate)'
}

# --------------------------------------------------- 5. clean-environment (no .env)
if ($SkipCleanEnv) {
    Write-Step 'Gate 5: clean-environment run (SKIPPED)'
} else {
    Write-Step 'Gate 5: isolated clean-environment run (live configuration stays in place)'
    $cleanResult = Invoke-Python @('-B', 'tools/verify_clean.py') 600
    if ($cleanResult.Code -ne 0 -or $cleanResult.TimedOut -or $cleanResult.Text -notmatch '(?m)^GATE OK\s*$') {
        Add-Failure "isolated clean-environment run failed; last lines:`n$(($cleanResult.Text -split "`n" | Select-Object -Last 25) -join "`n")"
    } else {
        Add-Pass 'test gate passed in an isolated source copy; private files untouched'
    }
}

# ---------------------------------------------------------------- 6. LOC accounting
# Note: untracked files are invisible to `git diff`.
Write-Step 'Gate 6: LOC accounting vs origin/main'
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& git rev-parse --verify --quiet origin/main *> $null
$haveMain = ($LASTEXITCODE -eq 0)
$diff = ''
$gitCode = 0
if ($haveMain) {
    $diff = (& git diff --stat origin/main 2>$null | Out-String)
    $gitCode = $LASTEXITCODE
}
$ErrorActionPreference = $previous
if (-not $haveMain) {
    Write-Host '  note: origin/main is not fetched; LOC accounting skipped' -ForegroundColor Yellow
} elseif ($gitCode -ne 0) {
    Add-Failure 'git diff --stat origin/main failed'
} elseif (-not $Quiet) {
    Write-Host $diff
}

Write-Step 'Summary'
if ($Script:Failures.Count -gt 0) {
    Write-Host "$($Script:Failures.Count) gate(s) failed:" -ForegroundColor Red
    foreach ($failure in $Script:Failures) { Write-Host "  - $failure" -ForegroundColor Red }
    exit 1
}
Write-Host 'All gates passed.' -ForegroundColor Green
exit 0
