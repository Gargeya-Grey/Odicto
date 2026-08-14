$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Odicto.lnk'
if (Test-Path $lnk) {
  $ws = New-Object -ComObject WScript.Shell
  $s = $ws.CreateShortcut($lnk)
  Write-Output "Shortcut Exists: True"
  Write-Output "Target: $($s.TargetPath)"
  Write-Output "Arguments: $($s.Arguments)"
  Write-Output "WorkDir: $($s.WorkingDirectory)"
  Write-Output "WindowStyle: $($s.WindowStyle)"
  Write-Output "Description: $($s.Description)"
} else { Write-Output "Shortcut Exists: False" }
Write-Output "---"
try {
  $t = schtasks /query /tn Odicto /v /fo LIST 2>$null
  if ($LASTEXITCODE -eq 0) {
    Write-Output "Task Exists: True"
    $t | Select-String -Pattern 'TaskName|Run As|Logon Mode|Trigger|Task To Run' | ForEach-Object { $_.ToString().Trim() }
  } else { Write-Output "Task Exists: False (Startup shortcut is the fallback)" }
} catch { Write-Output "Task Exists: unknown ($($_.Exception.Message))" }
Write-Output "---"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $repo 'dictation.pid')) { Write-Output "Running PID: $(Get-Content (Join-Path $repo 'dictation.pid'))"; } else { Write-Output "Running: not detected (dictation.pid missing)" }
