import sys
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from unittest import skipUnless
import numpy as np

# 1. Mock faster_whisper to avoid downloading/loading the Whisper model on test import
mock_faster_whisper = MagicMock()
sys.modules["faster_whisper"] = mock_faster_whisper

# Now we can safely import config, recorder, transcriber, refiner, typer, main
import config
from config import Config, parse_hold_hotkey, _sanitize_model_id
from recorder import AudioRecorder, play_beep
from transcriber import GeminiTranscriber, WhisperTranscriber, float32_to_wav_bytes
from refiner import TextRefiner, reset_openrouter_effort_cache
from typer import paste_text, get_selected_text
from app_state import AppState
from main import (
    DictationApp,
    is_pressed_exclusive,
    side_exclusive_scan_codes,
    _mutex_name_for_install,
)
import main as main_mod


class TestOdicto(unittest.TestCase):

    def setUp(self) -> None:
        # Reset state/config to defaults where necessary
        Config.LLM_PROVIDER = "ollama"
        Config.LLM_MODEL = "qwen2.5:1.5b-instruct"
        # Do not inherit the operator's STT_PROVIDER (often gemini) — tests mock
        # WhisperTranscriber and would otherwise construct a real Gemini client.
        self._real_stt = Config.STT_PROVIDER
        Config.STT_PROVIDER = "whisper"
        # CRITICAL: never run the real single-instance lock or orphan killer in tests.
        # initialize_app() acquires it when not held — that would taskkill a live
        # Odicto and take the Global mutex for the duration of the test run.
        self._real_lock_held = main_mod._INSTANCE_LOCK_HELD
        main_mod._INSTANCE_LOCK_HELD = True  # bypass lock acquisition + orphan kill in initialize_app

        # Terminal detection reads the real foreground window — under the test
        # runner that is a console, which would flip paste_text onto the typing
        # path. Force it off; terminal-specific tests patch it themselves.
        self._terminal_patch = patch("typer.foreground_is_terminal", return_value=False)
        self._terminal_patch.start()
        self.addCleanup(self._terminal_patch.stop)
        reset_openrouter_effort_cache()
        self._catalog_patch = patch(
            "openrouter_catalog.fetch_openrouter_catalog",
            return_value={"ok": True, "models": [], "reasoning": {}},
        )
        self._catalog_patch.start()
        self.addCleanup(self._catalog_patch.stop)

    def tearDown(self) -> None:
        Config.STT_PROVIDER = self._real_stt
        main_mod._INSTANCE_LOCK_HELD = self._real_lock_held

    @skipUnless(sys.platform == "win32", "keyboard scan codes are Windows-specific")
    def test_side_exclusive_scan_codes_right_ctrl(self) -> None:
        """right ctrl must not share the left-ctrl-only scan code used by is_pressed bugs."""
        import keyboard as kb

        left = set(kb.key_to_scan_codes("left ctrl"))
        right_exclusive = set(side_exclusive_scan_codes("right ctrl"))
        self.assertTrue(right_exclusive, "expected exclusive right-ctrl codes")
        self.assertFalse(
            right_exclusive & left,
            "exclusive right ctrl must not include left ctrl scan codes",
        )

    @skipUnless(sys.platform == "win32", "Windows scan-code disambiguation")
    @patch("platforms._keyboard.is_pressed")
    def test_is_pressed_exclusive_right_ctrl(
        self, mock_is_pressed: MagicMock
    ) -> None:
        """Left ctrl alone must not count as right ctrl for AI mode."""
        exclusive = side_exclusive_scan_codes("right ctrl")
        left_only = 29  # standard left-ctrl scan code on Windows

        def side_effect(key):
            # Simulate only left ctrl held (code 29 down; exclusive right codes up).
            if key == left_only:
                return True
            if isinstance(key, int) and key in exclusive:
                return False
            if key in ("ctrl", "left ctrl"):
                return True
            if key in ("right ctrl", "right control"):
                # Naïve library behavior — our helper must not rely on this path alone.
                return True
            return False

        mock_is_pressed.side_effect = side_effect
        self.assertFalse(is_pressed_exclusive("right ctrl"))

        def right_down(key):
            if isinstance(key, int) and key in exclusive:
                return True
            return False

        mock_is_pressed.side_effect = right_down
        self.assertTrue(is_pressed_exclusive("right ctrl"))

    def test_sanitize_model_id_strips_accidental_hash_tail(self) -> None:
        """Mid-value #old-model must not become part of the OpenRouter id."""
        self.assertEqual(
            _sanitize_model_id("minimax/minimax-m2.7#nvidia/old:free"),
            "minimax/minimax-m2.7",
        )
        self.assertEqual(_sanitize_model_id("google/gemini-2.0-flash-001"), "google/gemini-2.0-flash-001")

    def test_parse_hold_hotkey(self) -> None:
        """Hold chords split into modifiers + primary key."""
        self.assertEqual(parse_hold_hotkey("alt+x"), (("alt",), "x"))
        self.assertEqual(parse_hold_hotkey("ctrl+space"), (("ctrl",), "space"))
        self.assertEqual(
            parse_hold_hotkey("ctrl+shift+space"), (("ctrl", "shift"), "space")
        )
        self.assertEqual(parse_hold_hotkey("scroll lock"), ((), "scroll lock"))
        self.assertEqual(parse_hold_hotkey("  ALT + X  "), (("alt",), "x"))
        # ` is aliased to "grave" for the keyboard library
        self.assertEqual(parse_hold_hotkey("ctrl+grave"), (("ctrl",), "grave"))
        self.assertEqual(
            parse_hold_hotkey("ctrl+shift+grave"), (("ctrl", "shift"), "grave")
        )
        self.assertEqual(parse_hold_hotkey("ctrl+`"), (("ctrl",), "grave"))

    def test_match_active_chord_prefers_ai_when_shift_held(self) -> None:
        """Ctrl+Shift+grave is AI; Ctrl+grave alone is raw dictation."""
        app = DictationApp.__new__(DictationApp)
        app._hotkey_modifiers = ("ctrl",)
        app._ai_hotkey_modifiers = ("ctrl", "shift")
        # The chord matcher now reads the press-time modifier snapshot.
        app._pressed_mods_at_press = ("ctrl", "shift")
        self.assertTrue(app._match_active_chord())
        app._pressed_mods_at_press = ("ctrl",)
        self.assertFalse(app._match_active_chord())
        app._pressed_mods_at_press = ()
        self.assertIsNone(app._match_active_chord())

    def test_mutex_name_is_install_scoped(self) -> None:
        """Single-instance mutex must be stable and namespaced per install path."""
        name = _mutex_name_for_install()
        self.assertTrue(
            name.startswith("Global\\Odicto_SingleInstance_")
            or name.startswith("Local\\Odicto_SingleInstance_"),
            name,
        )
        self.assertEqual(name, _mutex_name_for_install())

    def test_bind_hotkeys_refuses_without_single_instance_lock(self) -> None:
        """STRICT: never install system-wide hooks unless the mutex is held."""
        import main as main_mod

        app = DictationApp.__new__(DictationApp)
        app._hotkey_physically_held = False
        was_held = main_mod._INSTANCE_LOCK_HELD
        try:
            main_mod._INSTANCE_LOCK_HELD = False
            with self.assertRaises(RuntimeError):
                app._bind_hotkeys()
        finally:
            main_mod._INSTANCE_LOCK_HELD = was_held

    @patch("config.Config.LLM_PROVIDER", "invalid")
    def test_config_validation_invalid_provider(self) -> None:
        """Verifies that Config.validate raises ValueError for invalid providers."""
        with self.assertRaises(ValueError):
            Config.validate()

    @patch("recorder.sd.InputStream")
    @patch("recorder.sf.write")
    def test_audio_recorder_lifecycle(
        self, mock_sf_write: MagicMock, mock_input_stream: MagicMock
    ) -> None:
        """Verifies the persistent stream lifecycle: open once, capture, stop, save."""
        mock_input_stream.return_value.start.return_value = None
        recorder = AudioRecorder(sample_rate=16000, channels=1)
        self.assertFalse(recorder.recording)
        # Stream is opened and started exactly once at construction.
        mock_input_stream.assert_called_once()
        mock_input_stream.return_value.start.assert_called_once()

        # Start recording (no re-open; just flips the capture flag).
        recorder.start()
        self.assertTrue(recorder.recording)
        mock_input_stream.assert_called_once()

        # Simulate audio buffer stream inputs via callback
        chunk1 = np.array([[0.1], [0.2]], dtype=np.float32)
        chunk2 = np.array([[0.3], [0.4]], dtype=np.float32)
        recorder._callback(chunk1, len(chunk1), None, None)
        recorder._callback(chunk2, len(chunk2), None, None)

        # Stop recording with optional debug WAV path
        test_filepath = "dummy_test.wav"
        success = recorder.stop(test_filepath)

        self.assertTrue(success)
        self.assertFalse(recorder.recording)
        # Persistent stream is NOT closed on stop (only on close()).
        mock_input_stream.return_value.stop.assert_not_called()
        mock_input_stream.return_value.close.assert_not_called()
        self.assertIsNotNone(recorder.last_audio_array)

        # Verify sf.write was called with correct concatenated data
        mock_sf_write.assert_called_once()
        args, kwargs = mock_sf_write.call_args
        self.assertEqual(args[0], test_filepath)
        # Session chunks are flattened to 1D mono.
        expected_data = np.concatenate([chunk1, chunk2], axis=0).reshape(-1)
        np.testing.assert_array_equal(args[1], expected_data)
        self.assertEqual(args[2], 16000)

        recorder.close()
        mock_input_stream.return_value.stop.assert_called_once()
        mock_input_stream.return_value.close.assert_called_once()

    @patch("recorder.sd.InputStream")
    @patch("recorder.sf.write")
    def test_audio_recorder_in_memory_only(
        self, mock_sf_write: MagicMock, mock_input_stream: MagicMock
    ) -> None:
        """Hot path should not touch disk when filepath is omitted."""
        recorder = AudioRecorder(sample_rate=16000, channels=1)
        recorder.start()
        chunk = np.array([[0.1], [0.2]], dtype=np.float32)
        recorder._callback(chunk, len(chunk), None, None)
        self.assertGreater(recorder.get_level(), 0.0)
        success = recorder.stop(filepath=None)
        self.assertTrue(success)
        mock_sf_write.assert_not_called()
        self.assertIsNotNone(recorder.last_audio_array)
        recorder.close()

    @patch("recorder.sd.InputStream")
    @patch("recorder.sf.write")
    def test_audio_recorder_mixdown_stereo_to_mono(
        self, mock_sf_write: MagicMock, mock_input_stream: MagicMock
    ) -> None:
        """CHANNELS=2 captures must be mixed to mono, not squeezed into a 2D array."""
        recorder = AudioRecorder(sample_rate=16000, channels=2)
        recorder.start()
        chunk = np.array([[0.1, 0.3], [0.2, 0.4]], dtype=np.float32)
        recorder._callback(chunk, len(chunk), None, None)
        success = recorder.stop(filepath=None)
        self.assertTrue(success)
        self.assertEqual(recorder.last_audio_array.ndim, 1)
        # mean([0.1,0.3])=0.2 ; mean([0.2,0.4])=0.3
        np.testing.assert_allclose(
            recorder.last_audio_array, np.array([0.2, 0.3], dtype=np.float32)
        )
        recorder.close()

    @patch("recorder.sd.InputStream")
    @patch("recorder.sf.write")
    def test_audio_recorder_pre_roll_seeds_session(
        self, mock_sf_write: MagicMock, mock_input_stream: MagicMock
    ) -> None:
        """Audio captured before start() (ring buffer) seeds the session, so the
        first spoken syllable is not clipped by stream start latency."""
        recorder = AudioRecorder(sample_rate=16000, channels=1)
        pre = np.array([[0.5], [0.6]], dtype=np.float32)
        recorder._callback(pre, len(pre), None, None)
        recorder.start()
        live = np.array([[0.7], [0.8]], dtype=np.float32)
        recorder._callback(live, len(live), None, None)
        success = recorder.stop(filepath=None)
        self.assertTrue(success)
        # Pre-roll + live capture, in order.
        np.testing.assert_allclose(
            recorder.last_audio_array,
            np.array([0.5, 0.6, 0.7, 0.8], dtype=np.float32),
        )
        recorder.close()

    @patch("transcriber.Config.WHISPER_DEVICE", "auto")
    @patch("transcriber.Config.WHISPER_MODEL_SIZE", "small.en")
    @patch("transcriber.WhisperModel")
    def test_whisper_transcriber_loading_fallback(
        self, mock_whisper_model: MagicMock
    ) -> None:
        """Larger models under auto fall back to CPU if CUDA fails."""
        mock_whisper_model.side_effect = [
            Exception("CUDA initialization failed"),
            MagicMock(),
        ]

        transcriber = WhisperTranscriber()
        self.assertEqual(transcriber.device, "cpu")
        self.assertEqual(transcriber.compute_type, "int8")

    @patch("transcriber.WhisperModel")
    @patch("transcriber.os.path.exists", return_value=True)
    def test_whisper_transcriber_transcribe(
        self, mock_exists: MagicMock, mock_whisper_model: MagicMock
    ) -> None:
        """Verifies transcription returns the segments' text joined together."""
        mock_segment1 = MagicMock()
        mock_segment1.text = "Hello"
        mock_segment2 = MagicMock()
        mock_segment2.text = " world"

        mock_model_instance = mock_whisper_model.return_value
        mock_model_instance.transcribe.return_value = (
            [mock_segment1, mock_segment2],
            MagicMock(),
        )

        transcriber = WhisperTranscriber()
        with patch("transcriber.Config.WHISPER_VAD", False):
            result = transcriber.transcribe("fake_audio.wav")
        self.assertEqual(result, "Hello world")

        # Ensure speed flags are applied
        kwargs = mock_model_instance.transcribe.call_args[1]
        self.assertTrue(kwargs.get("without_timestamps"))
        self.assertEqual(kwargs.get("beam_size"), 1)
        self.assertFalse(kwargs.get("vad_filter"))

    @patch("transcriber.WhisperModel")
    def test_whisper_transcribe_numpy(self, mock_whisper_model: MagicMock) -> None:
        """In-memory float32 arrays should be accepted without a file path."""
        mock_segment = MagicMock()
        mock_segment.text = "from memory"
        mock_model_instance = mock_whisper_model.return_value
        mock_model_instance.transcribe.return_value = ([mock_segment], MagicMock())

        transcriber = WhisperTranscriber()
        audio = np.zeros(1600, dtype=np.float32)
        with patch("transcriber.Config.WHISPER_VAD", False):
            result = transcriber.transcribe(audio)
        self.assertEqual(result, "from memory")
        kwargs = mock_model_instance.transcribe.call_args[1]
        self.assertFalse(kwargs.get("vad_filter"))

    @patch("transcriber.WhisperModel")
    def test_whisper_vad_on_long_clips(self, mock_whisper_model: MagicMock) -> None:
        """Clips >= 8s turn VAD back on so long silence is trimmed."""
        mock_segment = MagicMock()
        mock_segment.text = "long"
        mock_model_instance = mock_whisper_model.return_value
        mock_model_instance.transcribe.return_value = ([mock_segment], MagicMock())

        transcriber = WhisperTranscriber()
        audio = np.zeros(16000 * 9, dtype=np.float32)
        with patch("transcriber.Config.WHISPER_VAD", False), patch(
            "transcriber.Config.SAMPLE_RATE", 16000
        ):
            transcriber.transcribe(audio)
        kwargs = mock_model_instance.transcribe.call_args[1]
        self.assertTrue(kwargs.get("vad_filter"))

    @patch("recorder.sd.InputStream")
    def test_audio_recorder_retries_when_device_not_ready(
        self, mock_input_stream: MagicMock
    ) -> None:
        """Login-time WASAPI misses must retry instead of killing init."""
        good = MagicMock()
        mock_input_stream.side_effect = [OSError("Device unavailable"), good]
        with patch("recorder.time.sleep"):
            recorder = AudioRecorder(sample_rate=16000, channels=1)
        self.assertIs(recorder._stream, good)
        self.assertEqual(mock_input_stream.call_count, 2)
        recorder.close()

    @patch("recorder.sd.InputStream")
    def test_audio_recorder_status_callback_does_not_raise(
        self, mock_input_stream: MagicMock
    ) -> None:
        """PortAudio status in the callback must not throw (pythonw crash dialog)."""
        recorder = AudioRecorder(sample_rate=16000, channels=1)
        chunk = np.array([[0.1], [0.2]], dtype=np.float32)
        recorder._callback(chunk, len(chunk), None, "input overflow")
        recorder._callback(chunk, len(chunk), None, "input overflow")
        recorder.close()

    @patch("transcriber.Config.WHISPER_DEVICE", "auto")
    @patch("transcriber.Config.WHISPER_MODEL_SIZE", "tiny.en")
    @patch("transcriber.wait_for_cuda_driver")
    def test_whisper_auto_tiny_stays_on_cpu(
        self, mock_wait: MagicMock
    ) -> None:
        """tiny/base under auto skip CUDA so login does not pay a ~1GB GPU context."""
        factory = MagicMock(name="WhisperModel")
        with patch("transcriber.WhisperModel", factory):
            transcriber = WhisperTranscriber()
        self.assertEqual(transcriber.device, "cpu")
        self.assertEqual(transcriber.compute_type, "int8")
        self.assertEqual(factory.call_args.kwargs["device"], "cpu")
        mock_wait.assert_not_called()

    @patch("transcriber.Config.WHISPER_DEVICE", "auto")
    @patch("transcriber.Config.WHISPER_MODEL_SIZE", "base.en")
    @patch("transcriber.wait_for_cuda_driver")
    def test_whisper_auto_base_stays_on_cpu(
        self, mock_wait: MagicMock
    ) -> None:
        factory = MagicMock(name="WhisperModel")
        with patch("transcriber.WhisperModel", factory):
            transcriber = WhisperTranscriber()
        self.assertEqual(transcriber.device, "cpu")
        mock_wait.assert_not_called()

    @patch("transcriber.Config.WHISPER_DEVICE", "cuda")
    @patch("transcriber.Config.WHISPER_MODEL_SIZE", "tiny.en")
    @patch("transcriber.wait_for_cuda_driver")
    def test_whisper_explicit_cuda_still_uses_gpu(
        self, mock_wait: MagicMock
    ) -> None:
        factory = MagicMock(name="WhisperModel")
        with patch("transcriber.WhisperModel", factory):
            transcriber = WhisperTranscriber()
        self.assertEqual(transcriber.device, "cuda")
        mock_wait.assert_called()

    @patch("transcriber.Config.WHISPER_DEVICE", "auto")
    @patch("transcriber.Config.WHISPER_MODEL_SIZE", "small.en")
    @patch("transcriber.time.sleep")
    @patch("transcriber._probe_cuda_subprocess")
    def test_whisper_waits_for_cuda_then_loads_gpu(
        self,
        mock_probe: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Larger models under auto still wait for the GPU driver, then load CUDA."""
        mock_probe.side_effect = [False, False, True]
        factory = MagicMock(name="WhisperModel")
        factory.__module__ = "faster_whisper"
        with patch("transcriber.WhisperModel", factory), patch(
            "transcriber.CUDA_WAIT_SECONDS", 30.0
        ):
            transcriber = WhisperTranscriber()
        self.assertEqual(transcriber.device, "cuda")
        self.assertEqual(factory.call_args.kwargs["device"], "cuda")
        self.assertGreaterEqual(mock_probe.call_count, 3)
        mock_sleep.assert_called()

    def test_cuda_probe_skipped_for_injected_model(self) -> None:
        import transcriber as transcriber_mod

        with patch.object(transcriber_mod, "_probe_cuda_subprocess") as probe:
            self.assertTrue(transcriber_mod.wait_for_cuda_driver())
            probe.assert_not_called()

    def test_stdout_is_discarded_for_nul(self) -> None:
        """Setup restarts pythonw with stdout pointed at NUL, not None."""
        fake = MagicMock()
        fake.name = "nul"
        with patch.object(sys, "stdout", fake):
            self.assertTrue(main_mod._stdout_is_discarded())
        fake.name = "stdout"
        with patch.object(sys, "stdout", fake):
            self.assertFalse(main_mod._stdout_is_discarded())
        with patch.object(sys, "stdout", None):
            self.assertTrue(main_mod._stdout_is_discarded())

    def test_timestamp_writer_prefixes_lines(self) -> None:
        import io

        buf = io.StringIO()
        writer = main_mod._TimestampWriter(buf)
        writer.write("hello\nmore")
        text = buf.getvalue()
        self.assertIn(" hello\n", text)
        self.assertTrue(text[0].isdigit())
        self.assertTrue(text.endswith("more"))

    def test_trim_log_keeps_tail(self) -> None:
        import tempfile

        handle, path = tempfile.mkstemp()
        os.close(handle)
        try:
            with open(path, "wb") as f:
                f.write(b"OLD-HEAD\n" + (b"n" * 80) + b"\nKEEP-ME\n")
            with patch.object(main_mod, "_LOG_MAX_BYTES", 40), patch.object(
                main_mod, "_LOG_KEEP_BYTES", 24
            ):
                main_mod._trim_log(path)
            with open(path, "rb") as f:
                data = f.read()
            self.assertIn(b"KEEP-ME", data)
            self.assertNotIn(b"OLD-HEAD", data)
        finally:
            os.remove(path)

    def test_float32_to_wav_bytes_header(self) -> None:
        audio = np.zeros(1600, dtype=np.float32)
        wav = float32_to_wav_bytes(audio, 16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])
        self.assertGreater(len(wav), 44)

    def test_stt_provider_and_mode_helpers(self) -> None:
        with patch.object(Config, "GEMINI_TRANSCRIBE_MODE", "verbatim"):
            self.assertEqual(Config.gemini_transcribe_mode(), "verbatim")
        with patch.object(Config, "GEMINI_TRANSCRIBE_MODE", "smart"):
            self.assertEqual(Config.gemini_transcribe_mode(), "smart")
        with patch.object(Config, "STT_PROVIDER", "auto"), patch.object(
            Config, "GEMINI_API_KEY", "AIza-test"
        ):
            self.assertEqual(Config.effective_stt_provider(), "gemini")
        with patch.object(Config, "STT_PROVIDER", "auto"), patch.object(
            Config, "GEMINI_API_KEY", ""
        ):
            self.assertEqual(Config.effective_stt_provider(), "whisper")
        with patch.object(Config, "STT_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_API_KEY", ""
        ):
            self.assertEqual(Config.effective_stt_provider(), "whisper")
        with patch.object(Config, "LIVE_STT_PROVIDER", "whisper"), patch.object(
            Config, "GEMINI_API_KEY", "AIza-test"
        ):
            self.assertEqual(Config.effective_live_stt_provider(), "whisper")
        with patch.object(Config, "LIVE_STT_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_API_KEY", "AIza-test"
        ):
            self.assertEqual(Config.effective_live_stt_provider(), "gemini")
        with patch.object(Config, "LIVE_STT_PROVIDER", "auto"), patch.object(
            Config, "GEMINI_API_KEY", "AIza-test"
        ):
            self.assertEqual(Config.effective_live_stt_provider(), "gemini")
        with patch.object(Config, "LIVE_STT_PROVIDER", "auto"), patch.object(
            Config, "GEMINI_API_KEY", ""
        ):
            self.assertEqual(Config.effective_live_stt_provider(), "whisper")
        with patch.object(Config, "LIVE_STT_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_API_KEY", ""
        ):
            self.assertEqual(Config.effective_live_stt_provider(), "whisper")
        with patch.object(
            Config, "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto, Kubernetes, Odicto"
        ):
            self.assertEqual(
                Config.gemini_transcribe_vocabulary(), ["Odicto", "Kubernetes"]
            )
        with patch.object(Config, "GEMINI_TRANSCRIBE_LANGUAGE", "en-US, hi-IN"):
            self.assertEqual(
                Config.gemini_transcribe_language_codes(), ["en-US", "hi-IN"]
            )

    @patch("transcriber.google_genai")
    def test_gemini_transcriber_unary_smart(self, mock_genai: MagicMock) -> None:
        client = MagicMock()
        mock_genai.Client.return_value = client
        interaction = MagicMock()
        interaction.output_text = "Let's meet Wednesday."
        client.interactions.create.return_value = interaction
        with patch.object(Config, "GEMINI_API_KEY", "AIza-test"), patch.object(
            Config, "GEMINI_TRANSCRIBE_MODE", "smart"
        ), patch.object(Config, "GEMINI_TRANSCRIBE_LANGUAGE", ""), patch.object(
            Config, "GEMINI_TRANSCRIBE_VOCABULARY", ""
        ):
            transcriber = GeminiTranscriber()
            audio = np.zeros(1600, dtype=np.float32)
            self.assertEqual(transcriber.transcribe(audio), "Let's meet Wednesday.")
        kwargs = client.interactions.create.call_args.kwargs
        self.assertIn("gemini-3.5-transcribe", kwargs["model"])
        mode = kwargs["generation_config"]["transcription_config"]["mode"]
        self.assertEqual(mode["type"], "smart")
        self.assertEqual(kwargs["input"][0]["mime_type"], "audio/wav")
        self.assertTrue(kwargs["input"][0]["data"])

    @patch("transcriber.google_genai")
    def test_gemini_transcriber_mode_override_verbatim(
        self, mock_genai: MagicMock
    ) -> None:
        client = MagicMock()
        mock_genai.Client.return_value = client
        interaction = MagicMock()
        interaction.output_text = "um hello"
        client.interactions.create.return_value = interaction
        with patch.object(Config, "GEMINI_API_KEY", "AIza-test"), patch.object(
            Config, "GEMINI_TRANSCRIBE_MODE", "smart"
        ):
            transcriber = GeminiTranscriber()
            audio = np.zeros(1600, dtype=np.float32)
            self.assertEqual(transcriber.transcribe(audio, mode="verbatim"), "um hello")
        mode = client.interactions.create.call_args.kwargs["generation_config"][
            "transcription_config"
        ]["mode"]
        self.assertEqual(mode["type"], "verbatim")

    @patch("transcriber.WhisperTranscriber")
    @patch("transcriber.google_genai")
    def test_gemini_transcriber_falls_back_on_error(
        self, mock_genai: MagicMock, mock_whisper: MagicMock
    ) -> None:
        client = MagicMock()
        mock_genai.Client.return_value = client
        client.interactions.create.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED")
        mock_whisper.return_value.transcribe.return_value = "whisper text"
        with patch.object(Config, "GEMINI_API_KEY", "AIza-test"):
            transcriber = GeminiTranscriber()
            audio = np.zeros(1600, dtype=np.float32)
            self.assertEqual(transcriber.transcribe(audio), "whisper text")
        mock_whisper.return_value.transcribe.assert_called_once()

    def test_audio_recorder_chunk_listener(self) -> None:
        with patch("recorder.sd.InputStream") as mock_stream:
            mock_stream.return_value.start.return_value = None
            recorder = AudioRecorder(sample_rate=16000, channels=1)
            seen: list = []
            recorder.add_chunk_listener(lambda c: seen.append(c.copy()))
            recorder.start()
            chunk = np.array([0.1, 0.2], dtype=np.float32)
            recorder._callback(chunk.reshape(-1, 1), 2, None, None)
            self.assertEqual(len(seen), 1)
            np.testing.assert_allclose(seen[0], chunk)
            recorder.remove_chunk_listener(recorder._chunk_listeners[0])
            recorder._callback(chunk.reshape(-1, 1), 2, None, None)
            self.assertEqual(len(seen), 1)
            recorder.close()

    def test_audio_recorder_waveform_follows_signal(self) -> None:
        with patch("recorder.sd.InputStream") as mock_stream:
            mock_stream.return_value.start.return_value = None
            recorder = AudioRecorder(sample_rate=16000, channels=1)
            recorder.start()
            quiet = np.zeros(64, dtype=np.float32)
            loud = np.linspace(0.0, 0.9, 64, dtype=np.float32)
            recorder._callback(quiet.reshape(-1, 1), 64, None, None)
            recorder._callback(loud.reshape(-1, 1), 64, None, None)
            wave = recorder.get_waveform(8)
            self.assertEqual(len(wave), 8)
            self.assertGreater(max(wave), 0.05)
            self.assertGreater(wave[-1], wave[0])
            recorder.close()

    def test_effective_llm_model_and_api_base(self) -> None:
        """Provider flip picks the right model id and API base without hand-editing paths."""
        with patch.object(Config, "LLM_PROVIDER", "ollama"), patch.object(
            Config, "OLLAMA_MODEL", "phi4-mini:latest"
        ), patch.object(Config, "OPENROUTER_MODEL", "google/gemini-2.0-flash-001"), patch.object(
            Config, "LLM_API_BASE", "http://localhost:11434/v1"
        ), patch.object(
            Config, "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
        ):
            self.assertEqual(Config.effective_llm_model(), "phi4-mini:latest")
            self.assertEqual(Config.effective_llm_api_base(), "http://localhost:11434/v1")

        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "LLM_MODEL", "phi4-mini:latest"
        ), patch.object(Config, "OPENROUTER_MODEL", "google/gemini-2.0-flash-001"), patch.object(
            Config, "LLM_API_BASE", "http://localhost:11434/v1"
        ), patch.object(
            Config, "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
        ):
            self.assertEqual(Config.effective_llm_model(), "google/gemini-2.0-flash-001")
            self.assertEqual(
                Config.effective_llm_api_base(), "https://openrouter.ai/api/v1"
            )

        # Blank OPENROUTER_MODEL falls back to LLM_MODEL
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "LLM_MODEL", "some/openrouter-id"
        ), patch.object(Config, "OPENROUTER_MODEL", ""):
            self.assertEqual(Config.effective_llm_model(), "some/openrouter-id")

        # Gemini uses GEMINI_MODEL and the google-genai endpoint
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_MODEL", "gemini-3.7-flash"
        ):
            self.assertEqual(Config.effective_llm_model(), "gemini-3.7-flash")
            self.assertIn(
                "generativelanguage.googleapis.com",
                Config.effective_llm_api_base(),
            )

    @patch("refiner.Config.LLM_MAX_TOKENS", 512)
    @patch("refiner.OpenAI")
    def test_text_refiner_ollama(self, mock_openai: MagicMock) -> None:
        """Verifies TextRefiner correctly formats messages and uses fixed max_tokens."""
        mock_client = mock_openai.return_value
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello, world!"
        mock_client.chat.completions.create.return_value = mock_response

        refiner = TextRefiner()
        result = refiner.refine("hello world")

        self.assertEqual(result, "Hello, world!")
        mock_client.chat.completions.create.assert_called_once()
        kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["model"], Config.effective_llm_model())
        # No adaptive budgets — always Config.LLM_MAX_TOKENS
        self.assertEqual(kwargs["max_tokens"], 512)
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "hello world")
        self.assertEqual(messages[0]["content"], Config.effective_system_prompt())
        self.assertNotIn("LENGTH RULES", messages[0]["content"])

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_API_KEY", "sk-or-test")
    @patch("refiner.Config.OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
    @patch("refiner.Config.LLM_API_BASE", "http://localhost:11434/v1")
    @patch("refiner.Config.OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    @patch("refiner.Config.OPENROUTER_PROVIDER_SORT", "latency")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch("refiner.OpenAI")
    def test_text_refiner_openrouter(self, mock_openai: MagicMock) -> None:
        """OpenRouter uses cloud base URL + key and the OPENROUTER_MODEL slug."""
        mock_client = mock_openai.return_value
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Cloud reply"
        mock_client.chat.completions.create.return_value = mock_response

        refiner = TextRefiner()
        self.assertEqual(refiner.provider, "openrouter")
        self.assertEqual(refiner.model, "google/gemini-2.0-flash-001")
        mock_openai.assert_called_once()
        init_kwargs = mock_openai.call_args[1]
        self.assertEqual(init_kwargs["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(init_kwargs["api_key"], "sk-or-test")

        result = refiner.refine("hello from openrouter")
        self.assertEqual(result, "Cloud reply")
        kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["model"], "google/gemini-2.0-flash-001")
        extra = kwargs["extra_body"]
        self.assertEqual(extra["provider"]["sort"], "latency")
        self.assertEqual(extra["reasoning"]["effort"], "none")
        self.assertNotIn("keep_alive", extra)
        self.assertNotIn("options", extra)

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_API_KEY", "sk-or-test")
    @patch("refiner.Config.OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
    @patch("refiner.Config.LLM_MAX_TOKENS", "512")
    @patch("refiner.Config.OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    @patch("refiner.OpenAI")
    def test_openrouter_retries_when_reasoning_eats_the_token_budget(
        self, mock_openai: MagicMock
    ) -> None:
        """Empty first reply (all tokens spent on reasoning) must not paste raw."""
        mock_client = mock_openai.return_value
        empty = MagicMock()
        empty.choices = [MagicMock()]
        empty.choices[0].message.content = None
        empty.choices[0].finish_reason = "error"
        empty.usage = MagicMock(completion_tokens=382)
        filled = MagicMock()
        filled.choices = [MagicMock()]
        filled.choices[0].message.content = "Rewritten selected text."
        filled.choices[0].finish_reason = "stop"
        mock_client.chat.completions.create.side_effect = [empty, filled]

        refiner = TextRefiner()
        result = refiner.refine("Please reformat this.")
        self.assertEqual(result, "Rewritten selected text.")
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        first_tokens = mock_client.chat.completions.create.call_args_list[0][1]["max_tokens"]
        retry_tokens = mock_client.chat.completions.create.call_args_list[1][1]["max_tokens"]
        self.assertEqual(first_tokens, 512)
        self.assertGreaterEqual(retry_tokens, 2048)

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    def test_openrouter_glm53_starts_at_low(self) -> None:
        """GLM-5.3 rejects none/minimal; start at the lightest accepted effort."""
        from refiner import openrouter_effort_for_model

        self.assertEqual(openrouter_effort_for_model("z-ai/glm-5.3-flash"), "low")
        self.assertEqual(openrouter_effort_for_model("z-ai/glm-5.3"), "low")
        self.assertEqual(openrouter_effort_for_model("z-ai/glm-5.3-flash:free"), "low")
        self.assertEqual(
            openrouter_effort_for_model("z-ai/glm-5.3-flash", configured="none"),
            "low",
        )
        self.assertEqual(
            openrouter_effort_for_model("z-ai/glm-5.3-flash", configured="minimal"),
            "low",
        )
        self.assertEqual(
            openrouter_effort_for_model("z-ai/glm-5.3-flash", configured="medium"),
            "low",
        )
        self.assertEqual(
            openrouter_effort_for_model("z-ai/glm-5.3-flash", configured="max"),
            "max",
        )
        self.assertEqual(openrouter_effort_for_model("openai/gpt-5.6-luna"), "none")
        self.assertEqual(openrouter_effort_for_model("deepseek/deepseek-v4.1-flash"), "none")

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "high")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch.object(config, "_PRESENT_AT_IMPORT", frozenset({"OPENROUTER_REASONING_EFFORT"}))
    def test_openrouter_glm53_keeps_explicit_high(self) -> None:
        """An explicit OPENROUTER_REASONING_EFFORT must beat the built-in default.

        Pins _PRESENT_AT_IMPORT deliberately. OPENROUTER_REASONING_EFFORT is not listed in
        _IMPORT_SNAPSHOT, so _explicit() only reports it as provided when the key was in the
        environment at import time - patching the attribute alone does not make it an
        override. Without this pin the test passed only on a developer machine whose shell
        exports the key, and failed on CI where nothing does.
        """
        from refiner import openrouter_effort_for_model

        self.assertEqual(openrouter_effort_for_model("z-ai/glm-5.3-flash"), "high")

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_API_KEY", "sk-or-test")
    @patch("refiner.Config.OPENROUTER_MODEL", "z-ai/glm-5.3-flash")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch("refiner.OpenAI")
    def test_openrouter_glm53_does_not_send_none(self, mock_openai: MagicMock) -> None:
        mock_client = mock_openai.return_value
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "ok"
        mock_client.chat.completions.create.return_value = mock_response

        refiner = TextRefiner()
        self.assertEqual(refiner.refine("hello"), "ok")
        extra = mock_client.chat.completions.create.call_args[1]["extra_body"]
        self.assertEqual(extra["reasoning"]["effort"], "low")
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch("refiner.OpenAI")
    def test_test_provider_retries_when_reasoning_is_mandatory(
        self, mock_openai: MagicMock
    ) -> None:
        from refiner import test_provider

        mock_client = mock_openai.return_value
        err = Exception(
            "Error code: 400 - {'error': {'message': "
            "'Reasoning is mandatory for this endpoint and cannot be disabled.', "
            "'code': 400, 'metadata': {'provider_name': None}}}"
        )
        mock_client.chat.completions.create.side_effect = [err, MagicMock()]
        self.assertEqual(
            test_provider("openrouter", "sk-or-test", "acme/thinker-1", ""),
            "ok",
        )
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        first_effort = mock_client.chat.completions.create.call_args_list[0][1][
            "extra_body"
        ]["reasoning"]["effort"]
        retry_effort = mock_client.chat.completions.create.call_args_list[1][1][
            "extra_body"
        ]["reasoning"]["effort"]
        self.assertEqual(first_effort, "none")
        self.assertEqual(retry_effort, "low")

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch("refiner.OpenAI")
    def test_test_provider_glm53_sends_low(self, mock_openai: MagicMock) -> None:
        """Setup Test for GLM-5.3 Flash must not send effort=none."""
        from refiner import test_provider

        mock_client = mock_openai.return_value
        mock_client.chat.completions.create.return_value = MagicMock()
        self.assertEqual(
            test_provider("openrouter", "sk-or-test", "z-ai/glm-5.3-flash", ""),
            "ok",
        )
        extra = mock_client.chat.completions.create.call_args[1]["extra_body"]
        self.assertEqual(extra["reasoning"]["effort"], "low")
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.Config.LLM_REASONING_EFFORT", "")
    @patch("refiner.OpenAI")
    def test_test_provider_glm53_ping_stays_low_when_max_requested(
        self, mock_openai: MagicMock
    ) -> None:
        """Setup Test is a 1-token ping — do not run GLM max thinking."""
        from refiner import test_provider

        mock_client = mock_openai.return_value
        mock_client.chat.completions.create.return_value = MagicMock()
        self.assertEqual(
            test_provider(
                "openrouter",
                "sk-or-test",
                "z-ai/glm-5.3-flash",
                "",
                reasoning_effort="max",
            ),
            "ok",
        )
        extra = mock_client.chat.completions.create.call_args[1]["extra_body"]
        self.assertEqual(extra["reasoning"]["effort"], "low")

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_REASONING_EFFORT", "none")
    @patch("refiner.OpenAI")
    def test_test_provider_does_not_retry_other_400s(
        self, mock_openai: MagicMock
    ) -> None:
        from refiner import test_provider

        mock_client = mock_openai.return_value
        mock_client.chat.completions.create.side_effect = Exception(
            "Error code: 400 - {'error': {'message': 'max_tokens too large'}}"
        )
        result = test_provider("openrouter", "sk-or-test", "openai/gpt-5.6-luna", "")
        self.assertIn("400", result)
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("refiner.Config.LLM_PROVIDER", "gemini")
    @patch("refiner.Config.GEMINI_API_KEY", "AIza-test")
    @patch("refiner.Config.GEMINI_MODEL", "gemini-3.7-flash")
    @patch("refiner.Config.GEMINI_THINKING_LEVEL", "low")
    @patch("refiner.Config.GEMINI_MAX_OUTPUT_TOKENS", 4096)
    def test_text_refiner_gemini(self) -> None:
        """Gemini uses the google-genai Interactions API with the system prompt."""
        import refiner

        fake_interaction = MagicMock()
        fake_interaction.output_text = "Gemini reply"
        fake_interaction.id = "v1_abc123"
        mock_client = MagicMock()
        mock_client.interactions.create.return_value = fake_interaction

        with patch.object(refiner, "google_genai") as mock_genai:
            mock_genai.Client.return_value = mock_client
            r = TextRefiner()
            self.assertEqual(r.provider, "gemini")
            self.assertEqual(r.model, "gemini-3.7-flash")
            mock_genai.Client.assert_called_once_with(api_key="AIza-test")

            result = r.refine("hello from gemini", keep_history=True)
            self.assertEqual(result, "Gemini reply")

            result2 = r.refine("follow up question", keep_history=True)
            self.assertEqual(result2, "Gemini reply")

        first_kwargs = mock_client.interactions.create.call_args_list[0][1]
        self.assertEqual(first_kwargs["model"], "gemini-3.7-flash")
        self.assertEqual(first_kwargs["input"], "hello from gemini")
        self.assertIn("PLAIN HUMAN-READABLE TEXT", first_kwargs["system_instruction"])
        self.assertEqual(first_kwargs["system_instruction"], Config.effective_system_prompt())
        self.assertEqual(first_kwargs["generation_config"]["thinking_level"], "low")
        # First turn has no previous_interaction_id; the second turn chains
        # onto the stored id from the first reply.
        self.assertNotIn("previous_interaction_id", first_kwargs)
        self.assertEqual(
            mock_client.interactions.create.call_args_list[1][1]["previous_interaction_id"],
            "v1_abc123",
        )

    @patch("refiner.Config.LLM_PROVIDER", "meta")
    @patch("refiner.Config.META_API_KEY", "sk-meta-test")
    def test_text_refiner_meta_system_prompt_and_context(self) -> None:
        """Meta Responses payload passes system prompt in role:system and context in user message."""
        import refiner

        mock_client = MagicMock()
        mock_client.create_responses.return_value = "Meta response text"
        with patch.object(refiner, "_MetaClient", return_value=mock_client):
            r = refiner.TextRefiner()
            result = r.refine("translate to french", context="Selected sample text")
            self.assertEqual(result, "Meta response text")

            payload_args, _ = mock_client.create_responses.call_args
            input_payload = payload_args[0]
            self.assertEqual(input_payload[0]["role"], "system")
            self.assertIn("PLAIN HUMAN-READABLE TEXT", input_payload[0]["content"][0]["text"])
            self.assertIn("SELECTED CONTEXT:\n<<<\nSelected sample text\n>>>", input_payload[0]["content"][0]["text"])
            self.assertEqual(input_payload[1]["role"], "user")
            self.assertEqual(input_payload[1]["content"][0]["text"], "translate to french")

    def test_extract_meta_text_ignores_echoed_input(self) -> None:
        """1.3 Responses payloads echo the user turn; that must not become the paste."""
        import refiner

        data = {
            "output": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Please restructure this properly and remove the AI slop.",
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "The rewritten post."}],
                },
            ]
        }
        self.assertEqual(refiner._extract_meta_text(data), "The rewritten post.")

    def test_extract_meta_text_skips_input_when_assistant_missing(self) -> None:
        import refiner

        data = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Please restructure this properly and remove the AI slop.",
                        }
                    ],
                },
            ]
        }
        self.assertIsNone(refiner._extract_meta_text(data))

    def test_meta_client_sends_no_output_cap(self) -> None:
        """Reasoning is uncapped; the effort knob is the only thinking control."""
        import refiner

        client = refiner._MetaClient(
            api_key="sk-test", base_url="https://api.meta.ai/v1", model="m"
        )
        captured: dict = {}

        def fake_post(url: str, payload: dict, timeout) -> dict:
            captured.update(payload)
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Rewritten."}],
                    }
                ],
            }

        with patch.object(refiner._MetaClient, "_post_payload", side_effect=fake_post):
            text = client.create_responses(
                [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            )
        self.assertEqual(text, "Rewritten.")
        self.assertNotIn("max_output_tokens", captured)
        self.assertEqual(captured["reasoning"], {"effort": "low"})

    def test_meta_client_uncapped_timeout_allows_long_reasoning(self) -> None:
        """Medium/high effort can think 60s+; a 30s read timeout would kill it."""
        import inspect

        import refiner

        sig = inspect.signature(refiner._MetaClient.create_responses)
        default_timeout = sig.parameters["timeout"].default
        self.assertGreaterEqual(
            default_timeout[1],
            120.0,
            f"Meta read timeout {default_timeout} is too short for uncapped reasoning",
        )

    @patch("refiner.Config.LLM_PROVIDER", "gemini")
    @patch("refiner.Config.GEMINI_API_KEY", "AIza-test")
    def test_text_refiner_gemini_reset_clears_server_state(self) -> None:
        """reset chat must also drop the server-side previous_interaction_id."""
        import refiner

        with patch.object(refiner, "google_genai") as mock_genai:
            mock_client = MagicMock()
            mock_genai.Client.return_value = mock_client
            r = TextRefiner()
            r.client._last_interaction_id = "v1_old"
            r.refine("reset chat")
            self.assertIsNone(r.client._last_interaction_id)
            self.assertEqual(r.conversation_history, [])

    @patch("refiner.OpenAI")
    def test_text_refiner_conversation_history(self, mock_openai: MagicMock) -> None:
        """Verifies that TextRefiner correctly maintains and updates conversation history."""
        mock_client = mock_openai.return_value

        mock_resp1 = MagicMock()
        mock_resp1.choices = [MagicMock()]
        mock_resp1.choices[0].message.content = "My name is Assistant."

        mock_resp2 = MagicMock()
        mock_resp2.choices = [MagicMock()]
        mock_resp2.choices[0].message.content = "You said hello."

        mock_client.chat.completions.create.side_effect = [mock_resp1, mock_resp2]

        refiner = TextRefiner()

        res1 = refiner.refine("What is your name?", keep_history=True)
        self.assertEqual(res1, "My name is Assistant.")
        self.assertEqual(len(refiner.conversation_history), 2)
        self.assertEqual(
            refiner.conversation_history[0],
            {"role": "user", "content": "What is your name?"},
        )
        self.assertEqual(
            refiner.conversation_history[1],
            {"role": "assistant", "content": "My name is Assistant."},
        )

        res2 = refiner.refine("Repeat what I did.", keep_history=True)
        self.assertEqual(res2, "You said hello.")
        self.assertEqual(len(refiner.conversation_history), 4)

        calls = mock_client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)

        # Native multi-turn messages: system + history
        messages_call1 = calls[0][1]["messages"]
        self.assertEqual(messages_call1[0]["role"], "system")
        self.assertEqual(messages_call1[1]["content"], "What is your name?")

        messages_call2 = calls[1][1]["messages"]
        roles = [m["role"] for m in messages_call2]
        self.assertEqual(roles[0], "system")
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        contents = [m["content"] for m in messages_call2]
        self.assertIn("What is your name?", contents)
        self.assertIn("My name is Assistant.", contents)
        self.assertIn("Repeat what I did.", contents)

    @patch("refiner.OpenAI")
    def test_text_refiner_fresh_does_not_keep_history(self, mock_openai: MagicMock) -> None:
        """Default refine is a one-shot: no history written, later F6 turns stay empty."""
        mock_client = mock_openai.return_value
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "One shot"
        mock_client.chat.completions.create.return_value = mock_resp

        refiner = TextRefiner()
        refiner.refine("task A")
        self.assertEqual(refiner.conversation_history, [])

        refiner.refine("task B")
        messages_b = mock_client.chat.completions.create.call_args_list[1][1]["messages"]
        contents = [m["content"] for m in messages_b]
        self.assertIn("task B", contents)
        self.assertNotIn("task A", contents)
        self.assertEqual(refiner.conversation_history, [])

    @patch("refiner.OpenAI")
    def test_text_refiner_exception_fallback(self, mock_openai: MagicMock) -> None:
        """Verifies that TextRefiner gracefully returns raw text if API call fails."""
        mock_client = mock_openai.return_value
        mock_client.chat.completions.create.side_effect = Exception(
            "API connection timed out"
        )

        refiner = TextRefiner()
        result = refiner.refine("raw transcript text")
        self.assertEqual(result, "raw transcript text")
        self.assertEqual(refiner.conversation_history, [])

    @patch("refiner.OpenAI")
    def test_text_refiner_reset_chat_clears_history(self, mock_openai: MagicMock) -> None:
        """Speaking 'reset chat' clears multi-turn memory without an LLM call."""
        mock_client = mock_openai.return_value
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Hello, I am Assistant."
        mock_client.chat.completions.create.side_effect = [mock_resp, mock_resp]

        refiner = TextRefiner()

        # Build some history via keep-history queries.
        refiner.refine("What is your name?", keep_history=True)
        self.assertEqual(len(refiner.conversation_history), 2)
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

        # Reset clears history and does NOT call the LLM.
        result = refiner.refine("reset chat")
        self.assertEqual(result, "Chat memory cleared. Starting fresh.")
        self.assertEqual(refiner.conversation_history, [])
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

        # Case/punctuation variants also reset.
        refiner.refine("What is your name?", keep_history=True)
        self.assertEqual(len(refiner.conversation_history), 2)
        refiner.refine("Clear the conversation!")
        self.assertEqual(refiner.conversation_history, [])

    @patch("refiner.Config.LLM_PROVIDER", "none")
    def test_text_refiner_none_provider(self) -> None:
        """Verifies that TextRefiner immediately bypasses LLM if LLM_PROVIDER is 'none'."""
        refiner = TextRefiner()
        result = refiner.refine("raw transcript text")
        self.assertEqual(result, "raw transcript text")

    @patch("refiner.OpenAI")
    def test_text_refiner_with_context(self, mock_openai: MagicMock) -> None:
        """Context is prepended to the user message when provided."""
        mock_client = mock_openai.return_value
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Refactored response"
        mock_client.chat.completions.create.return_value = mock_response

        refiner = TextRefiner()
        result = refiner.refine("make this better", context="Hello world")
        self.assertEqual(result, "Refactored response")

        kwargs = mock_client.chat.completions.create.call_args[1]
        messages = kwargs["messages"]
        sys_msgs = [m for m in messages if m["role"] == "system"]
        self.assertEqual(len(sys_msgs), 1)
        self.assertIn("SELECTED CONTEXT:\n<<<\nHello world\n>>>", sys_msgs[0]["content"])
        user_msgs = [m for m in messages if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], "make this better")

    @patch("refiner.OpenAI")
    def test_text_refiner_without_context(self, mock_openai: MagicMock) -> None:
        """Without context, the user message is just the raw query."""
        mock_client = mock_openai.return_value
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Reply"
        mock_client.chat.completions.create.return_value = mock_response

        refiner = TextRefiner()
        refiner.refine("hello world")

        kwargs = mock_client.chat.completions.create.call_args[1]
        user_msgs = [m for m in kwargs["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], "hello world")

    @patch("typer.send_paste")
    @patch("typer._wait_modifiers_up")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_paste_text_flow(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_wait_mods: MagicMock,
        mock_send_paste: MagicMock,
    ) -> None:
        """Verifies clipboard injection backup, paste command execution, and clipboard restore."""
        state = {"clip": "original clipboard data"}
        mock_clipboard_read.side_effect = lambda: state["clip"]

        def fake_write(text: str) -> bool:
            state["clip"] = text
            return True

        mock_clipboard_write.side_effect = fake_write

        paste_text("injected text")

        mock_clipboard_write.assert_any_call("injected text")
        mock_send_paste.assert_called()
        mock_clipboard_write.assert_any_call("original clipboard data")
        self.assertEqual(state["clip"], "original clipboard data")

    def test_clipboard_write_verified_retries_busy_clipboard(self) -> None:
        """Transient clipboard-busy writes are retried and then verified."""
        import typer

        state = {"clip": "original", "writes": 0}

        def fake_write(text: str) -> bool:
            state["writes"] += 1
            if state["writes"] < 3:
                return False
            state["clip"] = text
            return True

        with patch("typer._clipboard_write", side_effect=fake_write), patch(
            "typer._clipboard_read", side_effect=lambda: state["clip"]
        ), patch("typer.time.sleep"):
            self.assertTrue(typer._clipboard_write_verified("payload"))
        self.assertEqual(state["clip"], "payload")
        self.assertGreaterEqual(state["writes"], 3)

    def test_paste_text_aborts_when_clipboard_write_fails(self) -> None:
        """A busy clipboard never triggers a paste of stale content."""
        with patch("typer._clipboard_read", return_value="user clip"), patch(
            "typer._clipboard_write_verified", return_value=False
        ), patch("typer.send_paste") as mock_paste, patch(
            "typer._wait_modifiers_up"
        ), patch(
            "typer.time.sleep"
        ):
            paste_text("payload")
            mock_paste.assert_not_called()

    @patch("typer.send_paste")
    @patch("typer._wait_modifiers_up")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_paste_text_restores_clipboard_with_verified_write(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_wait_mods: MagicMock,
        mock_send_paste: MagicMock,
    ) -> None:
        """Restore is verified (not blind) and waits out slow web apps."""
        import typer

        state = {"clip": "original clipboard data"}
        mock_clipboard_read.side_effect = lambda: state["clip"]

        def fake_write(text: str) -> bool:
            state["clip"] = text
            return True

        mock_clipboard_write.side_effect = fake_write
        sleeps: list = []
        with patch("typer.time.sleep", side_effect=lambda s: sleeps.append(s)):
            paste_text("injected text")

        mock_clipboard_write.assert_any_call("injected text")
        mock_send_paste.assert_called()
        self.assertEqual(state["clip"], "original clipboard data")
        self.assertTrue(any(s >= 0.15 for s in sleeps), sleeps)

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=False)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_with_selection(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """get_selected_text detects selection via sentinel change and restores clipboard."""
        # paste order: backup original, then polls after copy, then any extras.
        state = {"n": 0}

        def fake_read() -> str:
            state["n"] += 1
            # After sentinel is written (copy call #1), later reads see selection.
            if state["n"] == 1:
                return "original content"
            return "selected text"

        mock_clipboard_read.side_effect = fake_read
        mock_clipboard_write.return_value = True

        result = get_selected_text(timeout=0.15)

        self.assertEqual(result, "selected text")
        # First write is the sentinel, last restore is original content.
        write_args = [c.args[0] for c in mock_clipboard_write.call_args_list if c.args]
        self.assertTrue(any("odicto-sel-" in str(a) for a in write_args), write_args)
        self.assertEqual(write_args[-1], "original content")
        # Must attempt a keyboard copy path when WM_COPY is disabled in this test.
        mock_send_copy.assert_called()

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=False)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_no_selection(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """get_selected_text returns empty when clipboard never leaves the sentinel."""
        # Always return whatever was last written (sentinel sticks = no selection).
        last_written = {"v": ""}

        def fake_write(v: str) -> bool:
            last_written["v"] = v
            return True

        def fake_read() -> str:
            return last_written["v"] or "original"

        mock_clipboard_write.side_effect = fake_write
        mock_clipboard_read.side_effect = fake_read

        result = get_selected_text(timeout=0.12)

        self.assertEqual(result, "")

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=True)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_via_wm_copy(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """WM_COPY path captures selection without needing a synthetic copy chord when it works."""
        last_written = {"v": ""}

        def fake_write(v: str) -> bool:
            last_written["v"] = v
            return True

        def fake_read() -> str:
            # After WM_COPY "succeeds", app puts selection on clipboard.
            if "odicto-sel-" in last_written["v"]:
                return "highlighted paragraph"
            return last_written["v"]

        mock_clipboard_write.side_effect = fake_write
        mock_clipboard_read.side_effect = fake_read

        result = get_selected_text(timeout=0.15)

        self.assertEqual(result, "highlighted paragraph")
        mock_wm.assert_called()
        mock_send_copy.assert_not_called()

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=False)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_paste_error(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """get_selected_text returns empty string on clipboard read failure."""
        mock_clipboard_read.side_effect = Exception("clipboard error")
        mock_clipboard_write.side_effect = Exception("clipboard error")

        result = get_selected_text(timeout=0.1)

        self.assertEqual(result, "")

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=False)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_same_as_prior_clipboard(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """Selection equal to prior clipboard is still captured (sentinel trick)."""
        last_written = {"v": ""}

        def fake_write(v: str) -> bool:
            last_written["v"] = v
            return True

        def fake_read() -> str:
            # "App" copies selection that happens to equal the prior clipboard.
            if "odicto-sel-" in (last_written["v"] or ""):
                return "same text as before"
            return last_written["v"] or "same text as before"

        mock_clipboard_write.side_effect = fake_write
        mock_clipboard_read.side_effect = fake_read

        result = get_selected_text(timeout=0.15)
        self.assertEqual(result, "same text as before")

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=True)
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_wm_copy_ignored_falls_back_to_send_copy(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """When WM_COPY returns True but does not change the clipboard, send_copy is used."""
        last_written = {"v": ""}

        def fake_write(v: str) -> bool:
            last_written["v"] = v
            return True

        def fake_read() -> str:
            # During WM_COPY check, clipboard still holds whatever was written (sentinel).
            # Once send_copy is invoked, the app puts the selection on the clipboard.
            if mock_send_copy.called:
                return "fallback copied text"
            return last_written["v"]

        mock_clipboard_write.side_effect = fake_write
        mock_clipboard_read.side_effect = fake_read

        result = get_selected_text(timeout=0.15)
        self.assertEqual(result, "fallback copied text")
        mock_wm.assert_called()
        mock_send_copy.assert_called()

    @patch("PySide6.QtGui.QGuiApplication")
    def test_get_clipboard_image_success(self, mock_qguiapp: MagicMock) -> None:
        """get_clipboard_image converts a non-null QImage into PNG bytes."""
        from typer import get_clipboard_image
        from PySide6.QtGui import QImage, QColor

        img = QImage(20, 20, QImage.Format_ARGB32)
        img.fill(QColor("blue"))
        mock_app = MagicMock()
        mock_cb = MagicMock()
        mock_cb.image.return_value = img
        mock_app.clipboard.return_value = mock_cb
        mock_qguiapp.instance.return_value = mock_app

        png_bytes = get_clipboard_image()
        self.assertIsNotNone(png_bytes)
        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(len(png_bytes) > 0)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    @patch("PySide6.QtGui.QGuiApplication")
    def test_get_clipboard_image_scaled_down(self, mock_qguiapp: MagicMock) -> None:
        """get_clipboard_image downscales images exceeding max_dim (1600px)."""
        from typer import get_clipboard_image
        from PySide6.QtGui import QImage, QColor

        # 2000x1000 image
        large_img = QImage(2000, 1000, QImage.Format_ARGB32)
        large_img.fill(QColor("red"))
        mock_app = MagicMock()
        mock_cb = MagicMock()
        mock_cb.image.return_value = large_img
        mock_app.clipboard.return_value = mock_cb
        mock_qguiapp.instance.return_value = mock_app

        png_bytes = get_clipboard_image(max_dim=1600)
        self.assertIsNotNone(png_bytes)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    @patch("PySide6.QtGui.QGuiApplication")
    def test_get_clipboard_image_empty(self, mock_qguiapp: MagicMock) -> None:
        """get_clipboard_image returns None if clipboard image is null or empty."""
        from typer import get_clipboard_image
        from PySide6.QtGui import QImage

        null_img = QImage()
        mock_app = MagicMock()
        mock_cb = MagicMock()
        mock_cb.image.return_value = null_img
        mock_app.clipboard.return_value = mock_cb
        mock_qguiapp.instance.return_value = mock_app

        png_bytes = get_clipboard_image()
        self.assertIsNone(png_bytes)

    @patch("typer._get_clipboard_image_locked")
    @patch("typer._get_selected_text_locked")
    def test_capture_ai_context_preserves_image_and_text(
        self, mock_get_sel: MagicMock, mock_get_img: MagicMock
    ) -> None:
        """capture_ai_context retrieves both pre-existing image and highlighted text."""
        from typer import capture_ai_context

        mock_get_img.return_value = b"\x89PNGfakeimage"
        mock_get_sel.return_value = "selected code snippet"

        text_ctx, img_bytes = capture_ai_context(timeout=0.1)
        self.assertEqual(text_ctx, "selected code snippet")
        self.assertEqual(img_bytes, b"\x89PNGfakeimage")

    @patch("refiner.Config.LLM_PROVIDER", "gemini")
    @patch("refiner.Config.GEMINI_API_KEY", "AIza-test")
    def test_text_refiner_gemini_multimodal(self) -> None:
        """Gemini client creates multimodal Part when image_bytes is provided."""
        import refiner

        mock_client = MagicMock()
        mock_interaction = MagicMock()
        mock_interaction.output_text = "Multimodal explanation"
        mock_client.interactions.create.return_value = mock_interaction

        with patch.object(refiner, "google_genai") as mock_genai:
            mock_genai.Client.return_value = mock_client
            r = refiner.TextRefiner()
            fake_png = b"\x89PNGfakebytes"
            result = r.refine("explain this diagram", image_bytes=fake_png)
            self.assertEqual(result, "Multimodal explanation")

            call_kwargs = mock_client.interactions.create.call_args[1]
            input_val = call_kwargs["input"]
            self.assertIsInstance(input_val, list)
            self.assertEqual(len(input_val), 2)
            self.assertEqual(input_val[1], "explain this diagram")

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_API_KEY", "sk-or-test")
    @patch("refiner.OpenAI")
    def test_text_refiner_openrouter_multimodal(self, mock_openai: MagicMock) -> None:
        """OpenRouter client creates OpenAI image_url part when image_bytes is provided."""
        import refiner

        mock_client = mock_openai.return_value
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Vision response"
        mock_client.chat.completions.create.return_value = mock_resp

        r = refiner.TextRefiner()
        fake_png = b"\x89PNGtestvision"
        result = r.refine("what is in this screenshot", image_bytes=fake_png)
        self.assertEqual(result, "Vision response")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        messages = call_kwargs["messages"]
        user_msg = [m for m in messages if m["role"] == "user"][0]
        self.assertIsInstance(user_msg["content"], list)
        self.assertEqual(user_msg["content"][0]["text"], "what is in this screenshot")
        self.assertTrue(user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    @patch("refiner.Config.LLM_PROVIDER", "meta")
    @patch("refiner.Config.META_API_KEY", "sk-meta-test")
    def test_text_refiner_meta_multimodal_fallback(self) -> None:
        """Meta provider gracefully falls back to text-only when image_bytes is provided."""
        import refiner

        mock_client = MagicMock()
        mock_client.create_responses.return_value = "Meta text fallback reply"
        with patch.object(refiner, "_MetaClient", return_value=mock_client):
            r = refiner.TextRefiner()
            fake_png = b"\x89PNGmetafallback"
            result = r.refine("describe this", image_bytes=fake_png)
            self.assertEqual(result, "Meta text fallback reply")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_state_machine(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Verifies the global hotkey state machine flow and processing pipeline trigger."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            self.assertEqual(app.state, AppState.IDLE)

            mock_keyboard.is_pressed.return_value = True  # AI_MODIFIER held → AI mode
            mock_get_selected_text.return_value = ""  # no selected text
            # on_press() without a chord argument reads the press-time modifier
            # snapshot; emulate the hotkey handler populating it.
            app._pressed_mods_at_press = ("ctrl", "shift")

            # 1. Transition IDLE -> RECORDING
            app.on_press()
            self.assertEqual(app.state, AppState.RECORDING)
            self.assertTrue(app.use_llm)
            app.recorder.start.assert_called_once()

            # 2. Transition RECORDING -> PROCESSING
            app.recorder.stop.return_value = True
            fake_audio = np.zeros(100, dtype=np.float32)
            app.recorder.last_audio_array = fake_audio
            app.transcriber.transcribe.return_value = "raw speech text"
            app.refiner.refine.return_value = "Polished speech text."

            # Bypass MIN_HOLD_MS guard
            app._record_started_at = 0.0

            with patch("threading.Thread") as mock_thread:
                app.on_release()
                app.recorder.stop.assert_called_once_with(filepath=None)
                self.assertEqual(app.state, AppState.PROCESSING)
                mock_thread.assert_called_once()
                pipeline_call = mock_thread.call_args
                pipeline_target = (
                    pipeline_call[1].get("target")
                    if "target" in pipeline_call[1]
                    else pipeline_call[0][0]
                )
                pipeline_args = (
                    pipeline_call[1].get("args") or pipeline_call[0][1:]
                )
            # Run the worker with real threads so STT/selection overlap works.
            pipeline_target(*pipeline_args)

            app.transcriber.transcribe.assert_called_once()
            app.refiner.refine.assert_called_once_with(
                "raw speech text", context="", image_bytes=None, keep_history=False
            )
            mock_paste_text.assert_called_once_with("Polished speech text.")
            self.assertEqual(app.state, AppState.IDLE)
            self.assertEqual(app.last_status, "success")
            # Selection probe overlaps Whisper (not the hook thread).
            mock_get_selected_text.assert_called()

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_passes_selection_as_context(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Selected text is captured off-hook and passed to refine as context."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.time.sleep"):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            mock_get_selected_text.return_value = "highlighted draft paragraph"
            app._pressed_mods_at_press = ("ctrl", "shift")
            app.on_press(use_llm=True)
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(100, dtype=np.float32)
            app.transcriber.transcribe.return_value = "make this better"
            app.refiner.refine.return_value = "Improved draft."
            app._record_started_at = 0.0

            with patch("threading.Thread") as mock_thread:
                app.on_release()
                pipeline_call = mock_thread.call_args
                pipeline_target = (
                    pipeline_call[1].get("target")
                    if "target" in pipeline_call[1]
                    else pipeline_call[0][0]
                )
                pipeline_args = (
                    pipeline_call[1].get("args") or pipeline_call[0][1:]
                )
            pipeline_target(*pipeline_args)

            app.refiner.refine.assert_called_once_with(
                "make this better",
                context="highlighted draft paragraph",
                image_bytes=None,
                keep_history=False,
            )
            mock_paste_text.assert_called_once_with("Improved draft.")

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_handles_selection_exception_gracefully(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Exceptions in get_selected_text log an error and gracefully pass empty context."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.time.sleep"):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            mock_get_selected_text.side_effect = RuntimeError("Simulated clipboard failure")
            app._pressed_mods_at_press = ("ctrl", "shift")
            app.on_press(use_llm=True)
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(100, dtype=np.float32)
            app.transcriber.transcribe.return_value = "summarize this"
            app.refiner.refine.return_value = "Summary text."
            app._record_started_at = 0.0

            with patch("threading.Thread") as mock_thread:
                app.on_release()
                pipeline_call = mock_thread.call_args
                pipeline_target = (
                    pipeline_call[1].get("target")
                    if "target" in pipeline_call[1]
                    else pipeline_call[0][0]
                )
                pipeline_args = (
                    pipeline_call[1].get("args") or pipeline_call[0][1:]
                )
            pipeline_target(*pipeline_args)

            app.refiner.refine.assert_called_once_with(
                "summarize this",
                context="",
                image_bytes=None,
                keep_history=False,
            )
            mock_paste_text.assert_called_once_with("Summary text.")

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_ai_via_use_llm_arg(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Chord matcher passes use_llm=True for the AI hotkey."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
            app.ready = True
            app.on_press(use_llm=True)
            self.assertTrue(app.use_llm)
            app._set_state(AppState.IDLE)
            app.on_press(use_llm=False)
            self.assertFalse(app.use_llm)

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_bypass_llm(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Verifies that the dictation app bypasses the LLM when use_llm is False."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
            app.ready = True
            mock_keyboard.is_pressed.return_value = False
            app.on_press()
            self.assertFalse(app.use_llm)

            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(50, dtype=np.float32)
            app.transcriber.transcribe.return_value = "raw whisper text"
            app._record_started_at = 0.0

            with patch("threading.Thread") as mock_thread:
                app.on_release()
                call_kwargs = mock_thread.call_args[1]
                target_function = call_kwargs["target"]
                worker_args = call_kwargs.get("args", ())
                target_function(*worker_args)

                app.transcriber.transcribe.assert_called_once()
                app.refiner.refine.assert_not_called()
                mock_paste_text.assert_called_once_with("raw whisper text")
                self.assertEqual(app.state, AppState.IDLE)

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_ignores_hotkey_before_ready(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Hotkey presses during boot must not crash or start recording."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            # Do not call initialize_app / leave ready=False
            app.ready = False
            app.recorder = None
            app.on_press()
            self.assertEqual(app.state, AppState.IDLE)

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_short_hold_ignored(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Accidental taps shorter than MIN_HOLD_MS should not run the pipeline."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.MIN_HOLD_MS", 500), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
            app.ready = True
            app.on_press()
            # Hold barely started
            app._record_started_at = __import__("time").monotonic()
            with patch("threading.Thread") as mock_thread:
                app.on_release()
                mock_thread.assert_not_called()
            self.assertEqual(app.state, AppState.IDLE)
            app.transcriber.transcribe.assert_not_called()

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_f6_keeps_conversation(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """Holding F6 (CTRL_KEEP_CONTEXT_KEYS) with Ctrl+` (or the AI chord)
        runs AI with conversation memory. Without F6, AI is a fresh one-shot."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.CTRL_KEEP_CONTEXT_KEYS", ("f6",)), patch(
            "main.time.sleep"
        ):
            mock_get_selected_text.return_value = ""
            app = DictationApp()
            app.initialize_app()
            app.ready = True

            # Fresh AI chord: no F6 → keep_history stays False, memory not wiped
            # at press (fresh is handled inside refine, not by resetting first).
            app._pressed_mods_at_press = ("ctrl", "shift")
            app.on_press(use_llm=True)
            self.assertTrue(app.use_llm)
            self.assertFalse(app._keep_history)
            mock_refiner.return_value.reset_context.assert_not_called()

            app._set_state(AppState.IDLE)
            app._last_cycle_end = 0.0

            # User holds F6 + Ctrl (dictation chord) → AI + keep memory.
            app._pressed_mods_at_press = ("ctrl", "f6")
            app.on_press(use_llm=False)
            self.assertTrue(app.use_llm)
            self.assertTrue(app._keep_history)
            self.assertIsNone(app._capture_mode_override)
            mock_refiner.return_value.reset_context.assert_not_called()

            app._set_state(AppState.RECORDING)
            app._record_started_at = 0.0
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(50, dtype=np.float32)
            app.transcriber.transcribe.return_value = "draft"
            app.refiner.refine.return_value = "Draft reply"
            with patch("threading.Thread") as mock_thread:
                app.on_release()
                pipeline_call = mock_thread.call_args
                pipeline_target = (
                    pipeline_call[1].get("target")
                    if "target" in pipeline_call[1]
                    else pipeline_call[0][0]
                )
                pipeline_args = (
                    pipeline_call[1].get("args") or pipeline_call[0][1:]
                )
            pipeline_target(*pipeline_args)
            self.assertEqual(app.state, AppState.IDLE)
            app.refiner.refine.assert_called_once_with(
                "draft", context="", image_bytes=None, keep_history=True
            )

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_dictation_app_reset_context_hotkey(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """RESET_CONTEXT_HOTKEY (F5) clears refiner history without recording.

        The reset is bound via hook_key (add_hotkey fails for plain single keys
        in the keyboard library), so this simulates the KEY_UP event through the
        bound handler.
        """
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.RESET_CONTEXT_HOTKEY", "f5"):
            # Simulate a fully-held single-instance lock so _bind_hotkeys
            # (which requires the mutex handle + lockfile) can register hooks.
            import main as main_mod

            was_held = main_mod._INSTANCE_LOCK_HELD
            main_mod._INSTANCE_LOCK_HELD = True
            # The reset handler compares against KEY_UP ("up" in the
            # real library); make the mocked platform module match.
            mock_keyboard.KEY_UP = "up"
            mock_keyboard.KEY_DOWN = "down"
            try:
                app = DictationApp()
                app.initialize_app()
                app.ready = True

                # Capture the handler that _bind_hotkeys registered for F5.
                # hook_key registers one callback per scan code, so find the F5
                # binding by key name rather than asserting an exact count.
                f5_binds = [
                    c
                    for c in mock_keyboard.hook_key.call_args_list
                    if c[0][0] == "f5"
                ]
                self.assertTrue(f5_binds, "F5 reset key not bound via hook_key")
                reset_handler = f5_binds[0][0][1]
                self.assertIsNotNone(reset_handler)

                # Simulate a physical F5 release (KEY_UP) → reset fires.
                reset_handler(type("Evt", (), {"event_type": "up"})())
                mock_refiner.return_value.reset_context.assert_called_once()

                # KEY_DOWN alone must not fire (fires on release).
                reset_handler(type("Evt", (), {"event_type": "down"})())
                self.assertEqual(
                    mock_refiner.return_value.reset_context.call_count, 1
                )
            finally:
                main_mod._INSTANCE_LOCK_HELD = was_held

    def _grave_handler(self, mock_keyboard):
        binds = [
            c for c in mock_keyboard.hook_key.call_args_list if c[0][0] == "grave"
        ]
        self.assertTrue(binds, "grave primary key not bound via hook_key")
        return binds[0][0][1]

    @staticmethod
    def _key_evt(kind: str):
        return type("Evt", (), {"event_type": kind})()

    @patch("main.Config.HOTKEY_TOGGLE", True)
    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_hotkey_toggle_tap_starts_and_second_tap_stops(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """HOTKEY_TOGGLE: down starts, up does not stop, second down runs the pipeline."""
        mock_keyboard.KEY_UP = "up"
        mock_keyboard.KEY_DOWN = "down"
        mock_keyboard.lock_is_held.return_value = True
        mock_keyboard.is_pressed.side_effect = lambda k: k == "ctrl"
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
        app.ready = True
        handler = self._grave_handler(mock_keyboard)

        handler(self._key_evt("down"))
        self.assertEqual(app.state, AppState.RECORDING)
        self.assertFalse(app.use_llm)
        handler(self._key_evt("up"))
        self.assertEqual(app.state, AppState.RECORDING)

        app._record_started_at = 0.0
        app.recorder.stop.return_value = True
        app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
        with patch("threading.Thread") as mock_thread:
            handler(self._key_evt("down"))
        self.assertEqual(app.state, AppState.PROCESSING)
        args = mock_thread.call_args[1].get("args") or mock_thread.call_args[0][1:]
        self.assertEqual(args[1], False)

    @patch("main.Config.HOTKEY_TOGGLE", True)
    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_hotkey_toggle_ai_chord_keeps_llm_mode(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        mock_keyboard.KEY_UP = "up"
        mock_keyboard.KEY_DOWN = "down"
        mock_keyboard.lock_is_held.return_value = True
        mock_keyboard.is_pressed.side_effect = lambda k: k in ("ctrl", "shift")
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
        app.ready = True
        handler = self._grave_handler(mock_keyboard)

        handler(self._key_evt("down"))
        self.assertEqual(app.state, AppState.RECORDING)
        self.assertTrue(app.use_llm)
        handler(self._key_evt("up"))
        self.assertTrue(app.use_llm)

        app._record_started_at = 0.0
        app.recorder.stop.return_value = True
        app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
        with patch("threading.Thread") as mock_thread:
            handler(self._key_evt("down"))
        args = mock_thread.call_args[1].get("args") or mock_thread.call_args[0][1:]
        self.assertEqual(args[1], True)

    @patch("main.Config.HOTKEY_TOGGLE", False)
    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_hotkey_hold_mode_stops_on_release(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        mock_keyboard.KEY_UP = "up"
        mock_keyboard.KEY_DOWN = "down"
        mock_keyboard.lock_is_held.return_value = True
        mock_keyboard.is_pressed.side_effect = lambda k: k == "ctrl"
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("threading.Thread"):
            app = DictationApp()
            app.initialize_app()
        app.ready = True
        handler = self._grave_handler(mock_keyboard)

        handler(self._key_evt("down"))
        self.assertEqual(app.state, AppState.RECORDING)
        app._record_started_at = 0.0
        app.recorder.stop.return_value = True
        app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
        with patch("threading.Thread") as mock_thread:
            handler(self._key_evt("up"))
        self.assertEqual(app.state, AppState.PROCESSING)
        mock_thread.assert_called()

    @patch("main.Config.HOTKEY_TOGGLE", True)
    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_hotkey_toggle_does_not_steal_live_session(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        mock_keyboard.KEY_UP = "up"
        mock_keyboard.KEY_DOWN = "down"
        mock_keyboard.lock_is_held.return_value = True
        mock_keyboard.is_pressed.side_effect = lambda k: k == "ctrl"
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.LIVE_HOTKEY", "f7"), patch.object(
            Config, "effective_live_stt_provider", return_value="whisper"
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.on_live_toggle()
            self.assertTrue(app.live_active)
            self.assertEqual(app.state, AppState.RECORDING)

            handler = self._grave_handler(mock_keyboard)
            with patch("threading.Thread") as mock_thread:
                handler(self._key_evt("down"))
                mock_thread.assert_not_called()
            self.assertTrue(app.live_active)
            self.assertEqual(app.state, AppState.RECORDING)

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_toggle_tap_to_talk(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.LIVE_HOTKEY", "f7"), patch.object(
            Config, "STT_PROVIDER", "whisper"
        ), patch.object(
            Config, "effective_stt_provider", return_value="whisper"
        ), patch.object(
            Config, "effective_live_stt_provider", return_value="whisper"
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.recorder.stop.return_value = True
            fake_audio = np.zeros(1600, dtype=np.float32)
            app.recorder.last_audio_array = fake_audio

            app.on_live_toggle()
            self.assertEqual(app.state, AppState.RECORDING)
            self.assertTrue(app.live_active)
            self.assertFalse(app.use_llm)
            app.recorder.start.assert_called()

            app._record_started_at = 0.0
            with patch("threading.Thread") as mock_thread:
                app.on_live_toggle()
            self.assertEqual(app.state, AppState.PROCESSING)
            self.assertFalse(app.live_active)
            mock_thread.assert_called()
            pipeline_call = mock_thread.call_args
            args = pipeline_call.kwargs.get("args") if hasattr(pipeline_call, "kwargs") else pipeline_call[1].get("args")
            self.assertEqual(args[1], False)  # use_llm
            self.assertEqual(args[4], "")  # no live transcript when STT is whisper

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_process_and_paste_uses_pre_transcript(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.transcriber.transcribe.side_effect = AssertionError(
                "unary STT should be skipped when live text is present"
            )
            app.process_and_paste(
                np.zeros(100, dtype=np.float32),
                False,
                "",
                False,
                "already transcribed",
            )
            mock_paste_text.assert_called_once_with("already transcribed")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_stop_skips_stt_when_caret_has_text(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch.object(Config, "STT_PROVIDER", "whisper"), patch.object(
            Config, "effective_stt_provider", return_value="whisper"
        ), patch.object(Config, "LIVE_POLISH", False):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
            app.on_live_toggle()
            self.assertTrue(app.live_active)
            app._record_started_at = 0.0
            with app._live_caret_lock:
                app._live_caret_current = "hello from live"
                app._live_caret_desired = "hello from live"
            with patch("threading.Thread") as mock_thread:
                app.on_live_toggle()
            self.assertFalse(app.live_active)
            self.assertEqual(app.state, AppState.IDLE)
            self.assertEqual(app.last_status, "success")
            app.transcriber.transcribe.assert_not_called()
            for call in mock_thread.call_args_list:
                target = call[1].get("target") if call[1] else None
                if target is None and call[0]:
                    target = call[0][0]
                self.assertNotEqual(getattr(target, "__name__", ""), "process_and_paste")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_stop_does_not_join_session_on_hook_thread(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch.object(Config, "STT_PROVIDER", "whisper"), patch.object(
            Config, "effective_stt_provider", return_value="whisper"
        ), patch.object(Config, "LIVE_POLISH", False):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
            session = MagicMock()
            app._live_session = session
            app.live_active = True
            app.state = AppState.RECORDING
            app._record_started_at = 0.0
            with app._live_caret_lock:
                app._live_caret_current = "hello from live"
                app._live_caret_desired = "hello from live"
            with patch("threading.Thread") as mock_thread:
                app.on_live_toggle()
            session.stop.assert_not_called()
            self.assertEqual(app.state, AppState.IDLE)
            self.assertEqual(app.last_status, "success")
            target = (
                mock_thread.call_args[1].get("target")
                if mock_thread.call_args[1]
                else mock_thread.call_args[0][0]
            )
            self.assertEqual(getattr(target, "__name__", ""), "_cleanup_live_session")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_cleanup_applies_api_final(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            session = MagicMock()
            session.stop.return_value = "hello world"
            with app._live_caret_lock:
                app._live_caret_current = "hello"
                app._live_caret_desired = "hello"
            app._cleanup_live_session(session, app._live_epoch)
            # On-screen text is kept intact without post-stop mutation
            self.assertEqual(app._live_caret_desired, "hello")
            session.stop.return_value = "totally different sentence"
            with app._live_caret_lock:
                app._live_caret_current = "But why the money"
                app._live_caret_desired = "But why the money"
            app._cleanup_live_session(session, app._live_epoch)
            self.assertEqual(app._live_caret_desired, "But why the money")
            session.stop.return_value = ""
            with app._live_caret_lock:
                app._live_caret_current = "keep me"
                app._live_caret_desired = "keep me"
            app._cleanup_live_session(session, app._live_epoch)
            self.assertEqual(app._live_caret_desired, "keep me")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_finish_live_session_branches(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """_finish_live_session handles on-screen text, stream fallback, and empty clips."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True

            # 1. On-screen text present -> keep on-screen text, success status
            session = MagicMock()
            session.stop.return_value = "streamed words"
            with app._live_caret_lock:
                app._live_caret_desired = "on-screen text"
            app._finish_live_session(session, None, app._live_epoch)
            self.assertEqual(app.last_status, "success")

            # 2. On-screen empty, but session.stop has text -> processes and pastes
            with app._live_caret_lock:
                app._live_caret_desired = ""
                app._live_caret_current = ""
            with patch.object(app, "process_and_paste") as mock_pap:
                app._finish_live_session(session, np.zeros(100), app._live_epoch)
                mock_pap.assert_called_once()

            # 3. Stale epoch -> discarded immediately
            with patch.object(app, "process_and_paste") as mock_pap:
                app._finish_live_session(session, None, app._live_epoch + 99)
                mock_pap.assert_not_called()

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_stop_routes_to_polish(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """F7 stop with on-screen text spawns the polish final off the hook thread."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch.object(Config, "STT_PROVIDER", "whisper"), patch.object(
            Config, "effective_stt_provider", return_value="whisper"
        ), patch.object(
            Config, "effective_live_stt_provider", return_value="gemini"
        ), patch("main.GeminiLiveSession") as mock_live_cls:
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(1600, dtype=np.float32)
            session = MagicMock()
            mock_live_cls.return_value = session
            app.on_live_toggle()
            self.assertTrue(app.live_active)
            self.assertIs(app._live_session, session)
            app._record_started_at = 0.0
            with app._live_caret_lock:
                app._live_caret_current = "raw draft"
                app._live_caret_desired = "raw draft"
            with patch("threading.Thread") as mock_thread:
                app.on_live_toggle()
            self.assertFalse(app.live_active)
            self.assertEqual(app.state, AppState.PROCESSING)
            self.assertEqual(app.last_status, "success")
            session.stop.assert_not_called()
            target = (
                mock_thread.call_args[1].get("target")
                if mock_thread.call_args[1]
                else mock_thread.call_args[0][0]
            )
            self.assertEqual(getattr(target, "__name__", ""), "_polish_live_session")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_polish_swaps_caret_text(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """_polish_caret_text replaces the streamed draft with the smart final."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            with app._live_caret_lock:
                app._live_caret_current = "raw rambling draft"
                app._live_caret_desired = "raw rambling draft"
            polisher = MagicMock()
            polisher.transcribe.return_value = "clean smart final"
            app._polish_transcriber = polisher
            with patch.object(app, "_flush_live_caret"):
                self.assertTrue(
                    app._polish_caret_text(
                        np.zeros(100, dtype=np.float32), app._live_epoch
                    )
                )
            polisher.transcribe.assert_called_once()
            self.assertEqual(app._live_caret_desired, "clean smart final")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_polish_failure_keeps_draft(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """No polish result (error / empty / stale epoch / no audio) keeps the draft."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            with app._live_caret_lock:
                app._live_caret_current = "kept draft"
                app._live_caret_desired = "kept draft"
            polisher = MagicMock()
            polisher.transcribe.side_effect = RuntimeError("API down")
            app._polish_transcriber = polisher
            with patch.object(app, "_flush_live_caret"):
                self.assertFalse(
                    app._polish_caret_text(
                        np.zeros(100, dtype=np.float32), app._live_epoch
                    )
                )
            self.assertEqual(app._live_caret_desired, "kept draft")
            polisher.transcribe.side_effect = None
            polisher.transcribe.return_value = "   "
            with patch.object(app, "_flush_live_caret"):
                self.assertFalse(
                    app._polish_caret_text(
                        np.zeros(100, dtype=np.float32), app._live_epoch
                    )
                )
            self.assertEqual(app._live_caret_desired, "kept draft")
            stale = MagicMock()
            stale.transcribe.return_value = "other text"
            app._polish_transcriber = stale
            with patch.object(app, "_flush_live_caret"):
                self.assertFalse(
                    app._polish_caret_text(
                        np.zeros(100, dtype=np.float32), app._live_epoch + 9
                    )
                )
            stale.transcribe.assert_not_called()
            self.assertFalse(app._polish_caret_text(None, app._live_epoch))
            stale.transcribe.assert_not_called()

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_live_polish_disabled_keeps_streamed_text(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """LIVE_POLISH=false restores the keep-the-draft stop behavior."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            session = MagicMock()
            session.stop.return_value = "live finals"
            polisher = MagicMock()
            app._polish_transcriber = polisher
            with app._live_caret_lock:
                app._live_caret_desired = "streamed draft"
            with patch.object(Config, "LIVE_POLISH", False):
                app._finish_live_session(session, np.zeros(100), app._live_epoch)
            self.assertEqual(app._live_caret_desired, "streamed draft")
            self.assertEqual(app.last_status, "success")
            polisher.transcribe.assert_not_called()

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_polish_live_session_flow(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_transcriber: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        """_polish_live_session stops the socket, swaps the text, and tears down."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            session = MagicMock()
            polisher = MagicMock()
            polisher.transcribe.return_value = "smart final"
            app._polish_transcriber = polisher
            with app._live_caret_lock:
                app._live_caret_current = "streamed draft"
                app._live_caret_desired = "streamed draft"
            with patch.object(app, "_flush_live_caret"):
                app._polish_live_session(session, np.zeros(100), app._live_epoch)
            session.stop.assert_called_once()
            self.assertEqual(app._live_caret_desired, "smart final")
            self.assertEqual(app.last_status, "success")
            self.assertEqual(app.state, AppState.IDLE)
            self.assertTrue(app._live_cleanup_done.is_set())

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.GeminiTranscriber")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_ai_chord_uses_local_whisper_not_gemini_smart(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_whisper: MagicMock,
        mock_gemini: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch.object(Config, "STT_PROVIDER", "gemini"), patch.object(
            Config, "effective_stt_provider", return_value="gemini"
        ), patch.object(Config, "LLM_PROVIDER", "gemini"):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            mock_get_selected_text.return_value = ""
            mock_whisper.return_value.transcribe.return_value = "ask the model"
            app.refiner.refine.return_value = "A reply."
            app.process_and_paste(
                np.zeros(100, dtype=np.float32), True, "", False, ""
            )
            mock_whisper.return_value.transcribe.assert_called()
            mock_gemini.return_value.transcribe.assert_not_called()
            mock_paste_text.assert_called_once_with("A reply.")

    @patch("main.Config.HOTKEY", "ctrl+grave")
    @patch("main.Config.AI_HOTKEY", "ctrl+shift+grave")
    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.GeminiTranscriber")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.get_selected_text")
    @patch("main.platforms")
    @patch("main.play_beep")
    def test_ai_chord_falls_back_to_gemini_verbatim(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
        mock_get_selected_text: MagicMock,
        mock_paste_text: MagicMock,
        mock_refiner: MagicMock,
        mock_whisper: MagicMock,
        mock_gemini: MagicMock,
        mock_recorder: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch.object(Config, "STT_PROVIDER", "gemini"), patch.object(
            Config, "effective_stt_provider", return_value="gemini"
        ), patch.object(Config, "LLM_PROVIDER", "gemini"):
            with patch("threading.Thread"):
                app = DictationApp()
                app.initialize_app()
            app.ready = True
            mock_get_selected_text.return_value = ""
            mock_whisper.return_value.transcribe.side_effect = RuntimeError("no model")
            mock_gemini.return_value.transcribe.return_value = "verbatim words"
            app.refiner.refine.return_value = "A reply."
            app.process_and_paste(
                np.zeros(100, dtype=np.float32), True, "", False, ""
            )
            mock_gemini.return_value.transcribe.assert_called()
            self.assertEqual(
                mock_gemini.return_value.transcribe.call_args.kwargs.get("mode"),
                "verbatim",
            )
            mock_paste_text.assert_called_once_with("A reply.")

    def test_apply_live_text_edits_tail_only(self) -> None:
        from typer import apply_live_text

        with patch("typer.send_backspaces") as mock_bs, patch(
            "typer.paste_text"
        ) as mock_paste, patch("typer.force_release_modifiers"), patch(
            "typer.send_text", return_value=True
        ) as mock_type:
            out = apply_live_text("hello wo", "hello world")
            self.assertEqual(out, "hello world")
            mock_bs.assert_not_called()
            mock_type.assert_called_once_with("rld")
            mock_paste.assert_not_called()
            mock_type.reset_mock()
            apply_live_text("hello world", "hello")
            mock_bs.assert_called_once_with(6)
            mock_paste.assert_not_called()
            mock_type.assert_not_called()

    def test_apply_live_text_falls_back_to_paste(self) -> None:
        from typer import apply_live_text

        with patch("typer.send_backspaces"), patch(
            "typer.paste_text"
        ) as mock_paste, patch("typer.force_release_modifiers"), patch(
            "typer.send_text", return_value=False
        ):
            apply_live_text("", "hello")
            mock_paste.assert_called_once_with("hello", restore_clipboard=False)

    def test_get_selected_text_retries_when_sentinel_stuck(self) -> None:
        from typer import get_selected_text

        sentinel_holder = {"v": ""}

        def fake_read() -> str:
            return sentinel_holder["v"]

        def fake_write(text: str) -> bool:
            sentinel_holder["v"] = text
            return True

        reads_after_copy = {"n": 0}

        def fake_copy() -> None:
            reads_after_copy["n"] += 1
            if reads_after_copy["n"] >= 2:
                sentinel_holder["v"] = "highlighted line"

        with patch("typer._clipboard_read", side_effect=fake_read), patch(
            "typer._clipboard_write", side_effect=fake_write
        ), patch("typer.wm_copy_foreground", return_value=False), patch(
            "typer.send_copy", side_effect=fake_copy
        ), patch("typer._wait_modifiers_up"), patch("typer.time.sleep"):
            self.assertEqual(get_selected_text(timeout=0.05), "highlighted line")

    def test_paste_text_skips_restore_when_asked(self) -> None:
        with patch("typer._clipboard_read", return_value="user clip"), patch(
            "typer._clipboard_write"
        ) as mock_write, patch("typer.send_paste"), patch(
            "typer.force_release_modifiers"
        ), patch("typer.time.sleep"):
            paste_text("live", restore_clipboard=False)
            written = [c[0][0] for c in mock_write.call_args_list]
            self.assertIn("live", written)
            self.assertNotIn("user clip", written)

    def test_paste_text_types_into_terminal_without_touching_clipboard(self) -> None:
        """A focused terminal is typed into: no paste chord, no clipboard churn."""
        with patch("typer._terminal_target", return_value=True), patch(
            "typer.send_text_bulk", return_value=True
        ) as mock_type, patch("typer.send_paste") as mock_paste, patch(
            "typer._clipboard_write_verified"
        ) as mock_write, patch("typer._wait_modifiers_up"), patch("typer.time.sleep"):
            paste_text("hello terminal")

        mock_type.assert_called_once_with("hello terminal")
        mock_paste.assert_not_called()
        mock_write.assert_not_called()

    def test_paste_text_falls_back_to_chord_when_terminal_typing_fails(self) -> None:
        """Typing can fail; the clipboard chord is still the safety net."""
        state = {"clip": "original"}

        def fake_write(text: str) -> bool:
            state["clip"] = text
            return True

        with patch("typer._terminal_target", return_value=True), patch(
            "typer.send_text_bulk", return_value=False
        ), patch("typer._clipboard_read", side_effect=lambda: state["clip"]), patch(
            "typer._clipboard_write", side_effect=fake_write
        ), patch("typer.send_paste") as mock_paste, patch(
            "typer._wait_modifiers_up"
        ), patch("typer.time.sleep"):
            paste_text("payload")

        mock_paste.assert_called()
        self.assertEqual(state["clip"], "original")

    def test_terminal_detection_respects_config_toggle(self) -> None:
        import typer

        with patch("typer.foreground_is_terminal", return_value=True), patch.object(
            Config, "TYPE_IN_TERMINAL", False
        ):
            self.assertFalse(typer._terminal_target())
        with patch("typer.foreground_is_terminal", return_value=True):
            self.assertTrue(typer._terminal_target())

    def test_terminal_detection_failure_defaults_to_paste(self) -> None:
        """An unreadable foreground window must not disable pasting everywhere."""
        import typer

        with patch("typer.foreground_is_terminal", side_effect=OSError("no display")):
            self.assertFalse(typer._terminal_target())

    @patch("typer.force_release_modifiers")
    @patch("typer.wm_copy_foreground", return_value=False)
    @patch("typer.send_copy_terminal")
    @patch("typer.send_copy")
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_get_selected_text_uses_terminal_copy_chord(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_copy: MagicMock,
        mock_send_copy_terminal: MagicMock,
        mock_wm: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        """In a terminal the probe sends Ctrl+Shift+C; plain Ctrl+C is SIGINT."""
        last_written = {"v": ""}

        def fake_write(v: str) -> bool:
            last_written["v"] = v
            return True

        def fake_read() -> str:
            if "odicto-sel-" in (last_written["v"] or ""):
                return "terminal selection"
            return last_written["v"]

        mock_clipboard_write.side_effect = fake_write
        mock_clipboard_read.side_effect = fake_read

        with patch("typer.foreground_is_terminal", return_value=True):
            result = get_selected_text(timeout=0.15)

        self.assertEqual(result, "terminal selection")
        mock_send_copy_terminal.assert_called()
        mock_send_copy.assert_not_called()

    def test_indicator_reset_label(self) -> None:
        """F5 reset shows a distinct HUD label."""
        from indicator import GuiState, status_label

        self.assertEqual(status_label(GuiState.RESET), "Context cleared")


class TestDictationIndicator(unittest.TestCase):
    """Premium Qt HUD — pure helpers + lightweight widget state machine tests."""

    def test_status_labels(self) -> None:
        from indicator import GuiState, status_label

        self.assertEqual(status_label(GuiState.BOOTING), "Starting")
        self.assertEqual(status_label(GuiState.RECORDING, use_llm=False), "Listening")
        # AI mode uses the same label; a separate violet "AI" chip is drawn in the HUD
        self.assertEqual(status_label(GuiState.RECORDING, use_llm=True), "Listening")
        self.assertEqual(status_label(GuiState.PROCESSING, use_llm=False), "Transcribing")
        self.assertEqual(status_label(GuiState.PROCESSING, use_llm=True), "Thinking")
        self.assertEqual(status_label(GuiState.SUCCESS), "Done")
        self.assertEqual(
            status_label(GuiState.ERROR, last_status="empty"), "No speech"
        )
        self.assertEqual(
            status_label(GuiState.ERROR, last_status="error"), "Failed"
        )
        self.assertEqual(status_label(GuiState.RESET), "Context cleared")

    def test_indicator_state_machine(self) -> None:
        """Create a real offscreen QWidget and drive state transitions."""
        from PySide6.QtWidgets import QApplication
        from indicator import DictationIndicator, GuiState
        from app_state import AppState

        qt = QApplication.instance() or QApplication([])

        mock_app = MagicMock()
        mock_app.state = AppState.IDLE
        mock_app.last_status = None
        mock_app.use_llm = False
        mock_app.ready = False
        mock_app.recorder = None

        indicator = DictationIndicator(mock_app)
        self.assertEqual(indicator.gui_state, GuiState.BOOTING)

        # Ready + idle → hide
        mock_app.ready = True
        indicator._sync_from_app()
        self.assertEqual(indicator.gui_state, GuiState.HIDDEN)

        # Recording
        mock_app.state = AppState.RECORDING
        indicator._sync_from_app()
        self.assertEqual(indicator.gui_state, GuiState.RECORDING)

        # Processing
        mock_app.state = AppState.PROCESSING
        mock_app.use_llm = True
        indicator._sync_from_app()
        self.assertEqual(indicator.gui_state, GuiState.PROCESSING)

        # Success
        mock_app.state = AppState.IDLE
        mock_app.last_status = "success"
        indicator._sync_from_app()
        self.assertEqual(indicator.gui_state, GuiState.SUCCESS)

        # Error / empty
        mock_app.state = AppState.PROCESSING
        indicator._sync_from_app()
        mock_app.state = AppState.IDLE
        mock_app.last_status = "empty"
        indicator._sync_from_app()
        self.assertEqual(indicator.gui_state, GuiState.ERROR)

        # Hide request
        indicator._do_hide()
        self.assertEqual(indicator.gui_state, GuiState.HIDDEN)
        self.assertEqual(indicator._appear_target, 0.0)

        indicator._tick.stop()
        indicator.close()
        # Keep qt app alive for other tests; do not quit.

    def test_live_layout_requires_explicit_flag(self) -> None:
        from PySide6.QtWidgets import QApplication
        from indicator import DictationIndicator, GuiState
        from app_state import AppState

        QApplication.instance() or QApplication([])
        mock_app = MagicMock()
        mock_app.state = AppState.RECORDING
        mock_app.last_status = None
        mock_app.use_llm = False
        mock_app.ready = True
        mock_app.recorder = None
        mock_app.live_active = False
        mock_app.live_preview = ""
        indicator = DictationIndicator(mock_app)
        indicator.gui_state = GuiState.RECORDING
        self.assertFalse(indicator._is_live_layout())
        mock_app.live_active = True
        # Live captions go to the caret; HUD stays a one-row listening pill.
        self.assertFalse(indicator._is_live_layout())
        indicator._tick.stop()
        indicator.close()


class TestCrossPlatform(unittest.TestCase):
    """Facade dispatch, env merge, and provider-test helpers."""

    def test_terminal_identifier_matches_classes_processes_and_extras(self) -> None:
        from platforms.base import is_terminal_identifier

        # Window classes first (the strongest Windows signal), then processes.
        self.assertTrue(is_terminal_identifier(("CASCADIA_HOSTING_WINDOW_CLASS", "")))
        self.assertTrue(is_terminal_identifier(("ConsoleWindowClass", "")))
        self.assertTrue(is_terminal_identifier(("", "WindowsTerminal.exe")))
        self.assertTrue(is_terminal_identifier(("", "mintty.exe")))
        self.assertTrue(is_terminal_identifier(("", "gnome-terminal-server")))
        # macOS bundle ids.
        self.assertTrue(is_terminal_identifier(("", "com.googlecode.iterm2")))
        # EXTRA_TERMINAL_APPS is the user's escape hatch for an unknown terminal.
        self.assertTrue(is_terminal_identifier(("My-Term",), ("my-term",)))
        self.assertFalse(is_terminal_identifier(("Chrome_WidgetWin_1", "chrome.exe")))
        self.assertFalse(is_terminal_identifier(("SunAwtFrame", "idea64.exe")))
        self.assertFalse(is_terminal_identifier(("", "")))

    def test_terminal_config_shape(self) -> None:
        self.assertIsInstance(Config.TYPE_IN_TERMINAL, bool)
        self.assertIsInstance(Config.EXTRA_TERMINAL_APPS, tuple)

    def test_kill_other_skips_venv_parent(self) -> None:
        """The Windows venv launcher stub must not be taskkilled /T (self-kill)."""
        if sys.platform == "win32":
            import platforms.windows as backend
            enumerate_name = "_enumerate_odicto_pids"
        else:
            import platforms._posix as backend
            enumerate_name = "enumerate_odicto_pids"

        handle, path = tempfile.mkstemp()
        os.close(handle)
        try:
            with open(path, "w", encoding="ascii") as f:
                f.write("99")
            with patch.object(backend, enumerate_name, return_value=set()), patch.object(
                backend.os, "getpid", return_value=100
            ), patch.object(backend.os, "getppid", return_value=99), patch.object(
                backend.subprocess, "run"
            ) as mock_run:
                killed = backend.kill_other_odicto_processes(path)
            self.assertEqual(killed, [])
            mock_run.assert_not_called()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_kill_other_still_kills_foreign_pid(self) -> None:
        if sys.platform == "win32":
            import platforms.windows as backend
            enumerate_name = "_enumerate_odicto_pids"
        else:
            import platforms._posix as backend
            enumerate_name = "enumerate_odicto_pids"

        with patch.object(backend, enumerate_name, return_value={333}), patch.object(
            backend.os, "getpid", return_value=100
        ), patch.object(backend.os, "getppid", return_value=99), patch.object(
            backend.subprocess, "run"
        ) as mock_run:
            if sys.platform != "win32":
                with patch.object(backend, "kill_process_tree") as mock_tree:
                    killed = backend.kill_other_odicto_processes(None)
                    mock_tree.assert_called_once_with(333)
            else:
                killed = backend.kill_other_odicto_processes(None)
                mock_run.assert_called()
        self.assertEqual(killed, [333])

    def test_keyboard_backend_normalizes_aliases(self) -> None:
        from config import normalize_key_name

        self.assertEqual(normalize_key_name("`"), "grave")
        self.assertEqual(normalize_key_name("backtick"), "grave")
        self.assertEqual(normalize_key_name("ctrl"), "ctrl")
        self.assertEqual(normalize_key_name("Ctrl"), "ctrl")

    def test_validate_hotkey_pair_rejects_bare_primary(self) -> None:
        from config import validate_hotkey_pair

        with self.assertRaises(ValueError):
            validate_hotkey_pair("a", "ctrl+a")

    def test_validate_hotkey_pair_rejects_mismatched_primary(self) -> None:
        from config import validate_hotkey_pair

        with self.assertRaises(ValueError):
            validate_hotkey_pair("ctrl+a", "ctrl+b")

    def test_validate_hotkey_pair_rejects_identical_mods(self) -> None:
        from config import validate_hotkey_pair

        with self.assertRaises(ValueError):
            validate_hotkey_pair("ctrl+a", "ctrl+a")

    def test_validate_hotkey_pair_accepts_valid(self) -> None:
        from config import validate_hotkey_pair

        validate_hotkey_pair("ctrl+a", "ctrl+shift+a")

    @patch("platforms.base.pyperclip")
    def test_base_clipboard_write_masks_none(self, mock_pyperclip: MagicMock) -> None:
        from platforms import base

        self.assertTrue(base.clipboard_write("x"))
        mock_pyperclip.copy.assert_called_once_with("x")

    def test_setup_web_parse_and_mask(self) -> None:
        from setup_web import _parse_env_text, _mask_key

        parsed = _parse_env_text("LLM_PROVIDER=meta\nMETA_API_KEY=abc\n# comment\n")
        self.assertEqual(parsed["LLM_PROVIDER"], "meta")
        self.assertEqual(parsed["META_API_KEY"], "abc")
        self.assertTrue(_mask_key("META_API_KEY"))
        self.assertTrue(_mask_key("GEMINI_API_KEY"))
        self.assertFalse(_mask_key("GOOGLE_API_KEY"))
        self.assertFalse(_mask_key("LLM_PROVIDER"))

    def test_setup_web_merge_env_quotes_system_prompt(self) -> None:
        import setup_web

        with patch.object(setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")):
            try:
                with open(setup_web.ENV_PATH, "w") as f:
                    f.write("LLM_PROVIDER=meta\nSYSTEM_PROMPT=\n")
                setup_web.merge_env({"SYSTEM_PROMPT": "Line one\nLine two"})
                with open(setup_web.ENV_PATH) as f:
                    text = f.read()
                self.assertIn('SYSTEM_PROMPT="Line one\\nLine two"', text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_setup_web_switching_provider_preserves_saved_keys(self) -> None:
        """Save-under-new-provider must keep every other provider's stored key.

        The browser round-trips stored secrets as a masked sentinel; merge_env
        must skip it. This is the 'never ask for the same key twice' guarantee.
        """
        import setup_web

        with patch.object(setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")):
            try:
                with open(setup_web.ENV_PATH, "w", encoding="utf-8") as f:
                    f.write("LLM_PROVIDER=gemini\nGEMINI_API_KEY=AIza-stored-key\n")
                # User flips the dropdown to meta, types a Meta key, and leaves
                # the Gemini field untouched (browser submits the mask).
                setup_web.merge_env({
                    "LLM_PROVIDER": "meta",
                    "META_API_KEY": "sk-meta-new",
                    "GEMINI_API_KEY": setup_web._MASKED,
                    "OPENROUTER_API_KEY": "",
                })
                with open(setup_web.ENV_PATH, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("LLM_PROVIDER=meta", text)
                self.assertIn("META_API_KEY=sk-meta-new", text)
                self.assertIn("GEMINI_API_KEY=AIza-stored-key", text)
                self.assertNotIn(setup_web._MASKED, text)
                # Template anchors the slot (blank = unset) so it still appears.
                self.assertIn("OPENROUTER_API_KEY=", text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_setup_web_merge_env_writes_hotkey_toggle(self) -> None:
        import setup_web

        with patch.object(setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")):
            try:
                with open(setup_web.ENV_PATH, "w", encoding="utf-8") as f:
                    f.write("LLM_PROVIDER=none\n")
                setup_web.merge_env({"HOTKEY_TOGGLE": "false"})
                with open(setup_web.ENV_PATH, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("HOTKEY_TOGGLE=false", text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_setup_web_page_includes_system_prompt(self) -> None:
        import setup_web

        html_page = setup_web._page()
        self.assertIn('name="SYSTEM_PROMPT"', html_page)
        self.assertIn("PLAIN HUMAN-READABLE TEXT", html_page)
        self.assertIn("var DEFAULT_SYSTEM_PROMPT =", html_page)
        self.assertIn('name="STT_PROVIDER"', html_page)
        self.assertIn('name="GEMINI_TRANSCRIBE_MODE"', html_page)
        self.assertIn("stt_mode_toggle", html_page)
        self.assertIn('id="stt-gemini-fields"', html_page)
        self.assertIn("function syncSttProvider", html_page)
        self.assertIn('name="LIVE_HOTKEY"', html_page)
        self.assertIn('name="HOTKEY_TOGGLE"', html_page)
        self.assertIn("gearbox", html_page)
        self.assertIn("Long ride", html_page)
        self.assertIn("Short ride", html_page)
        self.assertIn("function syncTranscribeMode", html_page)
        self.assertIn('id="prompt_slot"', html_page)
        self.assertIn('id="prompt_overlay"', html_page)
        self.assertIn("prompt-panel", html_page)
        self.assertNotIn("autoGrow(promptEl)", html_page)
        self.assertNotIn('name="SYSTEM_PROMPT_FILE"', html_page)
        self.assertIn("prompt.txt", html_page)
        self.assertIn("prompt.txt.example", html_page)
        self.assertIn("restarts Odicto", html_page)
        from config import ENV_DEFAULTS as _env_defaults
        self.assertIn(_env_defaults["META_MODEL"], html_page)
        self.assertIn("MODEL_DEFAULTS.meta", html_page)
        self.assertIn('name="OPENROUTER_REASONING_EFFORT"', html_page)
        self.assertIn('name="OPENROUTER_PROVIDER_SORT"', html_page)
        self.assertIn('id="openrouter_reasoning_select"', html_page)
        self.assertIn('id="openrouter_sort_select"', html_page)
        self.assertIn("Backend default (none)", html_page)
        self.assertIn("Backend default (latency)", html_page)
        with open(setup_web.__file__, encoding="utf-8") as src_file:
            setup_src = src_file.read()
        self.assertNotIn("muse-spark-1.2", setup_src)

    def test_restart_odicto_stops_then_starts(self) -> None:
        import setup_web

        with patch("platforms.kill_other_odicto_processes") as mock_kill, patch(
            "platforms.spawn_detached"
        ) as mock_spawn, patch("os.path.isfile", return_value=True):
            msg = setup_web.restart_odicto()
        mock_kill.assert_called_once()
        mock_spawn.assert_called_once()
        args = mock_spawn.call_args[0][0]
        self.assertTrue(str(args[1]).endswith("main.py"))
        self.assertIn("restarting", msg.lower())

    def test_config_system_prompt_falls_back_to_default(self) -> None:
        from config import DEFAULT_SYSTEM_PROMPT, Config

        self.assertTrue(Config.SYSTEM_PROMPT)
        self.assertIn("PLAIN HUMAN-READABLE TEXT", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("personal assistant", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Do not transcribe", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("em dash", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Do not start with a list", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Never repeat the instruction back", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Selected text", DEFAULT_SYSTEM_PROMPT)
        self.assertNotIn("\u2014", DEFAULT_SYSTEM_PROMPT)
        self.assertNotIn("- item", DEFAULT_SYSTEM_PROMPT.replace('"- item"', ""))

    def test_setup_web_merge_env_preserves_and_updates(self) -> None:
        """Template-anchored merge: positions stay, unknowns survive under marker."""
        import setup_web

        with patch.object(setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")):
            try:
                with open(setup_web.ENV_PATH, "w") as f:
                    f.write("# keep me\nLLM_PROVIDER=meta\nCUSTOM_KEY=keep\n")
                setup_web.merge_env({"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-test"})
                with open(setup_web.ENV_PATH) as f:
                    text = f.read()
                self.assertIn("LLM_PROVIDER=openrouter", text)
                self.assertIn("OPENROUTER_API_KEY=sk-or-test", text)
                setup_web.merge_env(
                    {
                        "OPENROUTER_REASONING_EFFORT": "none",
                        "OPENROUTER_PROVIDER_SORT": "latency",
                    }
                )
                with open(setup_web.ENV_PATH) as f:
                    text = f.read()
                self.assertIn("OPENROUTER_REASONING_EFFORT=none", text)
                self.assertIn("OPENROUTER_PROVIDER_SORT=latency", text)
                # Unknown keys are appended under the preservation marker.
                self.assertIn("CUSTOM_KEY=keep", text)
                self.assertIn("kept from your previous", text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_setup_web_reset_env_writes_example(self) -> None:
        import tempfile

        import setup_web

        with tempfile.TemporaryDirectory() as root, patch.object(
            setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")
        ), patch.object(
            setup_web, "ENV_EXAMPLE_PATH", new=os.path.join(os.getcwd(), ".env.example")
        ), patch("config._prompt_dir", return_value=root):
            try:
                live = os.path.join(root, "prompt.txt")
                with open(live, "w", encoding="utf-8") as f:
                    f.write("private prompt\n")
                with open(setup_web.ENV_PATH, "w", encoding="utf-8") as f:
                    f.write("LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=sk-or-test\n")
                setup_web.reset_env()
                with open(setup_web.ENV_PATH, encoding="utf-8") as f:
                    text = f.read()
                self.assertNotIn("sk-or-test", text)
                self.assertIn("LLM_PROVIDER=", text)
                self.assertFalse(os.path.exists(live))
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_refiner_test_provider_none(self) -> None:
        from refiner import test_provider

        self.assertEqual(test_provider("none", "", "", ""), "ok")

    def test_refiner_test_provider_gemini_needs_key(self) -> None:
        from refiner import test_provider

        self.assertIn("GEMINI_API_KEY", test_provider("gemini", "", "gemini-3.7-flash", ""))

    def test_setup_web_page_js_parses(self) -> None:
        """The rendered page's JS must be valid. A Python f-string escape
        bug once emitted a raw newline inside a string literal, breaking the
        whole script: the provider dropdown died and the key fields stayed
        hidden."""
        import re
        import shutil
        import subprocess
        import tempfile

        import setup_web

        node = shutil.which("node")
        if not node:
            self.skipTest("node not available for JS syntax check")

        page = setup_web._page()
        match = re.search(r"<script>(.*?)</script>", page, re.S)
        self.assertIsNotNone(match, "page must contain a script block")
        js = match.group(1)
        self.assertIn("initCustomSelect();", js)
        self.assertIn("function showProvider", js)
        self.assertIn("function syncSttProvider", js)
        self.assertIn("function togglePromptExpand", js)
        self.assertIn("function initGearbox", js)
        self.assertIn("function setHotkeyToggle", js)
        self.assertIn("OPENROUTER_REASONING_EFFORT", js)
        self.assertIn("OPENROUTER_PROVIDER_SORT", js)
        self.assertIn("function filterOpenrouterEffort", js)
        self.assertIn("function loadOpenrouterCatalog", js)
        self.assertIn("/openrouter-models", js)
        self.assertIn("function syncPromptPanelHeight", js)
        self.assertIn("function setTestTag", js)
        self.assertIn("test_tag", page)

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            path = f.name
        try:
            result = subprocess.run(
                [node, "--check", path], capture_output=True, text=True
            )
            self.assertEqual(
                result.returncode, 0, f"JS syntax error: {result.stderr}"
            )
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def test_refiner_test_provider_unknown(self) -> None:
        from refiner import test_provider

        self.assertIn("Unknown provider", test_provider("bogus", "", "", ""))

    def test_odicto_status_reports_backend(self) -> None:
        import platforms

        self.assertIn(platforms.hotkey_backend_name(), ("keyboard", "pynput"))


class TestEnvExampleParity(unittest.TestCase):
    """Keeps .env.example and the built-in defaults from drifting apart.

    The promise: a commented-out line means "use the default shown in the
    comment", so an uncommented value must EQUAL the code default, every
    documented key must be one the app actually reads, and vice versa.
    """

    EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example")
    PROMPT_EXAMPLE_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "prompt.txt.example"
    )

    @classmethod
    def setUpClass(cls) -> None:
        with open(cls.EXAMPLE_PATH, encoding="utf-8") as f:
            cls.lines = f.read().splitlines()
        import re

        cls.key_re = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")

    def _all_keys(self) -> list:
        keys = []
        for line in self.lines:
            m = self.key_re.match(line)
            if m:
                keys.append(m.group(1))
        return keys

    def test_uncommented_values_equal_builtin_defaults(self) -> None:
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            self.assertIn(key, config.KNOWN_ENV_KEYS, f"undocumented key {key}")
            expected = config.ENV_DEFAULTS[key]
            actual = value.split(" #")[0].strip().strip('"').strip("'")
            # ``KEY=`` (blank) means "use the built-in default" — the cascade
            # treats it like a commented line, so it inherently matches.
            if actual == "":
                continue
            # ``*_MODEL`` defaults live in OPENROUTER_FALLBACK_MODEL / provider
            # defaults, not in ENV_DEFAULTS. The .env value is the user's
            # current selection (grown via the setup page), not a parity check.
            if key in ("OPENROUTER_MODEL", "OPENROUTER_MODEL_HISTORY", "GEMINI_MODEL", "GEMINI_MODEL_HISTORY", "META_MODEL", "META_MODEL_HISTORY", "OLLAMA_MODEL", "OLLAMA_MODEL_HISTORY"):
                continue
            self.assertEqual(
                actual,
                expected,
                f".env.example ships {key}={actual!r} but the built-in default "
                f"is {expected!r} — update one of them (they must match).",
            )

    def test_every_known_key_is_documented(self) -> None:
        # History keys and the per-provider *current model* slot are user-grown
        # via the setup page — exempt the history holders from doc parity, and
        # exempt current-model keys whose defaults live outside ENV_DEFAULTS.
        EXEMPT_DOC = {
            "OLLAMA_MODEL_HISTORY",
            "OPENROUTER_MODEL_HISTORY",
            "GEMINI_MODEL_HISTORY",
            "META_MODEL_HISTORY",
        }
        documented = set(self._all_keys())
        for key in sorted(config.KNOWN_ENV_KEYS):
            if key in EXEMPT_DOC:
                continue
            self.assertIn(
                key,
                documented,
                f"{key} is readable by the app but missing from .env.example",
            )

    def test_every_documented_key_is_known(self) -> None:
        for key in self._all_keys():
            self.assertIn(key, config.KNOWN_ENV_KEYS, f"stale/unknown doc key {key}")

    def test_prompt_example_matches_builtin_default(self) -> None:
        with open(self.PROMPT_EXAMPLE_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, config.DEFAULT_SYSTEM_PROMPT.rstrip() + "\n")


class TestConfigCascade(unittest.TestCase):
    """One generic knob drives every provider; per-provider keys are overrides."""

    def test_model_cascade_generic_then_override(self) -> None:
        # Generic-only: blank provider-specific model -> LLM_MODEL wins.
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_MODEL", ""
        ), patch.object(Config, "LLM_MODEL", "my/model"):
            self.assertEqual(Config.effective_llm_model(), "my/model")
        # Override: explicit GEMINI_MODEL beats the generic value.
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_MODEL", "gemini-9.9-flash"
        ), patch.object(Config, "LLM_MODEL", "my/model"):
            self.assertEqual(Config.effective_llm_model(), "gemini-9.9-flash")

    def test_api_key_per_provider(self) -> None:
        """Each provider resolves only its own key; nothing is shared."""
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_API_KEY", "sk-or"
        ):
            self.assertEqual(Config.effective_api_key(), "sk-or")
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_API_KEY", ""
        ):
            self.assertEqual(Config.effective_api_key(), "")
        with patch.object(Config, "LLM_PROVIDER", "meta"), patch.object(
            Config, "META_API_KEY", ""
        ):
            # An OpenRouter key must not leak into Meta mode.
            with patch.object(Config, "OPENROUTER_API_KEY", "sk-or"):
                self.assertEqual(Config.effective_api_key(), "")
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_API_KEY", "AIza-x"
        ):
            self.assertEqual(Config.effective_api_key(), "AIza-x")

    def test_max_tokens_cascade_and_floor(self) -> None:
        # Providers without a dedicated ceiling follow the generic cap.
        with patch.object(Config, "LLM_PROVIDER", "ollama"), patch.object(
            Config, "LLM_MAX_TOKENS", 333
        ):
            self.assertEqual(Config.effective_max_output_tokens(), 333)
        # Explicit Gemini ceiling overrides it.
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_MAX_OUTPUT_TOKENS", 777
        ), patch.object(Config, "LLM_MAX_TOKENS", 333):
            self.assertEqual(Config.effective_max_output_tokens(), 777)
        # Meta has no output cap: reasoning is uncapped, so the generic value
        # is only a display label, and no META ceiling knob exists.
        with patch.object(Config, "LLM_PROVIDER", "meta"), patch.object(
            Config, "LLM_MAX_TOKENS", 333
        ):
            self.assertEqual(Config.effective_max_output_tokens(), 333)

    def test_reasoning_effort_mapping(self) -> None:
        # Unified knob reaches the resolvers...
        with patch.object(Config, "LLM_PROVIDER", "ollama"), patch.object(
            Config, "LLM_REASONING_EFFORT", "high"
        ):
            self.assertEqual(Config.effective_reasoning_effort(), "high")
        # ...and each client gets a provider-safe mapping.
        with patch.object(Config, "LLM_PROVIDER", "meta"), patch.object(
            Config, "META_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "minimal"):
            self.assertEqual(Config.meta_reasoning_effort(), "low")  # minimal->low
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_THINKING_LEVEL", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "none"):
            self.assertEqual(Config.gemini_thinking_level(), "minimal")  # none->minimal
        with patch.object(Config, "LLM_PROVIDER", "gemini"), patch.object(
            Config, "GEMINI_THINKING_LEVEL", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "bogus"):
            self.assertEqual(Config.gemini_thinking_level(), "minimal")
        with patch.object(Config, "LLM_PROVIDER", "meta"), patch.object(
            Config, "META_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "bogus"):
            self.assertEqual(Config.meta_reasoning_effort(), "low")
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "minimal"):
            self.assertEqual(Config.openrouter_reasoning_effort(), "minimal")
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", ""):
            self.assertEqual(Config.openrouter_reasoning_effort(), "none")
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", "bogus"):
            self.assertEqual(Config.openrouter_reasoning_effort(), "none")
        with patch.object(Config, "OPENROUTER_PROVIDER_SORT", "throughput"):
            self.assertEqual(Config.openrouter_provider_sort(), "throughput")
        with patch.object(Config, "OPENROUTER_PROVIDER_SORT", "bogus"):
            self.assertEqual(Config.openrouter_provider_sort(), "latency")
        with patch.object(Config, "LLM_PROVIDER", "openrouter"), patch.object(
            Config, "OPENROUTER_REASONING_EFFORT", ""
        ), patch.object(Config, "LLM_REASONING_EFFORT", ""), patch.object(
            Config, "OPENROUTER_PROVIDER_SORT", "latency"
        ):
            body = Config.openrouter_extra_body()
            self.assertEqual(body["provider"]["sort"], "latency")
            self.assertEqual(body["reasoning"]["effort"], "none")
            self.assertEqual(
                Config.openrouter_extra_body(effort="low")["reasoning"]["effort"],
                "low",
            )

    def test_prompt_txt_wins_over_example(self) -> None:
        import tempfile

        from config import DEFAULT_SYSTEM_PROMPT

        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "prompt.txt"), "w", encoding="utf-8") as f:
                f.write("LIVE PRIVATE PROMPT\n")
            with open(os.path.join(root, "prompt.txt.example"), "w", encoding="utf-8") as f:
                f.write(DEFAULT_SYSTEM_PROMPT.rstrip() + "\n")
            with patch("config._prompt_dir", return_value=root), patch.object(
                Config, "SYSTEM_PROMPT", "INLINE"
            ), patch.object(Config, "SYSTEM_PROMPT_FILE", ""):
                self.assertEqual(Config.effective_system_prompt(), "LIVE PRIVATE PROMPT")
                self.assertEqual(Config.prompt_source_label(), "prompt.txt")

    def test_missing_prompt_txt_uses_example(self) -> None:
        import tempfile

        from config import DEFAULT_SYSTEM_PROMPT

        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "prompt.txt.example"), "w", encoding="utf-8") as f:
                f.write("SHIPPED EXAMPLE PROMPT\n")
            with patch("config._prompt_dir", return_value=root), patch.object(
                Config, "SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT
            ), patch.object(Config, "SYSTEM_PROMPT_FILE", ""):
                self.assertEqual(
                    Config.effective_system_prompt(), "SHIPPED EXAMPLE PROMPT"
                )
                self.assertEqual(Config.prompt_source_label(), "prompt.txt.example")

    def test_no_prompt_files_uses_builtin(self) -> None:
        import tempfile

        from config import DEFAULT_SYSTEM_PROMPT

        with tempfile.TemporaryDirectory() as root:
            with patch("config._prompt_dir", return_value=root), patch.object(
                Config, "SYSTEM_PROMPT_FILE", ""
            ), patch.object(Config, "_explicit", return_value=False):
                self.assertEqual(
                    Config.effective_system_prompt(), DEFAULT_SYSTEM_PROMPT.strip()
                )
                self.assertEqual(Config.prompt_source_label(), "built-in default")

    def test_legacy_inline_prompt_when_no_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            with patch("config._prompt_dir", return_value=root), patch.object(
                Config, "SYSTEM_PROMPT", "INLINE PROMPT"
            ), patch.object(Config, "SYSTEM_PROMPT_FILE", ""), patch.object(
                Config, "_explicit", return_value=True
            ):
                self.assertEqual(Config.effective_system_prompt(), "INLINE PROMPT")

    def test_apply_prompt_save_writes_and_restores(self) -> None:
        import tempfile

        import setup_web
        from config import DEFAULT_SYSTEM_PROMPT

        with tempfile.TemporaryDirectory() as root:
            with patch("config._prompt_dir", return_value=root):
                err = setup_web.apply_prompt_save("my custom persona")
                self.assertEqual(err, "")
                live = os.path.join(root, "prompt.txt")
                with open(live, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "my custom persona")
                err = setup_web.apply_prompt_save(DEFAULT_SYSTEM_PROMPT)
                self.assertEqual(err, "")
                self.assertFalse(os.path.exists(live))

    def test_config_warnings_detect_typos_and_legacy(self) -> None:
        fake_env = {
            "HOTKEY": "ctrl+grave",
            "HOTKEYY": "oops",
            "LLM_API_KEY": "sk-old-generic",
            "MODEL_API_KEY": "sk-legacy",
            "GOOGLE_API_KEY": "AIza-legacy",
            "CTRL_KEEP_CONTEXT_KEYS": "f6",
        }
        with patch.object(config, "load_env_file_keys", return_value=fake_env), patch.object(
            config, "KNOWN_ENV_KEYS", frozenset({"HOTKEY", "CTRL_KEEP_CONTEXT_KEYS"})
        ):
            warnings = config.config_warnings()
        joined = "\n".join(warnings)
        self.assertIn("HOTKEYY", joined)
        self.assertIn("MODEL_API_KEY", joined)
        self.assertIn("GOOGLE_API_KEY", joined)
        # The removed generic key gets a dedicated deprecation explanation.
        self.assertIn("LLM_API_KEY", joined)
        self.assertIn("no longer used", joined)

    def test_explain_rows_are_grouped_and_secret_masked(self) -> None:
        rows = Config.explain()
        self.assertGreater(len(rows), 10)
        groups = [r["group"] for r in rows]
        self.assertIn("Provider", groups)
        self.assertIn("Prompt", groups)
        secret_rows = [r for r in rows if r["label"] == "API key"]
        self.assertEqual(len(secret_rows), 1)
        self.assertTrue(secret_rows[0]["secret"])


class TestOpenrouterCatalog(unittest.TestCase):
    def setUp(self) -> None:
        from openrouter_catalog import reset_openrouter_catalog

        reset_openrouter_catalog()

    def tearDown(self) -> None:
        from openrouter_catalog import reset_openrouter_catalog

        reset_openrouter_catalog()

    def test_parse_reasoning_and_clamp_from_live_shape(self) -> None:
        from openrouter_catalog import (
            clamp_openrouter_effort,
            parse_openrouter_models,
            peek_openrouter_reasoning,
        )
        import openrouter_catalog as oc

        payload = parse_openrouter_models(
            {
                "data": [
                    {
                        "id": "z-ai/glm-5.3-flash",
                        "name": "Z.ai: GLM 5.3 Flash",
                        "reasoning": {
                            "mandatory": True,
                            "default_enabled": True,
                            "supported_efforts": ["max", "high", "low"],
                            "default_effort": "max",
                        },
                    },
                    {
                        "id": "deepseek/deepseek-v4.1-flash",
                        "name": "DeepSeek V4.1 Flash",
                        "reasoning": {
                            "mandatory": False,
                            "default_enabled": True,
                            "supported_efforts": ["max", "high", "low"],
                            "default_effort": "high",
                        },
                    },
                    {
                        "id": "openai/gpt-5.6-luna",
                        "name": "GPT-5.6 Luna",
                        "reasoning": {
                            "mandatory": False,
                            "supported_efforts": [
                                "max",
                                "xhigh",
                                "high",
                                "medium",
                                "low",
                                "none",
                            ],
                            "default_effort": "medium",
                        },
                    },
                ]
            }
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["models"]), 3)
        oc._cache = payload
        oc._cache_at = 1.0
        glm = peek_openrouter_reasoning("z-ai/glm-5.3-flash:batch")
        self.assertIsNotNone(glm)
        self.assertTrue(glm["mandatory"])
        self.assertEqual(
            clamp_openrouter_effort("z-ai/glm-5.3-flash", "none"), "low"
        )
        self.assertEqual(
            clamp_openrouter_effort("z-ai/glm-5.3-flash", "minimal"), "low"
        )
        self.assertEqual(
            clamp_openrouter_effort("z-ai/glm-5.3-flash", "medium"), "low"
        )
        self.assertEqual(
            clamp_openrouter_effort("z-ai/glm-5.3-flash", "max"), "max"
        )
        self.assertEqual(
            clamp_openrouter_effort("deepseek/deepseek-v4.1-flash", "none"),
            "none",
        )
        self.assertEqual(
            clamp_openrouter_effort("openai/gpt-5.6-luna", "none"), "none"
        )

    def test_fetch_error_is_soft(self) -> None:
        from openrouter_catalog import fetch_openrouter_catalog

        with patch(
            "openrouter_catalog.urllib.request.urlopen",
            side_effect=OSError("offline"),
        ):
            payload = fetch_openrouter_catalog()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reasoning"], {})


if __name__ == "__main__":
    unittest.main()
