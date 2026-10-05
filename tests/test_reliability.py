"""Regression tests for input ownership and optional polish. No hooks or network."""
import base64
import ctypes
import sys
import os
import tempfile
import json
import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from app_state import AppState
from config import Config, ENV_DEFAULTS
from main import DictationApp
from recorder import AudioRecorder
from refiner import TextRefiner, _GeminiClient, _MetaClient
from indicator import GuiState, status_label


def app_fixture():
    app = DictationApp.__new__(DictationApp)
    app.state = AppState.PROCESSING
    app.state_lock = threading.Lock()
    app._lifecycle_lock = threading.Lock()
    app._closing = threading.Event()
    app._runtime_enabled = False
    app.ready = True
    app.recorder = MagicMock()
    app.transcriber = MagicMock()
    app.transcriber.transcribe.return_value = "hello world"
    app.refiner = None
    app.indicator = None
    app._live_epoch = 3
    app._live_session = None
    app.live_active = False
    app.live_preview = ""
    app._live_committed = ""
    app._speech_backends = {}
    app._last_cycle_end = 0.0
    app._record_started_at = 0.0
    app.audio_filepath = "unused-test-audio.wav"
    app.last_status = None
    app.use_llm = False
    app._cycle = None
    app._cycle_seq = 0
    app._capture_seq = 0
    app._early_probe = None
    app.status_detail = None
    app._preflight_error = None
    app._hooks_bound = False
    app._cleanup_temp_file = MagicMock()
    return app


def refiner_fixture(provider="groq"):
    with patch.object(Config, "LLM_PROVIDER", "none"):
        result = TextRefiner()
    result.provider = provider
    result.model = "openai/gpt-oss-20b"
    result.client = MagicMock()
    result.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Hello world."), finish_reason="stop")])
    return result


