"""Opt-in always-admin regressions.

Every native boundary is replaced: no test asks for UAC, registers a task, stops or starts
Odicto, or reads .env / prompt.txt. On Windows the helper's task-reuse and shortcut-ownership
functions run for real against fake objects (TestHelperDecisions) and the helper is parsed,
never run. UAC cancellation and stop-failure ordering are only source-order checks
(TestStaticOrdering); the elevated lifecycle itself is verified separately at runtime.
"""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import windows_admin

ROOT = Path(__file__).resolve().parent.parent
HELPER_PS1 = ROOT / "scripts" / "windows" / "start_dictation_admin.ps1"
CREATE_NO_WINDOW = 0x08000000


class _AdminRootCase(unittest.TestCase):
    """A scratch install folder whose path contains spaces, with a stub helper script."""

    marker = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="odicto admin test ")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(os.path.realpath(tmp.name), "Odicto Install")
        scripts = os.path.join(self.root, "scripts", "windows")
        os.makedirs(scripts)
        self.helper = os.path.join(scripts, "start_dictation_admin.ps1")
        with open(self.helper, "w", encoding="utf-8") as handle:
            handle.write("# stub\n")
        if self.marker:
            with open(os.path.join(self.root, ".odicto-admin-enabled"), "w", encoding="ascii") as handle:
                handle.write("enabled\n")
        self.system_root = os.path.join(os.path.realpath(tmp.name), "Win Dir")
        self.powershell = os.path.join(
            self.system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        env = patch.dict(os.environ, {"SystemRoot": self.system_root})
        env.start()
        self.addCleanup(env.stop)


class TestRunIfNeeded(_AdminRootCase):
    def setUp(self):
        super().setUp()
        self.windows = self._patch(windows_admin, "_is_windows", return_value=True)
        self.probe = self._patch(windows_admin, "_is_user_an_admin", return_value=False)
        self.run = self._patch(windows_admin.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0))

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def test_not_windows_never_probes_or_launches(self):
        self.windows.return_value = False
        self.assertIsNone(windows_admin.run_if_needed(self.root))
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_already_elevated_runs_normally(self):
        self.probe.return_value = True
        self.assertIsNone(windows_admin.run_if_needed(self.root))
        self.run.assert_not_called()

    def test_no_marker_never_probes_or_launches(self):
        os.remove(os.path.join(self.root, ".odicto-admin-enabled"))
        self.assertIsNone(windows_admin.run_if_needed(self.root))
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_configured_non_admin_launches_hidden_helper_with_absolute_spaced_paths(self):
        self.run.return_value = SimpleNamespace(returncode=0)
        self.assertEqual(windows_admin.run_if_needed(self.root), 0)
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command, [
            self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", self.helper, "-Action", "Start"])
        self.assertTrue(os.path.isabs(command[0]) and os.path.isabs(command[5]))
        self.assertIn(" ", command[5])
        kwargs = self.run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertFalse(kwargs.get("shell", False))

    def test_accepts_path_objects(self):
        windows_admin.run_if_needed(Path(self.root))
        self.assertEqual(self.run.call_args.args[0][5], self.helper)

    def test_action_and_timeout_are_forwarded(self):
        self.run.return_value = SimpleNamespace(returncode=0)
        windows_admin.run_if_needed(self.root, "WaitReady", 30.0)
        command = self.run.call_args.args[0]
        self.assertEqual(command[-4:], ["-Action", "WaitReady", "-TimeoutSeconds", "30"])
        windows_admin.run_if_needed(self.root, "WaitReady", 30.2)
        self.assertEqual(self.run.call_args.args[0][-1], "31")
        windows_admin.run_if_needed(self.root, "Stop")
        self.assertNotIn("-TimeoutSeconds", self.run.call_args.args[0])

    def test_helper_exit_code_is_returned(self):
        self.run.return_value = SimpleNamespace(returncode=5)
        self.assertEqual(windows_admin.run_if_needed(self.root, "Stop"), 5)

    def test_unknown_admin_state_is_an_error_not_a_normal_run(self):
        self.probe.side_effect = OSError("shell32 unavailable")
        with self.assertRaisesRegex(RuntimeError, "elevation state"):
            windows_admin.run_if_needed(self.root)
        self.run.assert_not_called()

    def test_missing_helper_is_an_error(self):
        os.remove(self.helper)
        with self.assertRaisesRegex(RuntimeError, "helper is missing"):
            windows_admin.run_if_needed(self.root)
        self.run.assert_not_called()

    def test_powershell_failure_is_an_error(self):
        self.run.side_effect = FileNotFoundError("powershell.exe")
        with self.assertRaisesRegex(RuntimeError, "PowerShell could not run"):
            windows_admin.run_if_needed(self.root)

    def test_missing_system_root_is_an_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SystemRoot"):
                windows_admin.run_if_needed(self.root)
        self.run.assert_not_called()

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            windows_admin.run_if_needed(self.root, "Format")
        self.run.assert_not_called()


class TestCliDelegation(unittest.TestCase):
    def setUp(self):
        import odicto
        import platforms
        self.cli = odicto
        self.platforms = platforms
        self.admin = self._patch(windows_admin, "run_if_needed", return_value=None)
        self.kill = self._patch(platforms, "kill_other_odicto_processes", return_value=[])
        self.release = self._patch(platforms, "release_lock")
        self.spawn = self._patch(platforms, "spawn_detached")

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def test_stop_is_delegated_before_any_kill(self):
        self.admin.return_value = 9
        self.assertEqual(self.cli.cmd_stop(SimpleNamespace()), 9)
        self.admin.assert_called_once_with(self.cli._repo_root(), "Stop", None)
        self.kill.assert_not_called()
        self.release.assert_not_called()

    def test_stop_unchanged_when_not_delegated(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.cmd_stop(SimpleNamespace()), 0)
        self.kill.assert_called_once()
        self.release.assert_called_once()

    def test_start_is_delegated_before_any_spawn(self):
        self.admin.return_value = 0
        self.assertEqual(self.cli.cmd_start(SimpleNamespace()), 0)
        self.admin.assert_called_once_with(self.cli._repo_root(), "Start", None)
        self.spawn.assert_not_called()

    def test_start_unchanged_when_not_delegated(self):
        self.spawn.return_value = SimpleNamespace(pid=4321)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.cmd_start(SimpleNamespace()), 0)
        self.spawn.assert_called_once()

    def test_wait_ready_forwards_its_timeout(self):
        self.admin.return_value = 1
        self.assertEqual(self.cli.cmd_wait_ready(SimpleNamespace(timeout=12.5)), 1)
        self.admin.assert_called_once_with(self.cli._repo_root(), "WaitReady", 12.5)

    def test_unusable_admin_mode_fails_instead_of_running_normally(self):
        self.admin.side_effect = RuntimeError("elevation state unknown")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.cli.cmd_stop(SimpleNamespace()), 1)
        self.assertIn("elevation state unknown", err.getvalue())
        self.kill.assert_not_called()


