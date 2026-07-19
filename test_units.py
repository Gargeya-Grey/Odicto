import sys
import os
import unittest
from unittest.mock import patch, MagicMock
import numpy as np

# 1. Mock faster_whisper to avoid downloading/loading the Whisper model on test import
mock_faster_whisper = MagicMock()
sys.modules["faster_whisper"] = mock_faster_whisper

# Now we can safely import config, recorder, transcriber, refiner, typer, main
from config import Config, parse_hold_hotkey
from recorder import AudioRecorder, play_beep
from transcriber import WhisperTranscriber
from refiner import TextRefiner, estimate_max_tokens, should_use_full_history
from typer import paste_text
from app_state import AppState
from main import DictationApp


class TestOdicto(unittest.TestCase):

    def setUp(self) -> None:
        # Reset state/config to defaults where necessary
        Config.LLM_PROVIDER = "ollama"
        Config.LLM_MODEL = "qwen2.5:1.5b-instruct"

    def test_parse_hold_hotkey(self) -> None:
        """Hold chords split into modifiers + primary key."""
        self.assertEqual(parse_hold_hotkey("alt+x"), (("alt",), "x"))
        self.assertEqual(
            parse_hold_hotkey("ctrl+shift+space"), (("ctrl", "shift"), "space")
        )
        self.assertEqual(parse_hold_hotkey("scroll lock"), ((), "scroll lock"))
        self.assertEqual(parse_hold_hotkey("  ALT + X  "), (("alt",), "x"))

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
        """Verifies the audio recording start, callback buffer appending, and stop/save lifecycle."""
        recorder = AudioRecorder(sample_rate=16000, channels=1)
        self.assertFalse(recorder.recording)

        # Start recording
        recorder.start()
        self.assertTrue(recorder.recording)
        mock_input_stream.assert_called_once()
        mock_input_stream.return_value.start.assert_called_once()

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
        mock_input_stream.return_value.stop.assert_called_once()
        mock_input_stream.return_value.close.assert_called_once()
        self.assertIsNotNone(recorder.last_audio_array)

        # Verify sf.write was called with correct concatenated data
        mock_sf_write.assert_called_once()
        args, kwargs = mock_sf_write.call_args
        self.assertEqual(args[0], test_filepath)
        expected_data = np.concatenate([chunk1, chunk2], axis=0)
        np.testing.assert_array_equal(args[1], expected_data)
        self.assertEqual(args[2], 16000)

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

    def test_adaptive_max_tokens(self) -> None:
        """Short questions get a tight token budget; complex ones use more."""
        self.assertLessEqual(estimate_max_tokens("Are you working?", 150), 40)
        self.assertLessEqual(estimate_max_tokens("hello", 150), 40)
        self.assertGreaterEqual(
            estimate_max_tokens(
                "Please explain in detail how transformers work step by step "
                "with attention mechanisms and positional encodings thoroughly.",
                150,
            ),
            140,
        )
        self.assertFalse(should_use_full_history("relations"))
        self.assertTrue(should_use_full_history("what about the earlier plan"))
        self.assertTrue(
            should_use_full_history(
                "Can you summarize the tradeoffs between microservices and a monolith "
                "for a mid-size team with limited DevOps capacity?"
            )
        )

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

    @patch("refiner.OpenAI")
    def test_text_refiner_ollama(self, mock_openai: MagicMock) -> None:
        """Verifies TextRefiner correctly formats messages and calls the LLM provider."""
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
        # Short query must use a reduced token budget
        self.assertLessEqual(kwargs["max_tokens"], 64)
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "hello world")
        # System prompt must teach adaptive length
        self.assertIn("LENGTH RULES", messages[0]["content"])

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

        res_reset = refiner.refine("reset chat")
        self.assertEqual(res_reset, "Conversation history cleared.")
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

    @patch("refiner.Config.LLM_PROVIDER", "none")
    def test_text_refiner_none_provider(self) -> None:
        """Verifies that TextRefiner immediately bypasses LLM if LLM_PROVIDER is 'none'."""
        refiner = TextRefiner()
        result = refiner.refine("raw transcript text")
        self.assertEqual(result, "raw transcript text")

    @patch("typer.pyperclip")
    @patch("typer.keyboard")
    def test_paste_text_flow(
        self, mock_keyboard: MagicMock, mock_pyperclip: MagicMock
    ) -> None:
        """Verifies clipboard injection backup, paste command execution, and clipboard restore."""
        mock_pyperclip.paste.return_value = "original clipboard data"

        paste_text("injected text")

        mock_pyperclip.copy.assert_any_call("injected text")
        mock_keyboard.send.assert_called_once_with("ctrl+v")
        mock_pyperclip.copy.assert_any_call("original clipboard data")

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.keyboard")
    @patch("main.play_beep")
    def test_dictation_app_state_machine(
        self,
        mock_play_beep: MagicMock,
        mock_keyboard: MagicMock,
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
                target_function = mock_thread.call_args[1]["target"]
                args = mock_thread.call_args[1].get("args") or mock_thread.call_args[0][1:]
                if not args:
                    # args may be positional in call_args[0]
                    args = mock_thread.call_args[0][1:] if len(mock_thread.call_args[0]) > 1 else mock_thread.call_args[1].get("args", ())

                # Execute worker pipeline synchronously with captured args
                call_kwargs = mock_thread.call_args[1]
                worker_args = call_kwargs.get("args", ())
                target_function(*worker_args)

                app.transcriber.transcribe.assert_called_once()
                app.refiner.refine.assert_called_once_with("raw speech text")
                mock_paste_text.assert_called_once_with("Polished speech text.")
                self.assertEqual(app.state, AppState.IDLE)
                self.assertEqual(app.last_status, "success")

    @patch("socket.socket")
    @patch("main.AudioRecorder")
    @patch("main.WhisperTranscriber")
    @patch("main.TextRefiner")
    @patch("main.paste_text")
    @patch("main.keyboard")
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
    @patch("main.keyboard")
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
    @patch("main.keyboard")
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


class TestDictationIndicator(unittest.TestCase):
    """Premium Qt HUD — pure helpers + lightweight widget state machine tests."""

    def test_status_labels(self) -> None:
        from indicator import GuiState, status_label

        self.assertEqual(status_label(GuiState.BOOTING), "Starting")
        self.assertEqual(status_label(GuiState.RECORDING, use_llm=False), "Listening")
        self.assertEqual(status_label(GuiState.RECORDING, use_llm=True), "Listening · AI")
        self.assertEqual(status_label(GuiState.PROCESSING, use_llm=False), "Transcribing")
        self.assertEqual(status_label(GuiState.PROCESSING, use_llm=True), "Thinking")
        self.assertEqual(status_label(GuiState.SUCCESS), "Done")
        self.assertEqual(
            status_label(GuiState.ERROR, last_status="empty"), "No speech"
        )
        self.assertEqual(
            status_label(GuiState.ERROR, last_status="error"), "Failed"
        )

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


if __name__ == "__main__":
    unittest.main()
