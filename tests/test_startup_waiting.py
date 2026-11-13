"""Start-up that waits: the owner says what it waits for, and the launcher accepts a running owner.

Fakes only: no microphone, model download, network, keyboard hook or real Odicto process.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import main
import odicto
import transcriber
from app_state import AppState
from config import Config
from tests import test_managed_distribution as fixtures
from tests.test_managed_launch import Child
from tests.test_pipeline_deadlines import _Base
from tests.test_reliability import app_fixture

dist = fixtures.dist
APP_DIR = Path(odicto.__file__).resolve().parent


class TestWaitReadyReportsWaiting(unittest.TestCase):
    """`odicto.py wait-ready --report-waiting` for a verified owner that is not ready."""

    def health(self, **update):
        value = {"pid": 12345, "updated_at": time.time(), "ready": False, "initialized": False,
                 "launch_token": "tok", "code_root": "root",
                 "init": {"stage": None, "reason": None},
                 "microphone": {"closed": False, "callback_age_s": 0.05, "callback_count": 0}}
        value.update(update)
        return value

    def check(self, health, *, report=True, timeout=0, token=None, root=None, verified=True, created=None):
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "dictation.pid"), "w") as handle:
                handle.write("12345")
            with open(os.path.join(directory, "dictation-health.json"), "w") as handle:
                json.dump(health, handle)
            owner = MagicMock()
            owner.create_time.return_value = time.time() - 20 if created is None else created
            args = SimpleNamespace(timeout=timeout, launch_token=token, expected_root=root)
            if report is not None:
                args.report_waiting = report
            with patch.object(odicto, "_data_root", return_value=directory), \
                    patch("psutil.Process", return_value=owner), \
                    patch("platforms.base.is_odicto_command", return_value=verified), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = odicto.cmd_wait_ready(args)
        return code, output.getvalue(), errors.getvalue()

    def test_each_waiting_state_prints_its_marker_and_exit_code(self):
        cases = (
            ("mic", {"init": {"stage": None, "reason": "mic"}}, 20),
            ("mic", {"initialized": True}, 20),  # only the microphone clause of readiness fails
            ("speech", {"init": {"stage": "speech", "reason": "speech"}}, 20),
            ("speech-download", {"init": {"stage": "speech-download", "reason": None}}, 21),
        )
        for state, update, code in cases:
            with self.subTest(state=state, update=update):
                result, output, _ = self.check(self.health(**update))
                self.assertEqual((result, output), (code, f"ODICTO-WAITING {state}\n"))

    def test_a_failed_speech_try_that_downloads_again_is_speech_not_download(self):
        health = self.health(init={"stage": "speech-download", "reason": "speech"})
        self.assertEqual(self.check(health)[:2], (20, "ODICTO-WAITING speech\n"))

    def test_other_stages_and_an_owner_with_no_init_record_are_not_waiting(self):
        for init in ({"stage": None, "reason": "ai"}, {"stage": None, "reason": "hotkeys"},
                     {"stage": "ai", "reason": None}, {"stage": "mic", "reason": None},
                     {"stage": "speech", "reason": None}, {}, None):
            with self.subTest(init=init):
                health = self.health(init=init)
                code, output, errors = self.check(health)
                self.assertEqual((code, output), (1, ""))
                self.assertIn("Readiness not confirmed", errors)
        health = self.health()
        del health["init"], health["initialized"]  # an owner older than this change
        self.assertEqual(self.check(health)[:2], (1, ""))

    def test_a_ready_owner_is_ready_and_prints_no_marker(self):
        health = self.health(ready=True, initialized=True,
                             microphone={"closed": False, "callback_age_s": 0.05, "callback_count": 4})
        code, output, _ = self.check(health)
        self.assertEqual(code, 0)
        self.assertIn("Odicto ready", output)
        self.assertNotIn("ODICTO-WAITING", output)

    def test_a_ready_owner_with_an_old_heartbeat_is_not_reported_as_waiting(self):
        # The owner says it is ready; only this reader's freshness window failed.
        health = self.health(ready=True, initialized=True, updated_at=time.time() - 4,
                             microphone={"closed": False, "callback_age_s": 0.05, "callback_count": 4})
        self.assertEqual(self.check(health)[:2], (1, ""))

    def test_without_the_flag_a_waiting_owner_is_still_not_ready(self):
        for report in (False, None):  # None: a namespace that never had the attribute
            for update in ({"init": {"stage": None, "reason": "mic"}}, {"initialized": True},
                           {"init": {"stage": "speech-download", "reason": None}}):
                with self.subTest(report=report, update=update):
                    code, output, errors = self.check(self.health(**update), report=report)
                    self.assertEqual((code, output), (1, ""))
                    self.assertIn("Readiness not confirmed", errors)

    def test_an_unverified_owner_never_counts_as_waiting(self):
        waiting = {"init": {"stage": None, "reason": "mic"}}
        cases = {
            "stale heartbeat": dict(health=self.health(updated_at=time.time() - 11, **waiting)),
            "pid mismatch": dict(health=self.health(pid=12346, **waiting)),
            "launch token": dict(health=self.health(**waiting), token="other"),
            "code root": dict(health=self.health(**waiting), root="elsewhere"),
            "not an Odicto process": dict(health=self.health(**waiting), verified=False),
            "process younger than heartbeat": dict(health=self.health(**waiting), created=time.time() + 5),
        }
        for name, case in cases.items():
            with self.subTest(name):
                code, output, _ = self.check(**case)
                self.assertEqual((code, output), (1, ""))

    def test_the_token_and_root_rules_apply_to_a_waiting_owner_that_matches(self):
        health = self.health(init={"stage": None, "reason": "mic"})
        code, output, _ = self.check(health, token="tok", root="root")
        self.assertEqual((code, output), (20, "ODICTO-WAITING mic\n"))

    def test_the_first_waiting_poll_ends_the_wait(self):
        started = time.monotonic()
        code, output, _ = self.check(self.health(init={"stage": None, "reason": "speech"}), timeout=30)
        self.assertEqual((code, output), (20, "ODICTO-WAITING speech\n"))
        self.assertLess(time.monotonic() - started, 5)

    def test_both_parsers_accept_the_flag(self):
        parsed = dist._app_parser().parse_args(["wait-ready", "--timeout", "0", "--report-waiting"])
        self.assertTrue(parsed.report_waiting)
        self.assertIsNone(dist._app_args_refused(["wait-ready", "--timeout", "0", "--report-waiting"]))
        with patch.object(sys, "argv", ["odicto.py", "wait-ready", "--timeout", "0", "--report-waiting"]), \
                patch.object(odicto, "cmd_wait_ready", return_value=0) as handler:
            self.assertEqual(odicto.main(), 0)
        self.assertTrue(handler.call_args.args[0].report_waiting)
        with patch.object(sys, "argv", ["odicto.py", "wait-ready"]), \
                patch.object(odicto, "cmd_wait_ready", return_value=0) as handler:
            odicto.main()
        self.assertFalse(handler.call_args.args[0].report_waiting)
        self.assertNotIn("report-waiting", dist._app_parser().format_help())


class TestStartupState(unittest.TestCase):
    """The manager's probe: a waiting state needs the exit code and the marker to agree."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manager = dist.Manager(Path(self.temporary.name) / "managed")
        self.directory = Path(self.temporary.name) / "release"

    def state(self, code, stdout="", token=""):
        with patch.object(dist.subprocess, "run",
                          return_value=SimpleNamespace(returncode=code, stdout=stdout)) as run:
            return self.manager._startup_state(self.directory, Path("python"), token), run

    def test_the_command_asks_for_the_waiting_report_with_this_release_and_token(self):
        _, run = self.state(0, token="t" * 32)
        command = run.call_args.args[0]
        self.assertEqual(command[2:], ["wait-ready", "--timeout", "0", "--report-waiting", "--expected-root",
                                       str(self.directory), "--launch-token", "t" * 32])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        _, run = self.state(0)
        self.assertNotIn("--launch-token", run.call_args.args[0])

    def test_each_state_needs_its_code_and_its_marker(self):
        for code, marker, expected in ((0, "", "ready"), (20, "mic", "mic"), (20, "speech", "speech"),
                                       (21, "speech-download", "speech-download")):
            with self.subTest(code=code, marker=marker):
                text = f"ODICTO-WAITING {marker}\n" if marker else "Odicto ready (app PID 1)\n"
                self.assertEqual(self.state(code, text)[0], expected)

    def test_a_code_without_the_marker_is_none(self):
        for code in (20, 21, 3, 1, 2, -1, 75):
            with self.subTest(code=code):
                self.assertEqual(self.state(code, "")[0], "none")
                self.assertEqual(self.state(code, "some other output\n")[0], "none")

    def test_a_marker_that_disagrees_with_the_code_is_none(self):
        for code, text in ((20, "ODICTO-WAITING speech-download\n"), (21, "ODICTO-WAITING mic\n"),
                           (21, "ODICTO-WAITING speech\n"), (20, "ODICTO-WAITING bogus\n"),
                           (3, "ODICTO-WAITING mic\n"), (1, "ODICTO-WAITING speech\n"),
                           (20, "ODICTO-WAITING mic\nODICTO-WAITING speech\n"), (20, "ODICTO-WAITING\n"),
                           (20, "ODICTO-WAITING \n"), (21, " \nODICTO-WAITING\t\n"),
                           (20, "ODICTO-WAITING mic extra\n"), (20, "ODICTO-WAITINGmic\n")):
            with self.subTest(code=code, text=text):
                self.assertEqual(self.state(code, text)[0], "none")

    def test_a_dead_or_late_probe_is_none(self):
        for error in (subprocess.TimeoutExpired("wait-ready", 15), OSError("no python"),
                      subprocess.SubprocessError("broken")):
            with self.subTest(error=type(error).__name__):
                with patch.object(dist.subprocess, "run", side_effect=error):
                    self.assertEqual(self.manager._startup_state(self.directory, Path("python")), "none")

    def test_strict_counts_only_a_ready_owner(self):
        for state, strict, expected in (("ready", True, "ready"), ("ready", False, "ready"),
                                        ("mic", True, "none"), ("mic", False, "mic"),
                                        ("speech", False, "speech"), ("speech", True, "none"),
                                        ("speech-download", False, "speech-download"),
                                        ("speech-download", True, "none"), ("none", False, "none")):
            with self.subTest(state=state, strict=strict):
                with patch.object(self.manager, "_startup_state", return_value=state):
                    self.assertEqual(self.manager._started_state(self.directory, Path("python"),
                                                                 strict=strict), expected)


