# Installer helpers for Odicto (Windows). Dot-sourced by install.ps1.
# Defines functions only; sourcing this file has no side effects.
#
# Rules these helpers keep:
#   * A Python or a .venv is judged by running it, never by checking that a file exists.
#   * Supported versions are 3.10 to 3.12 (numpy<2 has no wheels for 3.13+).
#   * Nothing is deleted. A broken .venv is renamed aside, never removed.
#   * Nothing here reads .env, prompt.txt or any other private file.

function Test-OdictoPythonVersion {
    param([int]$Major, [int]$Minor)
    return ($Major -eq 3 -and $Minor -ge 10 -and $Minor -le 12)
}

function Invoke-OdictoPythonProbe {
    # Runs "<exe> -I -c <print version>" with a hard timeout and returns
    # Ok / Major / Minor / Patch / Version / Detail. An executable that exists but
    # cannot start (for example a venv whose base Python was removed) is Ok=$false.
    param([Parameter(Mandatory)][string]$Exe, [int]$TimeoutSeconds = 20)
    $result = [pscustomobject]@{ Ok = $false; Major = 0; Minor = 0; Patch = 0; Version = ''; Detail = '' }
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        $result.Detail = 'file not found'
        return $result
    }
    $proc = $null
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = '-I -c "import sys;print(sys.version_info[0],sys.version_info[1],sys.version_info[2])"'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            try { $proc.Kill() } catch { }
            $result.Detail = "no answer after $TimeoutSeconds seconds"
            return $result
        }
        $proc.WaitForExit()
        $out = [string]$outTask.Result
        if ($proc.ExitCode -ne 0) {
            $firstError = ([string]$errTask.Result -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
            $result.Detail = "exit code $($proc.ExitCode)"
            if ($firstError) { $result.Detail += ": " + $firstError.Trim() }
            return $result
        }
        if ($out -match '(?m)^\s*(\d+) (\d+) (\d+)\s*$') {
            $result.Major = [int]$Matches[1]
            $result.Minor = [int]$Matches[2]
            $result.Patch = [int]$Matches[3]
            $result.Version = "$($result.Major).$($result.Minor).$($result.Patch)"
            $result.Ok = $true
        } else {
            $result.Detail = 'it did not report a Python version'
        }
    } catch {
        $result.Detail = $_.Exception.Message
    } finally {
        if ($proc) { $proc.Dispose() }
    }
    return $result
}

function Test-OdictoVenvInterpreter {
    # True for python.exe inside a virtual environment. Those are not base installs
    # and vanish with their venv, so they are never picked as the base Python.
    param([Parameter(Mandatory)][string]$Exe)
    $dir = Split-Path -Parent $Exe
    if (-not $dir) { return $false }
    if (Test-Path -LiteralPath (Join-Path $dir 'pyvenv.cfg')) { return $true }
    $parent = Split-Path -Parent $dir
    return ([bool]$parent -and (Test-Path -LiteralPath (Join-Path $parent 'pyvenv.cfg')))
}

