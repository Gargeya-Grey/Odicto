"""Admin mode regressions.

Every native boundary is replaced: no test asks for UAC, registers or runs a task, stops or
starts Odicto, signals a real Odicto, or reads .env / prompt.txt. The decision paths
(start, restart, stop with admin mode on and off) run with subprocess, schtasks, the quit
event and the process probes mocked. On Windows the helper's task-reuse and shortcut-ownership
functions run for real against fake objects (TestHelperDecisions) and the helper is parsed,
never run. One Windows test uses a real named event for a scratch install folder (its name
derives from that folder), never this install's event. UAC cancellation and stop-failure
ordering are only source-order checks (TestStaticOrdering); the elevated lifecycle itself is
verified separately at runtime.
"""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import windows_admin

ROOT = Path(__file__).resolve().parent.parent
HELPER_PS1 = ROOT / "scripts" / "windows" / "admin_mode.ps1"
CREATE_NO_WINDOW = 0x08000000
SID = "S-1-5-21-111-222-333-1001"


class _AdminRootCase(unittest.TestCase):
    """A scratch install folder whose path contains spaces, with a stub helper script."""

    marker = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="odicto admin test ")
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(os.path.realpath(tmp.name), "Odicto Install")
        scripts = os.path.join(self.root, "scripts", "windows")
        os.makedirs(scripts)
        self.helper = os.path.join(scripts, "admin_mode.ps1")
        with open(self.helper, "w", encoding="utf-8") as handle:
            handle.write("# stub\n")
        self.marker_file = os.path.join(self.root, ".odicto-admin-enabled")
        if self.marker:
            with open(self.marker_file, "w", encoding="ascii") as handle:
                handle.write("enabled\n")
        self.system_root = os.path.join(os.path.realpath(tmp.name), "Win Dir")
        self.powershell = os.path.join(
            self.system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        env = patch.dict(os.environ, {"SystemRoot": self.system_root})
        env.start()
        self.addCleanup(env.stop)

    def patch_object(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock


class TestAdminModeEnabled(_AdminRootCase):
    def test_true_only_on_windows_with_the_marker(self):
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        self.assertTrue(windows_admin.admin_mode_enabled(self.root))
        os.remove(self.marker_file)
        self.assertFalse(windows_admin.admin_mode_enabled(self.root))

    def test_false_elsewhere_even_with_the_marker(self):
        self.patch_object(windows_admin, "_is_windows", return_value=False)
        self.assertFalse(windows_admin.admin_mode_enabled(self.root))

    def test_accepts_path_objects(self):
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        self.assertTrue(windows_admin.admin_mode_enabled(Path(self.root)))


class TestRunIfNeeded(_AdminRootCase):
    """The helper command line and the cases where run_if_needed carries on unchanged."""

    def setUp(self):
        super().setUp()
        self.windows = self.patch_object(windows_admin, "_is_windows", return_value=True)
        self.probe = self.patch_object(windows_admin, "_is_user_an_admin", return_value=False)
        self.run = self.patch_object(windows_admin.subprocess, "run",
                                     return_value=SimpleNamespace(returncode=0))

    def test_not_windows_never_probes_or_launches(self):
        self.windows.return_value = False
        self.assertIsNone(windows_admin.run_if_needed(self.root, "Configure"))
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_already_elevated_runs_normally(self):
        self.probe.return_value = True
        for action in windows_admin.ACTIONS:
            with self.subTest(action=action):
                self.assertIsNone(windows_admin.run_if_needed(self.root, action))
        self.run.assert_not_called()

    def test_no_marker_never_probes_or_launches(self):
        os.remove(self.marker_file)
        for action in windows_admin.ACTIONS:
            with self.subTest(action=action):
                self.assertIsNone(windows_admin.run_if_needed(self.root, action))
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_configure_launches_hidden_helper_with_absolute_spaced_paths(self):
        self.assertEqual(windows_admin.run_if_needed(self.root, "Configure"), 0)
        self.run.assert_called_once()
        command = self.run.call_args.args[0]
        self.assertEqual(command, [
            self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", self.helper, "-Action", "Configure"])
        self.assertTrue(os.path.isabs(command[0]) and os.path.isabs(command[5]))
        self.assertIn(" ", command[5])
        kwargs = self.run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertFalse(kwargs.get("shell", False))

    def test_accepts_path_objects(self):
        windows_admin.run_if_needed(Path(self.root), "Disable")
        self.assertEqual(self.run.call_args.args[0][5], self.helper)

    def test_timeout_is_forwarded_and_rounded_up(self):
        windows_admin.run_if_needed(self.root, "Configure", 30.0)
        command = self.run.call_args.args[0]
        self.assertEqual(command[-4:], ["-Action", "Configure", "-TimeoutSeconds", "30"])
        windows_admin.run_if_needed(self.root, "Configure", 30.2)
        self.assertEqual(self.run.call_args.args[0][-1], "31")
        windows_admin.run_if_needed(self.root, "Disable")
        self.assertNotIn("-TimeoutSeconds", self.run.call_args.args[0])

    def test_helper_exit_code_is_returned(self):
        self.run.return_value = SimpleNamespace(returncode=5)
        self.assertEqual(windows_admin.run_if_needed(self.root, "Disable"), 5)

    def test_wait_ready_needs_no_elevation(self):
        self.assertIsNone(windows_admin.run_if_needed(self.root, "WaitReady", 30.0))
        self.run.assert_not_called()

    def test_unknown_admin_state_is_an_error_not_a_normal_run(self):
        self.probe.side_effect = OSError("shell32 unavailable")
        with self.assertRaisesRegex(RuntimeError, "elevation state"):
            windows_admin.run_if_needed(self.root, "Configure")
        self.run.assert_not_called()

    def test_missing_helper_is_an_error(self):
        os.remove(self.helper)
        with self.assertRaisesRegex(RuntimeError, "helper is missing"):
            windows_admin.run_if_needed(self.root, "Configure")
        self.run.assert_not_called()

    def test_powershell_failure_is_an_error(self):
        self.run.side_effect = FileNotFoundError("powershell.exe")
        with self.assertRaisesRegex(RuntimeError, "PowerShell could not run"):
            windows_admin.run_if_needed(self.root, "Configure")

    def test_missing_system_root_is_an_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SystemRoot"):
                windows_admin.run_if_needed(self.root, "Configure")
        self.run.assert_not_called()

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            windows_admin.run_if_needed(self.root, "Format")
        self.run.assert_not_called()


class _LifecycleCase(_AdminRootCase):
    """Admin mode on, not elevated: every native step is a mock that logs its call order."""

    def setUp(self):
        super().setUp()
        self.log = []
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        self.patch_object(windows_admin, "_is_user_an_admin", return_value=False)
        self.pid = self.patch_object(windows_admin, "_owner_pid", return_value=None)
        self.ready = self.patch_object(windows_admin, "_owner_ready", return_value=False)
        self.quit = self.patch_object(windows_admin, "_request_quit_detail",
                                      side_effect=lambda root, timeout: self.step("quit", (True, "")))
        self.task = self.patch_object(windows_admin, "_schtasks_run",
                                      side_effect=lambda: self.step("task", (True, "")))
        self.wait = self.patch_object(windows_admin, "_wait_ready",
                                      side_effect=lambda root, timeout, quiet=False: self.step("wait", 0))
        self.new_owner = self.patch_object(windows_admin, "_wait_for_new_owner",
                                           side_effect=lambda *a: self.step("new-owner", True))
        self.helper_run = self.patch_object(
            windows_admin, "_run_helper",
            side_effect=lambda root, action, timeout=None, **kw: self.step("helper:" + action, 0))
        out = io.StringIO()
        self.out = out
        redirect = contextlib.redirect_stdout(out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def step(self, name, result):
        self.log.append(name)
        return result


class TestEnsureRunning(_LifecycleCase):
    def test_running_and_ready_owner_is_left_alone_with_no_elevation(self):
        self.ready.return_value = True
        self.assertEqual(windows_admin.run_if_needed(self.root, "Ensure"), 0)
        self.assertEqual(self.log, [])
        self.assertIn("Odicto is already running (admin mode).", self.out.getvalue())

    def test_not_running_runs_the_task_then_waits_for_readiness(self):
        self.assertEqual(windows_admin.run_if_needed(self.root, "Ensure"), 0)
        self.assertEqual(self.log, ["task", "wait"])
        self.wait.assert_called_once_with(self.root, 30)

    def test_readiness_failure_is_the_exit_code(self):
        self.wait.side_effect = lambda *a, **k: 1
        self.assertEqual(windows_admin.run_if_needed(self.root, "Ensure"), 1)

    def test_missing_task_falls_back_to_the_helper_and_says_why(self):
        self.task.side_effect = lambda: (False, "ERROR: The system cannot find the file specified.")
        self.assertEqual(windows_admin.run_if_needed(self.root, "Ensure"), 0)
        self.helper_run.assert_called_once_with(self.root, "Start", None)
        self.wait.assert_not_called()
        self.assertIn("cannot find the file specified", self.out.getvalue())
        self.assertIn("administrator prompt", self.out.getvalue())

    def test_ensure_never_asks_the_owner_to_quit(self):
        windows_admin.run_if_needed(self.root, "Ensure")
        self.quit.assert_not_called()


class TestRestart(_LifecycleCase):
    def test_owner_quits_and_is_gone_before_the_task_starts_a_new_one(self):
        self.pid.return_value = 4242
        self.assertEqual(windows_admin.run_if_needed(self.root, "Start"), 0)
        self.assertEqual(self.log, ["quit", "task", "new-owner", "wait"])
        self.quit.assert_called_once_with(self.root, 10.0)
        self.new_owner.assert_called_once_with(self.root, 4242, 5.0)
        self.helper_run.assert_not_called()

    def test_failed_quit_uses_the_helper_and_never_starts_a_second_owner(self):
        self.pid.return_value = 4242
        self.quit.side_effect = lambda root, timeout: self.step(
            "quit", (False, "this Odicto has no quit signal (it is an older version)"))
        self.assertEqual(windows_admin.run_if_needed(self.root, "Start", 45), 0)
        self.assertEqual(self.log, ["quit", "helper:Start"])
        self.helper_run.assert_called_once_with(self.root, "Start", 45)
        self.task.assert_not_called()
        self.assertIn("no quit signal", self.out.getvalue())

    def test_no_owner_means_nothing_to_quit(self):
        self.assertEqual(windows_admin.run_if_needed(self.root, "Start"), 0)
        self.assertEqual(self.log, ["task", "wait"])
        self.quit.assert_not_called()

    def test_task_start_is_repeated_once_when_no_new_owner_appears(self):
        self.pid.return_value = 4242
        self.new_owner.side_effect = lambda *a: self.step("new-owner", False)
        self.assertEqual(windows_admin.run_if_needed(self.root, "Start"), 0)
        self.assertEqual(self.log, ["quit", "task", "new-owner", "task", "wait"])

    def test_missing_task_on_restart_falls_back_to_the_helper(self):
        self.pid.return_value = 4242
        self.task.side_effect = lambda: (False, "task not found")
        windows_admin.run_if_needed(self.root, "Start")
        self.helper_run.assert_called_once_with(self.root, "Start", None)
        self.assertIn("task not found", self.out.getvalue())


class TestStop(_LifecycleCase):
    def test_quit_event_stops_the_owner_without_elevation(self):
        self.pid.return_value = 4242
        self.assertEqual(windows_admin.run_if_needed(self.root, "Stop"), 0)
        self.assertEqual(self.log, ["quit"])
        self.helper_run.assert_not_called()

    def test_missing_event_or_slow_owner_falls_back_to_the_helper_and_says_why(self):
        self.pid.return_value = 4242
        self.quit.side_effect = lambda root, timeout: self.step("quit", (False, "Odicto did not quit in 10 seconds"))
        self.assertEqual(windows_admin.run_if_needed(self.root, "Stop"), 0)
        self.assertEqual(self.log, ["quit", "helper:Stop"])
        self.assertIn("did not quit in 10 seconds", self.out.getvalue())

    def test_no_known_owner_carries_on_with_the_ordinary_stop(self):
        self.assertIsNone(windows_admin.run_if_needed(self.root, "Stop"))
        self.assertEqual(self.log, [])


class TestSwitchOffNeverElevates(_AdminRootCase):
    marker = False

    def test_start_stop_ensure_do_nothing_when_admin_mode_is_off(self):
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        probe = self.patch_object(windows_admin, "_is_user_an_admin", return_value=False)
        run = self.patch_object(windows_admin.subprocess, "run")
        quit_ = self.patch_object(windows_admin, "_request_quit_detail")
        for action in ("Start", "Ensure", "Stop", "WaitReady"):
            with self.subTest(action=action):
                self.assertIsNone(windows_admin.run_if_needed(self.root, action))
        probe.assert_not_called()
        run.assert_not_called()
        quit_.assert_not_called()


class TestSetAdminMode(_AdminRootCase):
    """set_admin_mode runs the helper once and never raises."""

    marker = False

    def setUp(self):
        super().setUp()
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        # GitHub's Windows runners are elevated; pin the probe so tests mean a normal caller.
        self.elevated = self.patch_object(windows_admin, "_is_user_an_admin", return_value=False)
        self.scratch = tempfile.mkdtemp(prefix="odicto-admin-test-scratch-")
        self.addCleanup(lambda: os.path.isdir(self.scratch) and os.rmdir(self.scratch))
        self.patch_object(windows_admin.tempfile, "mkdtemp", return_value=self.scratch)
        self.spawn = MagicMock()
        platforms = __import__("platforms")
        self.patch_object(platforms, "spawn_detached", new=self.spawn)
        self.receipt = {"ok": True, "steps": ["elevated"], "error": ""}
        self.effect = None
        self.run = self.patch_object(windows_admin.subprocess, "run", side_effect=self.fake_run)
        self.commands = []

    def fake_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        receipt_path = command[command.index("-ReceiptPath") + 1]
        with open(receipt_path, "w", encoding="utf-8") as handle:
            json.dump(self.receipt, handle)
        if self.effect:
            self.effect()
        return SimpleNamespace(returncode=0 if self.receipt.get("ok") else 1)

    def write_marker(self):
        with open(self.marker_file, "w", encoding="ascii") as handle:
            handle.write("enabled\n")

    def make_pythonw(self):
        folder = os.path.join(self.root, ".venv", "Scripts")
        os.makedirs(folder)
        with open(os.path.join(folder, "pythonw.exe"), "wb") as handle:
            handle.write(b"")
        return os.path.join(folder, "pythonw.exe")

    def test_not_windows_is_a_plain_sentence(self):
        windows_admin._is_windows.return_value = False
        self.assertEqual(windows_admin.set_admin_mode(self.root, True),
                         (False, "Admin mode is only available on Windows."))
        self.run.assert_not_called()

    def test_turning_on_runs_one_helper_start_with_install_startup(self):
        self.effect = self.write_marker
        ok, message = windows_admin.set_admin_mode(self.root, True)
        self.assertTrue(ok, message)
        self.assertEqual(len(self.commands), 1)
        command, kwargs = self.commands[0]
        self.assertEqual(command[:7], [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                       "-File", self.helper, "-Action"])
        self.assertEqual(command[7:9], ["Start", "-InstallStartup"])
        self.assertEqual(kwargs["timeout"], 180.0)
        self.assertEqual(kwargs["cwd"], self.root)
        self.spawn.assert_not_called()  # the one elevated run restarts Odicto itself

    def test_turning_on_without_the_marker_afterwards_is_not_success(self):
        ok, message = windows_admin.set_admin_mode(self.root, True)
        self.assertFalse(ok)
        self.assertIn("not changed", message)

    def test_cancelled_prompt_changes_nothing_and_says_so(self):
        self.receipt = {"ok": False, "steps": ["elevation not granted"],
                        "error": "Elevation was cancelled or failed; Odicto and its settings were left unchanged."}
        ok, message = windows_admin.set_admin_mode(self.root, True)
        self.assertFalse(ok)
        self.assertIn("cancelled; nothing changed", message)
        self.spawn.assert_not_called()

    def test_helper_failure_carries_its_reason(self):
        self.receipt = {"ok": False, "steps": ["elevated", "failed"],
                        "error": "A scheduled task named 'Odicto' already exists and is not exactly this install's task."}
        ok, message = windows_admin.set_admin_mode(self.root, False)
        self.assertFalse(ok)
        self.assertIn("already exists", message)
        self.assertEqual(self.commands[0][0][7:], ["Disable", "-ReceiptPath",
                                                   os.path.join(self.scratch, "receipt.json"),
                                                   "-TimeoutSeconds", "30"])
        self.spawn.assert_not_called()

    def test_turning_off_starts_a_normal_odicto_after_the_helper(self):
        self.write_marker()
        pythonw = self.make_pythonw()
        self.effect = lambda: os.remove(self.marker_file)
        ok, message = windows_admin.set_admin_mode(self.root, False)
        self.assertTrue(ok, message)
        self.assertEqual(self.commands[0][0][7], "Disable")
        self.assertNotIn("-InstallStartup", self.commands[0][0])
        self.spawn.assert_called_once_with([pythonw, os.path.join(self.root, "main.py")])
        self.assertIn("off", message)

    def test_turning_off_from_an_elevated_caller_starts_nothing(self):
        """A setup page opened from the elevated owner's tray would start Odicto elevated."""
        self.write_marker()
        self.make_pythonw()
        self.effect = lambda: os.remove(self.marker_file)
        for probe in ({"return_value": True}, {"side_effect": OSError("no shell32")}):
            with self.subTest(probe=probe):
                os.makedirs(self.scratch, exist_ok=True)  # each helper run removes it
                self.elevated.configure_mock(**probe)
                ok, message = windows_admin.set_admin_mode(self.root, False)
                self.assertTrue(ok, message)
                self.assertIn("start_dictation.bat", message)
                self.spawn.assert_not_called()
                self.write_marker()

    def test_turning_off_with_the_marker_left_behind_does_not_start_anything(self):
        self.write_marker()
        self.make_pythonw()
        ok, _ = windows_admin.set_admin_mode(self.root, False)
        self.assertFalse(ok)
        self.spawn.assert_not_called()

    def test_turning_off_without_pythonw_still_reports_the_switch(self):
        self.write_marker()
        self.effect = lambda: os.remove(self.marker_file)
        ok, message = windows_admin.set_admin_mode(self.root, False)
        self.assertTrue(ok)
        self.assertIn(".venv is missing", message)
        self.spawn.assert_not_called()

    def test_never_raises(self):
        for failure in (subprocess.TimeoutExpired("powershell", 1), FileNotFoundError("powershell.exe"),
                        RuntimeError("boom"), ValueError("odd")):
            with self.subTest(failure=type(failure).__name__):
                self.run.side_effect = failure
                ok, message = windows_admin.set_admin_mode(self.root, True)
                self.assertFalse(ok)
                self.assertIsInstance(message, str)
                self.assertTrue(message)

    def test_missing_helper_is_a_sentence_not_an_exception(self):
        os.remove(self.helper)
        ok, message = windows_admin.set_admin_mode(self.root, True)
        self.assertFalse(ok)
        self.assertIn("helper", message)
        self.run.assert_not_called()

    def test_scratch_receipt_folder_is_removed(self):
        os.makedirs(os.path.join(self.scratch, "left"), exist_ok=True)
        self.effect = self.write_marker
        windows_admin.set_admin_mode(self.root, True)
        self.assertFalse(os.path.exists(self.scratch))
        os.makedirs(self.scratch)  # for the cleanup hook


class TestQuitEvent(unittest.TestCase):
    def test_name_is_stable_and_install_specific(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = windows_admin.quit_event_name(one)
            self.assertEqual(first, windows_admin.quit_event_name(one))
            self.assertEqual(first, windows_admin.quit_event_name(Path(one)))
            self.assertEqual(first, windows_admin.quit_event_name(os.path.join(one, "sub", "..")))
            self.assertNotEqual(first, windows_admin.quit_event_name(two))
        self.assertRegex(first, r"^Local\\Odicto-quit-[0-9a-f]{12}$")

    def test_name_ignores_letter_case_only_where_the_file_system_does(self):
        with tempfile.TemporaryDirectory() as folder:
            same = windows_admin.quit_event_name(folder) == windows_admin.quit_event_name(folder.swapcase())
            self.assertEqual(same, os.path.normcase("A") == os.path.normcase("a"))

    def test_security_descriptor_lets_the_user_signal_and_medium_integrity_write(self):
        sddl = windows_admin.quit_event_sddl(SID)
        self.assertEqual(sddl, "D:(A;;0x100002;;;%s)S:(ML;;NW;;;ME)" % SID)
        mask = int(sddl.split("(A;;")[1].split(";")[0], 16)
        self.assertEqual(mask, 0x0002 | 0x00100000)  # EVENT_MODIFY_STATE | SYNCHRONIZE
        self.assertTrue(sddl.endswith("S:(ML;;NW;;;ME)"))

    def test_name_is_session_local(self):
        self.assertTrue(windows_admin.quit_event_name(ROOT).startswith("Local\\"))


class TestQuitListener(unittest.TestCase):
    def test_non_windows_is_a_no_op(self):
        with patch.object(windows_admin, "_is_windows", return_value=False), \
                patch.object(windows_admin, "_create_quit_event") as create:
            self.assertIsNone(windows_admin.start_quit_listener(ROOT, lambda: None))
        create.assert_not_called()

    def test_creation_failure_never_raises(self):
        out = io.StringIO()
        with patch.object(windows_admin, "_is_windows", return_value=True), \
                patch.object(windows_admin, "_current_user_sid", return_value=SID), \
                patch.object(windows_admin, "_create_quit_event", side_effect=OSError("denied")), \
                contextlib.redirect_stdout(out):
            self.assertIsNone(windows_admin.start_quit_listener(ROOT, lambda: None))
        self.assertIn("denied", out.getvalue())

    def run_listener(self, signalled):
        fired = threading.Event()

        def quit_now(get_app):
            fired.set()

        get_app = lambda: "the app"
        with patch.object(windows_admin, "_is_windows", return_value=True), \
                patch.object(windows_admin, "_current_user_sid", return_value=SID), \
                patch.object(windows_admin, "_create_quit_event", return_value="HANDLE") as create, \
                patch.object(windows_admin, "_wait_for_event", return_value=signalled) as wait, \
                patch.object(windows_admin, "graceful_quit", side_effect=quit_now) as quit_, \
                contextlib.redirect_stdout(io.StringIO()):
            thread = windows_admin.start_quit_listener(ROOT, get_app)
            thread.join(5)
            return thread, create, wait, quit_, fired, get_app

    def test_a_signal_runs_the_graceful_quit_on_a_daemon_thread(self):
        thread, create, wait, quit_, fired, get_app = self.run_listener(True)
        create.assert_called_once_with(windows_admin.quit_event_name(ROOT), windows_admin.quit_event_sddl(SID))
        wait.assert_called_once_with("HANDLE")
        quit_.assert_called_once_with(get_app)
        self.assertTrue(fired.is_set())
        self.assertTrue(thread.daemon)

    def test_a_failed_wait_does_not_quit(self):
        _, _, _, quit_, fired, _ = self.run_listener(False)
        quit_.assert_not_called()
        self.assertFalse(fired.is_set())

    def test_the_listener_starts_only_after_the_lock_in_app_main(self):
        text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        gate = text.index("if not claim_install(_pid_path):")
        listener = text.index("windows_admin.start_quit_listener(")
        validate = text.index("Config.validate()", gate)
        self.assertLess(gate, listener)
        self.assertLess(listener, validate)
        self.assertEqual(text.count("start_quit_listener("), 1)


class TestGracefulQuit(unittest.TestCase):
    def test_posts_the_tray_quit_to_the_qt_thread(self):
        indicator = SimpleNamespace(_qt_app="QT", _tray="TRAY")
        app = SimpleNamespace(indicator=indicator)
        with patch.object(windows_admin, "_post_qt_quit") as post:
            windows_admin.graceful_quit(lambda: app)
        post.assert_called_once_with("QT", "TRAY")

    def test_waits_for_an_app_that_is_still_being_built(self):
        app = SimpleNamespace(indicator=SimpleNamespace(_qt_app="QT", _tray=None))
        answers = [None, None, app]
        with patch.object(windows_admin, "_post_qt_quit") as post:
            windows_admin.graceful_quit(lambda: answers.pop(0), poll=0.001)
        post.assert_called_once_with("QT", None)
        self.assertEqual(answers, [])

    def test_a_failing_get_app_is_retried(self):
        app = SimpleNamespace(indicator=SimpleNamespace(_qt_app="QT", _tray=None))
        calls = []

        def get_app():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("not yet")
            return app

        with patch.object(windows_admin, "_post_qt_quit") as post:
            windows_admin.graceful_quit(get_app, poll=0.001)
        post.assert_called_once()

    def test_without_an_event_loop_it_shuts_down_directly(self):
        app = SimpleNamespace(indicator=None, _shutdown=MagicMock())
        with patch.object(windows_admin.os, "_exit") as exit_:
            windows_admin.graceful_quit(lambda: app)
        app._shutdown.assert_called_once_with()
        exit_.assert_called_once_with(0)


class TestRequestQuit(_AdminRootCase):
    def setUp(self):
        super().setUp()
        self.patch_object(windows_admin, "_is_windows", return_value=True)
        self.log = []
        self.pid = self.patch_object(windows_admin, "_owner_pid", return_value=777)
        self.signal = self.patch_object(windows_admin, "_signal_quit_event",
                                        side_effect=lambda name: self.log.append("signal") or True)
        self.gone = self.patch_object(windows_admin, "_wait_pid_exit",
                                      side_effect=lambda pid, timeout: self.log.append("wait") or True)

    def test_signals_the_installs_event_then_waits_for_the_owner_pid(self):
        self.assertTrue(windows_admin.request_quit(self.root))
        self.signal.assert_called_once_with(windows_admin.quit_event_name(self.root))
        self.assertEqual(self.gone.call_args.args[0], 777)
        self.assertLessEqual(self.gone.call_args.args[1], 10.0)
        self.assertEqual(self.log, ["signal", "wait"])

    def test_non_windows_is_false_and_touches_nothing(self):
        windows_admin._is_windows.return_value = False
        self.assertFalse(windows_admin.request_quit(self.root))
        self.signal.assert_not_called()

    def test_no_event_is_false(self):
        self.signal.side_effect = lambda name: False
        ok, reason = windows_admin._request_quit_detail(self.root, 10.0)
        self.assertFalse(ok)
        self.assertIn("no quit signal", reason)
        self.gone.assert_not_called()

    def test_owner_that_does_not_exit_in_time_is_false(self):
        self.gone.side_effect = lambda pid, timeout: False
        self.assertFalse(windows_admin.request_quit(self.root, 3.0))
        self.assertIn("did not quit in 3 seconds", windows_admin._request_quit_detail(self.root, 3.0)[1])

    def test_no_running_owner_is_false_without_signalling(self):
        self.pid.return_value = None
        self.assertFalse(windows_admin.request_quit(self.root))
        self.signal.assert_not_called()

    def test_errors_become_false(self):
        self.signal.side_effect = OSError("denied")
        self.assertFalse(windows_admin.request_quit(self.root))


@unittest.skipUnless(sys.platform == "win32", "uses a real Windows named event")
class TestQuitEventRoundTrip(unittest.TestCase):
    """The ctypes path for a scratch install folder: its event name is not this install's."""

    def test_signal_reaches_the_listener_and_the_owner_exit_is_awaited(self):
        with tempfile.TemporaryDirectory(prefix="odicto quit test ") as folder:
            owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            self.addCleanup(owner.kill)
            with open(os.path.join(folder, "dictation.pid"), "w", encoding="ascii") as handle:
                handle.write(str(owner.pid))

            def quit_now(get_app):
                owner.kill()

            with patch.object(windows_admin, "graceful_quit", side_effect=quit_now), \
                    contextlib.redirect_stdout(io.StringIO()):
                thread = windows_admin.start_quit_listener(folder, lambda: None)
                self.assertIsNotNone(thread)
                started = time.monotonic()
                self.assertTrue(windows_admin.request_quit(folder, 15.0))
            self.assertLess(time.monotonic() - started, 15.0)
            self.assertIsNotNone(owner.poll())

    def test_no_event_means_false(self):
        with tempfile.TemporaryDirectory(prefix="odicto quit test ") as folder:
            with open(os.path.join(folder, "dictation.pid"), "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            self.assertFalse(windows_admin.request_quit(folder, 1.0))

    def test_the_descriptor_text_is_accepted_by_windows(self):
        with tempfile.TemporaryDirectory(prefix="odicto sddl test ") as folder:
            sid = windows_admin._current_user_sid()
            self.assertRegex(sid, r"^S-1-5-")
            handle = windows_admin._create_quit_event(
                windows_admin.quit_event_name(folder), windows_admin.quit_event_sddl(sid))
            self.assertTrue(handle)
            self.assertTrue(windows_admin._signal_quit_event(windows_admin.quit_event_name(folder)))
            self.assertTrue(windows_admin._wait_for_event(handle))
            windows_admin._win32().k32.CloseHandle(handle)

    def test_a_second_owner_does_not_inherit_a_stale_quit_request(self):
        with tempfile.TemporaryDirectory(prefix="odicto stale test ") as folder:
            name = windows_admin.quit_event_name(folder)
            sddl = windows_admin.quit_event_sddl(windows_admin._current_user_sid())
            old = windows_admin._create_quit_event(name, sddl)
            self.assertTrue(windows_admin._signal_quit_event(name))
            new = windows_admin._create_quit_event(name, sddl)  # old owner still exiting
            kernel = windows_admin._win32().k32
            self.assertNotEqual(kernel.WaitForSingleObject(new, 0), 0)
            kernel.CloseHandle(new)
            kernel.CloseHandle(old)


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

    def test_start_in_admin_mode_means_start_unless_already_running(self):
        self.admin.return_value = 0
        self.assertEqual(self.cli.cmd_start(SimpleNamespace()), 0)
        self.admin.assert_called_once_with(self.cli._repo_root(), "Ensure", None)
        self.spawn.assert_not_called()

    def test_start_unchanged_when_not_delegated(self):
        self.spawn.return_value = SimpleNamespace(pid=4321)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.cmd_start(SimpleNamespace()), 0)
        self.spawn.assert_called_once()
        self.kill.assert_not_called()

    def test_restart_in_admin_mode_is_the_admin_restart(self):
        self.admin.return_value = 0
        self.assertEqual(self.cli.cmd_restart(SimpleNamespace()), 0)
        self.admin.assert_called_once_with(self.cli._repo_root(), "Start", None)
        self.kill.assert_not_called()
        self.spawn.assert_not_called()

    def test_restart_without_admin_mode_stops_then_starts_once(self):
        self.spawn.return_value = SimpleNamespace(pid=4321)
        order = []
        self.kill.side_effect = lambda path: order.append("kill") or []
        self.spawn.side_effect = lambda args: order.append("spawn") or SimpleNamespace(pid=4321)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.cmd_restart(SimpleNamespace()), 0)
        self.assertEqual(order, ["kill", "spawn"])

    def test_restart_does_not_start_when_the_stop_failed(self):
        self.kill.side_effect = RuntimeError("cannot stop")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli.cmd_restart(SimpleNamespace()), 1)
        self.spawn.assert_not_called()

    def test_wait_ready_never_asks_for_elevation(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.cli, "_repo_root", return_value=directory), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli.cmd_wait_ready(SimpleNamespace(timeout=0)), 1)
        self.admin.assert_not_called()

    def test_unusable_admin_mode_fails_instead_of_running_normally(self):
        self.admin.side_effect = RuntimeError("elevation state unknown")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.cli.cmd_stop(SimpleNamespace()), 1)
        self.assertIn("elevation state unknown", err.getvalue())
        self.kill.assert_not_called()

    def test_restart_subcommand_is_registered(self):
        out = io.StringIO()
        with patch.object(sys, "argv", ["odicto", "--help"]), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                self.cli.main()
        self.assertIn("restart", out.getvalue())


class TestWaitReadyForAnElevatedOwner(unittest.TestCase):
    """A normal process cannot read an elevated owner's working directory (AccessDenied)."""

    def check(self, argv):
        import odicto
        import psutil
        from platforms import base
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "dictation.pid"), "w") as f:
                f.write("12345")
            with open(os.path.join(directory, "dictation-health.json"), "w") as f:
                json.dump({"pid": 12345, "updated_at": time.time(), "ready": True,
                           "microphone": {"closed": False, "callback_age_s": 0.05, "callback_count": 1}}, f)
            owner = MagicMock()
            owner.create_time.return_value = time.time() - 20
            owner.cmdline.return_value = [argv[0], argv[1].format(root=base.install_root())]
            owner.cwd.side_effect = psutil.AccessDenied(12345)
            with patch.object(odicto, "_repo_root", return_value=directory), \
                    patch("psutil.Process", return_value=owner), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return odicto.cmd_wait_ready(SimpleNamespace(timeout=0))

    def test_absolute_main_py_is_verified_without_the_working_directory(self):
        self.assertEqual(self.check(["pythonw.exe", os.path.join("{root}", "main.py")]), 0)

    def test_relative_script_cannot_be_verified_and_is_not_ready(self):
        self.assertEqual(self.check(["pythonw.exe", "main.py"]), 1)

    def test_other_script_is_not_ready(self):
        self.assertEqual(self.check(["pythonw.exe", os.path.join("{root}", "other.py")]), 1)


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

    def test_install_without_admin_mode_keeps_the_same_level_restart(self):
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
                             side_effect=lambda root, action="Start": self.calls.append(("admin", root, action)) or self.exit_code)
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
        self.assertEqual([call_[0] for call_ in self.calls], ["admin", "app"])
        self.assertEqual(self.calls[0][1], ROOT)
        self.assertEqual(self.calls[1][1], str(ROOT / "app" / "main.py"))

    def test_a_double_click_start_never_restarts_a_running_owner(self):
        self.run_root_main()
        self.assertEqual(self.calls[0][2], "Ensure")

    def test_helper_exit_code_ends_the_launcher_without_the_app(self):
        self.exit_code = 7
        with self.assertRaises(SystemExit) as raised:
            self.run_root_main()
        self.assertEqual(raised.exception.code, 7)
        self.assertEqual([call_[0] for call_ in self.calls], ["admin"])

    def test_zero_from_helper_is_still_an_exit_not_a_second_start(self):
        self.exit_code = 0
        with self.assertRaises(SystemExit) as raised:
            self.run_root_main()
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("app", [call_[0] for call_ in self.calls])

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
$wanted = @('ConvertTo-UserSid', 'Test-EnumIs', 'Test-ZeroTime', 'Get-TaskMismatch', 'Test-TaskMatches', 'Test-OwnShortcut', 'Get-ShortcutInfo', 'Write-StartupShortcut')
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

