"""Managed startup hands off ownership before activation can proceed."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

import odicto
from tests import test_managed_distribution as fixtures

dist = fixtures.dist


class Child:
    def __init__(self, code=None, wait_error=False):
        self.stdin = io.BytesIO()
        self.code = code
        self.wait_error = wait_error
        self.wait_calls = 0
        self.pid = os.getpid()

    def poll(self):
        return self.code

    def wait(self, timeout):
        self.wait_calls += 1
        if self.wait_error:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        self.code = 2
        return self.code


class TestManagedLaunch(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestManagedDistribution(methodName="test_new_install_creates_settings_once_from_the_example")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.old = self.fixture.initial()

    def update(self):
        self.fixture.add_release("v1.3.0")

    def test_startup_exit_after_spawn_rolls_back(self):
        self.update()
        child = Child(code=3)
        with patch.object(self.manager, "_ready", return_value=False), patch.object(dist.subprocess, "Popen", return_value=child):
            self.assertEqual(self.manager.run(["start"]), 3)
        self.assertEqual(self.manager.active()["directory"], self.old["directory"])
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_failure_to_execute_child_rolls_back(self):
        self.update()
        with patch.object(self.manager, "_ready", return_value=False), patch.object(dist.subprocess, "Popen", side_effect=OSError("fixture execute failure")):
            self.assertEqual(self.manager.run(["start"]), 1)
        self.assertEqual(self.manager.active()["directory"], self.old["directory"])

    def test_never_ready_child_is_cancelled_before_rollback(self):
        self.update()
        child = Child()
        with patch.object(self.manager, "_ready", return_value=False), patch.object(dist.subprocess, "Popen", return_value=child), patch.object(dist, "STARTUP_TIMEOUT", 0):
            self.assertEqual(self.manager.run(["start"]), 1)
        self.assertTrue(child.stdin.closed)
        self.assertEqual(child.wait_calls, 1)
        self.assertEqual(self.manager.active()["directory"], self.old["directory"])

    def test_uncertain_child_exit_preserves_intent_and_defers_rollback(self):
        self.update()
        child = Child(wait_error=True)
        with patch.object(self.manager, "_ready", return_value=False), patch.object(dist.subprocess, "Popen", return_value=child), patch.object(dist, "STARTUP_TIMEOUT", 0):
            self.assertEqual(self.manager.run(["start"]), 1)
        self.assertEqual(self.manager.active()["version"], "v1.3.0")
        self.assertTrue((self.manager.home / "launch-intent.json").exists())
        with self.assertRaises(dist.BusyError):
            self.manager.rollback()

    def test_activation_stays_blocked_until_child_readiness(self):
        child = Child()
        staged = self.manager.stage(self.fixture.add_release("v1.3.0"))
        probes = []

        def ready(directory, python, token=""):
            if not token:
                return False
            with self.assertRaises(dist.BusyError):
                self.manager.activate(staged)
            probes.append(token)
            return True

        with patch.object(self.manager, "_ready", side_effect=ready), patch.object(dist.subprocess, "Popen", return_value=child):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
        self.assertEqual(len(probes), 1)
        self.assertEqual(child.wait_calls, 0)
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_setup_handoff_releases_launch_lock_while_writer_blocks_activation(self):
        staged = self.manager.stage(self.fixture.add_release("v1.3.0"))
        child = Child()

        def spawn(*args, **kwargs):
            environment = kwargs["env"]
            Path(environment["ODICTO_LAUNCH_RECEIPT"]).write_text(json.dumps({
                "token": environment["ODICTO_LAUNCH_TOKEN"], "state": "setup-owned"}))
            return child

        with dist.file_lock(self.manager.data / "setup.lock"):
            with patch.object(dist.subprocess, "Popen", side_effect=spawn):
                self.assertEqual(self.manager.run(["setup"], automatic_updates=False), 0)
            with self.assertRaises(dist.BusyError):
                self.manager.activate(staged)
            with patch.object(self.manager, "_ready", return_value=True):
                self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
            with patch.object(dist.subprocess, "Popen", return_value=Child(code=0)):
                self.assertEqual(self.manager.run(["setup"], automatic_updates=False), 0)
        self.manager.activate(staged)
        self.assertEqual(self.manager.active()["version"], "v1.3.0")

    def reservation(self, state=None):
        token = "a" * 32
        receipt = self.manager.home / ("launch-receipt-" + token + ".json")
        dist.atomic_json(self.manager.home / "launch-intent.json", {
            "token": token, "receipt": receipt.name, "directory": self.old["directory"],
            "supervisor_pid": 12345, "child_pid": 12346})
        if state:
            dist.atomic_json(receipt, {"token": token, "pid": 12347, "state": state})

    def test_abandoned_reservation_recovers_only_after_supervisor_and_child_exit(self):
        self.reservation()
        with patch.object(self.manager, "_pid_alive", return_value=True), self.assertRaises(dist.BusyError):
            self.manager._recover_launch_intent()
        self.assertTrue((self.manager.home / "launch-intent.json").exists())
        with patch.object(self.manager, "_pid_alive", return_value=False):
            self.manager._recover_launch_intent()
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_dead_prepared_child_allows_deferred_rollback(self):
        self.reservation("owner-starting")
        with patch.object(self.manager, "_pid_alive", return_value=False):
            self.manager._recover_launch_intent()
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_crashed_supervisor_after_ready_handoff_does_not_block_running_owner(self):
        self.reservation("owner-starting")
        with patch.object(self.manager, "_pid_alive", return_value=True), patch.object(self.manager, "_ready", return_value=True):
            self.manager._recover_launch_intent()
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_late_child_cannot_prepare_after_reservation_was_retired(self):
        self.reservation()
        receipt = self.manager.home / ("launch-receipt-" + "a" * 32 + ".json")
        with patch.object(self.manager, "_pid_alive", return_value=False):
            self.manager._recover_launch_intent()
        with patch.dict(os.environ, {"ODICTO_MANAGED_HOME": str(self.manager.home),
                                     "ODICTO_LAUNCH_TOKEN": "a" * 32,
                                     "ODICTO_LAUNCH_RECEIPT": str(receipt)}):
            with self.assertRaises(OSError):
                odicto._managed_receipt("owner-starting")
        self.assertFalse(receipt.exists())


class TestManagedChildProtocol(unittest.TestCase):
    def test_guard_does_not_block_normal_interpreter_shutdown(self):
        bootstrap = "import sys;sys.path.insert(0,sys.argv[1]);import odicto;odicto._managed_startup_guard()"
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(Path(odicto.__file__).parent)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(b"S")
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_private_pipe_eof_cancels_child_and_release_permits_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / "fixture.py"
            program.write_text("import time\ntime.sleep(0.15)\n", encoding="utf-8")
            bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);import odicto;"
                         "odicto._main_py=lambda:sys.argv[2];odicto.cmd_managed_owner(None)")
            environment = dict(os.environ, ODICTO_MANAGED_HOME=str(root), ODICTO_LAUNCH_TOKEN="synthetic-token",
                               ODICTO_LAUNCH_RECEIPT=str(root / "receipt.json"))
            (root / "launch-intent.json").write_text(json.dumps({"token": "synthetic-token", "receipt": "receipt.json"}))
            for payload, expected in ((b"S", 2), (b"SR", 0)):
                with self.subTest(payload=payload):
                    result = subprocess.run([sys.executable, "-c", bootstrap, str(Path(odicto.__file__).parent), str(program)],
                                            env=environment, input=payload, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, timeout=15)
                    self.assertEqual(result.returncode, expected, result.stderr.decode(errors="replace"))

    def test_existing_setup_reuses_page_without_starting_a_server(self):
        import setup_web
        environment = {"ODICTO_MANAGED_HOME": "synthetic-home", "ODICTO_LAUNCH_TOKEN": "synthetic-token"}
        with patch.dict(os.environ, environment), patch.object(odicto, "_managed_startup_guard"), patch(
            "state_lock.StateLease.__enter__", side_effect=OSError("owned")
        ), patch.object(setup_web, "_existing_setup_url", return_value="http://127.0.0.1:8765"), patch(
            "webbrowser.open"
        ) as open_page, patch.object(setup_web, "_run_server") as server:
            self.assertEqual(odicto.cmd_managed_setup(None), 0)
        open_page.assert_called_once_with("http://127.0.0.1:8765")
        server.assert_not_called()

    def test_readiness_rejects_stale_launch_token_and_other_release(self):
        import psutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dictation.pid").write_text("12345", encoding="ascii")
            health = {"pid": 12345, "updated_at": time.time(), "ready": True,
                      "launch_token": "old-launch", "code_root": str(root),
                      "microphone": {"closed": False, "callback_age_s": 0.01, "callback_count": 1}}
            (root / "dictation-health.json").write_text(json.dumps(health), encoding="utf-8")
            owner = MagicMock()
            owner.create_time.return_value = time.time() - 20
            with patch.object(odicto, "_data_root", return_value=str(root)), patch.object(psutil, "Process", return_value=owner), patch(
                "platforms.base.is_odicto_command", return_value=True
            ):
                for token, code_root, expected in (("new-launch", str(root), 1),
                                                    ("old-launch", str(root / "different"), 1),
                                                    ("old-launch", str(root), 0)):
                    self.assertEqual(odicto.cmd_wait_ready(SimpleNamespace(timeout=0, launch_token=token,
                                                                          expected_root=code_root)), expected)
