"""Windows stop regressions with all process operations replaced by test doubles."""
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows process lifecycle")
class TestWindowsStop(unittest.TestCase):
    def setUp(self):
        from platforms import windows
        self.backend = windows
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = os.path.join(directory, "dictation.pid")
        with open(self.path, "w", encoding="ascii") as handle:
            handle.write("123456")
        self.stack.enter_context(patch.object(windows, "_self_and_parent_pids", return_value={999}))
        self.enumerate = self.stack.enter_context(patch.object(windows, "_enumerate_odicto_pids", return_value={123456}))
        self.run = self.stack.enter_context(patch.object(windows.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
        self.exists = self.stack.enter_context(patch("psutil.pid_exists", return_value=False))
        self.sleep = self.stack.enter_context(patch.object(windows.time, "sleep"))

    def assert_preserved(self):
        with open(self.path, encoding="ascii") as handle:
            self.assertEqual(handle.read(), "123456")

    def test_execution_error_preserves_ownership(self):
        self.run.side_effect = FileNotFoundError("taskkill unavailable")
        with self.assertRaises((OSError, RuntimeError)):
            self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()

    def test_taskkill_timeout_preserves_ownership(self):
        self.run.side_effect = subprocess.TimeoutExpired("taskkill", 5)
        with self.assertRaises((OSError, RuntimeError)):
            self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()

    def test_refused_stop_preserves_ownership(self):
        self.run.return_value = SimpleNamespace(returncode=5)
        self.exists.return_value = True
        with self.assertRaises(PermissionError):
            self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()

    def test_parent_tree_stop_tolerates_already_exited_child(self):
        self.enumerate.return_value = {123456, 123457}
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=128)]
        self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [123456, 123457])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.run.call_count, 2)

    def test_live_process_after_deadline_cancels_restart(self):
        self.exists.return_value = True
        with patch.object(self.backend.time, "monotonic", side_effect=[0.0, 8.0]):
            with self.assertRaisesRegex(RuntimeError, "did not exit"):
                self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()

    def test_waits_until_native_process_exits(self):
        self.exists.side_effect = [True, False]
        self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [123456])
        self.sleep.assert_called_once()
        self.assertFalse(os.path.exists(self.path))

    def test_verification_error_preserves_ownership(self):
        self.exists.side_effect = OSError("process metadata unavailable")
        with self.assertRaises((OSError, RuntimeError)):
            self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()

    def test_success_needs_no_shell_poll_or_fixed_sleep(self):
        self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [123456])
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][0], "taskkill")
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 8.0)
        self.sleep.assert_not_called()

    def test_inaccessible_saved_owner_cancels_stop(self):
        import psutil
        self.enumerate.return_value = set()
        with patch("psutil.Process", side_effect=psutil.AccessDenied(123456)):
            with self.assertRaisesRegex(RuntimeError, "verify"):
                self.backend.kill_other_odicto_processes(self.path)
        self.assert_preserved()
        self.run.assert_not_called()

    def test_recycled_saved_pid_never_kills_unrelated_process(self):
        self.enumerate.return_value = set()
        process = MagicMock()
        process.cmdline.return_value = ["python.exe", "other.py"]
        process.cwd.return_value = self.backend.base.install_root()
        with patch("psutil.Process", return_value=process):
            self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [])
        self.run.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_exited_saved_pid_can_be_removed(self):
        import psutil
        self.enumerate.return_value = set()
        with patch("psutil.Process", side_effect=psutil.NoSuchProcess(123456)):
            self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [])
        self.run.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_saved_owner_missed_by_enumeration_is_verified_before_stop(self):
        self.enumerate.return_value = set()
        process = MagicMock()
        process.cmdline.return_value = ["python.exe", "main.py"]
        process.cwd.return_value = self.backend.base.install_root()
        with patch("psutil.Process", return_value=process):
            self.assertEqual(self.backend.kill_other_odicto_processes(self.path), [123456])
        self.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
