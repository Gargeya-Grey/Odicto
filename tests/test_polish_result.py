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
