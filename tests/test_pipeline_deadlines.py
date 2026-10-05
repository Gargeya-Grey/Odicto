"""Pipeline deadlines, cancel, init retry and shutdown order. Fakes only:
no microphone, network, clipboard or keyboard hooks."""
import io
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import numpy as np

import main
from app_state import AppState
from config import Config
from indicator import GuiState, status_label
from platforms.preflight import Problem
from tests.test_reliability import app_fixture


def _blocking(result, entered=None):
    """A provider call that blocks until released, then returns ``result``."""
    release = threading.Event()

    def run(*args, **kwargs):
        if entered is not None:
            entered.set()
        release.wait(5)
        return result

    return run, release


class _Base(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"PLAY_AUDIO_CUES": False, "POLISH_DICTATION": False,
                            "LOG_TRANSCRIPTS": False, "RETRIGGER_COOLDOWN_MS": 0}.items():
            self.stack.enter_context(patch.object(Config, name, value))
        self.paste = self.stack.enter_context(patch("main.paste_text"))


class TestStageDeadlines(_Base):
    def test_cloud_stt_deadline_uses_local_fallback(self):
        app = app_fixture()
        slow, release = _blocking("late cloud words")
        self.addCleanup(release.set)
        app.transcriber.transcribe.side_effect = slow
        app.transcriber.local_fallback.return_value = "local words"
        with patch.object(Config, "STT_DEADLINE_SECONDS", 0.1), patch.object(
            Config, "effective_stt_provider", return_value="groq"
        ):
            app.process_and_paste(np.zeros(10), False)
        app.transcriber.local_fallback.assert_called_once()
        self.paste.assert_called_once_with("local words")
        self.assertEqual(app.last_status, "stt_fallback")
        self.assertEqual(app.state, AppState.IDLE)

    def test_cloud_and_local_stt_both_time_out_insert_nothing(self):
        app = app_fixture()
        slow, release = _blocking("late cloud words")
        slow_local, release_local = _blocking("late local words")
        self.addCleanup(release.set)
        self.addCleanup(release_local.set)
        app.transcriber.transcribe.side_effect = slow
        app.transcriber.local_fallback.side_effect = slow_local
        started = time.monotonic()
        with patch.object(Config, "STT_DEADLINE_SECONDS", 0.1), patch.object(
            Config, "effective_stt_provider", return_value="gemini"
        ):
            app.process_and_paste(np.zeros(10), False)
        self.assertLess(time.monotonic() - started, 1.5)
        self.paste.assert_not_called()
        self.assertEqual(app.last_status, "stt_timeout")
        self.assertEqual(app.state, AppState.IDLE)
        release.set()
        release_local.set()
        time.sleep(0.1)
        self.paste.assert_not_called()

    def test_whisper_only_timeout_is_an_error_without_fallback(self):
        app = app_fixture()
        slow, release = _blocking("late words")
        self.addCleanup(release.set)
        app.transcriber.transcribe.side_effect = slow
        with patch.object(Config, "STT_DEADLINE_SECONDS", 0.1), patch.object(
            Config, "effective_stt_provider", return_value="whisper"
        ):
            app.process_and_paste(np.zeros(10), False)
        app.transcriber.local_fallback.assert_not_called()
        self.paste.assert_not_called()
        self.assertEqual(app.last_status, "stt_timeout")
        self.assertIn("timed out", status_label(GuiState.ERROR, last_status=app.last_status))

    def _cloud_app(self, post):
        """Real CloudTranscriber with a fake HTTP post and a counting fake Whisper."""
        import transcriber
        whisper = self.stack.enter_context(patch.object(transcriber, "WhisperTranscriber"))
        whisper.return_value.transcribe.return_value = "local words"
        self.stack.enter_context(patch.object(transcriber, "_post_bytes", side_effect=post))
        self.stack.enter_context(patch.object(Config, "effective_stt_provider", return_value="groq"))
        app = app_fixture()
        app.transcriber = transcriber.CloudTranscriber("groq")
        app.transcriber._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
        return app, whisper.return_value.transcribe

    def test_cloud_stage_timeout_runs_exactly_one_whisper_decode(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def post(*args, **kwargs):
            release.wait(5)
            raise RuntimeError("read timed out")  # the abandoned request's own timeout

        app, whisper_decode = self._cloud_app(post)
        with patch.object(Config, "STT_DEADLINE_SECONDS", 0.1):
            app.process_and_paste(np.ones(1600, dtype=np.float32) * 0.1, False)
        self.paste.assert_called_once_with("local words")
        self.assertEqual(app.last_status, "stt_fallback")
        release.set()  # the abandoned cloud call now fails
        time.sleep(0.2)
        self.assertEqual(whisper_decode.call_count, 1)

    def test_cloud_error_runs_the_pipeline_fallback_once(self):
        app, whisper_decode = self._cloud_app(RuntimeError("HTTP 500"))
        app.process_and_paste(np.ones(1600, dtype=np.float32) * 0.1, False)
        self.assertEqual(whisper_decode.call_count, 1)
        self.paste.assert_called_once_with("local words")
        self.assertEqual(app.last_status, "success")

    def test_llm_deadline_inserts_raw_transcript_and_late_reply_never_pastes(self):
        app = app_fixture()
        app.refiner = MagicMock()
        slow, release = _blocking("late AI reply")
        self.addCleanup(release.set)
        app.refiner.refine.side_effect = slow
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 0.1):
            app.process_and_paste(None, True, pre_context="selected", pre_transcript="raw question")
        self.paste.assert_called_once_with("raw question")
        self.assertEqual(app.last_status, "ai_timeout")
        self.assertEqual(app.state, AppState.IDLE)
        self.assertIn("raw", status_label(GuiState.SUCCESS, True, app.last_status))
        release.set()
        time.sleep(0.15)
        self.assertEqual(self.paste.call_args_list, [call("raw question")])


