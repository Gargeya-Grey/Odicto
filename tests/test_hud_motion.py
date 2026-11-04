"""HUD motion and paste-delivery contract. No real paste, microphone or hooks.

Done shows on the frame the text is delivered; the clipboard wait and
PROCESSING still run until the restore finishes.
"""

from __future__ import annotations

import math
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

import typer
from app_state import AppState
from config import Config
from indicator import (
    GuiState,
    _FIT_SLACK,
    _GAP_ORB_TEXT,
    _GAP_TEXT_CHIP,
    _GAP_WAVE_TEXT,
    _INSET_R,
    _ORB,
    _ORB_INSET,
    _RECORDING_WAVE_W,
    _RESIZE_AFTER_TEXT_S,
    cubic_bezier,
    fit_pill_width,
    fit_recording_width,
)
from tests.test_clipboard_safety import ClipboardSafetyBase
from tests.test_reliability import app_fixture


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _hud(**attrs):
    from indicator import DictationIndicator

    _qt_app()
    app = MagicMock()
    app.state = AppState.IDLE
    app.last_status = None
    app.use_llm = False
    app.ready = True
    app.recorder = None
    app.live_active = False
    app.live_preview = ""
    for name, value in attrs.items():
        setattr(app, name, value)
    hud = DictationIndicator(app)
    return app, hud


def _close(hud) -> None:
    hud._tick.stop()
    hud._hide_timer.stop()
    hud.close()


class TestCubicBezier(unittest.TestCase):
    """The one easing primitive: CSS cubic-bezier(x1, y1, x2, y2)."""

    def test_the_linear_curve_is_the_identity(self):
        linear = cubic_bezier(0.0, 0.0, 1.0, 1.0)
        for i in range(11):
            self.assertAlmostEqual(linear(i / 10), i / 10, places=6)

    def test_a_symmetric_curve_is_slow_at_both_ends(self):
        ease_in_out = cubic_bezier(0.42, 0.0, 0.58, 1.0)
        self.assertAlmostEqual(ease_in_out(0.5), 0.5, places=6)
        self.assertLess(ease_in_out(0.25), 0.25)
        self.assertGreater(ease_in_out(0.75), 0.75)

    def test_clamps_outside_the_unit_range(self):
        curve = cubic_bezier(0.2, 0.0, 0.0, 1.0)
        self.assertEqual(curve(-0.5), 0.0)
        self.assertEqual(curve(1.5), 1.0)


class TestPillWidth(unittest.TestCase):
    def test_pure_fit_grows_with_text_and_chip(self):
        self.assertLess(fit_pill_width(40), fit_pill_width(160))
        self.assertLess(fit_pill_width(160), fit_pill_width(160, chip_w=24))
        chrome = _ORB_INSET + _ORB + _GAP_ORB_TEXT + _INSET_R
        self.assertGreaterEqual(fit_pill_width(160), chrome + 160)
        self.assertGreaterEqual(fit_pill_width(160, 24), chrome + 160 + _GAP_TEXT_CHIP + 24)

    def test_short_label_is_narrower_and_long_label_is_not_clipped(self):
        from PySide6.QtGui import QFontMetrics

        _, hud = _hud()
        try:
            done = hud._target_width("Done")
            long_label = "Clipboard not restored"
            long_w = hud._target_width(long_label)
            self.assertLess(done, long_w)
            text_w = QFontMetrics(hud._font).horizontalAdvance(long_label)
            text_room = long_w - (_ORB_INSET + _ORB + _GAP_ORB_TEXT) - _INSET_R
            self.assertGreaterEqual(text_room, text_w)
            # The AI chip is reserved only when it is shown.
            self.assertGreater(hud._target_width("Transcribing", "AI"),
                               hud._target_width("Transcribing"))
        finally:
            _close(hud)

    def test_listening_width_hugs_the_waveform_and_the_word(self):
        from PySide6.QtGui import QFontMetrics

        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            text_w = QFontMetrics(hud._font).horizontalAdvance("Listening")
            fit = fit_recording_width(text_w)
            chrome = (_ORB_INSET + _ORB + _GAP_ORB_TEXT + _RECORDING_WAVE_W + _GAP_WAVE_TEXT
                      + _INSET_R + _FIT_SLACK)
            # No reserved width: the capsule is the disc, the waveform, the word, and the insets.
            self.assertEqual(fit, int(math.ceil(chrome + text_w)))
            narrow = fit_recording_width(1.0)
            self.assertLess(narrow, 252)
            self.assertEqual(hud._pill_w, fit)
            hud._phase = "hidden"
            hud._pill_w = 10
            hud._sync_geometry()
            self.assertEqual(hud._w_tw.b, float(fit))
            self.assertIsNone(hud._resize_hold)
        finally:
            _close(hud)