class TestSetupRestart(unittest.TestCase):
    def setUp(self):
        import platforms
        import setup_web
        self.setup_web = setup_web
        self.platforms = platforms
        self.admin = self._patch(windows_admin, "run_if_needed", return_value=None)
        self.kill = self._patch(platforms, "kill_other_odicto_processes", return_value=[])
        self.spawn = self._patch(platforms, "spawn_detached")

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def test_elevated_restart_replaces_the_in_process_kill(self):
        self.admin.return_value = 0
        message = self.setup_web.restart_odicto()
        self.assertIn("restarting", message)
        self.admin.assert_called_once_with(str(self.setup_web.ROOT), "Start")
        self.kill.assert_not_called()
        self.spawn.assert_not_called()

    def test_failed_elevated_restart_raises_and_kills_nothing(self):
        self.admin.return_value = 3
        with self.assertRaisesRegex(RuntimeError, "exit code 3"):
            self.setup_web.restart_odicto()
        self.kill.assert_not_called()
        self.spawn.assert_not_called()

    def test_unconfigured_install_keeps_the_same_level_restart(self):
        self.assertIsInstance(self.setup_web.restart_odicto(), str)
        self.admin.assert_called_once()
        self.kill.assert_called_once()


class TestRootBootstrap(unittest.TestCase):
    """Root main.py decides on elevation after the path insert and before the app runs."""

    def setUp(self):
        saved = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), saved))
        self.real_run_path = runpy.run_path
        self.calls = []
        admin = patch.object(windows_admin, "run_if_needed",
                             side_effect=lambda root: self.calls.append(("admin", root)) or self.exit_code)
        app = patch.object(runpy, "run_path",
                           side_effect=lambda *a, **k: self.calls.append(("app", a[0])))
        admin.start()
        app.start()
        self.addCleanup(admin.stop)
        self.addCleanup(app.stop)
        self.exit_code = None

    def run_root_main(self):
        return self.real_run_path(str(ROOT / "main.py"), run_name="__main__")

    def test_unconfigured_start_runs_the_app_after_the_admin_check(self):
        self.run_root_main()
        self.assertEqual([name for name, _ in self.calls], ["admin", "app"])
        self.assertEqual(self.calls[0][1], ROOT)
        self.assertEqual(self.calls[1][1], str(ROOT / "app" / "main.py"))

    def test_helper_exit_code_ends_the_launcher_without_the_app(self):
        self.exit_code = 7
        with self.assertRaises(SystemExit) as raised:
            self.run_root_main()
        self.assertEqual(raised.exception.code, 7)
        self.assertEqual([name for name, _ in self.calls], ["admin"])

    def test_zero_from_helper_is_still_an_exit_not_a_second_start(self):
        self.exit_code = 0
        with self.assertRaises(SystemExit) as raised:
            self.run_root_main()
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("app", [name for name, _ in self.calls])

    def test_unusable_admin_mode_never_starts_unelevated(self):
        windows_admin.run_if_needed.side_effect = RuntimeError("cannot read elevation")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.run_root_main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.calls, [])

    def test_unusable_admin_mode_exits_one_when_stderr_is_missing_or_broken(self):
        # pythonw has no stderr (None), and a closed pipe makes writes raise.
        class BrokenStderr:
            def write(self, text):
                raise OSError("pipe closed")

            def flush(self):
                raise OSError("pipe closed")

        for name, stream in (("none", None), ("broken", BrokenStderr())):
            with self.subTest(stderr=name):
                self.calls.clear()
                windows_admin.run_if_needed.side_effect = RuntimeError("cannot read elevation")
                with patch.object(sys, "stderr", stream):
                    with self.assertRaises(SystemExit) as raised:
                        self.run_root_main()
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(self.calls, [])


