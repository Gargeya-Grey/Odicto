#Requires -Version 5.1
[CmdletBinding()]
param([string]$HomePath)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'tools\distribution\bootstrap.ps1') @PSBoundParameters
if ($LASTEXITCODE) { exit $LASTEXITCODE }
