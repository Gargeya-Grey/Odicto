#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Inspect','Enable','Disable','Migrate','MigrateAdmin')][string]$Action,
    [Parameter(Mandatory=$true)][string]$HomePath,
    [string]$SourceRoot,
    [string]$TransactionPath,
    [string]$ReceiptPath,
    [string]$CallerSid
)
$ErrorActionPreference = 'Stop'
$ManagedHome = [IO.Path]::GetFullPath($HomePath).TrimEnd('\')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$launcher = Join-Path $ManagedHome 'bin\odicto.ps1'
$normalized = $ManagedHome.ToLowerInvariant()
$sha = [Security.Cryptography.SHA256]::Create()
$hash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)))).Replace('-','').ToLowerInvariant().Substring(0,12)
$ManagedTaskName = 'Odicto-managed-' + $hash
$ManagedArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" admin-task' -f $launcher
$marker = Join-Path $ManagedHome 'data\.odicto-admin-enabled'
function Write-Result([bool]$Ok, [string]$ErrorText, $Result) {
    $value = @{ok=$Ok;error=$ErrorText;result=$Result;home=$ManagedHome}
    $json = $value | ConvertTo-Json -Depth 8 -Compress
    if ($ReceiptPath) { [IO.File]::WriteAllText($ReceiptPath,$json,(New-Object Text.UTF8Encoding($false))) }
    if ($Action -eq 'Inspect') { [Console]::Out.WriteLine($json) }
}
# The elevated child writes receipts, the marker and backups that Administrators own. The same
# account reads them only through an explicit inheritable grant on their directory. Approval by a
# different account is refused: that account must not own this home's startup or its files.
function Assert-SameAccount([string]$Caller, [string]$Current) {
    if ($Caller -and $Caller -ne $Current) {
        throw 'Administrator approval came from a different account; nothing changed. Sign in as the account that owns this Odicto installation'
    }
}
# DACL only (Access section): no SACL privilege. Existing entries stay; no other account is added.
function Grant-SameAccount([string]$Path, [string]$UserSid) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'A managed private directory is linked or missing; it was preserved'
    }
    $user = New-Object Security.Principal.SecurityIdentifier($UserSid)
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $acl = [IO.Directory]::GetAccessControl($item.FullName, [Security.AccessControl.AccessControlSections]::Access)
    # A deny entry (explicit or inherited) can block the same account despite an allow; never report success then.
    if (@($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { "$($_.AccessControlType)" -eq 'Deny' }).Count) {
        throw 'A managed private directory has a deny entry in its access list; it was preserved'
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IdentityReference -eq $user -and $rule.AccessControlType -eq 'Allow' -and
            ($rule.FileSystemRights -band $full) -eq $full -and $rule.InheritanceFlags -eq $inherit -and
            $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None) { return }
    }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($user, $full, $inherit, 'None', 'Allow')))
    [IO.Directory]::SetAccessControl($item.FullName, $acl)
}
function Read-Task([string]$Name) {
    try { return Get-ScheduledTask -TaskPath '\' -TaskName $Name -ErrorAction Stop }
    catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound' -or "$($_.FullyQualifiedErrorId)" -like 'CmdletizationQuery_NotFound*') { return $null }
        throw
    }
}
function Assert-ManagedTask($Task) {
    if (-not $Task) { return }
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    if ($actions.Count -ne 1 -or $actions[0].Execute -ine $PowerShellExe -or
        $actions[0].Arguments -ine $ManagedArguments -or $actions[0].WorkingDirectory.TrimEnd('\') -ine $ManagedHome -or
        (ConvertTo-UserSid $Task.Principal.UserId) -ne $sid -or
        -not (Test-EnumIs $Task.Principal.RunLevel 'Highest' 1) -or
        -not (Test-EnumIs $Task.Principal.LogonType 'Interactive' 3) -or
        @($Task.Triggers | Where-Object { $null -ne $_ }).Count -ne 0 -or
        -not (Test-EnumIs $Task.Settings.MultipleInstances 'IgnoreNew' 2) -or
        -not (Test-ZeroTime $Task.Settings.ExecutionTimeLimit) -or
        $Task.Settings.Enabled -ne $true -or "$($Task.State)" -ieq 'Disabled' -or $Task.Settings.DisallowStartIfOnBatteries -ne $false -or
        $Task.Settings.StopIfGoingOnBatteries -ne $false) {
        throw 'Managed task has unknown ownership or settings; it was preserved'
    }
}
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


function Inspect-Source {
    $result = @{admin=$false;autostart=$false;source_task=$false;source_shortcut=$false}
    if (-not $SourceRoot) { return $result }
    $script:SourceRoot = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
    $sourceMarker = Join-Path $SourceRoot '.odicto-admin-enabled'
    $result.admin = Test-Path -LiteralPath $sourceMarker -PathType Leaf
    $task = Read-Task 'Odicto'
    if ($task) {
        $problems = @(Get-TaskMismatch $task $sid (Join-Path $SourceRoot '.venv\Scripts\pythonw.exe') (Join-Path $SourceRoot 'main.py') $SourceRoot)
        if ($problems.Count -eq 0) { $result.source_task=$true; $result.autostart=$true }
        else { throw "Source startup task 'Odicto' ownership or settings cannot be established; it was preserved. Inspect and retire it in Task Scheduler before migrating, then enable managed autostart if wanted." }
    }
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Odicto.lnk'
    if (Test-Path -LiteralPath $lnk) {
        if (-not (Test-OwnShortcut $lnk (Join-Path $SourceRoot '.venv\Scripts\pythonw.exe') (Join-Path $SourceRoot 'main.py') $SourceRoot (Join-Path $env:SystemRoot 'System32\schtasks.exe') 'Odicto')) {
            throw 'Source startup shortcut ownership cannot be established; it was preserved'
        }
        $result.source_shortcut=$true; $result.autostart=$true
    }
    return $result
}
try {
    $ownership = Get-Content -Raw -LiteralPath (Join-Path $ManagedHome '.odicto-managed.json') | ConvertFrom-Json
    if ($ownership.schema -ne 1 -or $ownership.home -cne $ManagedHome) { throw 'Managed home ownership is invalid' }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Stable managed launcher is missing' }
    $known = @($ownership.launchers.'bin/odicto.ps1')
    if ($known -notcontains (Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant()) { throw 'Stable launcher has unknown ownership' }
    $existing = Read-Task $ManagedTaskName
    Assert-ManagedTask $existing
    $source = Inspect-Source
    if ($Action -eq 'Inspect') { Write-Result $true '' $source; exit 0 }
    if ($Action -eq 'Migrate' -and -not $source.source_task -and -not $source.source_shortcut) { Write-Result $true '' @{}; exit 0 }
    if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $PSCommandPath),'-Action',$Action,'-HomePath',('"{0}"' -f $ManagedHome))
        if ($SourceRoot) { $arguments += @('-SourceRoot',('"{0}"' -f $SourceRoot)) }
        if ($TransactionPath) { $arguments += @('-TransactionPath',('"{0}"' -f $TransactionPath)) }
        if ($ReceiptPath) { $arguments += @('-ReceiptPath',('"{0}"' -f $ReceiptPath)) }
        $arguments += @('-CallerSid',$sid)
        $child = Start-Process -FilePath $PowerShellExe -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
        $null = $child.Handle; $child.WaitForExit(); exit $child.ExitCode
    }
    Assert-SameAccount $CallerSid $sid
    Grant-SameAccount $ManagedHome $sid
    Grant-SameAccount (Join-Path $ManagedHome 'data') $sid
    if ($Action -in @('Migrate','MigrateAdmin')) {
        if (-not $TransactionPath -or -not $SourceRoot) { throw 'Migration requires its transaction directory and source' }
        $transaction = [IO.Path]::GetFullPath($TransactionPath)
        $expected = Join-Path $ManagedHome 'migrations'
        if (-not $transaction.StartsWith($expected + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Migration backup escaped the managed home' }
        $pending = Get-Content -Raw -LiteralPath (Join-Path $SourceRoot '.odicto-migration.json') | ConvertFrom-Json
        $journal = Get-Content -Raw -LiteralPath (Join-Path $ManagedHome 'migration.json') | ConvertFrom-Json
        if ($journal.token -cne $pending.token -or $journal.source -cne $SourceRoot -or $journal.home -cne $ManagedHome -or $journal.state -notin @('pending','failed') -or -not (Get-Process -Id $journal.supervisor_pid -ErrorAction SilentlyContinue)) { throw 'Migration supervisor no longer owns its reservation' }
        if ($pending.schema -ne 1 -or $pending.home -cne $ManagedHome -or $pending.source -cne $SourceRoot -or $pending.state -ne 'pending') { throw 'Migration reservation is invalid' }
        Grant-SameAccount $transaction $sid
        if ($source.source_task) {
            $backup = Join-Path $transaction 'source-task.xml'
            if (-not (Test-Path -LiteralPath $backup)) { Export-ScheduledTask -TaskName 'Odicto' -TaskPath '\' | Set-Content -LiteralPath $backup -Encoding UTF8 }
        }
        $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Odicto.lnk'
        if ($source.source_shortcut -and -not (Test-Path -LiteralPath (Join-Path $transaction 'Odicto.lnk'))) { Copy-Item -LiteralPath $lnk -Destination (Join-Path $transaction 'Odicto.lnk') }
    }
    if ($Action -in @('Enable','MigrateAdmin')) {
        if (-not $existing) {
            $taskAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $ManagedArguments -WorkingDirectory $ManagedHome
            $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $ManagedTaskName -TaskPath '\' -Action $taskAction -Principal $principal -Settings $settings -Description ('Odicto managed home ' + $ManagedHome) | Out-Null
        }
        $created = Read-Task $ManagedTaskName
        if (-not $created) { throw 'The managed task was not found after registration' }
        Assert-ManagedTask $created
        $value = @{schema=1;home=$ManagedHome;task=$ManagedTaskName} | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($marker,$value,(New-Object Text.UTF8Encoding($false)))
    }
    if ($Action -eq 'Disable') {
        if ($existing) { Unregister-ScheduledTask -TaskName $ManagedTaskName -TaskPath '\' -Confirm:$false }
        if (Read-Task $ManagedTaskName) { throw 'The managed task still exists; the marker was preserved' }
        if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
    }
    if ($Action -in @('Migrate','MigrateAdmin')) {
        if ($source.source_task) { Unregister-ScheduledTask -TaskName 'Odicto' -TaskPath '\' -Confirm:$false }
        if ($source.source_shortcut) { Remove-Item -LiteralPath $lnk }
        $after = Inspect-Source
        if ($after.source_task -or $after.source_shortcut) { throw 'Source startup remains registered' }
    }
    Write-Result $true '' @{}
    exit 0
} catch {
    Write-Result $false "$($_.Exception.Message)" @{}
    exit 1
}
