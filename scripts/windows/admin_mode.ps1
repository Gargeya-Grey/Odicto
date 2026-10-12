# Admin mode switch for one install: the only script that elevates, and the only one that
# writes or deletes the .odicto-admin-enabled marker.
#
# Why: a Medium-integrity process cannot hook keys typed into an elevated window
# (an Administrator Windows Terminal), so the owner has to run elevated too. Admin mode is
# off by default. app/windows_admin.py runs this helper once per switch change (one UAC prompt).
# Daily start, restart and stop do not use it: they run the logon task and the quit event.
#
# Actions:
#   Start      stop the owner, start a fresh elevated one, wait ready. With -InstallStartup it first
#              registers the logon task and the Startup shortcut and writes the marker (admin mode on).
#   Stop       stop the owner (the fallback when the quit event is unavailable).
#   WaitReady  wait for the owner to be ready.
#   Configure  register the task and Startup shortcut, write the marker, nothing else.
#   Disable    admin mode off: stop the owner, unregister the logon task, point the Startup shortcut
#              back at pythonw, delete the marker. The caller then starts a normal Odicto.
#
# A non-elevated run re-launches this same script through UAC (never auto-approved) and
# returns the elevated exit code. Nothing is stopped or changed before elevation succeeds.
# It touches only the task named Odicto when it has exactly this install's shape, the
# Odicto.lnk Startup shortcut when it is one of this install's two shapes, and the marker.
# Anything else is refused and left alone. The receipt holds no secrets.
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'WaitReady', 'Configure', 'Disable')]
    [string]$Action = 'Start',
    [switch]$InstallStartup,
    [string]$ReceiptPath,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$MainPy = Join-Path $Root 'main.py'
$OdictoPy = Join-Path $Root 'odicto.py'
$MarkerPath = Join-Path $Root '.odicto-admin-enabled'
$TaskName = 'Odicto'
$script:Steps = New-Object System.Collections.Generic.List[string]
$script:Detail = ''

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Receipt([int]$Code, [string]$Err) {
    if (-not $ReceiptPath) { return }
    try {
        $receipt = [ordered]@{
            action   = $Action
            ok       = ($Code -eq 0)
            exit_code = $Code
            error    = $Err
            detail   = $script:Detail
            steps    = @($script:Steps)
            root     = $Root
            elevated = (Test-Admin)
            time     = (Get-Date).ToUniversalTime().ToString('o')
        }
        $path = [IO.Path]::GetFullPath($ReceiptPath)
        $dir = Split-Path -Parent $path
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        [IO.File]::WriteAllText($path, ($receipt | ConvertTo-Json -Depth 4),
            (New-Object Text.UTF8Encoding($false)))
    } catch {
        [Console]::Error.WriteLine("Odicto admin helper: could not write receipt: $($_.Exception.Message)")
    }
}

function Finish([int]$Code, [string]$Err = '') {
    Write-Receipt $Code $Err
    if ($Err) { [Console]::Error.WriteLine("Odicto admin helper ($Action): $Err") }
    exit $Code
}

function Add-Step([string]$Text) { $script:Steps.Add($Text) }

# Run a native program and keep its output; Continue so a stderr line is not a terminating error.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    $text = ($lines -join "`n")
    if ($text.Length -gt 2000) { $text = $text.Substring($text.Length - 2000) }
    $script:Detail = $text
    return $code
}

function Assert-Runtime([bool]$NeedPythonw) {
    foreach ($file in @($Py, $OdictoPy)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing $file" }
    }
    if ($NeedPythonw) {
        foreach ($file in @($Pyw, $MainPy)) {
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing $file" }
        }
    }
    $code = Invoke-Native $Py @('-c', 'import sys')
    if ($code -ne 0) { throw "$Py cannot run (exit $code); reinstall with install.ps1" }
    if ($NeedPythonw) {
        $pywProc = Start-Process -FilePath $Pyw -ArgumentList @('-c', '"import sys"') -WindowStyle Hidden -PassThru
        $null = $pywProc.Handle
        $pywProc.WaitForExit()
        if ($pywProc.ExitCode -ne 0) { throw "$Pyw cannot run (exit $($pywProc.ExitCode))" }
    }
    Add-Step 'runtime validated'
}

# Only a confirmed "no such task" counts as absent; any other error is a failure.
function Get-OdictoTask {
    try {
        return Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    } catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound' -or
            "$($_.FullyQualifiedErrorId)" -like 'CmdletizationQuery_NotFound*') { return $null }
        throw
    }
}

