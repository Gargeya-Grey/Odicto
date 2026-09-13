# One-command gate runner for the efficient-modular refactor experiment.
#
# Every gate the refactor relies on, in one place:
#   1. test_units.py must be byte-identical to the recorded baseline (rule 1)
#   2. test_units: expected test count and skipped count (a bare "OK" is not enough:
#      the setup-page JS gate self-skips when `node` is missing, so a dead gate
#      would still report OK)
#   3. test_equivalence: the independent oracle added for this refactor
#   4. import smoke test across all top-level modules
#   5. syntax check for platforms/macos.py and platforms/linux.py, which cannot be
#      imported on Windows (CI is the real gate for those; see .github/workflows/ci.yml)
#   6. a clean-environment run: .env and prompt.txt temporarily moved OUTSIDE the repo
#      so the suite runs like CI does. Backups live outside the repo on purpose -
#      `.env.bak` inside the repo is not gitignored and would put live API keys one
#      `git add -A` away from being committed.
#   7. LOC accounting against origin/main
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

# Hash of test_units.py. It is frozen so its assertions cannot be quietly weakened to make a
# change pass. It was re-based exactly once, deliberately: test_openrouter_glm53_keeps_explicit_high
# relied on the developer's shell exporting OPENROUTER_REASONING_EFFORT, so it passed locally and
# failed on CI. That edit pinned _PRESENT_AT_IMPORT inside the test. The test count (150) and every
# other assertion are unchanged.
$ExpectedTestUnitsHash = '4586445D62DF3979347CC970113FBB729EE17DB42FDA76FC38A8F6345CDEBC93'
$ExpectedUnitTestCount = 150

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
$ExpectedSkips = if ($OnWindows) { 0 } else { 2 }

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

# Runs python in a child job with a hard timeout, writing output to a file so partial
# output survives a kill. This exists because of a pre-existing hazard: the unit suite
# prints "OK" and then can stall at interpreter shutdown with
#   Exception ignored in: BaseEventLoop.__del__
#   AttributeError: 'ProactorEventLoop' object has no attribute '_ssock_'
# (Gemini Live asyncio teardown; triggered by GC timing). Treating output-based success
# as authoritative keeps the gate reliable without hiding a genuine hang, which would
# produce no "Ran N tests" / "OK" line at all.
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

function Get-SkipCount {
    param([string]$Output)
    $match = [regex]::Match($Output, 'skipped=(\d+)')
    if ($match.Success) { return [int]$match.Groups[1].Value }
    return 0
}

function Get-RunCount {
    param([string]$Output)
    $match = [regex]::Match($Output, 'Ran (\d+) tests?')
    if ($match.Success) { return [int]$match.Groups[1].Value }
    return -1
}

# ---------------------------------------------------------------- 1. test file frozen
Write-Step 'Gate 1: test_units.py is unchanged'
$actualHash = (Get-FileHash -LiteralPath (Join-Path $RepoRoot 'test_units.py') -Algorithm SHA256).Hash
if ($actualHash -ne $ExpectedTestUnitsHash) {
    Add-Failure "test_units.py changed (expected $ExpectedTestUnitsHash, got $actualHash). The oracle must stay frozen for this experiment."
} else {
    Add-Pass 'test_units.py matches the checkpoint hash'
}

# ------------------------------------------------------------- 2. the 150 unit tests
Write-Step 'Gate 2: unit tests (test_units)'
$units = Invoke-Python @('-m', 'unittest', 'test_units')
$runCount = Get-RunCount $units.Text
$skipCount = Get-SkipCount $units.Text
if ($runCount -ne $ExpectedUnitTestCount) {
    Add-Failure "expected $ExpectedUnitTestCount tests, ran $runCount"
} elseif ($skipCount -ne $ExpectedSkips) {
    Add-Failure "expected $ExpectedSkips skipped on this OS, saw $skipCount (a silently skipped gate is not a passing gate)"
} elseif ($units.Text -notmatch 'OK') {
    Add-Failure "test_units did not report OK"
} else {
    $note = if ($units.TimedOut) { ' (process lingered at shutdown; killed)' } else { '' }
    Add-Pass "$runCount ran, $skipCount skipped, OK$note"
}

# --------------------------------------------------------- 3. the equivalence oracle
Write-Step 'Gate 3: equivalence oracle (test_equivalence)'
$equiv = Invoke-Python @('-m', 'unittest', 'test_equivalence')
$equivSkips = Get-SkipCount $equiv.Text
if ($equiv.Text -notmatch 'OK') {
    Add-Failure "test_equivalence did not report OK"
} elseif ($equivSkips -ne 0) {
    Add-Failure "test_equivalence skipped $equivSkips tests - the oracle must always run in full"
} else {
    $note = if ($equiv.TimedOut) { ' (process lingered at shutdown; killed)' } else { '' }
    Add-Pass ("{0} ran, 0 skipped, OK{1}" -f (Get-RunCount $equiv.Text), $note)
}