function Test-OdictoPathUnder {
    param([string]$Path, [string]$Dir)
    if (-not $Path -or -not $Dir) { return $false }
    $prefix = ([IO.Path]::GetFullPath($Dir)).TrimEnd('\') + '\'
    return ([IO.Path]::GetFullPath($Path)).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-OdictoPythonCandidates {
    # Interpreters that are already installed, in discovery order: py launcher,
    # uv (its own and the ones it finds), PATH, then the default installer folders.
    # Nothing is downloaded. Paths are not checked yet; Find-OdictoPython runs each one.
    param([string]$UvPath)
    $found = New-Object System.Collections.Generic.List[string]

    $pyLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pyLauncher) {
        try {
            foreach ($line in @(& $pyLauncher.Source -0p 2>$null)) {
                if ("$line" -match '([A-Za-z]:\\.*?\.exe)\s*$') { $found.Add($Matches[1]) }
            }
        } catch { }
    }

    if ($UvPath) {
        try {
            $json = & $UvPath python list --only-installed --output-format json 2>$null
            if ($LASTEXITCODE -eq 0 -and $json) {
                foreach ($item in @(($json -join "`n") | ConvertFrom-Json)) {
                    if ($item.path) { $found.Add([string]$item.path) }
                }
            }
        } catch { }
    }

    foreach ($name in @('python3.12', 'python3.11', 'python3.10', 'python', 'python3')) {
        foreach ($cmd in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            if ($cmd.Source) { $found.Add($cmd.Source) }
        }
    }

    foreach ($minor in 12, 11, 10) {
        if ($env:LOCALAPPDATA) { $found.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python3$minor\python.exe")) }
        if ($env:ProgramFiles) { $found.Add((Join-Path $env:ProgramFiles "Python3$minor\python.exe")) }
    }
    return $found.ToArray()
}

function Get-OdictoUniquePaths {
    param([string[]]$Paths)
    $seen = @{}
    foreach ($p in $Paths) {
        if (-not $p) { continue }
        try { $key = ([IO.Path]::GetFullPath($p)).ToLowerInvariant() } catch { continue }
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $p
    }
}

function Find-OdictoPython {
    # Picks an installed Python 3.10-3.12: highest minor first, discovery order breaks
    # ties. Every candidate is run. Returns Path / Version / Minor, or $null when none
    # is installed (the caller then lets uv fetch one, or installs one).
    param([string]$UvPath, [string]$ExcludeUnder, [string[]]$Candidates)
    if (-not $PSBoundParameters.ContainsKey('Candidates')) {
        $Candidates = Get-OdictoPythonCandidates -UvPath $UvPath
    }
    $usable = @()
    foreach ($exe in @(Get-OdictoUniquePaths -Paths $Candidates)) {
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { continue }
        if ($ExcludeUnder -and (Test-OdictoPathUnder -Path $exe -Dir $ExcludeUnder)) { continue }
        if (Test-OdictoVenvInterpreter -Exe $exe) { continue }
        $probe = Invoke-OdictoPythonProbe -Exe $exe
        if ($probe.Ok -and (Test-OdictoPythonVersion -Major $probe.Major -Minor $probe.Minor)) {
            $usable += [pscustomobject]@{ Path = $exe; Version = $probe.Version; Minor = $probe.Minor }
        }
    }
    foreach ($minor in 12, 11, 10) {
        $hit = $usable | Where-Object { $_.Minor -eq $minor } | Select-Object -First 1
        if ($hit) { return $hit }
    }
    return $null
}

function Resolve-OdictoPythonChoice {
    # Checks the user's explicit -Python: a path to python.exe, or a version (3.10, 3.11, 3.12)
    # that must already be installed. A choice that is missing, will not run, or is
    # unsupported THROWS. It never falls back to another interpreter.
    param([Parameter(Mandatory)][string]$Spec, [string]$UvPath, [string]$ExcludeUnder, [string[]]$Candidates)
    $spec = $Spec.Trim()
    if ($spec -match '^3\.(\d+)$') {
        $minor = [int]$Matches[1]
        if (-not (Test-OdictoPythonVersion -Major 3 -Minor $minor)) {
            throw "-Python $spec is not supported. Odicto needs Python 3.10, 3.11 or 3.12 (numpy<2 has no wheels for 3.13+)."
        }
        if (-not $PSBoundParameters.ContainsKey('Candidates')) {
            $Candidates = Get-OdictoPythonCandidates -UvPath $UvPath
        }
        foreach ($exe in @(Get-OdictoUniquePaths -Paths $Candidates)) {
            if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { continue }
            if ($ExcludeUnder -and (Test-OdictoPathUnder -Path $exe -Dir $ExcludeUnder)) { continue }
            if (Test-OdictoVenvInterpreter -Exe $exe) { continue }
            $probe = Invoke-OdictoPythonProbe -Exe $exe
            if ($probe.Ok -and $probe.Major -eq 3 -and $probe.Minor -eq $minor) {
                return [pscustomobject]@{ Path = $exe; Version = $probe.Version; Minor = $probe.Minor }
            }
        }
        throw "-Python $spec was requested but no installed Python $spec was found. Install it, or pass -Python with the full path to its python.exe."
    }

    $exe = $spec
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        $cmd = $null
        if ($spec -notmatch '[\\/]') {
            $cmd = Get-Command $spec -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $cmd) { throw "-Python '$spec' was not found. Pass the full path to python.exe, or a version such as 3.12." }
        $exe = $cmd.Source
    }
    $exe = (Resolve-Path -LiteralPath $exe).ProviderPath
    if ($ExcludeUnder -and (Test-OdictoPathUnder -Path $exe -Dir $ExcludeUnder)) {
        throw "-Python '$spec' is inside the .venv that this installer manages. Pass a base Python install instead."
    }
    $probe = Invoke-OdictoPythonProbe -Exe $exe
    if (-not $probe.Ok) { throw "-Python '$spec' cannot run ($($probe.Detail)). Pass a working python.exe." }
    if (-not (Test-OdictoPythonVersion -Major $probe.Major -Minor $probe.Minor)) {
        throw "-Python '$spec' is Python $($probe.Version). Odicto needs Python 3.10, 3.11 or 3.12 (numpy<2 has no wheels for 3.13+). Pass another interpreter, or omit -Python to let the installer pick one."
    }
    return [pscustomobject]@{ Path = $exe; Version = $probe.Version; Minor = $probe.Minor }
}

function Get-OdictoVenvState {
    # Judges a venv by running its python.exe. State is one of:
    #   Missing      no .venv yet
    #   Healthy      runs, and is Python 3.10-3.12
    #   Unsupported  runs, but is another Python version
    #   Broken       exists but cannot run (copied from another machine, base Python removed, ...)
    param([Parameter(Mandatory)][string]$VenvDir)
    $state = [pscustomobject]@{ State = 'Missing'; Version = ''; Reason = '' }
    if (-not (Test-Path -LiteralPath $VenvDir)) { return $state }
    if (-not (Test-Path -LiteralPath $VenvDir -PathType Container)) {
        $state.State = 'Broken'
        $state.Reason = '.venv is not a directory'
        return $state
    }
    $python = Join-Path $VenvDir 'Scripts\python.exe'
    $probe = Invoke-OdictoPythonProbe -Exe $python
    if (-not $probe.Ok) {
        $state.State = 'Broken'
        $state.Reason = "Scripts\python.exe cannot run ($($probe.Detail))"
        $cfg = Join-Path $VenvDir 'pyvenv.cfg'
        if (Test-Path -LiteralPath $cfg -PathType Leaf) {
            foreach ($line in @(Get-Content -LiteralPath $cfg)) {
                if ("$line" -match '^\s*home\s*=\s*(.+?)\s*$') {
                    if (-not (Test-Path -LiteralPath $Matches[1])) {
                        $state.Reason += "; its base Python '$($Matches[1])' (pyvenv.cfg home) no longer exists"
                    }
                    break
                }
            }
        }
        return $state
    }
    $state.Version = $probe.Version
    if (Test-OdictoPythonVersion -Major $probe.Major -Minor $probe.Minor) {
        $state.State = 'Healthy'
    } else {
        $state.State = 'Unsupported'
        $state.Reason = "it runs Python $($probe.Version); Odicto needs 3.10, 3.11 or 3.12"
    }
    return $state
}

function New-OdictoStartMenuShortcuts {
    # Creates or refreshes the Start Menu folder "Odicto" with three shortcuts that run the
    # root launchers: "Odicto" (start_dictation.bat), "Odicto Setup" (setup.bat) and
    # "Stop Odicto" (stop_dictation.bat). Each has the Odicto icon and the install folder as
    # its working directory. Safe to run again: the same three files are rewritten, and nothing
    # else in the folder is touched. -ProgramsDir overrides the current user's Start Menu
    # Programs folder (tests use a scratch folder). Returns the folder path.
    param([Parameter(Mandatory)][string]$Root, [string]$ProgramsDir)
    $rootFull = ([IO.Path]::GetFullPath($Root)).TrimEnd('\')
    if (-not $ProgramsDir) { $ProgramsDir = [Environment]::GetFolderPath('Programs') }
    if (-not $ProgramsDir) { throw 'The Start Menu Programs folder of this user could not be found.' }
    $folder = Join-Path $ProgramsDir 'Odicto'
    $icon = Join-Path $rootFull 'assets\odicto.ico'
    $entries = @(
        @{ Name = 'Odicto';       Target = 'start_dictation.bat'; Style = 1; Text = 'Start Odicto dictation' },
        @{ Name = 'Odicto Setup'; Target = 'setup.bat';           Style = 7; Text = 'Open the Odicto setup page' },
        @{ Name = 'Stop Odicto';  Target = 'stop_dictation.bat';  Style = 1; Text = 'Stop Odicto' }
    )
    foreach ($entry in $entries) {
        if (-not (Test-Path -LiteralPath (Join-Path $rootFull $entry.Target) -PathType Leaf)) {
            throw "Missing $($entry.Target) in $rootFull."
        }
    }
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($entry in $entries) {
        $link = $shell.CreateShortcut((Join-Path $folder ($entry.Name + '.lnk')))
        $link.TargetPath = Join-Path $rootFull $entry.Target
        $link.Arguments = ''
        $link.WorkingDirectory = $rootFull
        $link.WindowStyle = $entry.Style
        $link.Description = $entry.Text
        if (Test-Path -LiteralPath $icon -PathType Leaf) { $link.IconLocation = "$icon,0" }
        $link.Save()
    }
    return $folder
}

function Get-OdictoProcessSnapshot {
    # One process list. A failed query throws (the caller treats that as "in use").
    @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
}

function Get-OdictoFolderSpellings {
    # The spellings of one folder that a process path or command line can contain: the long
    # form (GetFullPath, which in Windows PowerShell 5.1 expands 8.3 names) and the 8.3 short
    # form, for example C:\Users\RUNNER~1\... A folder that does not exist, or a volume without
    # 8.3 names, gives only the long form. No trailing backslash.
    param([Parameter(Mandatory)][string]$Dir)
    $long = ([IO.Path]::GetFullPath($Dir)).TrimEnd('\')
    $long
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        if ($fso.FolderExists($long)) {
            $short = ([string]$fso.GetFolder($long).ShortPath).TrimEnd('\')
            if ($short -and $short -ine $long) { $short }
        }
    } catch { }
}

function Get-OdictoVenvUsers {
    # Processes that run from this .venv, or run this install's main.py. A process can name
    # the install folder in any spelling: long, 8.3 short, partly short (C:\Users\RUNNER~1\...)
    # or \\?\-prefixed. The path and command line are matched as written against the long and
    # short spellings, and again after each rooted path in them is expanded to its long form.
    # Junction, subst and mapped-drive spellings are not resolved; dictation.lock still guards
    # Odicto itself. A failed process query throws: the caller must treat "cannot tell" as "in
    # use". A Python process whose path or command line cannot be read (another user, elevated)
    # also counts as in use, but only if it stays unreadable: a process that is starting or
    # exiting can lack metadata for a moment. The list is taken again after a short wait. A
    # process is dropped only when it is gone, or readable and not using this install. Any
    # Python process that is unreadable on the last look stays a user (fail closed).
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$VenvDir,
          [int]$Retries = 3, [int]$DelayMs = 200)
    $prefixes = @(Get-OdictoFolderSpellings -Dir $VenvDir | ForEach-Object { $_ + '\' })
    $mainPys = @(Get-OdictoFolderSpellings -Dir $Root | ForEach-Object { $_ + '\main.py' })
    # python.exe, pythonw.exe, python3.12.exe, or the 8.3 name of a longer one (PYTHON~1.EXE).
    $pythonName = '^(?:pythonw?[\d.]*|python~\d+)\.exe$'
    $ignoreCase = [StringComparison]::OrdinalIgnoreCase
    $scan = {
        $found = @{ Users = @(); Unreadable = @() }
        foreach ($p in @(Get-OdictoProcessSnapshot)) {
            if ($p.ProcessId -eq $PID) { continue }
            $exe = [string]$p.ExecutablePath
            $cmd = [string]$p.CommandLine
            # The executable path, then each quoted or bare word of the command line, in long form.
            # WMI reports a process in the spelling that started it (C:\Users\RUNNER~1\..., or
            # \\?\C:\...); GetFullPath expands 8.3 names in Windows PowerShell 5.1. A word that is
            # not a rooted path, or that GetFullPath rejects, gives ''. Inline: a function call per
            # process doubles the time of a look.
            $words = @($exe)
            foreach ($m in [regex]::Matches($cmd, '"([^"]*)"|([^\s"]+)')) { $words += $m.Groups[1].Value + $m.Groups[2].Value }
            $long = @(foreach ($word in $words) {
                $word = $word -replace '^\\\\\?\\(?=[A-Za-z]:\\)', ''
                if ($word -match '^(?:[A-Za-z]:[\\/]|\\\\)') { try { [IO.Path]::GetFullPath($word) } catch { '' } } else { '' }
            })
            $isPython = ([string]$p.Name) -match $pythonName -or
                        [IO.Path]::GetFileName($long[0]) -match $pythonName
            # One line per path, as written and in long form (a path never holds a line break).
            $lines = "`n" + ((@($exe) + $long) -join "`n") + "`n"
            $isUser = $false
            foreach ($prefix in $prefixes) {
                if ($cmd.IndexOf($prefix, $ignoreCase) -ge 0 -or
                    $lines.IndexOf("`n" + $prefix, $ignoreCase) -ge 0) { $isUser = $true }
            }
            if ($isPython) {
                foreach ($mainPy in $mainPys) {
                    if ($cmd.IndexOf($mainPy, $ignoreCase) -ge 0 -or
                        $lines.IndexOf("`n" + $mainPy + "`n", $ignoreCase) -ge 0) { $isUser = $true }
                }
            }
            if ($isUser) {
                $found.Users += $p
            } elseif ($isPython -and (-not $exe -or -not $cmd)) {
                $found.Unreadable += $p
            }
        }
        $found
    }
    # Every look judges every Python process that is unreadable at that moment, not only the
    # ones seen first. The last look decides: any process unreadable then (including a new
    # one, or a reused PID with a new start time) stays a user.
    $now = & $scan
    for ($i = 0; $i -lt $Retries -and $now.Unreadable.Count -gt 0; $i++) {
        Start-Sleep -Milliseconds $DelayMs
        $now = & $scan
    }
    return @($now.Users) + @($now.Unreadable)
}

function Move-OdictoVenvAside {
    # Renames <Root>\.venv to <Root>\.venv-backup-<timestamp> (a unique name) and returns
    # the new path. It never deletes. It refuses (throws) when the target is not exactly
    # <Root>\.venv, is a link, or is in use by a running process or Odicto (lock held).
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$VenvDir)
    $rootFull = ([IO.Path]::GetFullPath($Root)).TrimEnd('\')
    $venvFull = ([IO.Path]::GetFullPath($VenvDir)).TrimEnd('\')
    if ((Split-Path -Leaf $venvFull) -cne '.venv' -or (Split-Path -Parent $venvFull) -ine $rootFull) {
        throw "Refusing to move '$venvFull': it is not the .venv directly inside the install folder."
    }
    $item = Get-Item -LiteralPath $venvFull -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) { throw "Refusing to move '$venvFull': it is not a directory." }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to move '$venvFull': it is a link. Remove or fix the link yourself, then re-run."
    }
    # The real guard: hold the same byte lock the app takes on dictation.lock (msvcrt byte 0)
    # for the whole check-and-rename. A running or starting Odicto owns it, so we refuse.
    # The file is opened or created, never truncated, written or deleted.
    $quit = "Quit Odicto from its tray icon (right-click > Quit Odicto), then re-run."
    $lock = $null
    try {
        try {
            $lock = New-Object System.IO.FileStream((Join-Path $rootFull 'dictation.lock'),
                [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
            $lock.Lock(0, 1)
        } catch {
            $why = $_.Exception.Message
            if ($lock) { $lock.Dispose(); $lock = $null }
            throw "Odicto's install lock (dictation.lock) is held or cannot be opened ($why). $quit"
        }
        try {
            $users = @(Get-OdictoVenvUsers -Root $rootFull -VenvDir $venvFull)
        } catch {
            throw "Cannot tell whether Odicto is running ($($_.Exception.Message)). $quit"
        }
        if ($users.Count -gt 0) {
            $names = ($users | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }) -join ', '
            throw "Odicto or another program is using this .venv: $names. $quit"
        }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $n = 0
        do {
            $suffix = if ($n -eq 0) { '' } else { "-$n" }
            $backup = Join-Path $rootFull ".venv-backup-$stamp$suffix"
            $n++
        } while (Test-Path -LiteralPath $backup)
        [IO.Directory]::Move($venvFull, $backup)
        return $backup
    } finally {
        if ($lock) {
            try { $lock.Unlock(0, 1) } catch { }
            $lock.Dispose()
        }
    }
}

function New-OdictoVenv {
    # Creates <VenvDir> and confirms by running it. $Python is a Find/Resolve result, or
    # $null to let uv fetch Python 3.12 (needs $UvPath). A chosen interpreter is never
    # replaced by a download.
    param([Parameter(Mandatory)][string]$VenvDir, $Python, [string]$UvPath)
    if ($UvPath) {
        if ($Python) {
            & $UvPath venv --python $Python.Path --no-python-downloads $VenvDir | Out-Host
        } else {
            & $UvPath venv --python 3.12 $VenvDir | Out-Host
        }
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit code $LASTEXITCODE). Fix the error above and re-run install.ps1." }
    } else {
        if (-not $Python) { throw 'No Python 3.10-3.12 is available to create the .venv.' }
        & $Python.Path -m venv $VenvDir | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (exit code $LASTEXITCODE). Fix the error above and re-run install.ps1." }
    }
    $state = Get-OdictoVenvState -VenvDir $VenvDir
    if ($state.State -ne 'Healthy') {
        throw "The new .venv does not work ($($state.State): $($state.Reason)). The old one, if any, was kept beside it."
    }
    return $state
}