class TestLaunchAcceptsAWaitingOwner(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestManagedDistribution(
            methodName="test_new_install_creates_settings_once_from_the_example")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.old = self.fixture.initial()
        self.home = self.manager.home

    def child(self, code=None):
        child = Child(code=code)
        child.stdin = MagicMock(spec=io.BytesIO)
        child.stdin.closed = False
        return child

    def probe(self, *states):
        """A _startup_state double: the first states in turn, then the last one for ever."""
        queue = list(states)
        calls = []

        def state(directory, python, token=""):
            calls.append((time.monotonic(), token))
            return queue.pop(0) if len(queue) > 1 else queue[0]
        state.calls = calls
        return state

    def start(self, child, probe, **run_arguments):
        errors = io.StringIO()
        with patch.object(self.manager, "_startup_state", side_effect=probe), \
                patch.object(dist.subprocess, "Popen", return_value=child) as spawn, \
                patch.object(sys, "stderr", errors):
            result = self.manager.run(["start"], **{"automatic_updates": False, **run_arguments})
        return result, errors.getvalue(), spawn

    def test_mic_and_speech_are_accepted_at_once_with_the_same_handoff_as_ready(self):
        for state in ("ready", "mic", "speech"):
            with self.subTest(state=state):
                child = self.child()
                result, errors, _ = self.start(child, self.probe("none", state))
                self.assertEqual(result, 0)
                self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S", b"R"])
                child.stdin.close.assert_called_once()
                self.assertEqual(child.wait_calls, 0)
                self.assertFalse((self.home / "launch-intent.json").exists())
                self.assertFalse(list(self.home.glob("launch-receipt-*")))
                if state == "ready":
                    self.assertEqual(errors, "")
                    self.assertEqual(self.manager.launch_notice, "")
                else:
                    self.assertEqual(self.manager.launch_notice, state)
                    self.assertEqual(errors.strip(), dist._LAUNCH_NOTICES[state])

    def test_the_notice_texts_are_the_plain_lines(self):
        self.assertEqual(dist._LAUNCH_NOTICES, {
            "mic": "Odicto is running, but it cannot hear a microphone. Connect one or choose one in "
                   "Odicto Setup. Odicto tries again by itself.",
            "speech": "Odicto is running, but its speech model did not load. Check the internet "
                      "connection. Odicto tries again by itself.",
            "speech-download": "Odicto is running and still downloads its speech model. Dictation works "
                               "when the download ends."})

    def test_speech_download_is_accepted_only_after_the_wait(self):
        child = self.child()
        probe = self.probe("none", "speech-download")
        with patch.object(dist, "DOWNLOAD_ACCEPT_SECONDS", 0.4):
            result, errors, _ = self.start(child, probe)
        self.assertEqual(result, 0)
        downloading = [when for when, _ in probe.calls[1:]]
        self.assertGreaterEqual(len(downloading), 3, "the first polls must not accept a download")
        self.assertGreaterEqual(downloading[-1] - downloading[0], 0.2)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S", b"R"])
        self.assertEqual(errors.strip(), dist._LAUNCH_NOTICES["speech-download"])

    def test_a_download_that_outlasts_the_start_is_not_accepted(self):
        child = self.child()
        with patch.object(dist, "DOWNLOAD_ACCEPT_SECONDS", 100), patch.object(dist, "STARTUP_TIMEOUT", 0.35):
            result, errors, _ = self.start(child, self.probe("none", "speech-download"))
        self.assertEqual(result, 1)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S"])
        child.stdin.close.assert_called_once()
        self.assertEqual(child.wait_calls, 1)
        self.assertNotIn("is running", errors)

    def test_the_default_accept_time_is_below_every_caller_limit(self):
        self.assertEqual(dist.DOWNLOAD_ACCEPT_SECONDS, 45.0)
        self.assertLess(dist.DOWNLOAD_ACCEPT_SECONDS, dist.STARTUP_TIMEOUT)

    def test_a_strict_start_after_an_update_still_fails_and_rolls_back_for_a_waiting_owner(self):
        for state in ("mic", "speech", "speech-download"):
            with self.subTest(state=state):
                self.fixture.add_release("v1.3.0")
                child = self.child()
                with patch.object(dist, "STARTUP_TIMEOUT", 0.3), patch.object(dist, "DOWNLOAD_ACCEPT_SECONDS", 0):
                    result, errors, spawn = self.start(child, self.probe(state), automatic_updates=True)
                self.assertEqual(result, 1)
                spawn.assert_called_once()  # the waiting owner was not mistaken for an owner that runs
                self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S"])
                self.assertEqual(self.manager.active()["directory"], self.old["directory"])
                self.assertEqual(self.manager._read_json(self.manager.marker)["blocked_version"], "v1.3.0")
                self.assertIn("previous version is active again", errors)
                self.assertNotIn("is running", errors)
                self.assertFalse((self.home / "launch-intent.json").exists())
                # Allow the next subtest to check for and stage the same release again.
                marker = self.manager._read_json(self.manager.marker)
                marker.pop("blocked_version", None)
                marker.pop("last_check", None)
                dist.atomic_json(self.manager.marker, marker)
                (self.home / "pending.json").unlink(missing_ok=True)

    def test_a_start_with_no_activation_is_not_strict(self):
        seen = []
        original = self.manager._launch

        def launch(*args, **kwargs):
            seen.append(kwargs.get("strict"))
            return original(*args, **kwargs)
        with patch.object(self.manager, "_launch", side_effect=launch):
            self.start(self.child(), self.probe("none", "mic"))
        self.assertEqual(seen, [False])

    def test_an_update_before_start_makes_the_start_strict(self):
        self.fixture.add_release("v1.3.0")
        seen = []
        with patch.object(self.manager, "_launch", side_effect=lambda *a, **k: seen.append(k["strict"]) or 0):
            self.manager.run(["start"])
        self.assertEqual(seen, [True])

    def test_restart_and_setup_are_never_strict(self):
        seen = []
        with patch.object(self.manager, "_launch", side_effect=lambda *a, **k: seen.append(k["strict"]) or 0):
            self.manager.run(["restart"], automatic_updates=False)
            self.manager.run(["setup"], automatic_updates=False)
        self.assertEqual(seen, [False, False])

    def test_the_precheck_accepts_a_waiting_owner_without_spawning(self):
        for state in ("mic", "speech", "speech-download"):
            with self.subTest(state=state):
                result, errors, spawn = self.start(self.child(), self.probe(state))
                self.assertEqual(result, 0)
                spawn.assert_not_called()
                self.assertEqual(errors.strip(), dist._LAUNCH_NOTICES[state])

    def test_the_precheck_after_an_update_still_launches_for_a_waiting_owner(self):
        self.fixture.add_release("v1.3.0")
        child = self.child(code=3)
        result, _, spawn = self.start(child, self.probe("mic"), automatic_updates=True)
        self.assertEqual(result, 3)
        spawn.assert_called_once()

    def test_a_child_that_exits_or_a_dead_probe_is_still_a_failure(self):
        result, errors, _ = self.start(self.child(code=3), self.probe("none"))
        self.assertEqual(result, 3)
        self.assertIn("exited during startup (code 3)", errors)
        child = self.child()
        with patch.object(dist, "STARTUP_TIMEOUT", 0.3):
            result, _, _ = self.start(child, self.probe("none"))
        self.assertEqual(result, 1)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S"])

    def test_a_probe_that_disagrees_never_reaches_the_launcher_as_a_state(self):
        # Through the real probe: code 20 with no marker is none, so the child is cancelled.
        child = self.child()
        with patch.object(dist, "STARTUP_TIMEOUT", 0.3), \
                patch.object(dist.subprocess, "Popen", return_value=child), \
                patch.object(dist.subprocess, "run", return_value=SimpleNamespace(returncode=20, stdout="")):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 1)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S"])

    def test_a_ready_owner_after_a_waiting_poll_hands_off_without_a_notice(self):
        child = self.child()
        result, errors, _ = self.start(child, self.probe("none", "none", "ready"))
        self.assertEqual(result, 0)
        self.assertEqual(self.manager.launch_notice, "")
        self.assertEqual(errors, "")

    def test_the_notice_is_reset_for_every_run(self):
        self.start(self.child(), self.probe("mic"))
        self.assertEqual(self.manager.launch_notice, "mic")
        result, errors, _ = self.start(self.child(), self.probe("ready"))
        self.assertEqual((result, errors, self.manager.launch_notice), (0, "", ""))


