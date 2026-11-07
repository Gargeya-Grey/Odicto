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
pause
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
# A saved sign-in choice (on or off), or one a migration carried over, is kept. A pending
# receipt is an unfinished first attempt, so it is tried again.
$chosen = $migrated
$startup = Join-Path $managed 'autostart.json'
if (-not $chosen -and (Test-Path -LiteralPath $startup)) {
    try { $chosen = (Get-Content -LiteralPath $startup -Raw | ConvertFrom-Json).pending -ne $true } catch { }
}
Write-Host 'Installing Odicto. The first install takes a few minutes.'
Write-Host ''
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
Write-Host ''
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
& $odicto setup
if ($LASTEXITCODE -ne 0) { $failed += 'setup' }
$items = (Test-OdictoLink 'Odicto\Odicto.lnk') -and (Test-OdictoLink 'Odicto\Odicto Setup.lnk') -and
    (Test-OdictoLink 'Odicto\Stop Odicto.lnk')
if (-not $items -and -not $migrated) { $failed += 'shortcuts' }
Write-Host ''
Write-Host 'Odicto is running.'
if ('autostart' -in $failed) { Write-Host 'It does not start by itself when you sign in.' }
elseif (-not $chosen) { Write-Host 'It starts by itself when you sign in.' }
Write-Host 'To type with your voice: click in a text box, press Ctrl and the ` key (left of 1), speak, then press them again.'
if ('setup' -in $failed) { Write-Host 'Odicto Setup did not open.' }
else { Write-Host 'Your browser shows Odicto Setup.' }
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