# Both Startup shortcut shapes are written to a scratch folder and read back through the real
# COM object: what Disable writes must be recognised as this install's own shortcut.
Add-Result startup_shortcuts {
    & {
        $Root = $env:ODICTO_SCRATCH
        $Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
        $MainPy = Join-Path $Root 'main.py'
        $TaskName = 'Odicto'
        $schtasksPath = 'C:\Windows\System32\schtasks.exe'
        $direct = Join-Path $Root 'direct.lnk'
        $viaTask = Join-Path $Root 'task.lnk'
        Write-StartupShortcut $direct $false $schtasksPath
        Write-StartupShortcut $viaTask $true $schtasksPath
        $ws = New-Object -ComObject WScript.Shell
        $d = $ws.CreateShortcut($direct)
        $t = $ws.CreateShortcut($viaTask)
        [ordered]@{
            root = $Root
            direct_target = $d.TargetPath; direct_args = $d.Arguments; direct_dir = $d.WorkingDirectory
            direct_icon = $d.IconLocation; direct_style = $d.WindowStyle
            task_target = $t.TargetPath; task_args = $t.Arguments; task_icon = $t.IconLocation
            direct_owned = [bool](Test-OwnShortcut $direct $Pyw $MainPy $Root $schtasksPath 'Odicto')
            task_owned = [bool](Test-OwnShortcut $viaTask $Pyw $MainPy $Root $schtasksPath 'Odicto')
        }
    }
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
        cls._scratch = tempfile.TemporaryDirectory(prefix="odicto shortcut harness ")
        scratch = os.path.realpath(cls._scratch.name)
        os.makedirs(os.path.join(scratch, "assets"))
        with open(os.path.join(scratch, "assets", "odicto.ico"), "wb") as handle:
            handle.write(b"")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            env=dict(os.environ, ODICTO_HELPER=str(HELPER_PS1), ODICTO_SCRATCH=scratch),
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode != 0 or not result.stdout.strip():
            raise AssertionError("harness failed (%s): %s%s" % (result.returncode, result.stdout, result.stderr))
        cls.results = json.loads(result.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        cls._scratch.cleanup()

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

    def test_startup_shortcut_shapes_round_trip_as_this_installs_own_and_carry_the_icon(self):
        found = self.outcome("startup_shortcuts")
        root = found["root"]
        icon = (os.path.join(root, "assets", "odicto.ico") + ",0").lower()
        self.assertTrue(found["direct_owned"], found)
        self.assertTrue(found["task_owned"], found)
        self.assertEqual(os.path.join(root, ".venv", "Scripts", "pythonw.exe").lower(), found["direct_target"].lower())
        self.assertEqual('"%s"' % os.path.join(root, "main.py"), found["direct_args"])
        self.assertEqual(root.lower(), found["direct_dir"].lower())
        self.assertEqual(7, found["direct_style"])
        self.assertEqual('/Run /TN "Odicto"', found["task_args"])
        self.assertEqual(icon, found["direct_icon"].lower())
        self.assertEqual(icon, found["task_icon"].lower())

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

    def helper_section(self, name):
        text = self.read("scripts", "windows", "admin_mode.ps1")
        begin = text.index("function %s {" % name)
        return text[begin:text.index("\nfunction ", begin + 1) if "\nfunction " in text[begin + 1:] else len(text)]

    def test_helper_elevates_before_the_stop_or_configure_calls(self):
        text = self.read("scripts", "windows", "admin_mode.ps1")
        flow = text[text.index("\ntry {\n    if (-not (Test-Admin))"):]
        self.before(flow, "-Verb RunAs", "Assert-Runtime $needPythonw")
        self.before(flow, "-Verb RunAs", "Invoke-Configure\n")
        self.before(flow, "-Verb RunAs", "Invoke-Disable\n")
        self.before(flow, "-Verb RunAs", "$Py @($OdictoPy, 'stop')")

    def test_helper_cancels_the_start_when_the_stop_fails(self):
        text = self.read("scripts", "windows", "admin_mode.ps1")
        self.before(text, "stop failed, start cancelled", "$null = Start-Process -FilePath $Pyw")

    def test_helper_offers_the_disable_action(self):
        text = self.read("scripts", "windows", "admin_mode.ps1")
        self.assertIn("'Configure', 'Disable')]", text)
        self.assertIn("if ($Action -eq 'Disable')", text)

    def test_disable_refuses_before_it_stops_or_changes_anything(self):
        body = self.helper_section("Invoke-Disable")
        self.before(body, "Get-TaskMismatch", "Invoke-Native $Py @($OdictoPy, 'stop')")
        self.before(body, "Test-OwnShortcut", "Invoke-Native $Py @($OdictoPy, 'stop')")
        self.before(body, "Invoke-Native $Py @($OdictoPy, 'stop')", "Unregister-ScheduledTask")

    def test_disable_deletes_the_marker_last_and_only_after_the_task_is_gone(self):
        body = self.helper_section("Invoke-Disable")
        self.before(body, "Unregister-ScheduledTask", "Write-StartupShortcut $lnk $false")
        self.before(body, "if (Get-OdictoTask) { throw", "Remove-Item -LiteralPath $MarkerPath")
        self.before(body, "Write-StartupShortcut $lnk $false", "Remove-Item -LiteralPath $MarkerPath")
        self.assertEqual(body.count("Unregister-ScheduledTask"), 1)
        self.assertIn("-TaskName $TaskName -TaskPath '\\'", body)

    def test_disable_points_the_startup_shortcut_at_direct_pythonw_like_the_plain_installer(self):
        helper = self.read("scripts", "windows", "admin_mode.ps1")
        plain = self.read("scripts", "windows", "make_startup_shortcut.ps1")
        self.assertIn("$link.TargetPath = $Pyw", helper)
        self.assertIn("$link.Arguments = '\"{0}\"' -f $MainPy", helper)
        self.assertIn("Join-Path $repo '.venv\\Scripts\\pythonw.exe'", plain)
        self.assertIn("'\"' + $repo + '\\main.py\"'", plain)

    def test_both_startup_shortcut_shapes_carry_the_icon(self):
        self.assertIn("assets\\odicto.ico", self.read("scripts", "windows", "admin_mode.ps1"))
        self.assertIn("IconLocation", self.read("scripts", "windows", "admin_mode.ps1"))
        self.assertIn("assets\\odicto.ico", self.read("scripts", "windows", "make_startup_shortcut.ps1"))
        self.assertIn("IconLocation", self.read("scripts", "windows", "make_startup_shortcut.ps1"))

    def test_start_bat_never_starts_a_restart_before_the_admin_branch(self):
        text = self.read("scripts", "windows", "start_dictation.bat")
        self.before(text, ".odicto-admin-enabled", 'call "%~dp0stop_dictation.bat"')
        self.before(text, ":admin_restart", 'call "%~dp0stop_dictation.bat"')

    def test_run_debug_delegates_before_it_stops_anything(self):
        text = self.read("scripts", "windows", "run_debug.bat")
        self.before(text, ".odicto-admin-enabled", 'call "%~dp0stop_dictation.bat"')
        self.before(text, "odicto.py\" restart", 'call "%~dp0stop_dictation.bat"')

    def test_marker_is_gitignored(self):
        self.assertIn(".odicto-admin-enabled", self.read(".gitignore").splitlines())


if __name__ == "__main__":
    unittest.main()