class TestSetupSaveMessage(unittest.TestCase):
    def save(self, probe):
        import setup_web
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher.py"
            launcher.write_text("synthetic stable launcher")
            replies = [SimpleNamespace(returncode=0), probe]

            def run(command, **kwargs):
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            with patch.dict(os.environ, ODICTO_MANAGED_HOME=directory), \
                    patch("platforms.preflight.environment_problems", return_value=[]), \
                    patch.object(setup_web.subprocess, "run", side_effect=run) as execute:
                message = setup_web.restart_odicto()
            return message, execute, launcher

    def test_a_waiting_owner_is_named_in_the_save_message(self):
        message, execute, launcher = self.save(SimpleNamespace(returncode=20))
        self.assertEqual(message, "Settings saved. Odicto is running, but it waits for a microphone "
                                  "or its speech model.")
        self.assertTrue(message.started)
        probe = execute.call_args_list[1]
        self.assertEqual(probe.args[0], [sys.executable, str(launcher), "run", "--no-auto-updates",
                                         "wait-ready", "--timeout", "5", "--report-waiting"])
        self.assertTrue(probe.kwargs["capture_output"])
        self.assertEqual(probe.kwargs["timeout"], 20)

    def test_a_downloading_owner_is_named_in_the_save_message(self):
        message, _, _ = self.save(SimpleNamespace(returncode=21))
        self.assertEqual(message, "Settings saved. Odicto is running and still downloads its speech model.")
        self.assertTrue(message.started)

    def test_only_a_confirmed_owner_is_called_ready(self):
        message, _, _ = self.save(SimpleNamespace(returncode=0))
        self.assertEqual(message, "Settings saved. Odicto is ready with the new settings.")
        self.assertTrue(message.started)

    def test_any_other_answer_leaves_readiness_unconfirmed(self):
        # An owner that ended after the restart answers 1; a look that cannot run answers nothing.
        for probe in (SimpleNamespace(returncode=1), SimpleNamespace(returncode=3),
                      subprocess.TimeoutExpired("wait-ready", 20), OSError("no python")):
            with self.subTest(probe=probe):
                message, _, _ = self.save(probe)
                self.assertEqual(message, "Settings saved. Odicto restarted, but it did not confirm that it "
                                          "is ready. Run odicto status if dictation does not work.")
                self.assertTrue(message.started)


