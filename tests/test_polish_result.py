"""User-controlled result review never injects input into another application."""
import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtWidgets import QApplication
from polish_result import PolishResultDialog
from tests.test_reliability import refiner_fixture


class TestPolishResult(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_dialog_keeps_original_and_copies_only_on_explicit_action(self):
        dialog = PolishResultDialog("original text ", "edited text ")
        clipboard = MagicMock()
        try:
            with patch("polish_result.QApplication.clipboard", return_value=clipboard):
                self.assertEqual(dialog.original.toPlainText(), "original text ")
                self.assertTrue(dialog.original.isReadOnly())
                clipboard.setText.assert_not_called()
                dialog.polished.setPlainText("user adjusted text ")
                dialog.copy_button.click()
                clipboard.setText.assert_called_once_with("user adjusted text ")
        finally:
            dialog.close()

    def test_polish_preserves_selected_boundary_whitespace(self):
        for original in ("hello ", "  hello", "\n  hello\n\n", "\tfirst paragraph\n\nsecond paragraph  "):
            with self.subTest(original=original):
                refiner = refiner_fixture()
                refiner.client.chat.completions.create.return_value.choices[0].message.content = original.strip()
                self.assertEqual(refiner.polish(original), original)

    def test_recovery_preserves_complete_unicode_and_requires_explicit_copy(self):
        text = "नमस्ते\nsecond paragraph 🙂"
        dialog = PolishResultDialog(None, text, title="Odicto · Text ready", explanation="Manual paste needed")
        clipboard = MagicMock()
        try:
            with patch("polish_result.QApplication.clipboard", return_value=clipboard):
                self.assertIsNone(dialog.original)
                self.assertEqual(dialog.polished.toPlainText(), text)
                clipboard.setText.assert_not_called()
                dialog.copy_button.click()
                clipboard.setText.assert_called_once_with(text)
        finally:
            dialog.close()

    def test_pipeline_offers_complete_recovery_after_safe_injection_refusal(self):
        from tests.test_reliability import app_fixture
        from platforms.base import TextDeliveryUnavailable
        app = app_fixture()
        app.indicator = MagicMock()
        text = "नमस्ते\nsecond paragraph 🙂"
        with patch("main.paste_text", side_effect=TextDeliveryUnavailable("Manual paste needed")) as paste:
            app.process_and_paste(None, False, pre_transcript=text)
        paste.assert_called_once_with(text)
        app.indicator.show_copy_result.assert_called_once_with(text, "Manual paste needed")
        self.assertEqual(app.last_status, "copy_ready")

    def test_new_results_preserve_each_uncopied_recovery(self):
        from indicator import DictationIndicator
        from tests.test_reliability import app_fixture
        with patch.object(DictationIndicator, "_setup_tray"):
            indicator = DictationIndicator(app_fixture())
        try:
            indicator._show_copy_result("first complete transcript", "Manual paste needed")
            first = indicator._polish_dialog
            indicator._show_copy_result("second complete transcript", "Manual paste needed")
            second = indicator._polish_dialog
            indicator._show_polish_result("original", "polished")
            polish = indicator._polish_dialog
            self.assertEqual(indicator._result_dialogs, {first, second, polish})
            self.assertTrue(all(dialog.isVisible() for dialog in (first, second, polish)))
            self.assertEqual(first.polished.toPlainText(), "first complete transcript")
            first.dismiss_button.click()
            self.assertEqual(indicator._result_dialogs, {second, polish})
            self.assertTrue(second.isVisible())
            self.assertTrue(polish.isVisible())
        finally:
            for dialog in list(indicator._result_dialogs):
                dialog.close()
            indicator._tick.stop()
            indicator.close()