class TestPasteDelivery(ClipboardSafetyBase):
    def _order(self):
        order = []
        self.mocks["send_paste"].side_effect = lambda: order.append("paste")
        self.mocks["send_text_bulk"].side_effect = lambda text: order.append("typed") or True
        self.on_window = lambda t: order.append("window")
        return order

    def _run(self, order, **kwargs):
        with patch("typer.time.sleep", side_effect=lambda s: order.append(("sleep", s))):
            typer.paste_text("transcript", on_delivered=lambda: order.append("delivered"),
                             **kwargs)

    def test_paste_reports_delivery_before_the_restore_sleep(self):
        order = self._order()
        self._run(order)
        i_paste = order.index("paste")
        i_done = order.index("delivered")
        i_floor = order.index(("sleep", 0.15))
        self.assertLess(i_paste, i_done)
        self.assertLess(i_done, i_floor)
        self.assertLess(i_done, order.index("window"))
        self.assertEqual(self.clip.text, "user clip")  # the restore still ran

    def test_live_paste_without_restore_reports_delivery(self):
        order = self._order()
        self._run(order, restore_clipboard=False)
        self.assertLess(order.index("paste"), order.index("delivered"))

    def test_terminal_typing_reports_delivery(self):
        order = self._order()
        self.mocks["foreground_is_terminal"].return_value = True
        with patch.object(Config, "TYPE_IN_TERMINAL", True):
            self._run(order)
        self.assertEqual(order[-2:], ["typed", "delivered"])

    def test_type_instead_reports_delivery(self):
        order = self._order()
        self.clip.non_text = b"image"
        self.clip.complete = False
        self._run(order)
        self.assertNotIn("paste", order)
        self.assertEqual(order[-2:], ["typed", "delivered"])

    def test_failed_injection_never_reports_delivery(self):
        delivered = []
        self.mocks["send_paste"].side_effect = RuntimeError("blocked")
        with patch("typer.time.sleep"), self.assertRaises(RuntimeError):
            typer.paste_text("transcript", on_delivered=lambda: delivered.append(1))
        self.assertEqual(delivered, [])

    def test_callback_error_does_not_skip_the_restore(self):
        def boom():
            raise RuntimeError("hud fault")

        with patch("typer.time.sleep"):
            typer.paste_text("transcript", on_delivered=boom)
        self.assertEqual(self.clip.text, "user clip")

    def test_thread_hook_is_used_and_cleared(self):
        delivered = []
        with patch("typer.time.sleep"), typer.delivery_callback(lambda: delivered.append(1)):
            typer.paste_text("transcript")
        self.assertEqual(delivered, [1])
        with patch("typer.time.sleep"):
            typer.paste_text("transcript")
        self.assertEqual(delivered, [1])


class TestPipelineDelivery(unittest.TestCase):
    def test_delivery_sets_done_while_still_processing(self):
        app = app_fixture()
        seen = {}

        def paste(text, **kwargs):
            typer._delivered(None)  # what the real paste_text does after the chord
            seen.update(delivered=app.paste_delivered, status=app.last_status,
                        state=app.state)

        with patch("main.paste_text", side_effect=paste), \
                patch.object(Config, "POLISH_DICTATION", False):
            app.process_and_paste(np.zeros(10), False)
        self.assertEqual(seen, {"delivered": True, "status": "success",
                                "state": AppState.PROCESSING})
        self.assertEqual(app.last_status, "success")
        self.assertEqual(app.state, AppState.IDLE)
        self.assertFalse(app.paste_delivered)

    def test_notice_after_delivery_replaces_done(self):
        app = app_fixture()
        app._paste_restore_failed = lambda: True

        with patch("main.paste_text", side_effect=lambda text, **kw: typer._delivered(None)), \
                patch.object(Config, "POLISH_DICTATION", False):
            app.process_and_paste(np.zeros(10), False)
        self.assertEqual(app.last_status, "clipboard_not_restored")