class _SlowChat:
    """Fake chat client: each question blocks until released, then replies or fails."""

    def __init__(self):
        self.plans = {}

    def plan(self, question, reply=None, error=None):
        self.plans[question] = (threading.Event(), threading.Event(), reply, error)
        return self.plans[question]

    def create(self, **kwargs):
        entered, release, reply, error = self.plans[kwargs["messages"][-1]["content"]]
        entered.set()
        release.wait(5)
        if error is not None:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=reply), finish_reason="stop")], usage=None)


class TestAbandonedAiMemory(_Base):
    """A real TextRefiner: an abandoned AI call must never edit conversation memory."""

    def setUp(self):
        super().setUp()
        from tests.test_reliability import refiner_fixture
        self.app = app_fixture()
        self.app.refiner = refiner_fixture()
        self.chat = _SlowChat()
        self.app.refiner.client.chat.completions.create.side_effect = self.chat.create
        self.history = lambda: [(t["role"], t["content"]) for t in self.app.refiner.conversation_history]

    def start(self, question):
        worker = threading.Thread(target=self.app.process_and_paste, args=(None, True),
                                  kwargs={"pre_context": "ctx", "keep_history": True,
                                          "pre_transcript": question})
        worker.start()
        self.assertTrue(self.chat.plans[question][0].wait(1))
        return worker

    def cancel_and_join(self, worker):
        self.app._execute_hotkey_action("cancel", (), None)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app.last_status, "cancelled")

    def test_cancelled_call_failing_late_keeps_the_next_question(self):
        _, release_a, _, _ = self.chat.plan("question A", error=RuntimeError("offline"))
        _, release_b, _, _ = self.chat.plan("question B", reply="answer B")
        self.addCleanup(release_a.set)
        self.addCleanup(release_b.set)
        self.cancel_and_join(self.start("question A"))
        self.assertEqual(self.history(), [])
        worker_b = self.start("question B")
        release_a.set()  # A fails while B waits
        time.sleep(0.1)
        self.assertEqual(self.history(), [("user", "question B")])
        release_b.set()
        worker_b.join(2)
        self.assertEqual(self.history(), [("user", "question B"), ("assistant", "answer B")])
        self.paste.assert_called_once_with("answer B")

    def test_late_success_after_cancel_adds_nothing(self):
        _, release, _, _ = self.chat.plan("question A", reply="late answer")
        self.addCleanup(release.set)
        self.cancel_and_join(self.start("question A"))
        release.set()
        time.sleep(0.15)
        self.assertEqual(self.history(), [])
        self.paste.assert_not_called()

    def test_late_success_after_deadline_adds_nothing(self):
        _, release, _, _ = self.chat.plan("question A", reply="late answer")
        self.addCleanup(release.set)
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 0.1):
            self.app.process_and_paste(None, True, pre_context="ctx", keep_history=True,
                                       pre_transcript="question A")
        self.assertEqual(self.app.last_status, "ai_timeout")
        release.set()
        time.sleep(0.15)
        self.assertEqual(self.history(), [])
        self.paste.assert_called_once_with("question A")

    def test_late_success_after_reset_context_adds_nothing(self):
        entered, release, _, _ = self.chat.plan("question A", reply="late answer")
        self.addCleanup(release.set)
        refiner = self.app.refiner
        worker = threading.Thread(target=refiner.refine, args=("question A",), kwargs={"keep_history": True})
        worker.start()
        self.assertTrue(entered.wait(1))
        refiner.reset_context()  # F5 while the reply is still on its way
        release.set()
        worker.join(2)
        self.assertEqual(self.history(), [])

    def test_normal_f6_call_records_both_turns(self):
        _, release, _, _ = self.chat.plan("question A", reply="answer A")
        release.set()
        self.app.process_and_paste(None, True, pre_context="ctx", keep_history=True,
                                   pre_transcript="question A")
        self.assertEqual(self.history(), [("user", "question A"), ("assistant", "answer A")])
        self.paste.assert_called_once_with("answer A")
        self.assertEqual(self.app.last_status, "success")


