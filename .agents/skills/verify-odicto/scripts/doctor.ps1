# Read-only health check of the LIVE Odicto owner. Starts, stops and changes nothing.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File doctor.ps1 [-Root W:\AI-Projects\Odicto]
#
# -Root is the install that runs Odicto for real (holds dictation.pid, dictation.log,
# dictation-health.json). Default: this checkout if it holds dictation.pid, else the main
# git worktree (an agent usually works in a worktree; the owner runs from the main checkout).
# Exit code 0 = worth driving, 1 = a problem line was printed.
[CmdletBinding()]
param([string]$Root)

$ErrorActionPreference = 'Stop'
$problems = @()

if (-not $Root) {
    $here = (git rev-parse --show-toplevel 2>$null)
    if ($here -and (Test-Path (Join-Path $here 'dictation.pid'))) { $Root = $here }
    else {
        $main = (git worktree list --porcelain 2>$null | Select-Object -First 1) -replace '^worktree ', ''
        $Root = if ($main) { $main } else { $here }
    }
}
$Root = (Resolve-Path $Root).Path
Write-Host "root: $Root"

$version = Get-Content (Join-Path $Root 'VERSION') -ErrorAction SilentlyContinue
$codeLine = Select-String -Path (Join-Path $Root 'dictation.log') -Pattern 'Code version:' -ErrorAction SilentlyContinue | Select-Object -Last 1
Write-Host "VERSION file: $version; running build: $(if ($codeLine) { ($codeLine.Line -split 'Code version: ')[1] } else { 'unknown' })"

# 1. Owner process
$pidFile = Join-Path $Root 'dictation.pid'
$ownerPid = if (Test-Path $pidFile) { [int](Get-Content $pidFile -TotalCount 1) } else { 0 }
$owner = if ($ownerPid) { Get-Process -Id $ownerPid -ErrorAction SilentlyContinue } else { $null }
if (-not $owner) { $problems += "no live owner (pid file: $ownerPid)" }
else {
    $ws = [int]($owner.WorkingSet64 / 1MB); $priv = [int]($owner.PrivateMemorySize64 / 1MB)
    $prio = try { $owner.PriorityClass } catch { $null }
    if (-not $prio) { $prio = 'unreadable (elevated owner)' }
    Write-Host "owner: pid $ownerPid ($($owner.ProcessName)), started $($owner.StartTime), priority $prio"
    Write-Host "memory: working set $ws MB, private $priv MB"
    # The hot path needs ~100+ MB resident. A working set far below private memory means
    # Windows paged Odicto out; the next hotkey then waits on disk.
    if ($ws -lt 60) { $problems += "owner is paged out (working set $ws MB < 60 MB): expect hotkey lag" }
}

# 2. Stacked instances (double letters). Elevated owners hide their path; count by name too.
$pys = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue
$inRoot = @($pys | Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') })
$hidden = @($pys | Where-Object { -not $_.Path })
Write-Host "python processes: $(@($pys).Count) (in this install: $($inRoot.Count), path hidden: $($hidden.Count))"
if (($inRoot.Count + $hidden.Count) -gt 1) {
    Write-Host "  note: more than one candidate; check that only pid $ownerPid runs main.py"
}

# 3. Heartbeat (written every 2 s by the owner)
$healthFile = Join-Path $Root 'dictation-health.json'
if (Test-Path $healthFile) {
    $h = Get-Content $healthFile -Raw | ConvertFrom-Json
    $age = [math]::Round(([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) - $h.updated_at, 1)
    Write-Host "health: ready=$($h.ready) state=$($h.state) status=$($h.status) age=${age}s mic='$($h.microphone.device.name)' host_api=$($h.microphone.device.host_api)"
    if ($age -gt 10) { $problems += "health file is stale (${age}s): owner hung or gone" }
    if (-not $h.ready) { $problems += "owner reports ready=false" }
    if ($h.pid -ne $ownerPid) { $problems += "health pid $($h.pid) != pid file $ownerPid" }
} else { $problems += "no dictation-health.json" }

if ($problems) {
    $problems | ForEach-Object { Write-Host "PROBLEM: $_" -ForegroundColor Red }
    exit 1
}
Write-Host "OK: owner is worth driving" -ForegroundColor Green
exit 0