class TestHudDeliverySync(unittest.TestCase):
    def test_processing_with_delivery_shows_success(self):
        app, hud = _hud(state=AppState.PROCESSING, paste_delivered=True, last_status="success")
        try:
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.SUCCESS)
            # The clipboard wait is still running: no auto-hide yet.
            self.assertFalse(hud._hide_timer.isActive())

            app.state = AppState.IDLE
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.SUCCESS)
            self.assertTrue(hud._hide_timer.isActive())
            self.assertLessEqual(hud._hide_timer.interval(), 1200)
        finally:
            _close(hud)

    def test_processing_without_delivery_stays_processing(self):
        for flag in (None, False):
            with self.subTest(flag=flag):
                attrs = {"state": AppState.PROCESSING}
                if flag is not None:
                    attrs["paste_delivered"] = flag
                app, hud = _hud(**attrs)
                if flag is None:
                    del app.paste_delivered  # attribute absent
                try:
                    hud._sync_from_app()
                    self.assertEqual(hud.gui_state, GuiState.PROCESSING)
                finally:
                    _close(hud)

    def test_magicmock_flag_is_not_delivery(self):
        app, hud = _hud(state=AppState.PROCESSING)  # paste_delivered is a MagicMock
        try:
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.PROCESSING)
        finally:
            _close(hud)

    def test_label_change_eases_width_without_resizing_each_frame(self):
        app, hud = _hud(state=AppState.PROCESSING, paste_delivered=True, last_status="success")
        try:
            hud._sync_from_app()
            hud._phase = "open"
            hud._w_tw.snap(hud._pill_w)
            hud._h_tw.snap(hud._pill_h)
            done_w = hud._pill_w
            canvas_before = hud._canvas_w
            app.state = AppState.IDLE
            app.last_status = "clipboard_not_restored"
            hud._sync_from_app()
            self.assertGreater(hud._pill_w, done_w)
            # Canvas grew once, up front, to hold the target.
            self.assertGreaterEqual(hud._canvas_w, hud._pill_w + 2 * hud._pad)
            self.assertGreaterEqual(hud._canvas_w, canvas_before)
            # The painted width stays put until the new words are in, then 0.3 ms.
            self.assertEqual(hud._w_tw.b, float(done_w))
            hud._content_fade = 1.0
            hud._release_resize(0.0)
            self.assertEqual(hud._w_tw.b, float(done_w))
            hud._release_resize(hud._text_in_at + _RESIZE_AFTER_TEXT_S)
            self.assertEqual(hud._w_tw.b, float(hud._pill_w))
            self.assertFalse(hud._w_tw.done(hud._w_tw.t0))
        finally:
            _close(hud)

    def test_a_label_change_with_no_state_change_is_not_clipped(self):
        """F7 stop: 'Finalizing', then the app clears last_status and the label is 'Transcribing'."""
        from PySide6.QtGui import QFontMetrics

        app, hud = _hud(state=AppState.PROCESSING, last_status="finalizing")
        try:
            hud._sync_from_app()
            hud._phase = "open"
            hud._content_fade = 1.0  # the words of 'Finalizing' are fully in
            hud._w_tw.snap(hud._pill_w)
            hud._h_tw.snap(hud._pill_h)
            finalizing_w = hud._pill_w
            app.last_status = None  # process_and_paste starts: same state, longer word
            need = fit_pill_width(QFontMetrics(hud._font).horizontalAdvance("Transcribing"))
            self.assertGreater(need, finalizing_w, "the premise: the longer word needs a wider pill")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                hud._on_tick()  # the width starts moving a frame later, once the words are in
                time.sleep(0.01)
                if hud._pill_w >= need and hud._w_tw.b == float(hud._pill_w):
                    break
            self.assertGreaterEqual(hud._pill_w, need, "the label was clipped")
            self.assertEqual(hud._w_tw.b, float(hud._pill_w))  # and the painted width is on its way
        finally:
            _close(hud)

    def test_hide_plays_the_exit_then_hides(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            hud._phase = "open"
            hud._do_hide()
            self.assertEqual(hud._phase, "leaving")
            hud._advance_phase(hud._phase_t0 + 10.0)
            self.assertEqual(hud._phase, "hidden")
            self.assertEqual(hud._appear, 0.0)
        finally:
            _close(hud)


class TestLiveCaption(unittest.TestCase):
    """F7 captions: wrapped, revealed smoothly, never wider than the pill."""

    def _live(self, preview=""):
        app, hud = _hud(state=AppState.RECORDING, live_active=True, live_preview=preview)
        hud._sync_from_app()
        return app, hud

    def test_wrap_words_breaks_greedily_and_keeps_long_words_whole(self):
        from indicator import wrap_words
        self.assertEqual(wrap_words([], 4, 100), [])
        self.assertEqual(wrap_words([30, 30, 30], 5, 100), [(0, 3)])  # 30+5+30+5+30 = 100 fits
        self.assertEqual(wrap_words([30, 30, 31], 5, 100), [(0, 2), (2, 3)])
        self.assertEqual(wrap_words([20, 300, 20], 5, 100), [(0, 1), (1, 2), (2, 3)])
        lines = wrap_words([17] * 40, 4, 120)
        self.assertEqual([i for a, b in lines for i in range(a, b)], list(range(40)))
        for a, b in lines:
            self.assertLessEqual(17 * (b - a) + 4 * (b - a - 1), 120)

    def test_words_are_revealed_a_little_at_a_time_and_never_lag_behind(self):
        from indicator import _CAPTION_WORDS_PER_S
        _, hud = self._live("one two three four five six seven eight")
        try:
            hud._advance_caption(0.016)
            self.assertGreater(hud._cap_shown, 0.0)
            self.assertLess(hud._cap_shown, 8.0, "all eight words appeared in one frame")
            self.assertGreaterEqual(hud._cap_shown, _CAPTION_WORDS_PER_S * 0.016 - 1e-9)
            shown = []
            for _ in range(120):  # 2 s of frames
                hud._advance_caption(0.016)
                shown.append(hud._cap_shown)
            self.assertEqual(shown, sorted(shown), "the reveal went backwards")
            self.assertEqual(hud._cap_shown, 8.0)
            self.assertEqual(" ".join(hud._cap_lines), "one two three four five six seven eight")
        finally:
            _close(hud)

    def test_a_longer_text_carries_on_and_a_revised_word_is_shown_again(self):
        app, hud = self._live("alpha beta gamma delta")
        try:
            for _ in range(120):
                hud._advance_caption(0.016)
            self.assertEqual(hud._cap_shown, 4.0)
            app.live_preview = "alpha beta gamma delta epsilon"  # more words, same start
            hud._advance_caption(0.016)
            self.assertGreaterEqual(hud._cap_shown, 4.0)
            for _ in range(120):
                hud._advance_caption(0.016)
            app.live_preview = "alpha beta GAMMA delta epsilon"  # the service revised a word
            hud._advance_caption(0.016)
            self.assertLess(hud._cap_shown, 3.0, "the revised word was not typed again")
            for _ in range(120):
                hud._advance_caption(0.016)
            self.assertIn("GAMMA", " ".join(hud._cap_lines))
        finally:
            _close(hud)

    def test_lines_fit_the_pill_and_a_new_line_slides_up(self):
        from indicator import _CAPTION_LINE, _CAPTION_LINES, _CAPTION_PAD
        from PySide6.QtGui import QFontMetrics
        text = " ".join(f"word{i}" for i in range(60))
        _, hud = self._live(text)
        try:
            slid = False
            for _ in range(400):
                hud._advance_caption(0.016)
                slid = slid or hud._cap_scroll > 0.5
                self.assertLessEqual(hud._cap_scroll, _CAPTION_LINE * 1.5)
                self.assertLessEqual(len(hud._cap_lines), _CAPTION_LINES + 1)
                metrics = QFontMetrics(hud._font_caption)
                for line in hud._cap_lines:
                    self.assertLessEqual(metrics.horizontalAdvance(line),
                                         hud._pill_w - 2 * _CAPTION_PAD, line)
            self.assertTrue(slid, "no line ever slid up")
            self.assertEqual(hud._cap_scroll, 0.0, "the slide did not settle")
            self.assertTrue(hud._cap_lines[-1].endswith("word59"))
        finally:
            _close(hud)

    def _settle(self, hud, frames=1500):
        for _ in range(frames):
            hud._advance_caption(0.016)

    def test_a_long_text_slides_only_when_a_real_line_begins(self):
        """Past 80 words a window that slid would re-flow the lines and fake new ones."""
        from indicator import wrap_words
        words = [f"w{i % 7}{'x' * (i % 5)}" for i in range(220)]
        _, hud = self._live(" ".join(words))
        try:
            widths = [hud._word_width(w) for w in words]
            real_starts = {a for a, _ in wrap_words(widths, hud._word_width(" "), hud._caption_room())}
            seen, slides, previous = [], 0, 0.0
            for _ in range(1500):
                hud._advance_caption(0.016)
                if hud._cap_last_first is not None:
                    seen.append(hud._cap_last_first)
                if hud._cap_scroll > previous + 5.0:  # a slide jumps by a whole row (21 px)
                    slides += 1
                previous = hud._cap_scroll
            self.assertEqual(seen, sorted(seen), "the newest line moved back")
            self.assertLessEqual(set(seen) - {0}, real_starts, "a line began that is not in the text")
            lines = len(real_starts)
            self.assertLessEqual(slides, lines - 1, "more slides than lines that began")
            self.assertGreaterEqual(slides, 1)
        finally:
            _close(hud)

    def test_a_word_wider_than_the_pill_shows_its_end_and_stays_inside(self):
        from indicator import _CAPTION_PAD
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        long_word = "a" * 20 + "".join(chr(ord("b") + i % 20) for i in range(160)) + "END"
        for revealed in (True, False):  # fully shown, then mid fade-in
            with self.subTest(revealed=revealed):
                _, hud = self._live(f"short words then {long_word}")
                try:
                    self._settle(hud, 400)
                    if not revealed:
                        hud._cap_shown = 3.5
                        hud._advance_caption(0.0)
                        hud._cap_shown = 3.5
                        hud._advance_caption(0.0)
                    image = QImage(hud._canvas_w, hud._canvas_h, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(0)
                    painter = QPainter(image)
                    pill = QRectF(16, 16, hud._pill_w, hud._pill_h)
                    hud._paint_caption(painter, pill, 0.0)
                    painter.end()
                    low, high = 16 + _CAPTION_PAD - 2, 16 + hud._pill_w - _CAPTION_PAD + 2
                    columns = [x for x in range(image.width())
                               if any(image.pixelColor(x, y).alpha() for y in range(image.height()))]
                    self.assertTrue(columns, "nothing was painted")
                    self.assertGreaterEqual(min(columns), low, "text spilled past the left edge")
                    self.assertLessEqual(max(columns), high, "text spilled past the right edge")
                    if revealed:
                        self.assertTrue(hud._cap_lines[-1].endswith("END"))
                finally:
                    _close(hud)

    def test_a_revision_above_the_lines_on_screen_does_not_start_the_caption_over(self):
        words = [f"word{i}" for i in range(120)]
        app, hud = self._live(" ".join(words))
        try:
            self._settle(hud)
            self.assertEqual(hud._cap_shown, 120.0)
            self.assertGreater(hud._cap_visible_from, 60)
            revised = list(words)
            revised[2] = "REVISED"  # far above the lines on screen
            app.live_preview = " ".join(revised)
            hud._advance_caption(0.016)
            self.assertEqual(hud._cap_shown, 120.0, "an unseen revision rewound the caption")
            revised[118] = "LATE"  # inside the newest line
            app.live_preview = " ".join(revised)
            hud._advance_caption(0.016)
            self.assertLess(hud._cap_shown, 119.0, "a visible revision was not shown again")
        finally:
            _close(hud)

    def _lines_of(self, hud, words):
        from indicator import wrap_words
        return wrap_words([hud._word_width(w) for w in words], hud._word_width(" "),
                          hud._caption_room())

    def test_only_the_two_lines_that_are_read_count_as_visible(self):
        """The third line is only leaving. A change on it must not rewind the caption, and a
        change above the visible lines must not hide a visible one further on."""
        words = [f"word{i}" for i in range(120)]
        app, hud = self._live(" ".join(words))
        try:
            self._settle(hud)
            spans = self._lines_of(hud, words)
            leaving, read = spans[-3][0], spans[-2][0]
            self.assertEqual(hud._cap_visible_from, read)
            revised = list(words)
            revised[leaving] = "LEAVING"
            app.live_preview = " ".join(revised)
            hud._advance_caption(0.016)
            self.assertEqual(hud._cap_shown, 120.0, "a change on the leaving line rewound the caption")
            revised[2] = "FAR_ABOVE"  # an earlier invisible change, and a visible one together
            revised[read + 1] = "VISIBLE"
            app.live_preview = " ".join(revised)
            hud._advance_caption(0.016)
            self.assertLess(hud._cap_shown, read + 2, "the visible change was masked by an earlier one")
            self.assertGreater(hud._cap_shown, read - 1, "the caption started over")
        finally:
            _close(hud)

    def test_a_word_added_or_removed_above_the_lines_on_screen_keeps_them(self):
        """The words after an insertion move by one place; they did not change."""
        words = [f"word{i}" for i in range(120)]
        for edit, expected in (("insert", 121.0), ("delete", 119.0)):
            with self.subTest(edit=edit):
                app, hud = self._live(" ".join(words))
                try:
                    self._settle(hud)
                    edited = list(words)
                    if edit == "insert":
                        edited.insert(3, "INSERTED")
                    else:
                        del edited[3]
                    app.live_preview = " ".join(edited)
                    hud._advance_caption(0.001)
                    self.assertEqual(hud._cap_shown, expected, "the caption was revealed again")
                    # The lines re-wrap (a word above moved), but no word is hidden again.
                    self.assertTrue(hud._cap_lines[-1].endswith("word119"), hud._cap_lines)
                    self.assertEqual(hud._cap_fading, "")
                finally:
                    _close(hud)

    def test_dropping_a_word_from_the_front_scrolls_nothing(self):
        """Every line re-flows, but no new words appeared, so nothing new slides up."""
        words = [f"word{i}" for i in range(120)]
        app, hud = self._live(" ".join(words))
        try:
            self._settle(hud)
            for drop in range(1, 12):
                app.live_preview = " ".join(words[drop:])
                hud._advance_caption(0.001)
                self.assertEqual(hud._cap_scroll, 0.0, f"a false slide after dropping {drop} word(s)")
                self.assertGreater(hud._cap_shown, 100.0)
        finally:
            _close(hud)

    def test_an_oversized_word_shows_its_end_on_either_row(self):
        """What reaches the painter, not the stored line."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        drawn = []

        class Spy(QPainter):
            def drawText(self, *args):
                if len(args) == 2 and isinstance(args[1], str):
                    drawn.append(args[1])
                return super().drawText(*args)

        long_word = "".join(chr(ord("a") + i % 26) for i in range(200)) + "THE_END"
        _, hud = self._live(f"before {long_word} following")
        try:
            self._settle(hud, 600)
            image = QImage(hud._canvas_w, hud._canvas_h, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = Spy(image)
            hud._paint_caption(painter, QRectF(16, 16, hud._pill_w, hud._pill_h), 0.0)
            painter.end()
            self.assertEqual(hud._cap_lines[-2][-7:], "THE_END")  # the oversized word is the older row
            self.assertTrue(any(text.endswith("THE_END") for text in drawn), drawn)
            self.assertTrue(any(text.endswith("following") for text in drawn), drawn)
        finally:
            _close(hud)

    def test_a_preview_that_drops_its_oldest_words_keeps_the_caption_in_place(self):
        words = [f"word{i}" for i in range(120)]
        app, hud = self._live(" ".join(words))
        try:
            self._settle(hud)
            app.live_preview = " ".join(words[10:] + ["word120", "word121"])  # slid by ten
            hud._advance_caption(0.016)
            self.assertGreater(hud._cap_shown, 105.0, "the caption started over when the head slid")
            self._settle(hud, 200)
            self.assertTrue(hud._cap_lines[-1].endswith("word121"))
        finally:
            _close(hud)

    def test_repeated_words_are_not_mistaken_for_a_slide(self):
        from indicator import DictationIndicator
        old = ["no"] * 6
        self.assertEqual(DictationIndicator._preview_head_dropped(old, ["no"] * 7), 0)
        self.assertEqual(DictationIndicator._preview_head_dropped(
            ["a", "b", "c", "d", "e"], ["c", "d", "e", "f"]), 2)
        self.assertEqual(DictationIndicator._preview_head_dropped(
            ["a", "b", "c", "d", "e"], ["d", "e", "f"]), 0)  # two words in common: too weak

    def test_it_resets_when_the_preview_is_empty_or_not_text(self):
        app, hud = self._live("some live words")
        try:
            for _ in range(60):
                hud._advance_caption(0.016)
            self.assertTrue(hud._cap_lines)
            app.live_preview = ""
            hud._advance_caption(0.016)
            self.assertEqual((hud._cap_lines, hud._cap_words, hud._cap_shown), ([], [], 0.0))
            app.live_preview = MagicMock()  # a test double is not text
            hud._advance_caption(0.016)
            self.assertEqual(hud._cap_lines, [])
        finally:
            _close(hud)

    def test_a_frame_with_captions_paints_without_error(self):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtCore import QRectF
        _, hud = self._live("Hello, this is a longer live caption that needs two rows to be read well.")
        try:
            for _ in range(200):
                hud._advance_caption(0.016)
            image = QImage(hud._canvas_w, hud._canvas_h, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            hud._paint_caption(painter, QRectF(16, 16, hud._pill_w, hud._pill_h), 0.0)
            painter.end()
            self.assertTrue(any(image.pixelColor(x, y).alpha() for x in range(0, image.width(), 3)
                                for y in range(60, image.height(), 3)), "nothing was painted")
        finally:
            _close(hud)


class TestBrandLook(unittest.TestCase):
    """The pill shares the Odicto Mobile / setup page tokens and font."""

    def test_brand_font_loads_from_the_install_root(self):
        import tempfile
        from pathlib import Path

        from indicator import BRAND_FONT_FAMILY, load_brand_font

        _qt_app()
        self.assertEqual(load_brand_font(), BRAND_FONT_FAMILY)
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(load_brand_font(Path(empty)))  # missing file: system face

    def test_the_pill_paints_with_the_brand_font(self):
        from indicator import BRAND_FONT_FAMILY

        _, hud = _hud()
        try:
            self.assertEqual(hud._font.family(), BRAND_FONT_FAMILY)
            self.assertEqual(hud._font_chip.family(), BRAND_FONT_FAMILY)
        finally:
            _close(hud)

    def test_theme_colours_match_the_setup_page_tokens(self):
        import re
        from pathlib import Path

        from indicator import _Theme
        from paths import ROOT

        css = (Path(ROOT) / "assets" / "setup_template.html").read_text(encoding="utf-8")
        tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6});", css))
        pairs = {"accent": _Theme.accent, "action": _Theme.action, "action-hi": _Theme.action_hi,
                 "on-action": _Theme.on_action, "text": _Theme.text, "muted": _Theme.muted,
                 "raised-2": _Theme.raised, "danger": _Theme.danger, "warn": _Theme.warn,
                 "canvas": _Theme.ink}
        for token, colour in pairs.items():
            self.assertEqual(colour.name(), tokens[token].lower(), token)

    def test_each_state_has_the_phone_disc(self):
        from indicator import orb_kind

        self.assertEqual(orb_kind(GuiState.RECORDING), "listen")
        self.assertEqual(orb_kind(GuiState.BOOTING), "wait")
        self.assertEqual(orb_kind(GuiState.PROCESSING), "wait")
        self.assertEqual(orb_kind(GuiState.SUCCESS, "success"), "done")
        self.assertEqual(orb_kind(GuiState.SUCCESS, "clipboard_not_restored"), "notice")
        self.assertEqual(orb_kind(GuiState.ERROR, "empty"), "error")
        self.assertEqual(orb_kind(GuiState.RESET), "reset")


class TestMotionCurves(unittest.TestCase):
    def test_brand_easing_decelerates_and_exit_easing_accelerates(self):
        from indicator import ease_accelerate, ease_standard

        for curve in (ease_standard, ease_accelerate):
            self.assertEqual(curve(0.0), 0.0)
            self.assertEqual(curve(1.0), 1.0)
            self.assertEqual(curve(-1.0), 0.0)
            self.assertEqual(curve(2.0), 1.0)
            samples = [curve(i / 50) for i in range(51)]
            self.assertEqual(samples, sorted(samples))  # never runs backwards
        self.assertGreater(ease_standard(0.25), 0.5)    # most of the move happens early
        self.assertLess(ease_accelerate(0.25), 0.2)     # an exit starts gently, ends fast

    def test_exit_is_shorter_than_entry_and_both_are_brief(self):
        from indicator import _ENTER_S, _LEAVE_S

        self.assertLess(_LEAVE_S, _ENTER_S)
        self.assertLessEqual(_ENTER_S, 0.4)

    def test_disc_pop_overshoots_once_and_ends_exactly_at_rest(self):
        from indicator import _POP_FROM, pop_scale

        self.assertAlmostEqual(pop_scale(0.0), _POP_FROM)
        peak = max(pop_scale(i / 200) for i in range(201))
        self.assertGreater(peak, 1.0)
        self.assertLess(peak, 1.03)  # a soft overshoot, not a bounce
        self.assertEqual(pop_scale(1.0), 1.0)


class TestSizeSpring(unittest.TestCase):
    def _spring(self):
        from indicator import _Spring

        s = _Spring(100.0)
        s.value(0.0)
        s.to(300.0, 0.0)
        return s

    def test_the_result_does_not_depend_on_the_frame_interval(self):
        a, b, c = self._spring(), self._spring(), self._spring()
        for i in range(1, 16):
            a.value(i * 0.016)
        b.value(0.24)
        for t in (0.03, 0.05, 0.11, 0.17, 0.24):
            c.value(t)
        self.assertAlmostEqual(a.value(0.24), b.value(0.24), places=6)
        self.assertAlmostEqual(c.value(0.24), b.value(0.24), places=6)

    def test_size_never_overshoots_and_settles_exactly(self):
        s = self._spring()
        values = [s.value(i * 0.01) for i in range(1, 120)]
        self.assertTrue(all(v <= 300.0 for v in values))
        self.assertEqual(values, sorted(values))
        self.assertGreater(s.value(0.33), 297.0)  # within 1 % in about a third of a second
        self.assertTrue(s.done(1.5))
        self.assertEqual(s.value(1.5), 300.0)     # exact, so the frame can go static

    def test_retarget_keeps_the_motion_continuous(self):
        s = self._spring()
        before = s.value(0.1)
        s.to(50.0, 0.1)
        self.assertEqual(s.value(0.1), before)               # no jump at the retarget
        self.assertGreater(s.value(0.116), before)           # momentum carries on briefly
        self.assertFalse(s.done(0.1))


class TestWaveform(unittest.TestCase):
    def _recording_hud(self, level):
        recorder = MagicMock()
        recorder.get_waveform.side_effect = lambda n: [level] * n
        app, hud = _hud(state=AppState.RECORDING, recorder=recorder)
        hud._sync_from_app()
        return app, hud

    def test_voice_rises_fast_and_falls_slowly(self):
        app, hud = self._recording_hud(0.5)
        try:
            hud._advance_wave(0.05)
            rise = hud._level
            self.assertGreater(rise, 0.8 * min(1.0, 0.75 ** 0.7))  # most of the way in 50 ms
            app.recorder.get_waveform.side_effect = lambda n: [0.0] * n
            hud._advance_wave(0.05)
            self.assertGreater(hud._level, 0.5 * rise)  # release is slower than attack
        finally:
            _close(hud)

    def test_smoothing_is_the_same_at_any_frame_interval(self):
        _, one = self._recording_hud(0.4)
        _, three = self._recording_hud(0.4)
        try:
            one._advance_wave(0.048)
            for _ in range(3):
                three._advance_wave(0.016)
            self.assertAlmostEqual(one._level, three._level, places=9)
            for a, b in zip(one._bars, three._bars):
                self.assertAlmostEqual(a, b, places=9)
        finally:
            _close(one)
            _close(three)

    def test_centre_bars_move_more_than_end_bars(self):
        _, hud = self._recording_hud(0.6)
        try:
            for _ in range(20):
                hud._advance_wave(0.016)
            mid = len(hud._bars) // 2
            self.assertGreater(hud._bars[mid], 2 * hud._bars[0])
            self.assertAlmostEqual(hud._bars[0], hud._bars[-1])
        finally:
            _close(hud)

    def test_a_broken_recorder_reads_as_silence(self):
        recorder = MagicMock()
        recorder.get_waveform.side_effect = RuntimeError("stream gone")
        _, hud = _hud(state=AppState.RECORDING, recorder=recorder)
        try:
            hud._sync_from_app()
            hud._advance_wave(0.1)
            self.assertEqual(hud._level, 0.0)
        finally:
            _close(hud)


class TestDiscCrossFade(unittest.TestCase):
    """The disc cross-fades from the disc on screen, whatever changed it."""

    def _open_done(self):
        app, hud = _hud(state=AppState.PROCESSING, paste_delivered=True, last_status="success")
        hud._sync_from_app()
        hud._phase = "open"
        hud._glyph_fade = 1.0
        self.assertEqual(hud._orb_now(), "done")
        return app, hud

    def test_reset_flash_fades_from_done_not_a_stale_disc(self):
        _, hud = self._open_done()
        try:
            hud._do_flash_reset()
            self.assertEqual(hud._orb_now(), "reset")
            self.assertEqual(hud._last_orb, "done")
            self.assertEqual(hud._glyph_fade, 0.0)
        finally:
            _close(hud)

    def test_a_notice_set_while_processing_still_cross_fades(self):
        app, hud = self._open_done()
        try:
            app.last_status = "ai_fallback"  # main sets it before IDLE, with no notify
            self.assertEqual(hud._orb_now(), "notice")
            self.assertEqual(hud._last_orb, "done")
            self.assertEqual(hud._glyph_fade, 0.0)
        finally:
            _close(hud)

    def test_waking_from_the_slow_tick_does_not_count_the_wait(self):
        _, hud = _hud()
        try:
            hud._tick.setInterval(250)
            hud._last_tick = 0.0
            hud._wake_tick()
            self.assertEqual(hud._tick.interval(), 16)
            self.assertIsNone(hud._last_tick)  # the next tick steps one nominal frame
        finally:
            _close(hud)


class TestShadowCache(unittest.TestCase):
    def test_a_settled_pill_draws_its_shadow_once_and_a_moving_one_never_caches(self):
        app, hud = _hud(state=AppState.IDLE, last_status="empty")
        try:
            hud._sync_from_app()
            hud._w_tw.to(hud._pill_w + 60, time.monotonic())  # a size change in progress
            hud._phase = "open"
            hud._resize_hold = None
            hud.grab()
            self.assertIsNone(hud._shadow_cache)
            hud._w_tw.snap(hud._pill_w)
            hud._h_tw.snap(hud._pill_h)
            hud.grab()
            first = hud._shadow_cache[1]
            hud.grab()
            self.assertIs(hud._shadow_cache[1], first)  # reused, not redrawn
        finally:
            _close(hud)


if __name__ == "__main__":
    unittest.main()
