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

BEFORE = """$ErrorActionPreference = 'Stop'
Write-Host 'Installing Odicto. The first install takes a few minutes.'
Write-Host ''
try {
& {
"""

AFTER = """
}
} catch {
    Write-Host ''
    Write-Host ('Odicto was not installed: ' + $_.Exception.Message)
    Write-Host 'Check the internet connection, then double-click Install-Odicto.cmd again.'
    exit 1
}
$odicto = Join-Path $env:LOCALAPPDATA 'Odicto\\bin\\odicto.cmd'
if (-not (Test-Path -LiteralPath $odicto)) {
    Write-Host ('Odicto was not installed: ' + $odicto + ' is missing.')
    exit 1
}
Write-Host ''
& $odicto autostart
if ($LASTEXITCODE -ne 0) { Write-Host 'Odicto will not start by itself when you sign in. Use the Odicto item in the Start Menu.' }
& $odicto start
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Odicto did not start. Open the Start Menu and click Odicto to try again.'
    exit 1
}
& $odicto setup
Write-Host ''
Write-Host 'Odicto is running, and it starts by itself when you sign in.'
Write-Host 'To type with your voice: click in a text box, press Ctrl and the ` key (left of 1), speak, then press them again.'
Write-Host 'Your browser shows Odicto Setup. Odicto, Odicto Setup and Stop Odicto are in the Start Menu.'
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
