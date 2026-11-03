"""Explicit selection polish stays separate from voice transcription; no real hooks or input."""
import unittest
from unittest.mock import MagicMock, patch

from app_state import AppState
from config import Config
from main import _Cycle
from tests.test_reliability import app_fixture


class TestSelectionPolish(unittest.TestCase):
    def setUp(self):
        focus = patch("main.platforms.foreground_window_id", return_value=42)
        terminal = patch("main.platforms.foreground_is_terminal", return_value=False)
        self.focus = focus.start()
        self.terminal = terminal.start()
        self.addCleanup(focus.stop)
        self.addCleanup(terminal.stop)

    def fixture(self):
        app = app_fixture()
        app._cycle = _Cycle(1)
        app._cycle_seq = 1
        app.refiner = MagicMock()
        app.refiner.last_notice = ""
        app.refiner.polish.return_value = "These words are corrected."
        app.paste_delivered = False
        return app

    def test_shortcut_rejects_shared_primary_and_allows_blank_or_chord(self):
        for value in ("f7", "ctrl+f7", "ctrl+grave", "esc"):
            with self.assertRaises(ValueError):
                Config.validate_polish_hotkey(value, "ctrl+grave", "f7", "f5", "esc")
        Config.validate_polish_hotkey("", "ctrl+grave", "f7")
        Config.validate_polish_hotkey("ctrl+shift+p", "ctrl+grave", "f7")

    def test_polish_action_dispatches_without_recording(self):
        app = self.fixture()
        with patch.object(app, "_start_polish_selection") as start, patch.object(app, "on_press") as record:
            app._execute_hotkey_action("polish", (), None)
        start.assert_called_once_with()
        record.assert_not_called()

    def test_empty_selection_never_calls_model_or_pastes(self):
        app = self.fixture()
        with patch("main.get_selected_text", return_value=""), patch("main.paste_text") as paste:
            app._polish_selection()
        app.refiner.polish.assert_not_called()
        paste.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)

    def test_selected_text_is_polished_and_replaced_only_while_still_selected(self):
        app = self.fixture()
        with patch("main.get_selected_text", return_value="These words is wrong.") as selection, patch("main.paste_text") as paste:
            app._polish_selection()
        app.refiner.polish.assert_called_once_with("These words is wrong.")
        self.assertGreaterEqual(selection.call_count, 2)
        paste.assert_called_once_with("These words are corrected.")
        app.recorder.start.assert_not_called()
        app.transcriber.transcribe.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)

    def test_changed_selection_preserves_both_texts(self):
        app = self.fixture()
        with patch("main.get_selected_text", side_effect=["original selection", "different selection"]), patch("main.paste_text") as paste:
            app._polish_selection()
        app.refiner.polish.assert_called_once_with("original selection")
        paste.assert_not_called()

    def test_focus_change_during_model_call_keeps_document_untouched(self):
        app = self.fixture()
        self.focus.side_effect = [42, 42, 99]
        with patch("main.get_selected_text", return_value="original selection"), patch("main.paste_text") as paste:
            app._polish_selection()
        app.refiner.polish.assert_called_once_with("original selection")
        paste.assert_not_called()
        self.assertEqual(app.last_status, "polish_target")

    def test_terminal_selection_is_not_polished_or_typed(self):
        app = self.fixture()
        self.terminal.return_value = True
        with patch("main.get_selected_text") as selection, patch("main.paste_text") as paste:
            app._polish_selection()
        selection.assert_not_called()
        app.refiner.polish.assert_not_called()
        paste.assert_not_called()
        self.assertEqual(app.last_status, "polish_target")

    def test_unidentified_foreground_does_not_read_selection(self):
        app = self.fixture()
        self.focus.return_value = None
        with patch("main.get_selected_text") as selection, patch("main.paste_text") as paste:
            app._polish_selection()
        selection.assert_not_called()
        paste.assert_not_called()
        self.assertEqual(app.last_status, "polish_target")

    def test_unchanged_polish_does_not_replace_selection(self):
        app = self.fixture()
        app.refiner.polish.return_value = "already clean"
        with patch("main.get_selected_text", return_value="already clean"), patch("main.paste_text") as paste:
            app._polish_selection()
        paste.assert_not_called()

    def test_failed_model_does_not_replace_selection(self):
        app = self.fixture()
        app.refiner.polish.side_effect = RuntimeError("offline")
        with patch("main.get_selected_text", return_value="original selection"), patch("main.paste_text") as paste:
            app._polish_selection()
        paste.assert_not_called()
        self.assertEqual(app.state, AppState.IDLE)

    def test_empty_model_response_does_not_erase_selection(self):
        app = self.fixture()
        app.refiner.polish.return_value = ""
        with patch("main.get_selected_text", return_value="original selection"), patch("main.paste_text") as paste:
            app._polish_selection()
        paste.assert_not_called()

    def test_cancel_during_polish_discards_model_result(self):
        app = self.fixture()
        def cancelled(text):
            app._cycle.cancel.set()
            return "late polished text"
        app.refiner.polish.side_effect = cancelled
        with patch("main.get_selected_text", return_value="original selection"), patch("main.paste_text") as paste:
            app._polish_selection()
        paste.assert_not_called()

    def test_busy_app_does_not_start_selection_polish(self):
        for state in (AppState.RECORDING, AppState.PROCESSING):
            with self.subTest(state=state):
                app = self.fixture()
                app.state = state
                with patch("main.threading.Thread") as worker:
                    app._start_polish_selection()
                worker.assert_not_called()

    def test_raw_dictation_never_polishes_even_with_legacy_flag_enabled(self):
        for live in (False, True):
            with self.subTest(live=live):
                app = self.fixture()
                with patch.object(Config, "POLISH_DICTATION", True), patch("main.paste_text") as paste:
                    app.process_and_paste(None, False, pre_transcript="raw words unchanged", live=live)
                app.refiner.polish.assert_not_called()
                self.assertEqual(paste.call_args.args[0], "raw words unchanged")


if __name__ == "__main__":
    unittest.main()
