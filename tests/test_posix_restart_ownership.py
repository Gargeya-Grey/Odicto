"""POSIX shutdown proof uses fake processes and private fixture ownership files."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import psutil
from platforms import _posix

_enumerate_real = _posix.enumerate_odicto_pids


class TestPosixShutdownOwnership(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pid_file = self.root / "dictation.pid"
        self.pid_file.write_text("123456", encoding="ascii")
        self.process = MagicMock()
        self.process.name.return_value = "python3.12"
        self.process.cmdline.return_value = ["python3.12", "/fixture/main.py"]
        self.process.cwd.return_value = "/fixture"
        self.process.children.return_value = []
        self.process.create_time.return_value = 1000.0
        self.process.is_running.return_value = True
        self.native = SimpleNamespace(
            Process=MagicMock(return_value=self.process),
            pid_exists=MagicMock(return_value=False),
            NoSuchProcess=psutil.NoSuchProcess,
            Error=psutil.Error,
        )
        self.stack.enter_context(patch.object(_posix, "psutil", self.native))
        self.stack.enter_context(patch.object(_posix, "_self_and_parent_pids", return_value={999}))
        self.enumerate = self.stack.enter_context(patch.object(_posix, "enumerate_odicto_pids", return_value={123456}))
        self.stack.enter_context(patch.object(_posix.base, "lock_file_path", return_value=str(self.root / "dictation.lock")))
        self.stack.enter_context(patch.object(_posix, "_lock_file_obj", None))
        self.sleep = self.stack.enter_context(patch.object(_posix.time, "sleep"))

    def assert_preserved(self):
        self.assertEqual(self.pid_file.read_text(encoding="ascii"), "123456")

    def test_denied_root_owner_stop_keeps_metadata_and_reports_failure(self):
        self.process.kill.side_effect = PermissionError("root owns this process")
        self.native.pid_exists.return_value = True
        with self.assertRaisesRegex((PermissionError, RuntimeError), "stop|root"):
            _posix.kill_other_odicto_processes(str(self.pid_file))
        self.assert_preserved()

    def test_owner_exit_is_confirmed_before_metadata_is_removed(self):
        self.native.pid_exists.side_effect = [True, False]
        self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [123456])
        self.process.kill.assert_called_once()
        self.sleep.assert_called_once()
        self.assertFalse(self.pid_file.exists())

    def test_owner_that_survives_termination_cancels_restart(self):
        self.native.pid_exists.return_value = True
        with patch.object(_posix.time, "monotonic", side_effect=[0.0, 9.0]):
            with self.assertRaisesRegex(RuntimeError, "did not exit"):
                _posix.kill_other_odicto_processes(str(self.pid_file))
        self.assert_preserved()

    def test_unknown_liveness_keeps_ownership(self):
        self.native.pid_exists.side_effect = OSError("process table unavailable")
        with self.assertRaisesRegex(RuntimeError, "verify"):
            _posix.kill_other_odicto_processes(str(self.pid_file))
        self.assert_preserved()

    def test_inaccessible_saved_owner_cancels_without_signalling(self):
        self.enumerate.return_value = set()
        self.native.Process.side_effect = psutil.AccessDenied(123456)
        with self.assertRaisesRegex(RuntimeError, "verify"):
            _posix.kill_other_odicto_processes(str(self.pid_file))
        self.process.kill.assert_not_called()
        self.assert_preserved()

    def test_saved_owner_missed_by_enumeration_is_verified(self):
        self.enumerate.return_value = set()
        with patch.object(_posix.base, "is_odicto_command", return_value=True):
            self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [123456])
        self.process.kill.assert_called_once()
        self.assertFalse(self.pid_file.exists())

    def test_recycled_non_python_pid_is_not_signalled(self):
        self.enumerate.return_value = set()
        self.process.name.return_value = "unrelated-service"
        self.process.cmdline.side_effect = psutil.AccessDenied(123456)
        self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [])
        self.process.cmdline.assert_not_called()
        self.process.kill.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    def test_already_exited_process_is_safe_to_clear(self):
        self.native.Process.side_effect = psutil.NoSuchProcess(123456)
        self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [123456])
        self.assertFalse(self.pid_file.exists())

    def test_unknown_lock_owner_prevents_metadata_removal(self):
        self.enumerate.return_value = set()
        self.native.Process.side_effect = psutil.NoSuchProcess(123456)
        with patch("state_lock.StateLease.__enter__", side_effect=PermissionError("root-owned lock")):
            with self.assertRaisesRegex(RuntimeError, "lock|owner"):
                _posix.kill_other_odicto_processes(str(self.pid_file))
        self.assert_preserved()

    def test_child_termination_denied_is_not_ignored(self):
        child = MagicMock()
        child.kill.side_effect = psutil.AccessDenied(123457)
        self.process.children.return_value = [child]
        with self.assertRaises((PermissionError, RuntimeError)):
            _posix.kill_other_odicto_processes(str(self.pid_file))
        self.assert_preserved()

    def enumerate_process(self):
        self.process.info = {"pid": 123456, "cmdline": ["python3.12", "/fixture/main.py"], "cwd": "/fixture"}
        self.native.process_iter = MagicMock(return_value=[self.process])
        self.enumerate.side_effect = _enumerate_real
        self.stack.enter_context(patch.object(_posix.base, "is_odicto_command", return_value=True))

    def test_verified_process_object_is_not_reopened_before_termination(self):
        self.enumerate_process()
        self.native.Process.side_effect = AssertionError("A PID alone is not a process identity")
        self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [123456])
        self.process.kill.assert_called_once()
        self.native.Process.assert_not_called()

    def test_pid_replacement_after_enumeration_is_never_signalled(self):
        self.enumerate_process()
        self.process.is_running.side_effect = [True, False]
        replacement = MagicMock()
        replacement.children.return_value = []
        self.native.Process.return_value = replacement
        self.native.pid_exists.return_value = True
        with patch.object(_posix.time, "monotonic", side_effect=[0.0, 9.0]):
            with self.assertRaisesRegex(RuntimeError, "identity|reused"):
                _posix.kill_other_odicto_processes(str(self.pid_file))
        self.process.kill.assert_not_called()
        replacement.kill.assert_not_called()
        self.assert_preserved()

    def test_saved_verified_process_is_not_reopened(self):
        self.enumerate.return_value = set()
        replacement = MagicMock()
        self.native.Process.side_effect = [self.process, replacement]
        with patch.object(_posix.base, "is_odicto_command", return_value=True):
            self.assertEqual(_posix.kill_other_odicto_processes(str(self.pid_file)), [123456])
        self.native.Process.assert_called_once_with(123456)
        self.process.kill.assert_called_once()
        replacement.kill.assert_not_called()

    def test_exited_owned_child_does_not_reopen_its_recycled_pid(self):
        owned = MagicMock(pid=123456)
        owned.poll.return_value = 0
        _posix.terminate_process_tree(owned)
        self.native.Process.assert_not_called()


class TestSetupPosixRestart(unittest.TestCase):
    def test_denied_stop_is_actionable_and_never_spawns_replacement(self):
        import platforms
        import setup_web
        import windows_admin
        with patch.object(setup_web.sys, "platform", "linux"), patch(
            "platforms.preflight.environment_problems", return_value=[]
        ), patch.object(windows_admin, "run_if_needed", return_value=None), patch.object(
            setup_web, "_SPAWN_BLOCKED", threading.Event()
        ), patch.object(platforms, "kill_other_odicto_processes", side_effect=PermissionError("old owner runs as root")), \
                patch.object(platforms, "spawn_detached") as spawn:
            with self.assertRaisesRegex(RuntimeError, "quit|privilege"):
                setup_web.restart_odicto()
        spawn.assert_not_called()
