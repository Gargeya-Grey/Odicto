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
#   6. an isolated source copy runs clean-environment tests without moving live configuration
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
# change pass. It has been re-based twice, each deliberately and documented here:
# (1) test_openrouter_glm53_keeps_explicit_high relied on the developer's shell exporting
#     OPENROUTER_REASONING_EFFORT, so it passed locally and failed on CI. That edit pinned
#     _PRESENT_AT_IMPORT inside the test.
# (2) The F7 live polish feature (LIVE_POLISH: swap the streamed draft for the official
#     smart-mode final) updated two live-stop tests to the new intended behavior and added
#     five (150 -> 155). No existing assertion was weakened: both updated tests still hold
#     under LIVE_POLISH=false, and the new tests pin swap, failure-keeps-draft, and routing.
# (3) The LIVE_POLISH pass was removed: per the official Gemini Live transcription docs,
#     SMART-mode `input_transcription` finals are already the authoritative cleaned text,
#     so the whole-clip unary re-transcription on stop only added latency. The five polish
#     tests shrank to two: keep-streamed-text when no final exists, and cleanup swapping
#     the interim draft for the authoritative final (155 -> 151). No weakened assertion.
# (4) Speech providers: Groq, Grok, and OpenRouter, plus a CUDA warmup on local
#     Whisper. Raw dictation and AI mode now share the selected speech provider
#     (151 -> 156). The old AI-chord tests that required local Whisper were
#     replaced with tests that require the same engine on both chords. The
#     live-auto helper now follows that provider. No assertion was loosened
#     to hide a failure.
# (5) The direct Grok speech API was removed. Grok transcription is an
#     OpenRouter model slug. The Grok endpoint test went with it (156 -> 155).
# (6) Gemini Live stop now drains queued PCM before ending the stream and
#     commits the authoritative smart final over any interim draft. Two stop
#     tests were updated and one sender-drain test was added (155 -> 156).
# (7) F7 now previews in the HUD and inserts once while PROCESSING. Tests pin
#     that contract, valid Interactions image payloads, fail-closed insertion and
#     bounded CUDA probing. Removed three tests with the deleted caret editor and unused context helper (156 -> 153).
#     Independent test_reliability exercises the new failure modes and polish.
# (8) Folder layout: only the two fixture paths now resolve from tests/ to the install root.
#     All 153 assertions remain unchanged.
$ExpectedTestUnitsHash = 'C7C91E296A3C25DFA7BD642D5DC791F4D7653917EBB8478739DFB18DF8D95D4A'
$ExpectedUnitTestCount = 153

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

function Test-TestResult {
    param($Result, [int]$ExpectedCount, [int]$ExpectedSkipped = 0)
    return ($Result.Code -eq 0 -and -not $Result.TimedOut -and
        (Get-RunCount $Result.Text) -eq $ExpectedCount -and
        (Get-SkipCount $Result.Text) -eq $ExpectedSkipped -and
        $Result.Text -match '(?m)^OK(?: \(skipped=\d+\))?\s*$' -and
        $Result.Text -notmatch '(?m)^FAILED\b')
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
    Add-Failure "test_units.py changed (expected $ExpectedTestUnitsHash, got $actualHash). The oracle must stay frozen for this experiment."
} else {
    Add-Pass 'test_units.py matches the checkpoint hash'
}

# ------------------------------------------------------------- 2. the unit tests
Write-Step 'Gate 2: unit tests (test_units)'
$units = Invoke-Python @('-m', 'unittest', 'tests.test_units')
$runCount = Get-RunCount $units.Text
$skipCount = Get-SkipCount $units.Text
if ($runCount -ne $ExpectedUnitTestCount) {
    Add-Failure "expected $ExpectedUnitTestCount tests, ran $runCount"
} elseif ($skipCount -ne $ExpectedSkips) {
    Add-Failure "expected $ExpectedSkips skipped on this OS, saw $skipCount (a silently skipped gate is not a passing gate)"
} elseif (-not (Test-TestResult $units $ExpectedUnitTestCount $ExpectedSkips)) {
    Add-Failure "test_units did not exit cleanly with a successful summary:`n$($units.Text)"
} else {
    Add-Pass "$runCount ran, $skipCount skipped, OK"
}

# --------------------------------------------------------- 3. the equivalence oracle
Write-Step 'Gate 3: equivalence oracle (test_equivalence)'
# Reliability is independent of the historical equivalence oracle.
$reliability = Invoke-Python @('-m', 'unittest', 'tests.test_reliability')
$reliabilityCount = Get-RunCount $reliability.Text
$reliabilitySkips = Get-SkipCount $reliability.Text
$expectedReliabilitySkips = if ($OnWindows) { 0 } else { 1 }
if (-not (Test-TestResult $reliability 45 $expectedReliabilitySkips)) {
    Add-Failure "reliability regressions failed or count changed:`n$($reliability.Text)"
} else {
    Add-Pass '45 lifecycle, microphone, input ownership, HUD and polish regressions passed (only Win32 ABI is skipped off Windows)'
}

$boundaries = Invoke-Python @('-m', 'unittest', 'tests.test_shutdown', 'tests.test_readiness', 'tests.test_live_transport', 'tests.test_process_lifecycle', 'tests.test_capture_continuity', 'tests.test_capture_reconnect', 'tests.test_stt_results', 'tests.test_provider_deadlines', 'tests.test_hotkey_dispatch')
$boundarySkips = if ($OnWindows) { 0 } else { 12 }
if (-not (Test-TestResult $boundaries 46 $boundarySkips)) {
    Add-Failure "component boundary regressions failed:`n$($boundaries.Text)"
} else {
    Add-Pass "46 shutdown, readiness, capture, provider and process regressions passed ($boundarySkips Windows-only skips)"
}

$equiv = Invoke-Python @('-m', 'unittest', 'tests.test_equivalence')
$equivSkips = Get-SkipCount $equiv.Text
if (-not (Test-TestResult $equiv 15)) {
    Add-Failure "test_equivalence failed, timed out or did not run all 15 checks:`n$($equiv.Text)"
} elseif ($equivSkips -ne 0) {
    Add-Failure "test_equivalence skipped $equivSkips tests - the oracle must always run in full"
} else {
    Add-Pass '15 ran, 0 skipped, OK'
}

Write-Step 'Layout regressions (entry points and install-root paths)'
$layout = Invoke-Python @('-m', 'unittest', 'tests.test_layout')
if (-not (Test-TestResult $layout 4)) {
    Add-Failure "layout checks failed:`n$($layout.Text)"
} else {
    Add-Pass '4 layout checks passed without launching the application'
}

# --------------------------------------------------------------- 4. import smoke test
Write-Step 'Gate 4: import smoke test'
$smokeModules = 'main, refiner, indicator, setup_web, config, transcriber, typer, recorder, odicto, openrouter_catalog'
$smoke = Invoke-Python @('-c', "import sys; sys.path.insert(0, 'app'); import $smokeModules")
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

# --------------------------------------------------- 6. clean-environment (no .env)
if ($SkipCleanEnv) {
    Write-Step 'Gate 6: clean-environment run (SKIPPED)'
} else {
    Write-Step 'Gate 6: isolated clean-environment run (live configuration stays in place)'
    $cleanResult = Invoke-Python @('-B', 'tools/verify_clean.py')
    if (-not (Test-TestResult $cleanResult $ExpectedUnitTestCount $ExpectedSkips)) {
        Add-Failure "isolated clean-environment run failed:`n$($cleanResult.Text)"
    } else {
        Add-Pass '153 clean-environment tests passed in an isolated source copy; private files untouched'
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
