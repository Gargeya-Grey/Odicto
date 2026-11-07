"""Build Install-Odicto.cmd, the double-click installer for Windows.

The file is a cmd header plus PowerShell. The header runs the PowerShell part, which is
bootstrap.ps1 unchanged (the same verified install as install-managed.ps1), then turns on
start at sign-in, starts Odicto and opens Setup. Usage:
    python tools/distribution/build_easy_installer.py out/Install-Odicto.cmd
"""
from pathlib import Path
import sys

MARKER = "#ODICTO-POWERSHELL"
BOOTSTRAP = Path(__file__).resolve().with_name("bootstrap.ps1")

# The marker is split in the command line, so IndexOf finds only the marker line.
HEADER = """@echo off
setlocal DisableDelayedExpansion
title Odicto installer
set "ODICTO_INSTALLER=%~f0"
"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$t = [IO.File]::ReadAllText($env:ODICTO_INSTALLER); $m = '#' + 'ODICTO-POWERSHELL'; & ([scriptblock]::Create($t.Substring($t.IndexOf($m))))"
set "ODICTO_STATUS=%ERRORLEVEL%"
echo.
echo Press any key to close this window.
pause >nul
exit /b %ODICTO_STATUS%
"""

BEFORE = r"""$ErrorActionPreference = 'Stop'
$managed = Join-Path $env:LOCALAPPDATA 'Odicto'
$odicto = Join-Path $managed 'bin\odicto.cmd'
$installed = Test-Path -LiteralPath $odicto
$migrated = $false
$receipt = Join-Path $managed 'migration.json'
if (Test-Path -LiteralPath $receipt) {
    try { $migrated = (Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json).state -eq 'complete' } catch { }
}
# A new install turns on start at sign-in. An existing one keeps its choice (on or off), except
# an unfinished attempt: a pending receipt is tried again.
$chosen = $migrated -or $installed
$startup = Join-Path $managed 'autostart.json'
if (-not $migrated -and (Test-Path -LiteralPath $startup)) {
    try { if ((Get-Content -LiteralPath $startup -Raw | ConvertFrom-Json).pending -eq $true) { $chosen = $false } } catch { }
}
function Step([int]$Number, [string]$Text) {
    Write-Host ''
    Write-Host ("Step $Number of 3: " + $Text) -ForegroundColor Cyan
    try { $Host.UI.RawUI.WindowTitle = "Odicto installer - step $Number of 3" } catch { }
}
Write-Host ''
Write-Host '  Odicto installer' -ForegroundColor Cyan
Write-Host '  This window installs Odicto, free voice typing for Windows.'
Write-Host '  It takes a few minutes. You do not need to type anything here.'
Write-Host '  Technical text scrolls by while it works. That is normal.'
Step 1 'Downloading and installing Odicto (the longest step)'
try {
& {
"""

AFTER = r"""
}
} catch {
    Write-Host ''
    Write-Host ('Odicto was not installed: ' + $_.Exception.Message)
    if ($installed) { Write-Host 'If Odicto is open, stop it first (Start Menu: Stop Odicto).' }
    Write-Host 'Check the internet connection, then double-click Install-Odicto.cmd again.'
    exit 1
}
if (-not (Test-Path -LiteralPath $odicto)) {
    Write-Host ('Odicto was not installed: ' + $odicto + ' is missing.')
    exit 1
}
$menu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shell = New-Object -ComObject WScript.Shell
function Test-OdictoLink([string]$Name) {
    $path = Join-Path $menu $Name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $target = [string]$shell.CreateShortcut($path).TargetPath
    return ($target -and [string]::Equals([IO.Path]::GetFullPath($target), [IO.Path]::GetFullPath($odicto),
        [StringComparison]::OrdinalIgnoreCase))
}
# A source checkout's Start Menu item or sign-in shortcut: two copies must never run together.
# After a finished migration the source shortcuts forward to this install, so they are fine.
if (-not $migrated -and ((Test-OdictoLink 'Odicto\Odicto.lnk') -eq $false -or
        (Test-Path -LiteralPath (Join-Path $menu 'Startup\Odicto.lnk')))) {
    Write-Host ''
    Write-Host 'Odicto is installed, but another copy of Odicto is on this computer.'
    Write-Host 'Two copies must never run together, so the new copy was not started.'
    Write-Host 'Move your settings to the new copy first:'
    Write-Host '  https://github.com/Gargeya-Grey/Odicto#moving-from-a-source-checkout'
    exit 0
}
$failed = @()
Step 2 'Starting Odicto'
if (-not $chosen) {
    & $odicto autostart
    if ($LASTEXITCODE -ne 0) { $failed += 'autostart' }
}
& $odicto start
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Odicto did not start. Open the Start Menu and click Odicto to try again.'
    exit 1
}
Step 3 'Opening Odicto Setup in your browser'
& $odicto setup
if ($LASTEXITCODE -ne 0) { $failed += 'setup' }
$items = (Test-OdictoLink 'Odicto\Odicto.lnk') -and (Test-OdictoLink 'Odicto\Odicto Setup.lnk') -and
    (Test-OdictoLink 'Odicto\Stop Odicto.lnk')
if (-not $items -and -not $migrated) { $failed += 'shortcuts' }
$colour = if ($failed.Count) { 'Yellow' } else { 'Green' }
try { $Host.UI.RawUI.WindowTitle = 'Odicto installer - done' } catch { }
Write-Host ''
Write-Host '  ==========================================' -ForegroundColor $colour
if ($failed.Count) { Write-Host '  Odicto is running. Some steps did not finish.' -ForegroundColor $colour }
else { Write-Host '  Odicto is ready.' -ForegroundColor $colour }
Write-Host '  ==========================================' -ForegroundColor $colour
Write-Host ''
Write-Host 'Try it now:'
Write-Host '  1. Click in any text box, for example a new email.'
Write-Host '  2. Press Ctrl and the ` key together (the key under Esc, left of 1).'
Write-Host '  3. Speak. Then press Ctrl and ` again. Your words appear.'
Write-Host '  The first time can take a little longer.'
Write-Host ''
Write-Host 'Odicto runs in the background. Its icon is next to the clock.'
if ('autostart' -in $failed) { Write-Host 'It does not start by itself when you sign in.' }
elseif (-not $chosen) { Write-Host 'It starts by itself when you sign in.' }
if ('setup' -in $failed) { Write-Host 'Odicto Setup did not open. Open it from the Start Menu: Odicto Setup.' }
else { Write-Host 'Your browser shows Odicto Setup. Change the microphone or the keys there.' }
if ($items) { Write-Host 'Odicto, Odicto Setup and Stop Odicto are in the Start Menu.' }
elseif ($migrated) { Write-Host 'The Odicto items in the Start Menu open your old folder, which hands over to this copy.' }
else { Write-Host 'The Start Menu items were not made. To stop Odicto, right-click the Odicto icon next to the clock and click Quit Odicto.' }
if ($failed.Count) { exit 1 }
exit 0
"""


def build(bootstrap: str) -> bytes:
    body = "\n".join(line for line in bootstrap.splitlines() if not line.startswith("#Requires"))
    text = HEADER + MARKER + "\n" + BEFORE + body + AFTER
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    Path(argv[0]).write_bytes(build(BOOTSTRAP.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