class TestInitStage(_Base):
    """The owner publishes what start-up waits for."""

    def app(self):
        app = app_fixture()
        app.state = AppState.IDLE
        app.ready = False
        app.recorder = app.transcriber = app.refiner = None
        app.pid_file = "unused-test-pid"
        app._init_stage = app._init_reason = None
        for name, value in {"LLM_PROVIDER": "none", "_INIT_RETRY_DELAYS_S": (0.01,)}.items():
            target = main if name.startswith("_") else Config
            self.stack.enter_context(patch.object(target, name, value))
        self.stack.enter_context(patch.object(main, "_INSTANCE_LOCK_HELD", True))
        self.stack.enter_context(patch.object(Config, "effective_stt_provider", return_value="whisper"))
        self.stack.enter_context(patch("main.WhisperTranscriber"))
        self.stack.enter_context(patch("main.TextRefiner"))
        self.stack.enter_context(patch("main.environment_problems", return_value=[]))
        self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.bind = self.stack.enter_context(patch.object(app, "_bind_hotkeys"))
        self.seen = []
        app._notify_ui = lambda: self.seen.append((app.last_status, app._init_reason, app._init_stage))
        self.quiet = self.stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        return app

    def test_the_stage_names_each_step_while_it_runs_and_clears_at_the_end(self):
        app = self.app()
        stages = []
        recorder = MagicMock()

        def mic(**kwargs):
            stages.append(app._init_stage)
            return recorder
        main.WhisperTranscriber.side_effect = lambda *a, **k: stages.append(app._init_stage) or MagicMock()
        main.TextRefiner.side_effect = lambda *a, **k: stages.append(app._init_stage) or MagicMock()
        self.bind.side_effect = lambda *a, **k: stages.append(app._init_stage)
        with patch("main.AudioRecorder", side_effect=mic):
            app.initialize_app()
        self.assertEqual(stages, ["mic", "speech", "ai", "hotkeys"])
        self.assertIsNone(app._init_stage)
        self.assertIsNone(app._init_reason)
        self.assertTrue(app.ready)

    def test_only_the_steps_that_run_are_named(self):
        app = self.app()
        app.recorder = MagicMock()
        app.transcriber = MagicMock()
        stages = []
        main.TextRefiner.side_effect = lambda *a, **k: stages.append(app._init_stage) or MagicMock()
        with patch("main.AudioRecorder", side_effect=AssertionError("the microphone step must not run")):
            self.assertIsNone(app._init_attempt())
        self.assertEqual(stages, ["ai"])
        self.assertIsNone(app._init_stage)

    def test_the_stage_clears_on_every_exit_of_an_attempt(self):
        app = self.app()
        with patch("main.AudioRecorder", side_effect=RuntimeError("no device")):
            self.assertEqual(app._init_attempt(), "mic")
        self.assertIsNone(app._init_stage)
        with patch("main.AudioRecorder", return_value=MagicMock()):
            main.WhisperTranscriber.side_effect = RuntimeError("no model")
            self.assertEqual(app._init_attempt(), "speech")
            self.assertIsNone(app._init_stage)
            main.WhisperTranscriber.side_effect = None
            main.TextRefiner.side_effect = RuntimeError("no ai")
            self.assertEqual(app._init_attempt(), "ai")
            self.assertIsNone(app._init_stage)
            main.TextRefiner.side_effect = None
            self.bind.side_effect = RuntimeError("no hook")
            self.assertEqual(app._init_attempt(), "hotkeys")
            self.assertIsNone(app._init_stage)
            self.bind.side_effect = None
            app._closing.set()
            self.assertEqual(app._init_attempt(), "closing")
            self.assertIsNone(app._init_stage)

    def test_the_retry_loop_stores_the_failed_reason_and_a_success_of_that_step_clears_it(self):
        app = self.app()
        speech_saw = []
        main.WhisperTranscriber.side_effect = lambda *a, **k: speech_saw.append(
            (app._init_stage, app._init_reason)) or MagicMock()
        with patch("main.AudioRecorder", side_effect=[RuntimeError("no device"), MagicMock()]):
            app.initialize_app()
        # While the loop waited to retry, the reason was the failed step and no step ran.
        self.assertIn(("init_error", "mic", None), self.seen)
        # The retried mic step worked, so the old reason no longer labels the speech step.
        self.assertEqual(speech_saw, [("speech", None)])
        self.assertIsNone(app._init_reason)
        self.assertTrue(app.ready)

    def test_a_reason_of_a_later_step_survives_an_earlier_success(self):
        app = self.app()
        main.WhisperTranscriber.side_effect = [RuntimeError("no model"), MagicMock()]
        reasons = []
        main.TextRefiner.side_effect = lambda *a, **k: reasons.append(app._init_reason) or MagicMock()
        with patch("main.AudioRecorder", return_value=MagicMock()):
            app.initialize_app()
        self.assertIn(("init_error", "speech", None), self.seen)
        self.assertEqual(reasons, [None])  # the speech step then worked and cleared its reason
        self.assertIsNone(app._init_reason)

    def test_shutdown_leaves_no_reason(self):
        app = self.app()
        app._closing.set()
        with patch("main.AudioRecorder", return_value=MagicMock()):
            app.initialize_app()
        self.assertIsNone(app._init_reason)
        self.assertIsNone(app._init_stage)

    def test_the_snapshot_says_speech_download_only_while_the_model_downloads(self):
        app = self.app()
        app._init_reason = "mic"
        for stage, pending, expected in (("speech", True, "speech-download"), ("speech", False, "speech"),
                                         ("mic", True, "mic"), (None, True, None), ("ai", True, "ai")):
            with self.subTest(stage=stage, pending=pending):
                app._init_stage = stage
                with patch.object(transcriber, "model_download_pending", return_value=pending):
                    self.assertEqual(app._init_snapshot(), {"stage": expected, "reason": "mic"})

    def test_a_failing_pending_check_never_breaks_the_heartbeat(self):
        app = self.app()
        app._init_stage = "speech"
        with patch.object(transcriber, "model_download_pending", side_effect=RuntimeError("boom")):
            self.assertEqual(app._init_snapshot(), {"stage": "speech", "reason": None})

    def test_an_instance_that_never_ran_init_still_has_a_snapshot(self):
        app = app_fixture()  # built without __init__
        self.assertEqual(app._init_snapshot(), {"stage": None, "reason": None})

    def publish(self, app, microphone):
        class OneRound(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True
        app._closing = OneRound()
        app._runtime_enabled = True
        app.recorder = MagicMock()
        app.recorder.health_snapshot.return_value = microphone
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "DATA_ROOT", directory), \
                patch("main.platforms.lock_is_held", return_value=True):
            app._monitor_runtime()
            with open(os.path.join(directory, "dictation-health.json"), encoding="utf-8") as handle:
                return json.load(handle)

    def test_the_heartbeat_carries_initialized_and_init_beside_the_unchanged_ready_rule(self):
        app = self.app()
        app.ready = True
        app._init_stage, app._init_reason = None, None
        quiet = {"closed": False, "callback_count": 0, "callback_age_s": 0.0}
        health = self.publish(app, quiet)
        self.assertIs(health["initialized"], True)
        self.assertEqual(health["init"], {"stage": None, "reason": None})
        self.assertIs(health["ready"], False)  # initialized, but no audio yet: the old rule
        app = self.app()
        app.ready = True
        audio = {"closed": False, "callback_count": 5, "callback_age_s": 0.1}
        self.assertIs(self.publish(app, audio)["ready"], True)

    def test_the_heartbeat_of_a_booting_owner_names_the_stage(self):
        app = self.app()
        app.ready = False
        app._init_stage, app._init_reason = "speech", "mic"
        with patch.object(transcriber, "model_download_pending", return_value=True):
            health = self.publish(app, {"closed": False, "callback_count": 5, "callback_age_s": 0.1})
        self.assertIs(health["initialized"], False)
        self.assertIs(health["ready"], False)
        self.assertEqual(health["init"], {"stage": "speech-download", "reason": "mic"})