# A user name or SID string to a SID string; $null when it cannot be resolved.
function ConvertTo-UserSid([string]$UserId) {
    if (-not $UserId) { return $null }
    if ($UserId -match '^S-1-\d+(-\d+)+$') { return $UserId.ToUpperInvariant() }
    try {
        return (New-Object Security.Principal.NTAccount($UserId)).Translate(
            [Security.Principal.SecurityIdentifier]).Value.ToUpperInvariant()
    } catch {
        return $null
    }
}

# A CIM enum reads back as its name or its number depending on the provider.
function Test-EnumIs($Value, [string]$Name, [int]$Number) {
    return ("$Value" -ieq $Name -or "$Value" -eq "$Number")
}

# Unlimited run time is an explicit zero (PT0S); an unset limit means the 72 hour default.
function Test-ZeroTime($Value) {
    if ($Value -is [TimeSpan]) { return ($Value -eq [TimeSpan]::Zero) }
    return ("$Value" -ieq 'PT0S')
}

# Every way an existing task differs from the one this helper registers. Empty means it is
# exactly the owned shape, and only then may the helper reuse it.
function Get-TaskMismatch($Task, [string]$Sid, [string]$Exe, [string]$ScriptPath, [string]$WorkDir) {
    $problems = New-Object System.Collections.Generic.List[string]
    # @($null) has one element, so drop nulls before counting.
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    if ($actions.Count -ne 1) {
        $problems.Add("expected exactly 1 action, found $($actions.Count)")
    } else {
        $act = $actions[0]
        if ("$($act.Execute)" -ine $Exe) { $problems.Add("action program is '$($act.Execute)'") }
        if ("$($act.Arguments)" -ine ('"{0}"' -f $ScriptPath)) { $problems.Add("action arguments are '$($act.Arguments)'") }
        if ("$($act.WorkingDirectory)".TrimEnd('\') -ine $WorkDir.TrimEnd('\')) {
            $problems.Add("action working directory is '$($act.WorkingDirectory)'")
        }
    }

    $principal = $Task.Principal
    if ((ConvertTo-UserSid "$($principal.UserId)") -ne $Sid.ToUpperInvariant()) {
        $problems.Add("principal is '$($principal.UserId)', not the current user")
    }
    if (-not (Test-EnumIs $principal.LogonType 'Interactive' 3)) { $problems.Add("logon type is '$($principal.LogonType)'") }
    if (-not (Test-EnumIs $principal.RunLevel 'Highest' 1)) { $problems.Add("run level is '$($principal.RunLevel)'") }

    $triggers = @($Task.Triggers | Where-Object { $null -ne $_ })
    if ($triggers.Count -ne 1) {
        $problems.Add("expected exactly 1 trigger, found $($triggers.Count)")
    } else {
        $trig = $triggers[0]
        if ("$($trig.CimClass.CimClassName)" -ine 'MSFT_TaskLogonTrigger') { $problems.Add('trigger is not a logon trigger') }
        if ($trig.Enabled -ne $true) { $problems.Add('logon trigger is disabled') }
        if ((ConvertTo-UserSid "$($trig.UserId)") -ne $Sid.ToUpperInvariant()) {
            $problems.Add("logon trigger user is '$($trig.UserId)', not the current user")
        }
        if ("$($trig.Delay)" -ne '' -or "$($trig.StartBoundary)" -ne '' -or "$($trig.EndBoundary)" -ne '') {
            $problems.Add('logon trigger has a delay or time window')
        }
    }

    $settings = $Task.Settings
    if ($settings.Enabled -ne $true -or "$($Task.State)" -ieq 'Disabled') { $problems.Add('task is disabled') }
    if (-not (Test-ZeroTime $settings.ExecutionTimeLimit)) {
        $problems.Add("execution time limit is '$($settings.ExecutionTimeLimit)', not PT0S")
    }
    if (-not (Test-EnumIs $settings.MultipleInstances 'IgnoreNew' 2)) {
        $problems.Add("multiple-instances policy is '$($settings.MultipleInstances)', not IgnoreNew")
    }
    if ($settings.DisallowStartIfOnBatteries -ne $false) { $problems.Add('task does not start on battery') }
    if ($settings.StopIfGoingOnBatteries -ne $false) { $problems.Add('task stops when going on battery') }
    if ($settings.StartWhenAvailable -ne $true) { $problems.Add('task does not start when available') }
    return $problems.ToArray()
}

function Test-TaskMatches($Task, [string]$Sid, [string]$Exe, [string]$ScriptPath, [string]$WorkDir) {
    return (@(Get-TaskMismatch $Task $Sid $Exe $ScriptPath $WorkDir).Count -eq 0)
}

function Get-ShortcutInfo([string]$Path) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    return [pscustomobject]@{
        TargetPath       = $link.TargetPath
        Arguments        = $link.Arguments
        WorkingDirectory = $link.WorkingDirectory
    }
}