class TestCancel(_Base):
    def _start_blocked_pipeline(self, app):
        entered = threading.Event()
        slow, release = _blocking("late words", entered)
        self.addCleanup(release.set)
        app.transcriber.transcribe.side_effect = slow
        worker = threading.Thread(target=app.process_and_paste, args=(np.zeros(10), False))
        worker.start()
        self.assertTrue(entered.wait(1))
        return worker, release

    def test_cancel_during_processing_inserts_nothing_and_returns_to_idle(self):
        app = app_fixture()
        worker, release = self._start_blocked_pipeline(app)
        app._execute_hotkey_action("cancel", (), None)
        worker.join(2)
        self.assertFalse(worker.is_alive(), "cancel must not wait for the provider")
        self.assertEqual(app.last_status, "cancelled")
        self.assertEqual(app.state, AppState.IDLE)
        self.assertEqual(status_label(GuiState.ERROR, last_status="cancelled"), "Cancelled")
        release.set()  # the abandoned stage finishes late
        time.sleep(0.15)
        self.paste.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)

    def test_cancel_after_capture_stop_before_worker_starts(self):
        app = app_fixture()
        app.state = AppState.RECORDING
        app._record_started_at = 0.0
        app._keep_history = False
        app.recorder.stop.return_value = True
        app.recorder.last_audio_array = np.zeros(10)
        with patch("main.threading.Thread") as thread:
            app.on_release()
        app._cancel_processing()
        kwargs = thread.call_args.kwargs
        kwargs["target"](*kwargs["args"])
        app.transcriber.transcribe.assert_not_called()
        self.paste.assert_not_called()
        self.assertEqual(app.last_status, "cancelled")

    def _bind(self, app, cancel_key="esc"):
        hooks = {}
        with patch.object(Config, "HOTKEY", "ctrl+grave"), patch.object(Config, "AI_HOTKEY", "ctrl+shift+grave"), \
                patch.object(Config, "RESET_CONTEXT_HOTKEY", ""), patch.object(Config, "LIVE_HOTKEY", "f7"), \
                patch.object(Config, "CANCEL_HOTKEY", cancel_key), patch("main.ensure_can_bind_hotkeys"), \
                patch("main.platforms.hook_key",
                      side_effect=lambda key, fn, suppress: hooks.update({key: (fn, suppress)})):
            app._hotkey_physically_held = False
            app._live_key_held = False
            app._bind_hotkeys()
        return hooks

    def test_cancel_key_is_never_suppressed_and_ignored_outside_processing(self):
        app = app_fixture()
        hooks = self._bind(app)
        handler, suppress = hooks["esc"]
        self.assertFalse(suppress)
        down = SimpleNamespace(event_type=main.platforms.KEY_DOWN)
        for state in (AppState.IDLE, AppState.RECORDING):
            with self.subTest(state=state):
                app.state = state
                app._cycle = main._Cycle(1)
                self.assertTrue(handler(down), "Esc must reach the focused app")
                self.assertFalse(app._cycle.cancel.is_set())
                self.assertEqual(app.state, state)
        app.state = AppState.PROCESSING
        self.assertTrue(handler(down))
        self.assertTrue(app._cycle.cancel.is_set())

    def test_blank_cancel_key_binds_nothing(self):
        hooks = self._bind(app_fixture(), cancel_key="")
        self.assertNotIn("esc", hooks)
        self.assertEqual(set(hooks), {"grave", "f7"})