# --------------------------------------------------------------- 4. import smoke test
Write-Step 'Gate 4: import smoke test'
$smokeModules = 'main, refiner, indicator, setup_web, config, transcriber, typer, recorder, odicto, openrouter_catalog'
$smoke = Invoke-Python @('-c', "import $smokeModules")
if ($smoke.Code -ne 0) {
    Add-Failure "import smoke test failed:`n$($smoke.Text)"
} else {
    Add-Pass 'all top-level modules import'
}

# ------------------------------------------------- 5. macOS/Linux backends (syntax)
Write-Step 'Gate 5: cross-platform backend syntax (macOS/Linux are not importable here)'
# One file per invocation: PowerShell 5.1 splits a multi-line -c script into separate
# arguments, so a here-string would arrive at python truncated.
$syntaxFailed = $false
foreach ($target in @('platforms/macos.py', 'platforms/linux.py')) {
    $check = Invoke-Python @('-c', "compile(open('$target', encoding='utf-8').read(), '$target', 'exec')")
    if ($check.Code -ne 0) {
        $syntaxFailed = $true
        Add-Failure "syntax check failed for ${target}:`n$($check.Text)"
    }
}
if (-not $syntaxFailed) {
    Add-Pass 'platforms/macos.py and platforms/linux.py compile (CI is the real gate)'
}

# --------------------------------------------------- 6. clean-environment (no .env)
if ($SkipCleanEnv) {
    Write-Step 'Gate 6: clean-environment run (SKIPPED)'
} else {
    Write-Step 'Gate 6: clean-environment run (.env and prompt.txt moved outside the repo)'
    $envPath = Join-Path $RepoRoot '.env'
    $promptPath = Join-Path $RepoRoot 'prompt.txt'
    $hadEnv = Test-Path -LiteralPath $envPath
    $hadPrompt = Test-Path -LiteralPath $promptPath
    if (Test-Path $backupDir) { Remove-Item $backupDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

    # Moving .env is NOT sufficient. If the shell also exports the config keys (common when
    # a developer exports .env into their environment), os.getenv still sees them, so
    # _PRESENT_AT_IMPORT is non-empty and the run is not clean at all - it silently diverges
    # from CI. That is exactly how a real CI-only failure (test_openrouter_glm53_keeps_explicit_high)
    # stayed hidden locally. Clear config's known keys too, then restore them.
    $envGuard = @{}
    $knownKeys = (Invoke-Python @('-c', 'from config import KNOWN_ENV_KEYS; print(chr(10).join(sorted(KNOWN_ENV_KEYS)))')).Text
    foreach ($key in ($knownKeys -split "`r?`n" | Where-Object { $_ -match '^[A-Z][A-Z0-9_]*$' })) {
        $value = [Environment]::GetEnvironmentVariable($key)
        if ($null -ne $value) {
            $envGuard[$key] = $value
            Remove-Item -Path "Env:$key" -ErrorAction SilentlyContinue
        }
    }

    $cleanResult = $null
    try {
        if ($hadEnv) { Move-Item -LiteralPath $envPath -Destination (Join-Path $backupDir '.env') -Force }
        if ($hadPrompt) { Move-Item -LiteralPath $promptPath -Destination (Join-Path $backupDir 'prompt.txt') -Force }
        $cleanResult = Invoke-Python @('-m', 'unittest', 'test_units')
    } finally {
        if ($hadEnv -and -not (Test-Path -LiteralPath $envPath)) {
            Move-Item -LiteralPath (Join-Path $backupDir '.env') -Destination $envPath -Force
        }
        if ($hadPrompt -and -not (Test-Path -LiteralPath $promptPath)) {
            Move-Item -LiteralPath (Join-Path $backupDir 'prompt.txt') -Destination $promptPath -Force
        }
        foreach ($key in $envGuard.Keys) {
            Set-Item -Path "Env:$key" -Value $envGuard[$key]
        }
    }

    if ($hadEnv -and -not (Test-Path -LiteralPath $envPath)) {
        Add-Failure "'.env' was NOT restored after the clean-environment run - restore it from $backupDir"
    } elseif (-not $cleanResult -or $cleanResult.Text -notmatch 'OK') {
        Add-Failure "clean-environment run failed (this is how CI runs; a local .env can mask it):`n$($cleanResult.Text)"
    } else {
        $note = if ($cleanResult.TimedOut) { ' (process lingered at shutdown; killed)' } else { '' }
        Add-Pass "suite passes with no .env present and $($envGuard.Count) exported config vars cleared, all restored$note"
    }
}

# ---------------------------------------------------------------- 7. LOC accounting
# Note: untracked files are invisible to `git diff`, so the refactor's LOC figure is
# only complete once each phase is committed.
Write-Step 'Gate 7: LOC accounting vs origin/main'
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$diff = (& git diff --stat origin/main 2>$null | Out-String)
$gitCode = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($gitCode -ne 0) {
    Add-Failure 'git diff --stat origin/main failed (is origin/main fetched?)'
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