# Only the two shortcuts this install writes: direct pythonw with the quoted main.py, or the
# elevated task launcher. Target, arguments and working directory must all match.
function Test-OwnShortcut([string]$Path, [string]$Exe, [string]$ScriptPath, [string]$WorkDir, [string]$Schtasks, [string]$Name) {
    $link = Get-ShortcutInfo $Path
    if ("$($link.WorkingDirectory)".TrimEnd('\') -ine $WorkDir.TrimEnd('\')) { return $false }
    $direct = ("$($link.TargetPath)" -ieq $Exe -and "$($link.Arguments)" -ieq ('"{0}"' -f $ScriptPath))
    $viaTask = ("$($link.TargetPath)" -ieq $Schtasks -and "$($link.Arguments)" -ieq ('/Run /TN "{0}"' -f $Name))
    return ($direct -or $viaTask)
}

# The Startup shortcut. $ViaTask is the admin-mode shape (runs the highest-privilege logon
# task). Otherwise it is the plain shape make_startup_shortcut.ps1 writes (direct pythonw).
function Write-StartupShortcut([string]$Path, [bool]$ViaTask, [string]$Schtasks) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    if ($ViaTask) {
        $link.TargetPath = $Schtasks
        $link.Arguments = '/Run /TN "{0}"' -f $TaskName
        $link.Description = 'Start Odicto (elevated logon task) at login'
    } else {
        $link.TargetPath = $Pyw
        $link.Arguments = '"{0}"' -f $MainPy
        $link.Description = 'Start Odicto dictation at login (direct pythonw)'
    }
    $link.WorkingDirectory = $Root
    $link.WindowStyle = 7
    $icon = Join-Path $Root 'assets\odicto.ico'
    if (Test-Path -LiteralPath $icon) { $link.IconLocation = "$icon,0" }
    $link.Save()
}

function Invoke-Configure {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $userName = $identity.Name
    $sid = $identity.User.Value
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    $lnk = Join-Path $startup 'Odicto.lnk'

    $schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'

    # Check everything that can refuse before changing anything. A task is reused only when it
    # is exactly the shape registered below; anything else is refused, never rebuilt.
    $existing = Get-OdictoTask
    if ($existing) {
        $problems = @(Get-TaskMismatch $existing $sid $Pyw $MainPy $Root)
        if ($problems.Count -gt 0) {
            throw "A scheduled task named '$TaskName' already exists and is not exactly this install's task ($($problems -join '; ')). This helper never deletes or edits tasks; remove it in Task Scheduler, then rerun Configure."
        }
    }
    if ((Test-Path -LiteralPath $lnk) -and -not (Test-OwnShortcut $lnk $Pyw $MainPy $Root $schtasks $TaskName)) {
        throw "$lnk does not belong to this install; leaving it untouched."
    }

    if ($existing) {
        Add-Step 'logon task already registered'
    } else {
        $taskAction = New-ScheduledTaskAction -Execute $Pyw -Argument ('"{0}"' -f $MainPy) -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
        $principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $taskAction -Trigger $trigger `
            -Principal $principal -Settings $settings -Description 'Odicto dictation at logon, elevated' | Out-Null
        $created = Get-OdictoTask
        if (-not $created) { throw "Task '$TaskName' was not found after registration" }
        $problems = @(Get-TaskMismatch $created $sid $Pyw $MainPy $Root)
        if ($problems.Count -gt 0) {
            throw "Task '$TaskName' was registered but does not read back as expected ($($problems -join '; ')); the always-admin marker was not written."
        }
        Add-Step 'logon task registered'
    }

    Write-StartupShortcut $lnk $true $schtasks
    Add-Step 'startup shortcut written'

    [IO.File]::WriteAllText($MarkerPath, "Odicto always-admin enabled $((Get-Date).ToUniversalTime().ToString('o'))`r`n",
        (New-Object Text.UTF8Encoding($false)))
    Add-Step 'marker written'
}

function Invoke-Disable {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User.Value
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    $lnk = Join-Path $startup 'Odicto.lnk'
    $schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'

    # Refuse before changing anything: only this install's own task and shortcut are touched.
    $existing = Get-OdictoTask
    if ($existing) {
        $problems = @(Get-TaskMismatch $existing $sid $Pyw $MainPy $Root)
        if ($problems.Count -gt 0) {
            throw "Nothing was changed: a scheduled task named '$TaskName' exists but is not exactly this install's task ($($problems -join '; ')). Remove it in Task Scheduler yourself if you want admin mode off."
        }
    }
    if ((Test-Path -LiteralPath $lnk) -and -not (Test-OwnShortcut $lnk $Pyw $MainPy $Root $schtasks $TaskName)) {
        throw "Nothing was changed: $lnk does not belong to this install."
    }

    # Elevated stop (ownership-validated by odicto.py stop). A failed stop cancels the rest.
    $code = Invoke-Native $Py @($OdictoPy, 'stop')
    if ($code -ne 0) { throw "Nothing else was changed: odicto.py stop failed (exit $code)." }
    Add-Step 'owner stopped'

    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
        if (Get-OdictoTask) { throw "Task '$TaskName' is still registered after removal; the marker was kept." }
        Add-Step 'logon task removed'
    } else {
        Add-Step 'no logon task to remove'
    }

    Write-StartupShortcut $lnk $false $schtasks
    Add-Step 'startup shortcut points at pythonw'

    if (Test-Path -LiteralPath $MarkerPath) { Remove-Item -LiteralPath $MarkerPath -Force }
    Add-Step 'marker deleted'
}

try {
    if (-not (Test-Admin)) {
        # Not elevated yet: nothing has been stopped or changed. Re-run this script through UAC.
        # WaitForExit on the process object, not Start-Process -Wait, which can also wait for the
        # long-lived pythonw this helper starts.
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
            '-Action', $Action, '-TimeoutSeconds', $TimeoutSeconds)
        if ($InstallStartup) { $argList += '-InstallStartup' }
        if ($ReceiptPath) { $argList += @('-ReceiptPath', ('"{0}"' -f $ReceiptPath)) }
        $psExe = (Get-Process -Id $PID).Path
        try {
            $elevatedProc = Start-Process -FilePath $psExe -ArgumentList $argList -Verb RunAs `
                -WindowStyle Hidden -PassThru
            $null = $elevatedProc.Handle
            $elevatedProc.WaitForExit()
        } catch {
            Add-Step 'elevation not granted'
            Finish 1 "Elevation was cancelled or failed; Odicto and its settings were left unchanged. $($_.Exception.Message)"
        }
        if ($null -eq $elevatedProc.ExitCode) {
            Finish 1 'The elevated helper ended without an exit code.'
        }
        # The elevated run wrote its own receipt; do not overwrite it.
        exit ([int]$elevatedProc.ExitCode)
    }

    Add-Step 'elevated'
    $needPythonw = $Action -in @('Start', 'Configure') -or $InstallStartup
    Assert-Runtime $needPythonw

    if ($Action -eq 'Disable') {
        Invoke-Disable
        Finish 0
    }

    if ($Action -eq 'Configure' -or $InstallStartup) {
        Invoke-Configure
        if ($Action -eq 'Configure') { Finish 0 }
    }

    if ($Action -eq 'Stop') {
        $code = Invoke-Native $Py @($OdictoPy, 'stop')
        if ($code -ne 0) { Finish $code "odicto.py stop failed (exit $code)" }
        Add-Step 'stopped'
        Finish 0
    }

    if ($Action -eq 'WaitReady') {
        $code = Invoke-Native $Py @($OdictoPy, 'wait-ready', '--timeout', "$TimeoutSeconds")
        if ($code -ne 0) { Finish $code "readiness not confirmed (exit $code)" }
        Add-Step 'ready'
        Finish 0
    }

    # Start: stop the old owner (ownership-validated by odicto.py stop); a failed stop
    # cancels the start so two instances can never stack keyboard hooks.
    $code = Invoke-Native $Py @($OdictoPy, 'stop')
    if ($code -ne 0) { Finish $code "stop failed, start cancelled (exit $code)" }
    Add-Step 'previous owner stopped'
    $null = Start-Process -FilePath $Pyw -ArgumentList ('"{0}"' -f $MainPy) -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru
    Add-Step 'started'
    $code = Invoke-Native $Py @($OdictoPy, 'wait-ready', '--timeout', "$TimeoutSeconds")
    if ($code -ne 0) { Finish $code "started, but readiness not confirmed (exit $code)" }
    Add-Step 'ready'
    Finish 0
} catch {
    Add-Step 'failed'
    Finish 1 $_.Exception.Message
}