class TestLogging(_Base):
    def run_ai_cycle(self, app):
        app.refiner = MagicMock()
        app.refiner.refine.return_value = "SECRET REPLY"
        app.transcriber.transcribe.return_value = "SECRET QUESTION"
        with patch.object(app, "_capture_selection", return_value=("SECRET CONTEXT", None)), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out:
            app.process_and_paste(np.zeros(10), True)
        return out.getvalue()

    def test_transcripts_stay_out_of_the_log_by_default(self):
        app = app_fixture()
        output = self.run_ai_cycle(app)
        for secret in ("SECRET REPLY", "SECRET QUESTION", "SECRET CONTEXT"):
            self.assertNotIn(secret, output)
        self.assertIn("15 chars", output)  # lengths and timings remain
        self.paste.assert_called_once_with("SECRET REPLY")

    def test_log_transcripts_opt_in_keeps_text(self):
        app = app_fixture()
        with patch.object(Config, "LOG_TRANSCRIPTS", True):
            output = self.run_ai_cycle(app)
        self.assertIn("SECRET QUESTION", output)
        self.assertIn("SECRET REPLY", output)


class TestCaptureStart(_Base):
    def press(self, app, use_llm):
        app.state = AppState.IDLE
        app._pressed_mods_at_press = ()
        app.refiner = MagicMock()
        app.refiner.refine.return_value = "reply"
        app.recorder.stop.return_value = True
        app.recorder.last_audio_array = np.zeros(10)
        app.on_press(use_llm=use_llm)
        self.assertEqual(app.state, AppState.RECORDING)

    def release_and_run(self, app):
        app._record_started_at = 0.0
        with patch("main.threading.Thread") as thread:
            app.on_release()
        kwargs = thread.call_args.kwargs
        kwargs["target"](*kwargs["args"])

    def test_toggle_mode_ai_probes_at_capture_start_and_reuses_result(self):
        app = app_fixture()
        app._runtime_enabled = True
        probed = threading.Event()
        threads = []

        def probe():
            threads.append(threading.current_thread().name)
            probed.set()
            return "selected text", None

        with patch.object(Config, "HOTKEY_TOGGLE", True), patch.object(app, "_capture_selection", side_effect=probe):
            self.press(app, use_llm=True)
            self.assertTrue(probed.wait(1), "probe must start with the capture")
            self.release_and_run(app)
        self.assertEqual(threads, ["odicto-sel-early"])
        app.refiner.refine.assert_called_once_with(
            "hello world", context="selected text", image_bytes=None, keep_history=False)
        self.paste.assert_called_once_with("reply")

    def test_hold_mode_probes_at_release(self):
        app = app_fixture()
        app._runtime_enabled = True
        threads = []
        probe = lambda: (threads.append(threading.current_thread().name), ("", None))[1]  # noqa: E731
        with patch.object(Config, "HOTKEY_TOGGLE", False), patch.object(app, "_capture_selection", side_effect=probe):
            self.press(app, use_llm=True)
            time.sleep(0.05)
            self.assertEqual(threads, [])
            self.release_and_run(app)
        self.assertEqual(threads, ["odicto-sel"])

    def test_capture_start_prewarms_providers_without_blocking(self):
        app = app_fixture()
        app.refiner = MagicMock()
        app.transcriber.prewarm.side_effect = RuntimeError("must be swallowed")
        self.press(app, use_llm=True)
        app.transcriber.prewarm.assert_called_once()
        app.refiner.prewarm.assert_called_once()
        app.state = AppState.IDLE
        app.refiner.prewarm.reset_mock()
        self.press(app, use_llm=False)
        app.refiner.prewarm.assert_not_called()