class TestModelDownloadPending(unittest.TestCase):
    """`model_download_pending()`: the real faster-whisper class only, from the cache, no network."""

    def setUp(self):
        class RealModel:
            built = []

            def __init__(self, size, **kwargs):
                RealModel.built.append((size, kwargs, transcriber.model_download_pending()))
                if RealModel.fail:
                    raise RuntimeError("cannot build")
            fail = False
        self.real = RealModel
        self.calls = []

        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = cache.name

        def download_model(size, **kwargs):
            self.calls.append((size, kwargs))
            if self.cached is False:
                raise OSError("not in the cache")
            if self.cached is True:
                Path(self.cache, "model.bin").write_bytes(b"weights")
            return self.cache
        self.cached = False
        utils = type(sys)("faster_whisper.utils")
        utils.download_model = download_model
        package = type(sys)("faster_whisper")
        modules = patch.dict(sys.modules, {"faster_whisper": package, "faster_whisper.utils": utils})
        modules.start()
        self.addCleanup(modules.stop)
        for name, value in (("WhisperModel", RealModel), ("_REAL_WHISPER_MODEL", RealModel),
                            ("_DOWNLOADS_PENDING", 0)):
            patcher = patch.object(transcriber, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("WHISPER_MODEL_SIZE", "tiny.en"), ("WHISPER_DEVICE", "cpu")):
            patcher = patch.object(Config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet = patch("sys.stdout", new_callable=io.StringIO)
        quiet.start()
        self.addCleanup(quiet.stop)

    def build(self):
        return transcriber.WhisperTranscriber()

    def test_nothing_is_pending_before_or_after_a_build(self):
        self.assertFalse(transcriber.model_download_pending())
        self.cached = False
        self.build()
        self.assertFalse(transcriber.model_download_pending())

    def test_the_flag_is_true_only_while_an_uncached_real_model_is_built(self):
        self.cached = False
        self.build()
        self.assertEqual([seen for _, _, seen in self.real.built], [True])

    def test_a_cached_model_is_not_a_download(self):
        self.cached = True
        self.build()
        self.assertEqual([seen for _, _, seen in self.real.built], [False])

    def test_a_folder_without_its_weights_is_a_download(self):
        self.cached = "partial"  # an interrupted download left the folder, not model.bin
        self.build()
        self.assertEqual([seen for _, _, seen in self.real.built], [True])

    def test_the_flag_is_down_again_before_the_gpu_warmup(self):
        seen = []
        with patch.object(Config, "WHISPER_DEVICE", "cuda"), \
                patch.object(transcriber, "wait_for_cuda_driver", return_value=True), \
                patch.object(transcriber.WhisperTranscriber, "_warmup_gpu",
                             lambda _self: seen.append(transcriber.model_download_pending())):
            self.build()
        self.assertEqual([built for _, _, built in self.real.built], [True])  # the constructor saw it
        self.assertEqual(seen, [False])  # a hang in the warmup is not a download

    def test_the_cache_check_makes_no_network_call(self):
        self.cached = False
        self.build()
        self.assertEqual(self.calls, [("tiny.en", {"local_files_only": True})])

    def test_a_failing_build_clears_the_flag(self):
        self.real.fail = True
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertFalse(transcriber.model_download_pending())
        self.assertEqual(transcriber._DOWNLOADS_PENDING, 0)

    def test_a_test_double_never_triggers_the_cache_check(self):
        for double in (MagicMock(), lambda *a, **k: MagicMock()):
            with self.subTest(double=type(double).__name__):
                self.calls.clear()
                with patch.object(transcriber, "WhisperModel", double):
                    self.build()
                self.assertEqual(self.calls, [])
        with patch.object(transcriber, "_REAL_WHISPER_MODEL", None):
            self.assertFalse(transcriber._model_files_missing("tiny.en"))
        self.assertEqual(self.calls, [])

    def test_an_existing_folder_counts_as_local(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(Config, "WHISPER_MODEL_SIZE", folder):
            self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual([seen for _, _, seen in self.real.built], [False])

    def test_two_builds_at_once_keep_the_count(self):
        gates = [threading.Event(), threading.Event()]
        entered = [threading.Event(), threading.Event()]
        order = iter(range(2))
        claim = threading.Lock()

        class Slow(self.real):
            def __init__(self, size, **kwargs):
                with claim:
                    number = next(order)
                entered[number].set()
                gates[number].wait(5)
        with patch.object(transcriber, "WhisperModel", Slow), patch.object(transcriber, "_REAL_WHISPER_MODEL", Slow):
            threads = [threading.Thread(target=self.build) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered[0].wait(5) and entered[1].wait(5))
            self.assertEqual(transcriber._DOWNLOADS_PENDING, 2)
            gates[0].set()
            deadline = time.monotonic() + 5
            while transcriber._DOWNLOADS_PENDING != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            # One build is done; the other still downloads. A plain flag would be down here.
            self.assertEqual(transcriber._DOWNLOADS_PENDING, 1)
            self.assertTrue(transcriber.model_download_pending())
            gates[1].set()
            for thread in threads:
                thread.join(5)
        self.assertFalse(transcriber.model_download_pending())


class TestNothingImportedForTheCheck(unittest.TestCase):
    def test_the_pending_helpers_do_not_import_faster_whisper_or_the_hub(self):
        program = ("import sys, dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
                   f"sys.path.insert(0, {str(APP_DIR)!r})\n"
                   "import transcriber\n"
                   "assert transcriber.model_download_pending() is False\n"
                   "assert transcriber._model_files_missing('tiny.en') is False\n"
                   "print(sorted(m for m in ('faster_whisper', 'huggingface_hub', 'ctranslate2') if m in sys.modules))\n")
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, ODICTO_DATA_ROOT=directory, QT_QPA_PLATFORM="offscreen")
            done = subprocess.run([sys.executable, "-c", program], env=environment, capture_output=True,
                                  text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip().splitlines()[-1], "[]")


if __name__ == "__main__":
    unittest.main()
