import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from unittest import skipUnless
import numpy as np

# 1. Mock faster_whisper to avoid downloading/loading the Whisper model on test import
mock_faster_whisper = MagicMock()
sys.modules["faster_whisper"] = mock_faster_whisper

# Now we can safely import config, recorder, transcriber, refiner, typer, main
from config import Config, parse_hold_hotkey, _sanitize_model_id
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner
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
        # CRITICAL: never run the real single-instance lock or orphan killer in tests.
        # initialize_app() acquires it when not held — that would taskkill a live
        # Odicto and take the Global mutex for the duration of the test run.
        self._real_lock_held = main_mod._INSTANCE_LOCK_HELD
        main_mod._INSTANCE_LOCK_HELD = True  # bypass lock acquisition + orphan kill in initialize_app

    def tearDown(self) -> None:
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

    @patch("transcriber.WhisperModel")
    def test_whisper_transcriber_loading_fallback(
        self, mock_whisper_model: MagicMock
    ) -> None:
        """Verifies that WhisperTranscriber falls back to CPU if CUDA fails."""
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
        result = transcriber.transcribe("fake_audio.wav")
        self.assertEqual(result, "Hello world")

        # Ensure speed flags are applied
        kwargs = mock_model_instance.transcribe.call_args[1]
        self.assertTrue(kwargs.get("without_timestamps"))
        self.assertEqual(kwargs.get("beam_size"), 1)

    @patch("transcriber.WhisperModel")
    def test_whisper_transcribe_numpy(self, mock_whisper_model: MagicMock) -> None:
        """In-memory float32 arrays should be accepted without a file path."""
        mock_segment = MagicMock()
        mock_segment.text = "from memory"
        mock_model_instance = mock_whisper_model.return_value
        mock_model_instance.transcribe.return_value = ([mock_segment], MagicMock())

        transcriber = WhisperTranscriber()
        audio = np.zeros(1600, dtype=np.float32)
        result = transcriber.transcribe(audio)
        self.assertEqual(result, "from memory")

    def test_effective_llm_model_and_api_base(self) -> None:
        """Provider flip picks the right model id and API base without hand-editing paths."""
        with patch.object(Config, "LLM_PROVIDER", "ollama"), patch.object(
            Config, "LLM_MODEL", "phi4-mini:latest"
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
        self.assertNotIn("LENGTH RULES", messages[0]["content"])

    @patch("refiner.Config.LLM_PROVIDER", "openrouter")
    @patch("refiner.Config.OPENROUTER_API_KEY", "sk-or-test")
    @patch("refiner.Config.OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
    @patch("refiner.Config.LLM_API_BASE", "http://localhost:11434/v1")
    @patch("refiner.Config.OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
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
        # Ollama-only extra_body must not be attached for openrouter
        self.assertNotIn("extra_body", kwargs)

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

        res1 = refiner.refine("What is your name?")
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

        res2 = refiner.refine("Repeat what I did.")
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

        # Build some history via normal queries.
        refiner.refine("What is your name?")
        self.assertEqual(len(refiner.conversation_history), 2)
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

        # Reset clears history and does NOT call the LLM.
        result = refiner.refine("reset chat")
        self.assertEqual(result, "Chat memory cleared. Starting fresh.")
        self.assertEqual(refiner.conversation_history, [])
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

        # Case/punctuation variants also reset.
        refiner.refine("What is your name?")
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
        user_msgs = [m for m in messages if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        user_content = user_msgs[0]["content"]
        self.assertIn("Hello world", user_content)
        self.assertIn("make this better", user_content)
        self.assertIn("Context:", user_content)

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
    @patch("typer.clipboard_read")
    @patch("typer.clipboard_write")
    def test_paste_text_flow(
        self,
        mock_clipboard_write: MagicMock,
        mock_clipboard_read: MagicMock,
        mock_send_paste: MagicMock,
    ) -> None:
        """Verifies clipboard injection backup, paste command execution, and clipboard restore."""
        mock_clipboard_read.return_value = "original clipboard data"
        mock_clipboard_write.return_value = True

        paste_text("injected text")

        mock_clipboard_write.assert_any_call("injected text")
        mock_send_paste.assert_called()
        mock_clipboard_write.assert_any_call("original clipboard data")

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
        ), patch("threading.Thread"):
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

                # Run the pipeline worker synchronously (single worker thread).
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
                pipeline_target(*pipeline_args)

                app.transcriber.transcribe.assert_called_once()
                app.refiner.refine.assert_called_once_with("raw speech text", context="")
                mock_paste_text.assert_called_once_with("Polished speech text.")
                self.assertEqual(app.state, AppState.IDLE)
                self.assertEqual(app.last_status, "success")
                # Selection probe runs on the worker thread (not the hook thread).
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
        ), patch("threading.Thread"), patch("main.time.sleep"):
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
                "make this better", context="highlighted draft paragraph"
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
    def test_dictation_app_f6_force_fresh_ai(
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
        """Holding F6 (CTRL_FORCE_FRESH_KEYS) while pressing the AI chord forces
        a fresh-context AI reply: memory is wiped and use_llm stays True."""
        with patch("main.Config.PLAY_AUDIO_CUES", False), patch(
            "main.Config.SHOW_VISUAL_INDICATOR", False
        ), patch("main.Config.CTRL_FORCE_FRESH_KEYS", ("f6",)):
            app = DictationApp()
            app.initialize_app()
            app.ready = True

            # User holds the AI chord (ctrl+shift) AND f6.
            app._pressed_mods_at_press = ("ctrl", "shift", "f6")
            app.on_press(use_llm=True)
            self.assertTrue(app.use_llm)
            self.assertIsNone(app._capture_mode_override)
            mock_refiner.return_value.reset_context.assert_called_once()

            app._set_state(AppState.RECORDING)
            app._record_started_at = 0.0
            app.recorder.stop.return_value = True
            app.recorder.last_audio_array = np.zeros(50, dtype=np.float32)
            app.transcriber.transcribe.return_value = "draft"
            with patch("threading.Thread") as mock_thread:
                app.on_release()
                # Run the pipeline worker synchronously.
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


class TestCrossPlatform(unittest.TestCase):
    """Facade dispatch, env merge, and provider-test helpers."""

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
        self.assertFalse(_mask_key("LLM_PROVIDER"))

    def test_setup_web_merge_env_preserves_and_updates(self) -> None:
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
                self.assertIn("CUSTOM_KEY=keep", text)
                self.assertIn("# keep me", text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_setup_web_reset_env_writes_example(self) -> None:
        import setup_web

        with patch.object(setup_web, "ENV_PATH", new=os.path.join(os.getcwd(), ".env.test")), \
             patch.object(setup_web, "ENV_EXAMPLE_PATH", new=os.path.join(os.getcwd(), ".env.example")):
            try:
                with open(setup_web.ENV_PATH, "w") as f:
                    f.write("LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=sk-or-test\n")
                setup_web.reset_env()
                with open(setup_web.ENV_PATH) as f:
                    text = f.read()
                self.assertNotIn("sk-or-test", text)
                self.assertIn("LLM_PROVIDER=", text)
            finally:
                try:
                    os.remove(setup_web.ENV_PATH)
                except Exception:
                    pass

    def test_refiner_test_provider_none(self) -> None:
        from refiner import test_provider

        self.assertEqual(test_provider("none", "", "", ""), "ok")

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


if __name__ == "__main__":
    unittest.main()
