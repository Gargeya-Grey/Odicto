"""F7 live captions fail: the leaf says "Captions off"; the caption card says why and that the
text still arrives, naming the speech service the pipeline really uses. No pixels compared."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter

import hud_leaf as hl
from app_state import AppState
from config import Config
from indicator import GuiState, LIVE_FAILURE_LABELS, live_failure_message, status_label
from tests.test_hud_motion import _close, _hud

KEY = "live_key_rejected"
FAILED = "live_unavailable"
SERVICE = {"groq": "Groq", "openrouter": "OpenRouter", "whisper": "local Whisper",
           "gemini": "local Whisper"}


def _live(status, preview="", provider="whisper"):
    with patch.object(Config, "effective_stt_provider", return_value=provider):
        app, hud = _hud(state=AppState.RECORDING, live_active=True, live_preview=preview,
                        last_status=status)
        hud._sync_from_app()
        hud._live_failure_text()   # reads the provider while it is patched
    return app, hud


def _drawn(hud):
    """The strings the card paints, and the card's rect."""
    seen = []

    class Spy(QPainter):
        def drawText(self, *args):
            if len(args) == 2 and isinstance(args[1], str):
                seen.append(args[1])
            return super().drawText(*args)

    rect = QRectF(16, 16, hud._card_width(), hl.CARD_H)
    image = QImage(int(rect.width()) + 32, hl.CARD_H + 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = Spy(image)
    hud._paint_caption(painter, rect, 1.0)
    painter.end()
    return seen, rect


class TestLiveFailureText(unittest.TestCase):
    def test_a_rejected_key_names_the_service_that_makes_the_text(self):
        for effective, service in SERVICE.items():
            with patch.object(Config, "effective_stt_provider", return_value=effective):
                message = live_failure_message(KEY, Config.live_key_rejected_stt_provider())
            self.assertEqual(
                message, f"Gemini key rejected. Your text still goes through {service} when you tap F7.")

    def test_a_failed_live_call_says_the_text_still_arrives(self):
        for provider in SERVICE:
            self.assertEqual(live_failure_message(FAILED, provider),
                             "Live captions failed. Your text still arrives when you tap F7.")

    def test_other_statuses_have_no_message(self):
        for status in (None, "success", "finalizing", "error"):
            self.assertEqual(live_failure_message(status, "groq"), "")

    def test_the_leaf_label_is_captions_off_for_both_statuses(self):
        self.assertEqual(set(LIVE_FAILURE_LABELS), {KEY, FAILED})
        for status in (KEY, FAILED):
            self.assertEqual(status_label(GuiState.RECORDING, False, status), "Captions off")
            self.assertEqual(status_label(GuiState.RECORDING, True, status), "Captions off")
        self.assertEqual(status_label(GuiState.RECORDING), "Listening")

    def test_the_named_provider_is_the_normal_one_and_gemini_gives_way_to_whisper(self):
        for effective, used in (("whisper", "whisper"), ("groq", "groq"),
                                ("openrouter", "openrouter"), ("gemini", "whisper")):
            with patch.object(Config, "effective_stt_provider", return_value=effective):
                self.assertEqual(Config.live_key_rejected_stt_provider(), used, effective)


class TestLiveFailureCard(unittest.TestCase):
    def test_the_card_shows_the_message_when_it_has_no_captions(self):
        for status in (KEY, FAILED):
            for effective in SERVICE:
                with self.subTest(status=status, provider=effective):
                    _, hud = _live(status, provider=effective)
                    try:
                        seen, rect = _drawn(hud)
                        with patch.object(Config, "effective_stt_provider",
                                          return_value=effective):
                            expected = live_failure_message(
                                status, Config.live_key_rejected_stt_provider())
                        self.assertEqual(" ".join(seen), expected)
                        self.assertLessEqual(len(seen), 2)
                        for line in seen:
                            self.assertLessEqual(hud._fm_caption.horizontalAdvance(line),
                                                 rect.width() - 36, line)
                    finally:
                        _close(hud)

    def test_captions_that_arrived_stay_and_hide_the_message(self):
        _, hud = _live(KEY, preview="hello there this is live")
        try:
            for _ in range(80):
                hud._advance_caption(0.016)
            seen, _ = _drawn(hud)
            self.assertTrue(seen)
            self.assertNotIn("rejected", " ".join(seen))
            self.assertEqual(hud._label_for(GuiState.RECORDING, False), "Captions off")
        finally:
            _close(hud)

    def test_without_a_failure_the_card_asks_for_speech(self):
        _, hud = _live(None)
        try:
            self.assertEqual(_drawn(hud)[0], ["Speak, then tap F7 to insert"])
        finally:
            _close(hud)

    def test_the_message_follows_a_change_of_status(self):
        app, hud = _live(FAILED)
        try:
            self.assertIn("failed", " ".join(_drawn(hud)[0]))
            app.last_status = KEY
            self.assertIn("rejected", " ".join(_drawn(hud)[0]))
            app.last_status = None
            self.assertEqual(_drawn(hud)[0], ["Speak, then tap F7 to insert"])
        finally:
            _close(hud)


if __name__ == "__main__":
    unittest.main()
