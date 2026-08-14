# Create/refresh login startup: resilient shortcut + Task Scheduler fallback.
# Why both: Explorer Startup is fast but can be skipped by Fast Boot;
# the logon task is the reliable fallback.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$lnk = Join-Path $startup 'Odicto.lnk'
$bat = Join-Path $repo 'start_dictation.bat'
$task = 'Odicto'

if (-not (Test-Path $bat)) { Write-Error "Missing start script: $bat"; exit 1 }

# 1) Explorer Startup shortcut - direct pythonw (no bat). Fast lane so login
# never shows a hung console or blocks on PowerShell/CIM cold-start.
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = (Join-Path $repo '.venv\Scripts\pythonw.exe')
$s.Arguments = '"' + $repo + '\main.py"'
$s.WorkingDirectory = $repo
$s.WindowStyle = 7
$s.Description = 'Start Odicto dictation at login (direct pythonw)'
$s.Save()
Write-Output "Created: $lnk"
Write-Output "Target: $($s.TargetPath) $($s.Arguments)"
Write-Output "WorkDir: $($s.WorkingDirectory)"

# 2) Task Scheduler logon trigger - needs Admin consent. Attempt silently;
#    non-admin will get Access denied which is expected - shortcut is the
#    reliable fallback. The try/catch prevents a parse error from stopping
#    the script when future edits use interpolations.
try {
  $q = schtasks /query /tn $task 2>&1
  if ($LASTEXITCODE -eq 0) { schtasks /delete /tn $task /f 2>&1 | Out-Null }
  $trArg = "'" + $s.TargetPath + "' " + $s.Arguments + ""
  & schtasks /create /tn $task /sc onlogon /delay 0000:15 /rl limited /tr $trArg 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Output "Task: $task (on logon, 15s delay)"
  } elseif ($q -match 'Access is denied') {
    Write-Output "Task: not created (needs Admin once) - Startup shortcut will still start the app"
  } else {
    Write-Output "Task: not created - Startup shortcut will still start the app"
  }
} catch {
  Write-Output "Task: skipped - Startup shortcut will still start the app"
}