# The helper's decisions run here as real PowerShell, but only its function definitions are
# extracted from the file (parsed to an AST, never dot-sourced as a script), so its main flow,
# UAC, Task Scheduler and process stop/start code never execute. Every object the functions
# read is a fake, and stub cmdlets that throw guard the native calls.
_HARNESS_PS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:ODICTO_HELPER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'helper does not parse' }
$wanted = @('ConvertTo-UserSid', 'Test-EnumIs', 'Test-ZeroTime', 'Get-TaskMismatch', 'Test-TaskMatches', 'Test-OwnShortcut')
$defs = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $n.Name }, $true))
if ($defs.Count -ne $wanted.Count) { throw "found $($defs.Count) of $($wanted.Count) functions" }
foreach ($def in $defs) { Invoke-Expression $def.Extent.Text }

foreach ($native in 'Get-ScheduledTask', 'Register-ScheduledTask', 'Unregister-ScheduledTask', 'Set-ScheduledTask',
                    'Start-Process', 'Stop-Process', 'Start-ScheduledTask') {
    Set-Item -Path "function:$native" -Value ([scriptblock]::Create("throw 'native call $native from a unit harness'"))
}

$sid = 'S-1-5-21-111-222-333-1001'
$otherSid = 'S-1-5-21-111-222-333-1002'
$exe = 'C:\Odicto Install\.venv\Scripts\pythonw.exe'
$main = 'C:\Odicto Install\main.py'
$root = 'C:\Odicto Install'
$schtasks = 'C:\Windows\System32\schtasks.exe'
$realSid = ${function:ConvertTo-UserSid}

