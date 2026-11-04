"""Managed startup hands off ownership before activation can proceed."""
import io
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
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

    def test_startup_exit_names_the_log_to_read(self):
        child = Child(code=3)
        errors = io.StringIO()
        with patch.object(self.manager, "_ready", return_value=False), patch.object(
            dist.subprocess, "Popen", return_value=child
        ), patch.object(sys, "stderr", errors):
            self.assertEqual(self.manager.run(["start"]), 3)
        self.assertIn("exited during startup (code 3)", errors.getvalue())
        self.assertIn(str(self.manager.data / "dictation.log"), errors.getvalue())

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

    def test_stop_reports_busy_until_a_pending_start_has_handed_off(self):
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def spawn(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("The disposable start adapter was not released")
            return Child()

        def start():
            try:
                outcomes.append(self.manager.run(["start"], automatic_updates=False))
            except BaseException as error:
                outcomes.append(error)

        with patch.object(self.manager, "_ready", side_effect=lambda directory, python, token="": bool(token)), \
                patch.object(dist.subprocess, "Popen", side_effect=spawn), \
                patch.object(dist.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as stop:
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(dist.BusyError, "has not completed"):
                    self.manager.run(["stop"], automatic_updates=False)
                stop.assert_not_called()
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [0])

    def test_stop_refuses_an_unconfirmed_prior_start(self):
        self.reservation()
        with patch.object(self.manager, "_pid_alive", return_value=True), \
                patch.object(dist.subprocess, "run") as stop:
            with self.assertRaises(dist.BusyError):
                self.manager.run(["stop"], automatic_updates=False)
        stop.assert_not_called()

    def test_managed_setup_save_uses_the_stable_supervisor(self):
        import setup_web
        import platforms
        launcher = self.manager.home / "launcher.py"
        launcher.write_text("synthetic stable launcher")
        with patch.dict(os.environ, ODICTO_MANAGED_HOME=str(self.manager.home)), \
                patch("platforms.preflight.environment_problems", return_value=[]), \
                patch("windows_admin.run_if_needed", return_value=None) as admin, \
                patch.object(setup_web.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as restart, \
                patch.object(platforms, "kill_other_odicto_processes") as kill, \
                patch.object(platforms, "spawn_detached") as detached:
            message = setup_web.restart_odicto()
        restart.assert_called_once_with(
            [sys.executable, str(launcher), "run", "--no-auto-updates", "restart"], timeout=90)
        self.assertIn("ready", message)
        kill.assert_not_called()
        detached.assert_not_called()
        admin.assert_not_called()

    def test_failed_setup_supervisor_never_falls_back_to_raw_spawn(self):
        import setup_web
        import platforms
        launcher = self.manager.home / "launcher.py"
        launcher.write_text("synthetic stable launcher")
        for result in (SimpleNamespace(returncode=3), subprocess.TimeoutExpired("synthetic supervisor", 90)):
            with self.subTest(result=result), patch.dict(os.environ, ODICTO_MANAGED_HOME=str(self.manager.home)), \
                    patch("platforms.preflight.environment_problems", return_value=[]), \
                    patch("windows_admin.run_if_needed", return_value=None) as admin, \
                    patch.object(setup_web.subprocess, "run", side_effect=result if isinstance(result, Exception) else None,
                                 return_value=result), \
                    patch.object(platforms, "kill_other_odicto_processes") as kill, \
                    patch.object(platforms, "spawn_detached") as detached:
                with self.assertRaisesRegex(RuntimeError, "managed restart"):
                    setup_web.restart_odicto()
                kill.assert_not_called()
                detached.assert_not_called()
                admin.assert_not_called()

    def test_cli_run_can_disable_automatic_updates(self):
        with patch.object(dist, "Manager", return_value=self.manager), patch.object(self.manager, "bind_dependencies"), patch.object(self.manager, "run", return_value=0) as run:
            self.assertEqual(dist.main(["--home", str(self.manager.home), "run", "--no-auto-updates", "restart"]), 0)
        run.assert_called_once_with(["restart"], automatic_updates=False)

    def test_setup_closure_cannot_release_a_save_created_start_before_handoff(self):
        import setup_web
        import platforms
        launcher = self.manager.home / "launcher.py"
        launcher.write_text("synthetic stable launcher")
        staged = self.manager.stage(self.fixture.add_release("v1.3.0"))
        setup = ExitStack()
        setup.enter_context(dist.file_lock(self.manager.data / "setup.lock"))
        blocked = []

        def spawn(*args, **kwargs):
            setup.close()
            with self.assertRaises(dist.BusyError):
                self.manager.activate(staged)
            blocked.append(True)
            return Child()

        def run(command, **kwargs):
            if command[1] == str(launcher):
                self.assertEqual(command[2:], ["run", "--no-auto-updates", "restart"])
                return SimpleNamespace(returncode=self.manager.run(["restart"], automatic_updates=False))
            self.assertEqual(command[-1], "stop")
            return SimpleNamespace(returncode=0)

        try:
            with patch.dict(os.environ, ODICTO_MANAGED_HOME=str(self.manager.home)), \
                    patch("platforms.preflight.environment_problems", return_value=[]), \
                    patch("windows_admin.run_if_needed", return_value=None), \
                    patch.object(self.manager, "_ready", side_effect=lambda directory, python, token="": bool(token)), \
                    patch.object(dist.subprocess, "Popen", side_effect=spawn), \
                    patch.object(dist.subprocess, "run", side_effect=run), \
                    patch.object(platforms, "kill_other_odicto_processes") as kill, \
                    patch.object(platforms, "spawn_detached") as detached:
                self.assertIn("ready", setup_web.restart_odicto())
            self.assertEqual(blocked, [True])
            self.assertEqual(self.manager.active()["directory"], self.old["directory"])
            kill.assert_not_called()
            detached.assert_not_called()
        finally:
            setup.close()

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


class TestManagedOwnerLog(unittest.TestCase):
    """The admin task gives the owner a hidden console as stdout; it must still log to disk.
    Each case imports main in a child interpreter with a synthetic data root, so no live log,
    stream or faulthandler state of this process is touched."""

    def run_child(self, managed, marked=True):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"ODICTO_MANAGED_HOME", "ODICTO_LAUNCH_TOKEN", "ODICTO_DATA_ROOT"}}
            environment.update(ODICTO_DATA_ROOT=temporary, QT_QPA_PLATFORM="offscreen")
            if managed:
                environment.update(ODICTO_MANAGED_HOME=temporary, ODICTO_LAUNCH_TOKEN="a" * 32)
            mark = "import session_log; session_log.mark_managed_owner(True); " if marked else ""
            program = ("import sys; sys.path.insert(0, sys.argv[1]); " + mark + "import main; "
                       "print('Application ready!'); sys.stdout.flush()")
            result = subprocess.run([sys.executable, "-c", program, str(Path(odicto.__file__).parent)],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=120)
            log = Path(temporary) / "dictation.log"
            text = log.read_text(encoding="utf-8") if log.exists() else ""
        return result, text

    def test_managed_owner_logs_to_the_data_root_with_a_console_attached(self):
        result, text = self.run_child(managed=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn("----- session ", text)
        self.assertIn("Application ready!", text)
        self.assertNotIn(b"Application ready!", result.stdout)

    def test_inherited_launcher_variables_alone_do_not_attach_the_log(self):
        # A test runner or child process inherits the launcher's variables, not the in-memory mark.
        result, text = self.run_child(managed=True, marked=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(text, "")
        self.assertIn(b"Application ready!", result.stdout)

    def test_console_run_outside_a_managed_launch_keeps_its_console(self):
        result, text = self.run_child(managed=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(text, "")
        self.assertIn(b"Application ready!", result.stdout)


class TestLogPrivacyAndOwnership(unittest.TestCase):
    def test_provider_error_echoing_dictation_logs_only_type_and_status(self):
        import http_clients
        from config import Config
        echoed = RuntimeError("HTTP 400 {\"error\": \"bad input: my secret dictated sentence\"}")
        with patch.object(Config, "LOG_TRANSCRIPTS", False):
            summary = http_clients.error_summary(echoed)
        self.assertEqual(summary, "RuntimeError (HTTP 400)")
        self.assertNotIn("dictated", summary)
        with patch.object(Config, "LOG_TRANSCRIPTS", False):
            self.assertEqual(http_clients.error_summary(TimeoutError("secret words")), "TimeoutError")
        with patch.object(Config, "LOG_TRANSCRIPTS", True):
            self.assertIn("dictated", http_clients.error_summary(echoed))

    def test_ai_failure_line_keeps_the_dictation_out_of_the_log(self):
        import refiner
        from config import Config
        source = Path(refiner.__file__).read_text(encoding="utf-8")
        self.assertIn("({self.provider}): {error_summary(e)}", source)
        self.assertNotIn("({self.provider}): {e}", source)
        with patch.object(Config, "LOG_TRANSCRIPTS", False):
            self.assertNotIn("sentence", refiner.error_summary(RuntimeError("HTTP 422 my sentence")))

    def test_cloud_stt_fallback_line_hides_an_echoed_body_but_the_caller_keeps_it(self):
        import numpy as np
        import transcriber
        from config import Config
        audio = np.ones(1600, dtype=np.float32) * 0.1
        printed = io.StringIO()
        with patch.object(transcriber, "WhisperTranscriber") as whisper, patch.object(
            transcriber, "_post_bytes", side_effect=RuntimeError("HTTP 400 my secret sentence")
        ), patch.object(Config, "LOG_TRANSCRIPTS", False), patch.object(sys, "stdout", printed):
            whisper.return_value.transcribe.return_value = "local words"
            cloud = transcriber.CloudTranscriber("groq")
            cloud._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
            self.assertEqual(cloud.transcribe(audio), "local words")
            with self.assertRaisesRegex(transcriber.LocalFallbackDisabled, "secret sentence"):
                cloud.transcribe(audio, allow_local_fallback=False)
        self.assertIn("STT fallback to Whisper (RuntimeError (HTTP 400))", printed.getvalue())
        self.assertNotIn("secret", printed.getvalue())

    def test_gemini_live_errors_keep_the_dictation_out_of_the_log(self):
        import transcriber
        source = Path(transcriber.__file__).read_text(encoding="utf-8")
        assigned = [line.strip() for line in source.splitlines()
                    if "self._error = " in line and "error_summary" not in line]
        self.assertTrue(all(line.split("=", 1)[1].strip().startswith('"') for line in assigned),
                        assigned)

    def test_meta_incomplete_details_log_only_the_reason(self):
        import refiner
        output = io.StringIO()
        data = {"status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens", "text": "my secret sentence"},
                "output": [{"type": "message", "role": "assistant"}, {"type": "secret", "role": "secret"}],
                "usage": {"input_tokens": 12, "output_tokens": 64, "note": "my secret sentence",
                          "output_tokens_details": {"reasoning_tokens": 40, "x": "secret"}}}
        with patch.object(sys, "stdout", output):
            refiner._log_meta_response(data, 64)
            refiner._log_meta_response({"status": "secretstatus",
                                        "incomplete_details": {"reason": "secretword"}}, 64)
            refiner._log_meta_response({"status": "incomplete",
                                        "incomplete_details": "my secret sentence"}, 64)
        text = output.getvalue()
        self.assertIn("reason=max_output_tokens", text)
        self.assertIn("output=[message/assistant, ?/?]", text)
        self.assertIn("usage=input_tokens=12 output_tokens=64 reasoning_tokens=40", text)
        self.assertIn("status=?", text)
        self.assertEqual(text.count("reason=?"), 2)
        self.assertNotIn("secret", text)

    def test_wrapped_provider_error_keeps_its_http_status(self):
        import http_clients
        from config import Config

        class Response:
            status_code = 429

        class HTTPError(Exception):
            response = Response()

        try:
            try:
                raise HTTPError("429 Too Many Requests: my secret words")
            except HTTPError as cause:
                raise RuntimeError("Meta API error: my secret words") from cause
        except RuntimeError as wrapped:
            with patch.object(Config, "LOG_TRANSCRIPTS", False):
                self.assertEqual(http_clients.error_summary(wrapped), "RuntimeError (HTTP 429)")

    def test_test_package_drops_an_inherited_launch_token(self):
        import tests
        source = Path(tests.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ.pop("ODICTO_LAUNCH_TOKEN", None)', source)
        self.assertNotIn("ODICTO_LAUNCH_TOKEN", os.environ)

    def test_disabled_local_fallback_logs_the_cause_status_not_the_words(self):
        import http_clients
        import transcriber
        from config import Config
        from types import SimpleNamespace
        cause = RuntimeError("HTTP 429 my secret words")
        for cls, owner in ((transcriber.CloudTranscriber, SimpleNamespace(kind="groq")),
                           (transcriber.GeminiTranscriber, SimpleNamespace())):
            with self.assertRaises(transcriber.LocalFallbackDisabled) as caught:
                cls._whisper_fallback(owner, None, str(cause), False, error=cause)
            self.assertIn("secret words", str(caught.exception))  # the caller keeps the full reason
            with patch.object(Config, "LOG_TRANSCRIPTS", False):
                self.assertEqual(http_clients.error_summary(caught.exception), "RuntimeError (HTTP 429)")
        with self.assertRaises(transcriber.LocalFallbackDisabled) as caught:
            transcriber.CloudTranscriber._whisper_fallback(SimpleNamespace(kind="groq"), None,
                                                          "timed out", False)
        with patch.object(Config, "LOG_TRANSCRIPTS", False):
            self.assertEqual(http_clients.error_summary(caught.exception), "timed out")

    def test_trim_waits_for_another_process_that_holds_the_log_lock(self):
        import main as main_mod
        import session_log
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "dictation.log")
            Path(path).write_text("OLD-HEAD\n" + "n" * 200 + "\nKEEP\n", encoding="utf-8")
            program = (
                "import sys, time; sys.path.insert(0, sys.argv[1]); import session_log\n"
                "path = sys.argv[2]\n"
                "with session_log.LogLock(path):\n"
                "    print('locked', flush=True); time.sleep(0.8)\n"
                "    open(path, 'a', encoding='utf-8').write('LATE-PEER-LINE\\n')\n"
            )
            peer = subprocess.Popen([sys.executable, "-c", program, str(Path(main_mod.__file__).parent), path],
                                    stdout=subprocess.PIPE)
            try:
                self.assertEqual(peer.stdout.readline().strip(), b"locked")
                started = time.monotonic()
                with patch.object(main_mod, "_LOG_LOCK", session_log.LogLock(path)), patch.object(
                    main_mod, "_LOG_MAX_BYTES", 40), patch.object(main_mod, "_LOG_KEEP_BYTES", 30):
                    main_mod._trim_log(path)
                waited = time.monotonic() - started
            finally:
                peer.wait(timeout=30)
            text = Path(path).read_text(encoding="utf-8")
        self.assertGreater(waited, 0.3)
        self.assertIn("LATE-PEER-LINE", text)
        self.assertNotIn("OLD-HEAD", text)

    def test_trim_skips_when_another_process_holds_the_lock_too_long(self):
        import main as main_mod
        import session_log
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "dictation.log")
            original = "OLD-HEAD\n" + "n" * 200 + "\nKEEP\n"
            Path(path).write_text(original, encoding="utf-8")
            program = ("import sys, time; sys.path.insert(0, sys.argv[1]); import session_log\n"
                       "with session_log.LogLock(sys.argv[2]):\n"
                       "    print('locked', flush=True); time.sleep(3)\n")
            peer = subprocess.Popen([sys.executable, "-c", program, str(Path(main_mod.__file__).parent), path],
                                    stdout=subprocess.PIPE)
            try:
                self.assertEqual(peer.stdout.readline().strip(), b"locked")
                with patch.object(main_mod, "_LOG_LOCK", session_log.LogLock(path)), patch.object(
                    main_mod, "_LOG_MAX_BYTES", 40), patch.object(main_mod, "_LOG_KEEP_BYTES", 30):
                    main_mod._trim_log(path)
                self.assertEqual(Path(path).read_text(encoding="utf-8"), original)
            finally:
                peer.wait(timeout=30)
                peer.stdout.close()

    def test_writers_wait_briefly_then_barely_while_a_peer_holds_the_lock(self):
        import session_log
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "dictation.log")
            program = ("import sys, time; sys.path.insert(0, sys.argv[1]); import session_log\n"
                       "with session_log.LogLock(sys.argv[2]):\n"
                       "    print('locked', flush=True); time.sleep(3)\n")
            peer = subprocess.Popen([sys.executable, "-c", program, str(Path(session_log.__file__).parent),
                                     path], stdout=subprocess.PIPE)
            try:
                self.assertEqual(peer.stdout.readline().strip(), b"locked")
                lock = session_log.LogLock(path)
                started = time.monotonic()
                with lock:
                    self.assertFalse(lock.held)
                first = time.monotonic() - started
                started = time.monotonic()
                for _ in range(10):
                    with lock:
                        pass
                later = time.monotonic() - started
            finally:
                peer.wait(timeout=30)
                peer.stdout.close()
        self.assertLess(first, 1.0)
        self.assertLess(later, 1.0)  # ten writes in degraded mode, not ten full waits

    def test_main_failing_in_a_managed_start_is_logged_and_unmarks_the_owner(self):
        import session_log
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"ODICTO_MANAGED_HOME": temporary, "ODICTO_LAUNCH_TOKEN": "a" * 32}
        ), patch.object(odicto, "_data_root", return_value=temporary), patch.object(
            odicto, "_managed_startup_guard", return_value=None
        ), patch.object(odicto, "_managed_receipt", return_value=None), patch(
            "runpy.run_path", side_effect=FileNotFoundError(2, "No such file", "main.py")
        ):
            with self.assertRaises(FileNotFoundError):
                odicto.cmd_managed_owner(None)
            text = Path(temporary, "dictation.log").read_text(encoding="utf-8")
        self.assertIn("Managed owner failed", text)
        self.assertIn("FileNotFoundError", text)
        self.assertFalse(session_log.is_managed_owner())

    def test_openrouter_response_lines_log_only_known_values(self):
        import refiner
        from types import SimpleNamespace
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="my secret words")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=0, total_tokens=3, note="secret"))
        self.assertEqual(refiner._logged_finish_reason(response), "?")
        self.assertEqual(refiner._logged_usage(response),
                         "prompt_tokens=3 completion_tokens=0 total_tokens=3")
        response.choices[0].finish_reason = "length"
        self.assertEqual(refiner._logged_finish_reason(response), "length")
        source = Path(refiner.__file__).read_text(encoding="utf-8")
        self.assertNotIn("finish_reason={_choice_finish_reason", source)
        self.assertNotIn("usage={getattr(response", source)

    def test_gemini_live_task_errors_are_summarized_not_printed_verbatim(self):
        import transcriber
        from config import Config
        from types import SimpleNamespace
        session = transcriber.GeminiLiveSession()
        task = SimpleNamespace(cancelled=lambda: False, exception=lambda: RuntimeError("HTTP 500 my secret"))
        output = io.StringIO()
        with patch.object(Config, "LOG_TRANSCRIPTS", False), patch.object(sys, "stdout", output):
            transcriber.GeminiLiveSession._observe_task(session, task)
            transcriber.GeminiLiveSession._loop_exception(None, {"exception": RuntimeError("HTTP 502 my secret"),
                                                                 "message": "Task exception was never retrieved"})
        self.assertEqual(session._error, "RuntimeError (HTTP 500)")
        self.assertIn("RuntimeError (HTTP 502)", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_safe_traceback_keeps_frames_and_types_not_messages(self):
        import session_log
        cause_text, message = "my " + "secret cause", "my " + "secret words"  # source lines are shown
        try:
            try:
                raise ValueError(cause_text)
            except ValueError as cause:
                raise RuntimeError(message) from cause
        except RuntimeError as error:
            text = session_log.safe_traceback(type(error), error, error.__traceback__)
        self.assertIn("Traceback (most recent call last):", text)
        self.assertIn("test_managed_launch.py", text)
        self.assertIn("RuntimeError (message hidden", text)
        self.assertIn("from ValueError", text)
        self.assertNotIn("secret", text)

    def test_uncaught_exception_in_the_attached_log_hides_its_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"ODICTO_MANAGED_HOME", "ODICTO_LAUNCH_TOKEN", "ODICTO_DATA_ROOT"}}
            environment.update(ODICTO_DATA_ROOT=temporary, QT_QPA_PLATFORM="offscreen",
                               ODICTO_MANAGED_HOME=temporary, ODICTO_LAUNCH_TOKEN="a" * 32)
            program = ("import sys, threading; sys.path.insert(0, sys.argv[1]); import session_log; "
                       "session_log.mark_managed_owner(True); import main; "
                       "t = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError('thread secret'))); "
                       "t.start(); t.join(); raise RuntimeError('my secret words')")
            result = subprocess.run([sys.executable, "-c", program, str(Path(odicto.__file__).parent)],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            text = Path(temporary, "dictation.log").read_text(encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RuntimeError (message hidden", text)
        self.assertIn("Exception in thread", text)
        self.assertNotIn("secret", text)

    def test_queued_writers_all_get_the_short_wait(self):
        import session_log
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "dictation.log")
            program = ("import sys, time; sys.path.insert(0, sys.argv[1]); import session_log\n"
                       "with session_log.LogLock(sys.argv[2]):\n"
                       "    print('locked', flush=True); time.sleep(3)\n")
            peer = subprocess.Popen([sys.executable, "-c", program, str(Path(session_log.__file__).parent),
                                     path], stdout=subprocess.PIPE)
            try:
                self.assertEqual(peer.stdout.readline().strip(), b"locked")
                lock = session_log.LogLock(path)

                def write():
                    with lock:
                        pass

                writers = [threading.Thread(target=write) for _ in range(5)]
                started = time.monotonic()
                for writer in writers:
                    writer.start()
                for writer in writers:
                    writer.join()
                elapsed = time.monotonic() - started
            finally:
                peer.wait(timeout=30)
                peer.stdout.close()
        self.assertLess(elapsed, 0.8)  # one 0.25 s wait, then short waits; not 5 x 0.25 s

    def test_managed_failure_before_main_is_logged(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"ODICTO_MANAGED_HOME": temporary, "ODICTO_LAUNCH_TOKEN": "a" * 32}
        ), patch.object(odicto, "_data_root", return_value=temporary), patch.object(
            odicto, "_managed_startup_guard", return_value=None
        ), patch.object(odicto, "_managed_receipt", side_effect=PermissionError("access denied")), patch(
            "runpy.run_path"
        ) as run:
            with self.assertRaises(PermissionError):
                odicto.cmd_managed_owner(None)
            text = Path(temporary, "dictation.log").read_text(encoding="utf-8")
        run.assert_not_called()
        self.assertIn("Managed startup failed before main", text)
        self.assertIn("PermissionError: access denied", text)

    def test_only_the_lock_holder_trims_the_shared_log(self):
        import main as main_mod
        trims = []
        with patch.object(main_mod, "_trim_log", side_effect=trims.append), patch.object(
            main_mod, "_LOG_ATTACHED", True
        ), patch.object(main_mod.platforms, "acquire_lock", return_value=False):
            self.assertFalse(main_mod.acquire_single_instance_lock(1))
        self.assertEqual(trims, [])
        source = Path(main_mod.__file__).read_text(encoding="utf-8")
        attach = source[source.index("def attach_pythonw_log"):source.index("# Redirect before importing")]
        self.assertNotIn("_trim_log(", attach)


class TestManagedChildProtocol(unittest.TestCase):
    def setUp(self):
        # Children inherit this environment. A guard that ends early logs one line to the data
        # root, so each test points that at its own folder, never at the checkout's real log.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name)
        environment = patch.dict(os.environ, {"ODICTO_DATA_ROOT": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

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

    def test_waiting_guard_does_not_stall_other_stdin_operations(self):
        # Windows queues every operation on a synchronous pipe behind a pending ReadFile.
        # numpy's OpenBLAS load seeks stdin, so a guard that blocks in os.read hung the
        # real managed owner inside `import recorder` until the 60 s startup timeout.
        # The lease thread must be observed entering its wait (a read in the old guard, the pipe
        # poll in the fixed one), then the seek waits 1 s more, so the old guard is reliably red.
        bootstrap = textwrap.dedent("""
            import os, sys, threading, time
            sys.path.insert(0, sys.argv[1])
            import odicto
            real_read, entered = os.read, threading.Event()
            real_wait = getattr(odicto, "_wait_for_pipe_data", None)
            def observed_read(descriptor, size):
                if threading.current_thread() is not threading.main_thread():
                    entered.set()
                return real_read(descriptor, size)
            def observed_wait(descriptor):
                entered.set()
                return real_wait(descriptor)
            odicto.os.read = observed_read
            if real_wait is not None:
                odicto._wait_for_pipe_data = observed_wait
            odicto._managed_startup_guard()
            if not entered.wait(5.0):
                sys.exit(3)  # the lease thread never started waiting
            time.sleep(1.0)
            try:
                os.lseek(0, 0, 1)
            except OSError:
                pass
            sys.stdout.write("imported\\n")
            sys.stdout.flush()
            time.sleep(30)
        """)
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(Path(odicto.__file__).parent)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
        lines = []
        try:
            process.stdin.write(b"S")
            process.stdin.flush()
            reader.start()
            reader.join(timeout=10)
            self.assertEqual([line.strip() for line in lines], [b"imported"],
                             "startup stalled on stdin while the guard waited")
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10), 2, "launcher EOF must still cancel the child")
            self.assertIn("the launcher ended before readiness",
                          (self.data_root / "dictation.log").read_text(encoding="utf-8"))
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_lease_failure_after_start_ends_the_unconfirmed_child(self):
        # A failure inside the lease thread must not leave a child running without supervision.
        bootstrap = textwrap.dedent("""
            import os, sys, time
            sys.path.insert(0, sys.argv[1])
            import odicto
            real, calls = os.read, []
            def failing(descriptor, size):
                calls.append(descriptor)
                if len(calls) > 1:
                    raise OSError("synthetic lease failure")
                return real(descriptor, size)
            def broken_wait(descriptor):
                raise OSError("synthetic lease failure")
            odicto.os.read = failing
            odicto._wait_for_pipe_data = broken_wait
            odicto._managed_startup_guard()
            time.sleep(20)
        """)
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(Path(odicto.__file__).parent)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(b"S")
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=10), 2)  # stdin stays open: the launcher is alive
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