class TestReliability(unittest.TestCase):
    def test_capture_start_failure_is_visible_while_idle(self):
        from PySide6.QtWidgets import QApplication
        from indicator import DictationIndicator
        QApplication.instance() or QApplication([])
        app = app_fixture()
        app.state = AppState.IDLE
        hud = DictationIndicator(app)
        try:
            app.last_status = "error"
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.ERROR)
        finally:
            hud._tick.stop()
            hud.close()

    def test_digital_silence_never_reaches_whisper_decoder(self):
        with patch("recorder.sd.InputStream"), patch("main.threading.Thread") as worker:
            recorder = AudioRecorder()
            try:
                recorder.start()
                recorder._callback(np.zeros((1024, 1), dtype=np.float32), 1024, None, None)
                app = app_fixture()
                app.recorder = recorder
                app.state = AppState.RECORDING
                app._record_started_at = time.monotonic() - 5
                with patch.object(Config, "PLAY_AUDIO_CUES", False):
                    app.on_release()
                self.assertEqual(app.state, AppState.IDLE)
                self.assertEqual(app.last_status, "empty")
                worker.assert_not_called()
            finally:
                recorder.close()

    def test_live_capture_failure_discards_partial_text_and_reports_error(self):
        app = app_fixture()
        app.state = AppState.RECORDING
        app.live_active = True
        app._record_started_at = time.monotonic() - 5
        app.recorder.stop.side_effect = RuntimeError("Microphone stopped during recording")
        session = app._live_session = MagicMock()
        session.stop.return_value = "incomplete speech"
        app.live_preview = "incomplete speech"
        app.process_and_paste = MagicMock()
        with patch.object(Config, "PLAY_AUDIO_CUES", False), patch("main.threading.Thread") as worker:
            app.on_live_toggle()
            call = worker.call_args.kwargs
            call["target"](*call["args"])
        session.stop.assert_called_once()
        app.process_and_paste.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)
        self.assertEqual(app.last_status, "error")

    def test_callback_loss_during_capture_keeps_audio_and_flags_gap(self):
        # Owner rule (reversed from the old "reject after a gap"): the words
        # captured before the gap are kept, processed, and the HUD says "mic_gap".
        with patch("recorder.sd.InputStream"), patch("main.threading.Thread") as worker:
            recorder = AudioRecorder()
            try:
                recorder.start()
                recorder._callback(np.ones((1024, 1), dtype=np.float32), 1024, None, None)
                recorder._last_callback -= 4.0
                app = app_fixture()
                app.recorder = recorder
                app.state = AppState.RECORDING
                app._record_started_at = time.monotonic() - 5
                app._keep_history = False
                with patch.object(Config, "PLAY_AUDIO_CUES", False):
                    app.on_release()
                self.assertEqual(app.state, AppState.PROCESSING)
                self.assertFalse(recorder.recording)
                self.assertTrue(recorder.last_capture_gap)
                worker.assert_called_once()
                call = worker.call_args.kwargs
                self.assertIsNotNone(call["args"][0])
                with patch("main.paste_text") as paste, patch.object(Config, "POLISH_DICTATION", False):
                    call["target"](*call["args"])
                paste.assert_called_once_with("hello world")
                app.transcriber.transcribe.assert_called_once()
                self.assertEqual(app.last_status, "mic_gap")
                self.assertEqual(app.state, AppState.IDLE)
            finally:
                recorder.close()

    def test_dead_stream_without_audio_ends_as_error_without_transcription(self):
        with patch("recorder.sd.InputStream"), patch("main.threading.Thread") as worker:
            recorder = AudioRecorder()
            try:
                recorder.start()
                recorder._last_callback -= 4.0
                app = app_fixture()
                app.recorder = recorder
                app.state = AppState.RECORDING
                app._record_started_at = time.monotonic() - 5
                app._keep_history = False
                with patch.object(Config, "PLAY_AUDIO_CUES", False):
                    app.on_release()
                self.assertEqual(app.state, AppState.IDLE)
                self.assertEqual(app.last_status, "error")
                self.assertIsNone(recorder.last_audio_array)
                self.assertFalse(recorder.recording)
                worker.assert_not_called()
                app.transcriber.transcribe.assert_not_called()
            finally:
                recorder.close()

    def test_microphone_lifetime_has_no_background_resets(self):
        with patch("recorder.sd.InputStream") as opened, patch("recorder.threading.Thread") as worker:
            recorder = AudioRecorder()
            try:
                worker.assert_not_called()
                for _ in range(10):
                    recorder.health_snapshot()
                opened.return_value.active = False
                recorder.health_snapshot()
                self.assertEqual(opened.call_count, 1)
                opened.return_value.abort.assert_not_called()
                opened.return_value.close.assert_not_called()
            finally:
                recorder.close()

    def test_stalled_microphone_requires_fresh_callbacks_after_reconnect(self):
        with patch("recorder.sd.InputStream") as opened, patch("recorder.threading.Thread"):
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4.0
                with patch.object(recorder._callback_ready, "wait", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "unavailable"):
                        recorder.start()
                self.assertFalse(recorder.recording)
                self.assertEqual(opened.call_count, 2)
            finally:
                recorder.close()

    def test_restart_is_cancelled_when_windows_refuses_stop(self):
        import odicto
        with patch.object(odicto.platforms, "kill_other_odicto_processes", side_effect=PermissionError("access denied")), patch.object(
            odicto.platforms, "release_lock"
        ) as release:
            self.assertEqual(odicto.cmd_stop(None), 1)
        release.assert_not_called()
        if sys.platform == "win32":
            import platforms.windows as backend
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "dictation.pid")
                with open(path, "w") as f:
                    f.write("987654")
                with patch.object(backend, "_enumerate_odicto_pids", return_value={987654}), patch.object(
                    backend.subprocess, "run", return_value=SimpleNamespace(returncode=5)
                ), patch("psutil.pid_exists", return_value=True):
                    with self.assertRaises(PermissionError):
                        backend.kill_other_odicto_processes(path)
                self.assertTrue(os.path.exists(path), "failed stop must preserve runtime ownership metadata")

    def test_delayed_callback_does_not_abort_active_dictation(self):
        with patch("recorder.sd.InputStream") as opened, patch("recorder.threading.Thread"):
            recorder = AudioRecorder()
            try:
                recorder.start()
                recorder._last_callback -= 4.0
                recorder.health_snapshot()
                self.assertEqual(opened.call_count, 1, "a delayed callback must not restart a live recording")
                opened.return_value.abort.assert_not_called()
            finally:
                recorder.close()


    def test_input_health_measures_real_signal_without_changing_recording(self):
        stream = MagicMock()
        stream.device = 7
        with patch("recorder.sd.InputStream", return_value=stream), patch("recorder.threading.Thread"), patch(
            "recorder.sd.query_devices", return_value={"name": "Test microphone", "hostapi": 0}
        ), patch("recorder.sd.query_hostapis", return_value={"name": "Test API"}):
            recorder = AudioRecorder()
            try:
                self.assertEqual(recorder.health_snapshot()["callback_count"], 0)
                recorder.start()
                chunk = np.full((1024, 1), 0.125, dtype=np.float32)
                recorder._callback(chunk, 1024, None, None)
                snapshot = recorder.health_snapshot()
                self.assertEqual(snapshot["callback_count"], 1)
                self.assertEqual(snapshot["device"]["name"], "Test microphone")
                self.assertEqual(snapshot["input_peak"], 0.125)
                self.assertEqual(snapshot["input_rms"], 0.125)
                self.assertTrue(recorder.stop())
                np.testing.assert_array_equal(recorder.last_audio_array, chunk[:, 0])
                self.assertEqual(recorder.health_snapshot()["last_capture"]["rms"], 0.125)
                recorder._callback(np.zeros_like(chunk), 1024, None, None)
                self.assertEqual(recorder.health_snapshot()["input_rms"], 0.0)
                self.assertEqual(recorder.health_snapshot()["last_capture"]["rms"], 0.125)
                self.assertFalse(recorder.health_snapshot()["closed"])
            finally:
                recorder.close()

    def test_nonowner_cannot_overwrite_runtime_health(self):
        app = app_fixture()
        app._runtime_enabled = True
        with patch("main.platforms.lock_is_held", return_value=False), patch("builtins.open") as write:
            app._monitor_runtime()
        write.assert_not_called()
        app._runtime_enabled = False
        with patch("main.platforms.lock_is_held", return_value=True), patch("builtins.open") as write:
            app._monitor_runtime()
        write.assert_not_called()


    def test_duplicate_start_does_not_kill_running_application(self):
        import main
        with patch.object(main, "acquire_single_instance_lock", return_value=False), patch.object(
            main.platforms, "kill_other_odicto_processes"
        ) as kill:
            self.assertFalse(main.claim_install("unused.pid"))
        kill.assert_not_called()

    def test_orphan_sweep_only_runs_after_exclusive_ownership(self):
        import main
        calls = []
        with patch.object(main, "acquire_single_instance_lock", side_effect=lambda: calls.append("lock") or True), patch.object(
            main.platforms, "kill_other_odicto_processes", side_effect=lambda path: calls.append("sweep")
        ):
            self.assertTrue(main.claim_install("unused.pid"))
        self.assertEqual(calls, ["lock", "sweep"])

    def test_stop_failure_returns_to_idle_without_insertion(self):
        app = app_fixture()
        app.state = AppState.RECORDING
        app.recorder.stop.side_effect = RuntimeError("device disconnected")
        with patch.object(Config, "PLAY_AUDIO_CUES", False), patch("main.threading.Thread") as worker:
            app.on_release()
        self.assertEqual(app.state, AppState.IDLE)
        self.assertEqual(app.last_status, "error")
        worker.assert_not_called()


    def test_audio_callback_never_prints_status_to_disk(self):
        with patch("recorder.sd.InputStream"), patch("recorder.threading.Thread"):
            recorder = AudioRecorder()
            try:
                with patch("builtins.print") as output:
                    recorder._callback(np.zeros((1024, 1), dtype=np.float32), 1024, None, "input overflow")
                    output.assert_not_called()
                    recorder.health_snapshot()
                    output.assert_called_once()
            finally:
                recorder.close()

    def test_long_capture_copy_does_not_block_audio_callback(self):
        with patch("recorder.sd.InputStream"), patch("recorder.threading.Thread"):
            recorder = AudioRecorder()
            try:
                recorder.start()
                recorder._callback(np.zeros((1024, 1), dtype=np.float32), 1024, None, None)
                concatenate = np.concatenate
                def unlocked_copy(*args, **kwargs):
                    self.assertTrue(recorder._lock.acquire(blocking=False))
                    recorder._lock.release()
                    return concatenate(*args, **kwargs)
                with patch("recorder.np.concatenate", side_effect=unlocked_copy):
                    self.assertFalse(recorder.stop())
            finally:
                recorder.close()


    def test_failed_endpoint_close_prevents_second_device_open(self):
        endpoint = MagicMock()
        endpoint.start.side_effect = RuntimeError("start failed")
        def close(ignore_errors=True):
            if not ignore_errors:
                raise RuntimeError("driver busy")
        endpoint.close.side_effect = close
        with patch("recorder.sd.InputStream", return_value=endpoint) as opened:
            with self.assertRaisesRegex(RuntimeError, "driver busy"):
                AudioRecorder()
        self.assertEqual(opened.call_count, 1)

    def test_shutdown_during_device_open_never_starts_endpoint(self):
        with patch("recorder.sd.InputStream"), patch("recorder.threading.Thread"):
            recorder = AudioRecorder()
            recorder.close()
            recorder._closed.clear()
            endpoint = MagicMock()
            def opening(**kwargs):
                recorder._closed.set()
                return endpoint
            with patch("recorder.sd.InputStream", side_effect=opening):
                recorder._open_persistent_stream(delays=(0.0,))
            endpoint.start.assert_not_called()
            endpoint.close.assert_called_once()
            self.assertIsNone(recorder._stream)

    def test_recycled_pid_file_never_kills_unverified_process(self):
        if sys.platform == "win32":
            import platforms.windows as backend
            enumerator = "_enumerate_odicto_pids"
        else:
            import platforms._posix as backend
            enumerator = "enumerate_odicto_pids"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "dictation.pid")
            with open(path, "w") as f:
                f.write("987654")
            with patch.object(backend, enumerator, return_value=set()), patch.object(backend.subprocess, "run") as execute:
                self.assertEqual(backend.kill_other_odicto_processes(path), [])
            execute.assert_not_called()

    def test_status_rejects_stale_heartbeat_without_touching_microphone(self):
        import odicto
        with tempfile.TemporaryDirectory() as directory, patch.object(odicto, "_repo_root", return_value=directory), patch.object(
            odicto.platforms, "lock_is_held", return_value=False
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            with open(os.path.join(directory, "dictation.pid"), "w") as f:
                f.write("42")
            with open(os.path.join(directory, "dictation-health.json"), "w") as f:
                json.dump({"pid": 42, "updated_at": time.time() - 60}, f)
            self.assertEqual(odicto.cmd_status(None), 0)
            self.assertIn("stale or unavailable", output.getvalue())

    def test_agent_commands_mentioning_main_are_not_application_processes(self):
        from platforms import base
        root = base.install_root()
        script = os.path.join(root, "main.py")
        self.assertTrue(base.is_odicto_command([sys.executable, script]))
        self.assertTrue(base.is_odicto_command([sys.executable, "-B", "main.py"], root))
        self.assertFalse(base.is_odicto_command([sys.executable, "-c", f"print({script!r})"], root))
        self.assertFalse(base.is_odicto_command([sys.executable, "tools/test.py", script], root))
        self.assertFalse(base.is_odicto_command([sys.executable, script + ".backup"]))
        self.assertFalse(base.is_odicto_command(["bash", script]))

    def test_finished_f7_preview_does_not_reappear_when_normal_capture_stops(self):
        from PySide6.QtWidgets import QApplication
        from indicator import DictationIndicator
        qt = QApplication.instance() or QApplication([])
        for use_llm in (False, True):
            with self.subTest(use_llm=use_llm):
                app = app_fixture()
                app.live_preview = app._live_committed = "previous F7 transcript"
                app._pressed_mods_at_press = set()
                session = MagicMock()
                session.stop.return_value = "Previous F7 transcript."
                with patch("main.paste_text"):
                    app._finish_live_session(session, np.zeros(10), app._live_epoch)
                indicator = DictationIndicator(app)
                app.indicator = indicator
                try:
                    with patch.object(Config, "PLAY_AUDIO_CUES", False), patch.object(
                        Config, "RETRIGGER_COOLDOWN_MS", 0
                    ), patch("main.threading.Thread"):
                        app.on_press(use_llm=use_llm)
                        app._record_started_at = 0.0
                        app.on_release()
                    self.assertEqual(app.state, AppState.PROCESSING)
                    indicator._sync_from_app()
                    self.assertFalse(indicator._is_live_layout(), "ordinary capture must not show previous F7 captions")
                    self.assertEqual(indicator._pill_h, indicator._compact_h)
                    self.assertEqual(app.live_preview, "")
                    self.assertEqual(app._live_committed, "")
                finally:
                    indicator._tick.stop()
                    if indicator._tray is not None:
                        indicator._tray.hide()
                    indicator.close()

    def test_live_final_inserted_once_and_never_restores_old_clipboard(self):
        app = app_fixture()
        app.live_preview = "hello wor"
        session = MagicMock()
        session.stop.return_value = "Hello world."
        with patch("main.paste_text") as paste:
            app._finish_live_session(session, np.zeros(10), 3)
        paste.assert_called_once_with("Hello world.", restore_clipboard=False)
        app.transcriber.transcribe.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)

    def test_preview_survives_finalization_but_clears_on_every_pipeline_exit(self):
        from indicator import DictationIndicator
        for outcome in ("success", "empty", "error"):
            with self.subTest(outcome=outcome):
                app = app_fixture()
                app.live_preview = app._live_committed = "current F7 preview"
                hud = SimpleNamespace(app=app, gui_state=GuiState.PROCESSING)
                self.assertTrue(DictationIndicator._is_live_layout(hud))
                def paste(*args, **kwargs):
                    self.assertEqual(app.live_preview, "current F7 preview")
                    self.assertEqual(app.state, AppState.PROCESSING)
                with patch.object(app, "_live_transcribe", return_value="words" if outcome == "success" else "",
                                  side_effect=RuntimeError("STT failed") if outcome == "error" else None), patch(
                    "main.paste_text", side_effect=paste
                ), patch.object(Config, "POLISH_DICTATION", False):
                    app.process_and_paste(np.zeros(10), False, live=True)
                self.assertEqual(app.last_status, outcome)
                self.assertEqual(app.state, AppState.IDLE)
                self.assertEqual(app.live_preview, "")
                self.assertEqual(app._live_committed, "")
                self.assertFalse(DictationIndicator._is_live_layout(hud))

    def test_live_stop_blocks_both_hotkeys_until_insertion_finishes(self):
        app = app_fixture()
        app.state = AppState.RECORDING
        app.live_active = True
        session = app._live_session = MagicMock()
        app.recorder.last_audio_array = np.zeros(10)
        app.recorder.stop.return_value = True
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch("main.threading.Thread") as thread:
            app.on_live_toggle()
            self.assertEqual(app.state, AppState.PROCESSING)
            app.on_press(use_llm=True)
            app.on_live_toggle()
        thread.assert_called_once()
        session.stop.assert_not_called()
        app.recorder.start.assert_not_called()

    def test_late_callbacks_cannot_touch_new_session(self):
        app = app_fixture()
        app.state = AppState.RECORDING
        app.live_active = True
        app.live_preview = "new words"
        with patch("main.paste_text") as paste:
            app._on_live_interim("old words", 2)
            app._on_live_final("old final", 2)
        self.assertEqual(app.live_preview, "new words")
        self.assertEqual(app._live_committed, "")
        paste.assert_not_called()

    def test_interim_only_changes_preview(self):
        app = app_fixture()
        app.live_active = True
        with patch("main.paste_text") as paste:
            app._on_live_final("first sentence", 3)
            app._on_live_interim("second draft", 3)
        self.assertEqual(app.live_preview, "first sentence second draft")
        paste.assert_not_called()

    def test_explicit_live_provider_is_used_and_cached(self):
        app = app_fixture()
        with patch.object(Config, "effective_stt_provider", return_value="whisper"), patch.object(
            Config, "effective_live_stt_provider", return_value="groq"
        ), patch("main.CloudTranscriber") as factory:
            factory.return_value.transcribe.return_value = "cloud words"
            self.assertEqual(app._live_transcribe("audio"), "cloud words")
            app._live_transcribe("more audio")
        factory.assert_called_once_with("groq")
        app.transcriber.transcribe.assert_not_called()

    def test_raw_polish_is_optional_and_precedes_insertion(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                app = app_fixture()
                app.refiner = refiner_fixture()
                with patch.object(Config, "POLISH_DICTATION", enabled), patch.object(
                    Config, "effective_stt_provider", return_value="whisper"
                ), patch("main.paste_text") as paste:
                    app.process_and_paste(np.zeros(10), False)
                paste.assert_called_once_with("Hello world." if enabled else "hello world")
                self.assertEqual(app.refiner.conversation_history, [])

    def test_gemini_smart_skips_duplicate_polish(self):
        app = app_fixture()
        app.refiner = MagicMock()
        with patch.object(Config, "POLISH_DICTATION", True), patch.object(Config, "GEMINI_TRANSCRIBE_MODE", "smart"), patch.object(
            Config, "effective_live_stt_provider", return_value="gemini"
        ), patch("main.paste_text"):
            app.process_and_paste(None, False, pre_transcript="Already smart.", live=True)
        app.refiner.polish.assert_not_called()

    def test_ai_failure_is_visible_after_raw_insertion(self):
        app = app_fixture()
        app.refiner = refiner_fixture()
        app.refiner.client.chat.completions.create.side_effect = RuntimeError("offline")
        with patch("main.paste_text") as paste:
            app.process_and_paste(None, True, pre_context="context", pre_transcript="question")
        paste.assert_called_once_with("question")
        self.assertEqual(app.last_status, "ai_fallback")
        self.assertIn("raw", status_label(GuiState.SUCCESS, True, app.last_status))

    def test_insertion_error_is_not_success(self):
        app = app_fixture()
        with patch("main.paste_text", side_effect=RuntimeError("blocked")):
            app.process_and_paste(None, False, pre_transcript="words")
        self.assertEqual(app.last_status, "error")

    def test_polish_uses_model_override_and_preserves_ai_memory(self):
        refiner = refiner_fixture()
        history = [{"role": "user", "content": "private earlier question"}]
        refiner.conversation_history = list(history)
        with patch.object(Config, "POLISH_MODEL", "custom/model"):
            self.assertEqual(refiner.polish("reset chat"), "Hello world.")
        kwargs = refiner.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "custom/model")
        self.assertEqual(kwargs["messages"][1]["content"], "reset chat")
        self.assertNotIn("private earlier question", str(kwargs))
        self.assertEqual(refiner.conversation_history, history)

    def test_polish_deadline_and_no_overlapping_worker_or_late_overwrite(self):
        refiner = refiner_fixture()
        release = threading.Event()
        refiner.client.chat.completions.create.side_effect = lambda **kw: (release.wait(5), SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="late answer"), finish_reason="stop")]))[1]
        started = time.monotonic()
        try:
            self.assertEqual(refiner.polish("raw words"), "raw words")
            self.assertLess(time.monotonic() - started, 2.6)
            self.assertEqual(refiner.last_notice, "polish_fallback")
            self.assertEqual(refiner.polish("next capture"), "next capture")
            refiner.client.chat.completions.create.assert_called_once()
        finally:
            release.set()
        deadline = time.monotonic() + 1
        while refiner._polish_lock.locked() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(refiner.last_notice, "polish_fallback")
        self.assertEqual(refiner.conversation_history, [])

    def test_empty_truncated_and_failed_polish_all_keep_raw(self):
        for response in ("", "length", "exception"):
            with self.subTest(response=response):
                refiner = refiner_fixture()
                if response == "exception":
                    refiner.client.chat.completions.create.side_effect = RuntimeError("offline")
                else:
                    choice = refiner.client.chat.completions.create.return_value.choices[0]
                    choice.message.content = "" if not response else "incomplete"
                    choice.finish_reason = response or "stop"
                self.assertEqual(refiner.polish("keep raw"), "keep raw")
                self.assertEqual(refiner.last_notice, "polish_fallback")

    def test_groq_resolves_shared_key_and_nonlocal_endpoint(self):
        with patch.object(Config, "LLM_PROVIDER", "groq"), patch.object(Config, "GROQ_API_KEY", "test-key"), patch.object(
            Config, "GROQ_MODEL", "openai/gpt-oss-120b"
        ):
            self.assertEqual(Config.effective_llm_model(), "openai/gpt-oss-120b")
            self.assertEqual(Config.effective_api_key(), "test-key")
            self.assertEqual(Config.effective_llm_api_base(), ENV_DEFAULTS["GROQ_API_BASE"])

    def test_bound_audio_listener_removed_and_ring_is_five_seconds_stereo(self):
        with patch.object(AudioRecorder, "_open_persistent_stream"):
            recorder = AudioRecorder(sample_rate=16000, channels=2)
        recorder._stream = MagicMock(active=True)
        class Consumer:
            def push(self, audio):
                pass
        consumer = Consumer()
        recorder.add_chunk_listener(consumer.push)
        recorder.remove_chunk_listener(consumer.push)
        self.assertEqual(recorder._chunk_listeners, [])
        for _ in range(100):
            recorder._callback(np.ones((1024, 2), dtype=np.float32), 1024, None, None)
        self.assertEqual(sum(len(chunk) for chunk in recorder._ring), 80000)
        recorder.start()
        self.assertEqual(sum(len(chunk) for chunk in recorder.audio_data), 6400)
        self.assertTrue(all(chunk.ndim == 1 for chunk in recorder.audio_data))

    def test_cloud_preload_does_not_make_paid_generation_calls(self):
        for provider in ("groq", "gemini", "openrouter", "meta", "none"):
            refiner = refiner_fixture(provider)
            with patch("refiner.threading.Thread") as thread:
                refiner.preload()
            thread.assert_not_called()

    def test_groq_initialization_and_chat_use_saved_settings(self):
        with patch.object(Config, "LLM_PROVIDER", "groq"), patch.object(Config, "GROQ_API_KEY", "test-key"), patch(
            "refiner.OpenAI"
        ) as factory:
            refiner = TextRefiner()
            factory.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="An answer."), finish_reason="stop")])
            self.assertEqual(refiner.refine("a question"), "An answer.")
        self.assertEqual(factory.call_args.kwargs["base_url"], ENV_DEFAULTS["GROQ_API_BASE"])
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.return_value.chat.completions.create.call_args.kwargs["reasoning_effort"], "low")

    def test_gemini_polish_override_does_not_advance_server_memory(self):
        refiner = refiner_fixture("gemini")
        client = _GeminiClient.__new__(_GeminiClient)
        client.model = "ai/model"
        client._last_interaction_id = "earlier-conversation"
        client.client = MagicMock()
        client.client.interactions.create.return_value.output_text = "Hello world."
        refiner.client = client
        with patch.object(Config, "POLISH_MODEL", "polish/model"):
            self.assertEqual(refiner.polish("hello world"), "Hello world.")
        kwargs = client.client.interactions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "polish/model")
        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertNotIn("previous_interaction_id", kwargs)
        self.assertEqual(client._last_interaction_id, "earlier-conversation")

    def test_meta_polish_override_reaches_request_without_changing_ai_model(self):
        client = _MetaClient.__new__(_MetaClient)
        client.model = "ai/model"
        client.base_url = "https://example.invalid/v1"
        with patch.object(client, "_url", return_value="https://example.invalid/v1/responses"), patch.object(
            client, "_post_payload", return_value={"output_text": "Hello world."}
        ) as post:
            self.assertEqual(client.create_responses([], model="polish/model", timeout=(1, 2)), "Hello world.")
        self.assertEqual(post.call_args.args[1]["model"], "polish/model")
        self.assertEqual(client.model, "ai/model")

    def test_gemini_image_payload_validates_against_installed_sdk(self):
        from google.genai._gaos.types.interactions.createmodelinteraction import CreateModelInteraction
        refiner = refiner_fixture("gemini")
        client = _GeminiClient.__new__(_GeminiClient)
        client.model = "gemini-3.5-flash-lite"
        client._last_interaction_id = None
        client.client = MagicMock()
        client.client.interactions.create.return_value.output_text = "An answer."
        refiner.client = client
        self.assertEqual(refiner.refine("describe", image_bytes=b"png bytes"), "An answer.")
        kwargs = client.client.interactions.create.call_args.kwargs
        CreateModelInteraction.model_validate(kwargs)
        self.assertEqual(base64.b64decode(kwargs["input"][0]["data"]), b"png bytes")

    def test_setup_renders_polish_groq_and_image_controls_without_duplicate_ids(self):
        from html.parser import HTMLParser
        from tests.test_equivalence import _render_page
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__(); self.ids = []
            def handle_starttag(self, tag, attrs):
                if "id" in dict(attrs):
                    self.ids.append(dict(attrs)["id"])
        page = _render_page({"env": {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "fake-test-key",
            "GROQ_MODEL": "openai/gpt-oss-120b", "POLISH_DICTATION": "true", "POLISH_MODEL": "polish/model"}})
        parser = Elements(); parser.feed(page)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for key in ("GROQ_MODEL", "GROQ_API_KEY", "POLISH_DICTATION", "POLISH_MODEL", "AI_CLIPBOARD_IMAGE"):
            self.assertIn(key, parser.ids)
        self.assertNotIn("fake-test-key", page)
        self.assertNotIn("__POLISH_", page)
        self.assertIn('value="true" selected', page)
        self.assertIn('value="openai/gpt-oss-120b"', page)

    def test_non_16khz_capture_rejected_instead_of_changing_whisper_speed(self):
        with patch.object(Config, "LLM_PROVIDER", "none"), patch.object(Config, "SAMPLE_RATE", 44100):
            with self.assertRaisesRegex(ValueError, "16000"):
                Config.validate()

    def test_clipboard_image_request_runs_on_gui_thread(self):
        from PySide6.QtWidgets import QApplication
        from indicator import DictationIndicator
        qt = QApplication.instance() or QApplication([])
        indicator = DictationIndicator(app_fixture())
        returned = []
        read_threads = []
        def image():
            read_threads.append(threading.get_ident())
            return b"mock png"
        worker = threading.Thread(target=lambda: returned.append(indicator.capture_clipboard_image()))
        try:
            with patch("typer.get_clipboard_image", side_effect=image):
                worker.start()
                deadline = time.monotonic() + 1
                while worker.is_alive() and time.monotonic() < deadline:
                    qt.processEvents()
                    time.sleep(.005)
                worker.join(1)
            self.assertEqual(returned, [b"mock png"])
            self.assertEqual(read_threads, [threading.get_ident()])
        finally:
            indicator._tick.stop()
            if indicator._tray is not None:
                indicator._tray.hide()
            indicator.close()

    @unittest.skipUnless(sys.platform == 'win32', 'Win32 ABI')
    def test_win32_input_union_size_and_partial_input_never_retried(self):
        import platforms._keyboard as backend
        self.assertEqual(ctypes.sizeof(backend._win_input_type()), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        with patch.object(ctypes.windll.user32, "SendInput", return_value=1), patch.object(backend, "_require_keyboard") as keyboard:
            with self.assertRaises(RuntimeError):
                backend.send_text_bulk("emoji 😀")
            keyboard.assert_not_called()
        with patch.object(ctypes.windll.user32, "SendInput", side_effect=[256, OSError("interrupted")]), patch.object(
            backend, "_require_keyboard"
        ) as keyboard:
            with self.assertRaises(RuntimeError):
                backend.send_text_bulk("x" * 200)
            keyboard.assert_not_called()


if __name__ == '__main__':
    unittest.main()