class TestRecordingLimit(_Base):
    def test_limit_enqueues_stop_on_the_ordered_worker(self):
        app = app_fixture()
        app._runtime_enabled = True
        app.pid_file = "unused-test-pid"
        app.state = AppState.RECORDING
        app._capture_seq = 4
        stopped = threading.Event()
        names = []
        app.on_release = lambda: (names.append(threading.current_thread().name), stopped.set())
        self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.stack.enter_context(patch("main.platforms.lock_is_held", return_value=False))
        app._start_hotkey_worker()
        self.addCleanup(app._shutdown)
        recorder_thread = threading.Thread(target=app._on_recording_limit, name="odicto-rec-limit")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            recorder_thread.start()
            recorder_thread.join(1)
            self.assertTrue(stopped.wait(1))
        self.assertEqual(names, ["odicto-hotkey-actions"])
        self.assertIn("Recording limit reached", out.getvalue())

    def test_limit_for_an_old_capture_or_idle_does_nothing(self):
        app = app_fixture()
        app.on_release = MagicMock()
        app.on_live_toggle = MagicMock()
        app.state = AppState.RECORDING
        app._capture_seq = 5
        app._execute_hotkey_action("limit", 4, None)
        app.state = AppState.IDLE
        app._execute_hotkey_action("limit", 5, None)
        app.on_release.assert_not_called()
        app.state = AppState.RECORDING
        app.live_active = True
        app._execute_hotkey_action("limit", 5, None)
        app.on_live_toggle.assert_called_once()
        app.on_release.assert_not_called()


class TestInitRetry(_Base):
    def app(self):
        app = app_fixture()
        app.state = AppState.IDLE
        app.ready = False
        app.recorder = app.transcriber = app.refiner = None
        app.pid_file = "unused-test-pid"
        for name, value in {"LLM_PROVIDER": "none", "_INIT_RETRY_DELAYS_S": (0.01,)}.items():
            target = main if name.startswith("_") else Config
            self.stack.enter_context(patch.object(target, name, value))
        self.stack.enter_context(patch.object(main, "_INSTANCE_LOCK_HELD", True))
        self.stack.enter_context(patch.object(Config, "effective_stt_provider", return_value="whisper"))
        self.stack.enter_context(patch("main.WhisperTranscriber"))
        self.stack.enter_context(patch("main.TextRefiner"))
        self.problems = self.stack.enter_context(patch("main.environment_problems", return_value=[]))
        self.unhook = self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.bind = self.stack.enter_context(patch.object(app, "_bind_hotkeys"))
        self.seen = []
        app._notify_ui = lambda: self.seen.append((app.last_status, app.status_detail))
        return app

    def test_mic_failure_retries_then_binds_hooks_once(self):
        app = self.app()
        recorder = MagicMock()
        with patch("main.AudioRecorder", side_effect=[RuntimeError("no device"), recorder]) as factory, \
                patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            app.initialize_app()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args.kwargs["max_seconds"], Config.MAX_RECORDING_SECONDS)
        recorder.set_limit_callback.assert_called_once_with(app._on_recording_limit)
        self.bind.assert_called_once()
        self.assertTrue(app.ready)
        self.assertIn(("init_error", "mic"), self.seen)
        self.assertIsNone(app.last_status)
        self.assertEqual(status_label(GuiState.ERROR, last_status="init_error", detail="mic"), "Mic unavailable")

    def test_partial_hook_bind_is_removed_before_retry(self):
        app = self.app()
        self.bind.side_effect = [RuntimeError("hook failed"), None]
        with patch("main.AudioRecorder"), patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            app.initialize_app()
        self.assertEqual(self.bind.call_count, 2)
        self.unhook.assert_called_once()
        self.assertTrue(app.ready)

    def test_retry_stops_on_shutdown_and_never_binds(self):
        app = self.app()
        attempts = threading.Semaphore(0)

        def fail(**kwargs):
            attempts.release()
            raise RuntimeError("no device")

        with patch("main.AudioRecorder", side_effect=fail), patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            worker = threading.Thread(target=app.initialize_app)
            worker.start()
            for _ in range(3):
                self.assertTrue(attempts.acquire(timeout=1))
            app._closing.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.bind.assert_not_called()
        self.assertFalse(app.ready)
        self.assertEqual(app.last_status, "init_error")

    def test_preflight_and_config_warnings_print_once_and_name_the_error(self):
        app = self.app()
        self.problems.return_value = [
            Problem("warning", "wayland_session", "Wayland is limited."),
            Problem("error", "macos_accessibility", "Grant Accessibility."),
        ]
        with patch("main.AudioRecorder", side_effect=[RuntimeError("no device"), MagicMock()]), \
                patch.object(Config, "CONFIG_WARNINGS", ["HOTKEY was invalid."]), \
                patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO):
            app.initialize_app()
        text = out.getvalue()
        for line in ("Wayland is limited.", "Grant Accessibility.", "HOTKEY was invalid."):
            self.assertEqual(text.count(line), 1, line)
        self.assertIn(("init_error", "macos_accessibility"), self.seen)
        self.assertEqual(status_label(GuiState.ERROR, last_status="init_error", detail="macos_accessibility"),
                         "Allow Accessibility")