function New-FakeTask {
    [pscustomobject]@{
        Actions   = @([pscustomobject]@{ Execute = $exe; Arguments = "`"$main`""; WorkingDirectory = $root })
        Principal = [pscustomobject]@{ UserId = $sid; LogonType = 'Interactive'; RunLevel = 'Highest' }
        Triggers  = @([pscustomobject]@{
            CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }
            Enabled = $true; UserId = $sid; Delay = $null; StartBoundary = $null; EndBoundary = $null })
        Settings  = [pscustomobject]@{
            Enabled = $true; ExecutionTimeLimit = 'PT0S'; MultipleInstances = 'IgnoreNew'
            DisallowStartIfOnBatteries = $false; StopIfGoingOnBatteries = $false; StartWhenAvailable = $true }
        State     = 'Ready'
    }
}

$tasks = [ordered]@{
    good                 = { param($t) }
    numeric_enums        = { param($t) $t.Principal.LogonType = 3; $t.Principal.RunLevel = 1; $t.Settings.MultipleInstances = 2 }
    timespan_zero        = { param($t) $t.Settings.ExecutionTimeLimit = [TimeSpan]::Zero }
    old_weak_match       = { param($t)
        # What the first-action/user/Highest comparison used to accept.
        $t.Settings.Enabled = $false; $t.Triggers = @(); $t.Principal.LogonType = 'Password'
        $t.Settings.ExecutionTimeLimit = 'PT72H'; $t.Settings.MultipleInstances = 'Parallel' }
    disabled             = { param($t) $t.Settings.Enabled = $false }
    state_disabled       = { param($t) $t.State = 'Disabled' }
    no_trigger           = { param($t) $t.Triggers = @() }
    null_trigger         = { param($t) $t.Triggers = $null }
    two_triggers         = { param($t) $t.Triggers = @($t.Triggers[0], $t.Triggers[0]) }
    trigger_disabled     = { param($t) $t.Triggers[0].Enabled = $false }
    trigger_other_user   = { param($t) $t.Triggers[0].UserId = $otherSid }
    trigger_not_logon    = { param($t) $t.Triggers[0].CimClass.CimClassName = 'MSFT_TaskTimeTrigger' }
    trigger_delayed      = { param($t) $t.Triggers[0].Delay = 'PT15M' }
    password_logon       = { param($t) $t.Principal.LogonType = 'Password' }
    s4u_logon            = { param($t) $t.Principal.LogonType = 'S4U' }
    limited              = { param($t) $t.Principal.RunLevel = 'Limited' }
    other_user           = { param($t) $t.Principal.UserId = $otherSid }
    time_72h             = { param($t) $t.Settings.ExecutionTimeLimit = 'PT72H' }
    time_unset           = { param($t) $t.Settings.ExecutionTimeLimit = $null }
    parallel             = { param($t) $t.Settings.MultipleInstances = 'Parallel' }
    parallel_numeric     = { param($t) $t.Settings.MultipleInstances = 0 }
    queue                = { param($t) $t.Settings.MultipleInstances = 'Queue' }
    no_battery_start     = { param($t) $t.Settings.DisallowStartIfOnBatteries = $true }
    battery_stop         = { param($t) $t.Settings.StopIfGoingOnBatteries = $true }
    no_start_when_avail  = { param($t) $t.Settings.StartWhenAvailable = $false }
    no_action            = { param($t) $t.Actions = @() }
    two_actions          = { param($t) $t.Actions = @($t.Actions[0], $t.Actions[0]) }
    other_program        = { param($t) $t.Actions[0].Execute = 'C:\Other\pythonw.exe' }
    other_script         = { param($t) $t.Actions[0].Arguments = '"C:\Other\main.py"' }
    unquoted_script      = { param($t) $t.Actions[0].Arguments = $main }
    other_directory      = { param($t) $t.Actions[0].WorkingDirectory = 'C:\Other' }
}
$names = [ordered]@{
    name_ok         = { param($t) $t.Principal.UserId = 'TESTPC\alice'; $t.Triggers[0].UserId = 'TESTPC\alice' }
    name_other_user = { param($t) $t.Principal.UserId = 'TESTPC\mallory' }
    name_trigger    = { param($t) $t.Triggers[0].UserId = 'TESTPC\mallory' }
    name_unknown    = { param($t) $t.Principal.UserId = 'GHOST\nobody' }
}

$out = [ordered]@{}
function Add-Result($label, [scriptblock]$body) {
    try { $out[$label] = & $body } catch { $out[$label] = [ordered]@{ error = $_.Exception.Message } }
}

foreach ($name in $tasks.Keys) {
    Add-Result $name {
        $t = New-FakeTask
        $mutate = $tasks[$name]
        & $mutate $t
        [ordered]@{ match = [bool](Test-TaskMatches $t $sid $exe $main $root)
                    problems = @(Get-TaskMismatch $t $sid $exe $main $root | ForEach-Object { "$_" }) }
    }
}

# Account names resolve through NTAccount.Translate in the real helper; here a stub stands in
# for that lookup and defers to the real function for anything that already is a SID.
${function:ConvertTo-UserSid} = {
    param([string]$UserId)
    if ($UserId -match '^S-1-') { return (& $realSid $UserId) }
    switch ($UserId) {
        'TESTPC\alice'   { return $sid }
        'TESTPC\mallory' { return $otherSid }
        default          { return $null }
    }
}
foreach ($name in $names.Keys) {
    Add-Result $name {
        $t = New-FakeTask
        $mutate = $names[$name]
        & $mutate $t
        [ordered]@{ match = [bool](Test-TaskMatches $t $sid $exe $main $root)
                    problems = @(Get-TaskMismatch $t $sid $exe $main $root | ForEach-Object { "$_" }) }
    }
}
${function:ConvertTo-UserSid} = $realSid

Add-Result sid_forms {
    [ordered]@{ lower = "$(ConvertTo-UserSid 's-1-5-21-111-222-333-1001')"; empty = "$(ConvertTo-UserSid '')" }
}

# Shortcuts: Get-ShortcutInfo is replaced, so no COM object or file is touched.
$links = [ordered]@{
    direct_ok          = @($exe, "`"$main`"", $root)
    task_ok            = @($schtasks, '/Run /TN "Odicto"', $root)
    pythonw_other_main = @($exe, '"C:\Other\main.py"', $root)
    pythonw_no_args    = @($exe, '', $root)
    pythonw_other_dir  = @($exe, "`"$main`"", 'C:\Other')
    other_install      = @('C:\Other\.venv\Scripts\pythonw.exe', '"C:\Other\main.py"', 'C:\Other')
    workdir_only       = @('C:\Windows\notepad.exe', 'notes.txt', $root)
    root_in_arguments  = @('C:\Windows\notepad.exe', "`"$root\notes.txt`"", 'C:\Elsewhere')
    schtasks_other     = @($schtasks, '/Run /TN "Other"', $root)
    schtasks_delete    = @($schtasks, '/Delete /TN "Odicto" /F', $root)
    schtasks_extra     = @($schtasks, '/Run /TN "Odicto" /I', $root)
    schtasks_other_dir = @($schtasks, '/Run /TN "Odicto"', 'C:\Other')
}
function Get-ShortcutInfo([string]$Path) {
    $row = $links[$Path]
    [pscustomobject]@{ TargetPath = $row[0]; Arguments = $row[1]; WorkingDirectory = $row[2] }
}
foreach ($name in $links.Keys) {
    Add-Result "link_$name" {
        [ordered]@{ match = [bool](Test-OwnShortcut $name $exe $main $root $schtasks 'Odicto') }
    }
}

$out | ConvertTo-Json -Depth 5 -Compress
"""


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell harness")
class TestHelperDecisions(unittest.TestCase):
    """Task reuse and shortcut ownership, executed against fake objects."""

    @classmethod
    def setUpClass(cls):
        import base64
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(powershell):
            raise AssertionError("Windows PowerShell not found at %s" % powershell)
        encoded = base64.b64encode(_HARNESS_PS.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            env=dict(os.environ, ODICTO_HELPER=str(HELPER_PS1)),
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode != 0 or not result.stdout.strip():
            raise AssertionError("harness failed (%s): %s%s" % (result.returncode, result.stdout, result.stderr))
        cls.results = json.loads(result.stdout.strip().splitlines()[-1])

    def outcome(self, name):
        found = self.results[name]
        self.assertNotIn("error", found, found)
        return found

    def assert_accepted(self, name):
        found = self.outcome(name)
        self.assertTrue(found["match"], "%s was rejected: %s" % (name, found.get("problems")))
        self.assertEqual(found["problems"], [])

    def assert_rejected(self, name, reason=None):
        found = self.outcome(name)
        self.assertFalse(found["match"], "%s was accepted" % name)
        self.assertTrue(found["problems"], name)
        if reason:
            self.assertTrue(any(reason in problem for problem in found["problems"]),
                            "%s: %r not in %r" % (name, reason, found["problems"]))

    def test_exact_owned_task_is_accepted_including_enum_and_timespan_forms(self):
        for name in ("good", "numeric_enums", "timespan_zero", "name_ok"):
            with self.subTest(task=name):
                self.assert_accepted(name)

    def test_the_old_weak_comparison_shape_is_rejected_on_every_count(self):
        # Right first action, user and Highest, but disabled, no trigger, Password, 72 h, Parallel.
        self.assert_rejected("old_weak_match", "disabled")
        problems = " | ".join(self.outcome("old_weak_match")["problems"])
        for expected in ("expected exactly 1 trigger", "logon type is 'Password'", "PT72H", "Parallel"):
            self.assertIn(expected, problems)

    def test_disabled_task_is_rejected(self):
        self.assert_rejected("disabled", "disabled")
        self.assert_rejected("state_disabled", "disabled")

    def test_missing_or_wrong_logon_trigger_is_rejected(self):
        for name in ("no_trigger", "null_trigger", "two_triggers"):
            with self.subTest(task=name):
                self.assert_rejected(name, "exactly 1 trigger")
        self.assert_rejected("trigger_disabled", "trigger is disabled")
        self.assert_rejected("trigger_other_user", "logon trigger user")
        self.assert_rejected("trigger_not_logon", "not a logon trigger")
        self.assert_rejected("trigger_delayed", "delay or time window")

    def test_principal_must_be_the_current_user_interactive_highest(self):
        self.assert_rejected("password_logon", "logon type")
        self.assert_rejected("s4u_logon", "logon type")
        self.assert_rejected("limited", "run level")
        self.assert_rejected("other_user", "not the current user")

    def test_account_names_are_normalised_to_the_current_sid(self):
        self.assert_accepted("name_ok")
        self.assert_rejected("name_other_user", "not the current user")
        self.assert_rejected("name_trigger", "logon trigger user")
        self.assert_rejected("name_unknown", "not the current user")

    def test_run_time_limit_must_be_explicitly_unlimited(self):
        self.assert_rejected("time_72h", "PT0S")
        self.assert_rejected("time_unset", "PT0S")

    def test_only_ignore_new_is_accepted_for_parallel_runs(self):
        for name in ("parallel", "parallel_numeric", "queue"):
            with self.subTest(task=name):
                self.assert_rejected(name, "IgnoreNew")

    def test_battery_and_start_when_available_settings_are_required(self):
        self.assert_rejected("no_battery_start", "does not start on battery")
        self.assert_rejected("battery_stop", "stops when going on battery")
        self.assert_rejected("no_start_when_avail", "start when available")

    def test_exactly_one_action_with_this_installs_program_script_and_directory(self):
        self.assert_rejected("no_action", "exactly 1 action")
        self.assert_rejected("two_actions", "exactly 1 action")
        self.assert_rejected("other_program", "action program")
        self.assert_rejected("other_script", "action arguments")
        self.assert_rejected("unquoted_script", "action arguments")
        self.assert_rejected("other_directory", "working directory")

    def test_sid_forms(self):
        found = self.outcome("sid_forms")
        self.assertEqual(found["lower"], "S-1-5-21-111-222-333-1001")
        self.assertEqual(found["empty"], "")

    def test_only_this_installs_own_shortcuts_are_owned(self):
        for name in ("direct_ok", "task_ok"):
            self.assertTrue(self.outcome("link_" + name)["match"], name)
        for name in ("pythonw_other_main", "pythonw_no_args", "pythonw_other_dir", "other_install",
                     "workdir_only", "root_in_arguments", "schtasks_other", "schtasks_delete",
                     "schtasks_extra", "schtasks_other_dir"):
            with self.subTest(link=name):
                self.assertFalse(self.outcome("link_" + name)["match"])


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell parser")
class TestHelperParses(unittest.TestCase):
    def test_parses_without_running(self):
        """Ask the PowerShell parser for syntax errors. Nothing is executed."""
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(powershell):
            self.fail("Windows PowerShell not found at %s" % powershell)
        command = ("$e=$null;$t=$null;"
                   "[void][System.Management.Automation.Language.Parser]::ParseFile("
                   "$env:ODICTO_PS_PARSE_TARGET,[ref]$t,[ref]$e);"
                   "if($e.Count){$e|ForEach-Object{$_.ToString()};exit 1}")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-command", command],
            env=dict(os.environ, ODICTO_PS_PARSE_TARGET=str(HELPER_PS1)),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestStaticOrdering(unittest.TestCase):
    """Source-order evidence only. These do not run the helper or a launcher, so they cannot
    prove UAC-cancel or stop-failure behaviour; that is verified at runtime, separately."""

    def read(self, *parts):
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def before(self, text, first, second):
        self.assertIn(first, text)
        self.assertIn(second, text)
        self.assertLess(text.index(first), text.index(second), "%r must come before %r" % (first, second))

    def test_helper_elevates_before_the_stop_or_configure_calls(self):
        text = self.read("scripts", "windows", "start_dictation_admin.ps1")
        self.before(text, "-Verb RunAs", "Assert-Runtime $needPythonw")
        self.before(text, "-Verb RunAs", "Invoke-Configure\n")
        self.before(text, "-Verb RunAs", "$Py @($OdictoPy, 'stop')")

    def test_helper_cancels_the_start_when_the_stop_fails(self):
        text = self.read("scripts", "windows", "start_dictation_admin.ps1")
        self.before(text, "stop failed, start cancelled", "$null = Start-Process -FilePath $Pyw")

    def test_start_bat_elevates_before_the_restart_stop(self):
        text = self.read("scripts", "windows", "start_dictation.bat")
        self.before(text, ".odicto-admin-enabled", 'call "%~dp0stop_dictation.bat"')

    def test_run_debug_delegates_before_it_stops_anything(self):
        text = self.read("scripts", "windows", "run_debug.bat")
        self.before(text, ".odicto-admin-enabled", 'call "%~dp0stop_dictation.bat"')
        self.before(text, "start_dictation_admin.ps1", 'call "%~dp0stop_dictation.bat"')

    def test_marker_is_gitignored(self):
        self.assertIn(".odicto-admin-enabled", self.read(".gitignore").splitlines())


if __name__ == "__main__":
    unittest.main()