class TestShutdownOrder(_Base):
    def test_unhooks_before_lock_finishes_when_lock_held_and_flushes_clipboard(self):
        app = app_fixture()
        app.pid_file = "unused-test-pid"
        app._SHUTDOWN_LOCK_TIMEOUT_S = 0.2
        order = []
        unhook = self.stack.enter_context(patch("main.platforms.unhook_all", side_effect=lambda: order.append(
            ("unhook", app._closing.is_set()))))
        self.stack.enter_context(patch("main.flush_pending_restore", side_effect=lambda: order.append("flush")))
        self.assertTrue(app._lifecycle_lock.acquire())  # an insertion is "typing"
        try:
            started = time.monotonic()
            worker = threading.Thread(target=app._shutdown)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Quit must not wait on a running insertion")
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            app._lifecycle_lock.release()
        self.assertEqual(order[0], ("unhook", True), "hooks go first, after _closing is set")
        self.assertIn("flush", order)
        self.assertGreaterEqual(unhook.call_count, 1)
        self.assertFalse(app.ready)
        app.recorder.close.assert_called_once()

    def test_pipeline_does_not_start_insertion_after_closing(self):
        app = app_fixture()
        app.pid_file = "unused-test-pid"
        self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.stack.enter_context(patch("main.flush_pending_restore"))
        app.transcriber.transcribe.side_effect = lambda audio, **kw: (app._shutdown(), "words")[1]
        app.process_and_paste(np.zeros(10), False)
        self.paste.assert_not_called()


class TestHudNotices(unittest.TestCase):
    def test_new_notice_texts(self):
        self.assertEqual(status_label(GuiState.SUCCESS, last_status="mic_gap"), "Mic gap · check text")
        self.assertEqual(status_label(GuiState.SUCCESS, last_status="ai_timeout"), "AI timed out · raw text")
        self.assertEqual(status_label(GuiState.SUCCESS, last_status="stt_fallback"), "Cloud slow · local speech")
        self.assertEqual(status_label(GuiState.ERROR, last_status="init_error"), "Not ready · see log")
        self.assertEqual(status_label(GuiState.ERROR, last_status="error"), "Failed")
        self.assertEqual(status_label(GuiState.ERROR, last_status="empty"), "No speech")


if __name__ == "__main__":
    unittest.main()
