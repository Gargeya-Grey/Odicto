"""HUD motion and paste-delivery contract. No real paste, microphone or hooks.

Done shows on the frame the text is delivered; the clipboard wait and
PROCESSING still run until the restore finishes.

The widget (indicator.DictationIndicator) owns the state, the clock and the window. The look
is hud_leaf.LeafPainter (tests/test_hud_leaf.py). These tests check what the widget feeds the
painter and when it repaints. They never compare pixels (docs/engineering-notes.md, 3.5).
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import math
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

import hud_leaf as hl
import typer
from app_state import AppState
from config import Config
from indicator import (
    GuiState,
    _BLEED,
    _CAPTION_PAD,
    _COMPACT_H,
    _DT_MAX,
    _LIVE_H,
    _STEM_SETTLE_S,
    cubic_bezier,
)
from tests.test_clipboard_safety import ClipboardSafetyBase
from tests.test_reliability import app_fixture

FIELDS = tuple(f.name for f in dataclasses.fields(hl.Opening))


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


def _settle(hud) -> None:
    """Jump to the end of every animation, the way real time would."""
    for tween in (hud._w_tw, hud._h_tw):
        tween.snap(tween.b)
    hud._phase = "open"
    hud._phase_t0 = time.monotonic() - 10.0
    hud._glyph_fade = 1.0
    hud._voice.clear()


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


class TestLeafWidth(unittest.TestCase):
    """The window and the label width follow the painter's own fit."""

    def test_the_target_label_width_is_the_painters_fit(self):
        from PySide6.QtGui import QFontMetricsF

        app, hud = _hud(state=AppState.PROCESSING, use_llm=True)
        try:
            hud._sync_from_app()
            metrics = QFontMetricsF(hud._font)
            with_ai = metrics.horizontalAdvance("Thinking") + 24.0   # the AI glyph and its gap
            self.assertAlmostEqual(hud._label_target, with_ai, places=6)
            app.use_llm = False
            hud._sync_from_app()  # "Transcribing", no AI mark
            self.assertAlmostEqual(hud._label_target, metrics.horizontalAdvance("Transcribing"),
                                   places=6)
        finally:
            _close(hud)

    def test_listening_reserves_room_for_the_dots(self):
        from PySide6.QtGui import QFontMetricsF

        _, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            word = QFontMetricsF(hud._font).horizontalAdvance("Listening")
            self.assertAlmostEqual(hud._label_target, word + 17.0, places=6)
        finally:
            _close(hud)

    def test_a_long_label_is_wider_than_a_short_one(self):
        app, hud = _hud()
        try:
            hud.gui_state = GuiState.SUCCESS
            done = hud._layout_target()[0]
            app.last_status = "clipboard_not_restored"
            self.assertGreater(hud._layout_target()[0], done + 40.0)
        finally:
            _close(hud)

    def test_the_window_covers_the_leaf_the_card_and_the_shadow(self):
        app, hud = _hud(state=AppState.PROCESSING, use_llm=True)
        try:
            hud._sync_from_app()
            self.assertEqual(hud._canvas_w % 4, 0)
            self.assertEqual(hud._canvas_h % 4, 0)
            self.assertGreaterEqual(hud._canvas_w, hl.leaf_width(hud._label_target) + 2 * _BLEED)
            self.assertGreaterEqual(hud._canvas_h, _COMPACT_H + 2 * _BLEED)
            self.assertEqual(_BLEED, 32)  # the painter's shadow reaches 28 px; 12 px of rise
            app.state = AppState.RECORDING
            app.live_active = True
            hud._sync_from_app()
            self.assertGreaterEqual(hud._canvas_w, hl.CARD_MIN_W + 2 * _BLEED)
            self.assertGreaterEqual(hud._canvas_h, _LIVE_H + 2 * _BLEED)
            self.assertEqual(_LIVE_H, 100)  # leaf 36 + gap 6 + card 58
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
            _settle(hud)
            done_w = hud._label_target
            resizes = []
            real = hud.setFixedSize
            with patch.object(hud, "setFixedSize",
                              side_effect=lambda *a: resizes.append(a) or real(*a)):
                app.state = AppState.IDLE
                app.last_status = "clipboard_not_restored"
                hud._sync_from_app()
                self.assertGreater(hud._label_target, done_w)
                # The window grew once, up front, to hold the target.
                self.assertEqual(len(resizes), 1)
                self.assertGreaterEqual(hud._canvas_w,
                                        hl.leaf_width(hud._label_target) + 2 * _BLEED)
                self.assertEqual(hud._w_tw.b, hud._label_target)
                start = time.monotonic()
                values = [hud._w_tw.value(start + i * 0.016) for i in range(60)]
                for _ in range(30):
                    hud._on_tick()
                self.assertEqual(len(resizes), 1, "the window resized again while the width eased")
            self.assertGreaterEqual(values[0], done_w)
            self.assertLess(values[0], hud._label_target)
            self.assertEqual(values, sorted(values))                  # one smooth move
            self.assertLessEqual(max(values), hud._label_target)      # no overshoot
            self.assertEqual(values[-1], hud._label_target)           # and it ends exactly
        finally:
            _close(hud)

    def test_a_label_change_with_no_state_change_is_not_clipped(self):
        """F7 stop: 'Finalizing', then the app clears last_status and the label is 'Transcribing'."""
        from PySide6.QtGui import QFontMetricsF

        app, hud = _hud(state=AppState.PROCESSING, last_status="finalizing")
        try:
            hud._sync_from_app()
            _settle(hud)
            finalizing_w = hud._label_target
            app.last_status = None  # process_and_paste starts: same state, longer word
            need = QFontMetricsF(hud._font).horizontalAdvance("Transcribing")
            self.assertGreater(need, finalizing_w, "the premise: the longer word needs a wider label")
            hud._on_tick()  # one tick refits the label
            self.assertAlmostEqual(hud._label_target, need, places=6)
            frame = hud._leaf_frame(time.monotonic() + 2.0)  # after the spring has settled
            self.assertEqual(frame.label, "Transcribing")
            self.assertEqual(frame.label_w, hud._label_target)
            self.assertGreaterEqual(frame.label_w, need)
        finally:
            _close(hud)

    def test_hide_plays_the_exit_then_hides(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._do_hide()
            self.assertEqual(hud._phase, "leaving")
            self.assertTrue(hud.isVisible(), "the exit must play on screen")
            start = hud._phase_t0
            self.assertEqual(hud._opening(start), hl.OPEN)
            self.assertLess(hud._opening(start + 0.2).open_h, 1.0)
            self.assertEqual(hud._opening(start + 0.2).content, 0.0)
            end = start + hl.LEAVE_S + 0.01
            hud._advance_phase(end)
            self.assertEqual(hud._phase, "hidden")
            self.assertEqual(hud._opening(end).fade, 0.0)
            self.assertFalse(hud.isVisible())
        finally:
            _close(hud)

    def test_a_hide_during_the_entry_starts_from_the_current_opening_and_never_grows(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            now = time.monotonic()
            hud._phase, hud._phase_t0 = "entering", now - 0.3
            base = hud._opening(now)
            self.assertNotEqual(base, hl.OPEN)
            self.assertGreater(base.open_h, 0.0)
            hud._begin_leave(now)
            self.assertEqual(hud._phase, "leaving")
            self.assertEqual(hud._opening(now), base)
            for i in range(0, 130):
                o = hud._opening(now + i * 0.004)
                for name in FIELDS:
                    self.assertLessEqual(getattr(o, name), getattr(base, name), (i, name))
            self.assertEqual(hud._opening(now + hl.LEAVE_S).fade, 0.0)
        finally:
            _close(hud)

    def test_a_show_during_the_exit_opens_again_from_where_the_exit_is(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            now = time.monotonic()
            hud._begin_leave(now)
            later = now + 0.08
            closing = hud._opening(later)
            self.assertLess(closing.tip, 1.0)
            hud._begin_show(later)
            self.assertEqual(hud._phase, "entering")
            self.assertEqual(hud._opening(later), closing)  # no jump at the turn
            previous = closing
            for i in range(1, 60):
                o = hud._opening(later + i * 0.012)
                for name in FIELDS:
                    self.assertGreaterEqual(getattr(o, name), getattr(previous, name), (i, name))
                previous = o
            self.assertEqual(hud._opening(later + hl.ENTER_S), hl.OPEN)
        finally:
            _close(hud)

    def test_a_show_during_the_exit_does_not_play_the_vein_sweep_again(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            now = time.monotonic()
            hud._begin_leave(now)
            hud._begin_show(now + 0.15)
            for t in (0.0, 0.2, 0.4, 0.6):
                frame = hud._leaf_frame(now + 0.15 + t)
                self.assertGreaterEqual(frame.since_enter, 1.1 * hl.ENTER_S, t)  # past the sweep
        finally:
            _close(hud)


class TestLiveCaption(unittest.TestCase):
    """F7 captions: wrapped, revealed smoothly, never wider than the card."""

    def _live(self, preview=""):
        app, hud = _hud(state=AppState.RECORDING, live_active=True, live_preview=preview)
        hud._sync_from_app()
        return app, hud

    def _card_image(self, hud):
        """The caption painted alone into a card-sized image: (image, card rect)."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        rect = QRectF(16, 16, hud._card_width(), hl.CARD_H)
        image = QImage(int(rect.width()) + 32, hl.CARD_H + 32,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        hud._paint_caption(painter, rect, 1.0)
        painter.end()
        return image, rect

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

    def test_lines_fit_the_card_and_a_new_line_slides_up(self):
        from indicator import _CAPTION_LINE, _CAPTION_LINES
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
                                         hud._card_width() - 2 * _CAPTION_PAD, line)
            self.assertTrue(slid, "no line ever slid up")
            self.assertEqual(hud._cap_scroll, 0.0, "the slide did not settle")
            self.assertTrue(hud._cap_lines[-1].endswith("word59"))
        finally:
            _close(hud)

    def test_the_caption_room_is_the_cards_inner_width(self):
        _, hud = self._live("hello")
        try:
            self.assertEqual(hud._card_width(), 420.0)   # a short label: the card keeps its minimum
            self.assertEqual(hud._caption_room(), 420.0 - 2 * _CAPTION_PAD - 4)
        finally:
            _close(hud)

    def _settle_caption(self, hud, frames=1500):
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
                if hud._cap_scroll > previous + 5.0:  # a slide jumps by a whole row
                    slides += 1
                previous = hud._cap_scroll
            self.assertEqual(seen, sorted(seen), "the newest line moved back")
            self.assertLessEqual(set(seen) - {0}, real_starts, "a line began that is not in the text")
            lines = len(real_starts)
            self.assertLessEqual(slides, lines - 1, "more slides than lines that began")
            self.assertGreaterEqual(slides, 1)
        finally:
            _close(hud)

    def test_a_word_wider_than_the_card_shows_its_end_and_stays_inside(self):
        long_word = "a" * 20 + "".join(chr(ord("b") + i % 20) for i in range(160)) + "END"
        for revealed in (True, False):  # fully shown, then mid fade-in
            with self.subTest(revealed=revealed):
                _, hud = self._live(f"short words then {long_word}")
                try:
                    self._settle_caption(hud, 400)
                    if not revealed:
                        hud._cap_shown = 3.5
                        hud._advance_caption(0.0)
                        hud._cap_shown = 3.5
                        hud._advance_caption(0.0)
                    image, rect = self._card_image(hud)
                    low = rect.left() + _CAPTION_PAD - 2
                    high = rect.right() - _CAPTION_PAD + 2
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
            self._settle_caption(hud)
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
            self._settle_caption(hud)
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
                    self._settle_caption(hud)
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
            self._settle_caption(hud)
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
            self._settle_caption(hud, 600)
            image = QImage(int(hud._card_width()) + 32, hl.CARD_H + 32,
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = Spy(image)
            hud._paint_caption(painter, QRectF(16, 16, hud._card_width(), hl.CARD_H), 1.0)
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
            self._settle_caption(hud)
            app.live_preview = " ".join(words[10:] + ["word120", "word121"])  # slid by ten
            hud._advance_caption(0.016)
            self.assertGreater(hud._cap_shown, 105.0, "the caption started over when the head slid")
            self._settle_caption(hud, 200)
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

    def test_a_card_with_captions_paints_text_inside_the_card(self):
        _, hud = self._live("Hello, this is a longer live caption that needs two rows to be read well.")
        try:
            self._settle_caption(hud, 200)
            image, rect = self._card_image(hud)
            lit = [(x, y) for x in range(image.width()) for y in range(image.height())
                   if image.pixelColor(x, y).alpha() > 0]
            self.assertGreater(len(lit), 50, "nothing was painted")
            self.assertGreaterEqual(min(y for _, y in lit), rect.top())
            self.assertLessEqual(max(y for _, y in lit), rect.bottom())
        finally:
            _close(hud)

    def test_with_no_words_yet_the_card_asks_for_speech(self):
        drawn = []
        from PySide6.QtGui import QPainter

        class Spy(QPainter):
            def drawText(self, *args):
                if len(args) == 2 and isinstance(args[1], str):
                    drawn.append(args[1])
                return super().drawText(*args)

        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage
        _, hud = self._live("")
        try:
            image = QImage(500, 100, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = Spy(image)
            hud._paint_caption(painter, QRectF(16, 16, hud._card_width(), hl.CARD_H), 1.0)
            painter.end()
            self.assertEqual(drawn, ["Speak, then tap F7 to insert"])
        finally:
            _close(hud)


class TestLiveLayout(unittest.TestCase):
    """The card sits above the leaf; the leaf stays where it is."""

    def test_the_card_is_above_the_leaf_and_the_leaf_does_not_move(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            frame = hud._leaf_frame(time.monotonic())
            leaf_bottom_compact = hud.y() + frame.bottom
            window_h_compact = hud._canvas_h
            self.assertEqual(hud._pill_h, _COMPACT_H)

            app.live_active = True
            app.live_preview = "so the plan for next week is to ship the new pill first"
            hud._sync_from_app()
            self.assertEqual(hud._pill_h, _LIVE_H)
            _settle(hud)
            frame = hud._leaf_frame(time.monotonic())
            self.assertGreater(hud._canvas_h, window_h_compact)   # the window grew upward
            self.assertEqual(hud.y() + frame.bottom, leaf_bottom_compact)  # the leaf did not move
            geo = hl.leaf_geometry(frame)
            rect = hl.card_rect(geo, 1.0, 1.0)
            self.assertLess(rect.bottom(), geo.top)                       # above the leaf
            self.assertEqual(geo.top - rect.bottom(), hl.CARD_GAP)
            self.assertAlmostEqual(rect.center().x(), hud._canvas_w / 2.0, delta=1.0)  # centred
            self.assertGreaterEqual(rect.top(), 28.0)                     # room for the card's shadow
            self.assertLessEqual(rect.right() + 28.0, hud._canvas_w)

            app.live_active = False
            app.live_preview = ""
            hud._sync_from_app()
            self.assertEqual(hud._pill_h, _COMPACT_H)
            _settle(hud)
            hud._on_tick()   # the window shrinks once the card has gone
            self.assertEqual(hud._canvas_h, window_h_compact)
            frame = hud._leaf_frame(time.monotonic())
            self.assertEqual(hud.y() + frame.bottom, leaf_bottom_compact)
        finally:
            _close(hud)

    def test_the_card_fades_in_with_the_height_spring(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            now = time.monotonic()
            self.assertEqual(hud._card_progress(now), 0.0)
            app.live_active = True
            hud._sync_from_app()
            values = [hud._card_progress(now + i * 0.016) for i in range(60)]
            self.assertEqual(values, sorted(values))
            self.assertLess(values[1], 1.0)
            self.assertEqual(values[-1], 1.0)
        finally:
            _close(hud)

    def test_a_frame_with_captions_paints_the_card_and_its_words(self):
        app, hud = _hud(state=AppState.RECORDING, live_active=True,
                        live_preview="Hello, this is a longer live caption that needs two rows.")
        try:
            hud._sync_from_app()
            _settle(hud)
            for _ in range(200):
                hud._advance_caption(0.016)
            image = hud.grab().toImage()
            rect = hl.card_rect(hl.leaf_geometry(hud._leaf_frame(time.monotonic())), 1.0, 1.0)
            inside = [(x, y) for x in range(int(rect.left()) + 4, int(rect.right()) - 4)
                      for y in range(int(rect.top()) + 4, int(rect.bottom()) - 4)]
            solid = sum(1 for x, y in inside if image.pixelColor(x, y).alpha() > 200)
            self.assertGreater(solid, 0.9 * len(inside), "the card surface is missing")
            bright = sum(1 for x, y in inside if image.pixelColor(x, y).red() > 150)
            self.assertGreater(bright, 40, "no caption text on the card")
        finally:
            _close(hud)


class TestStemAndAiMark(unittest.TestCase):
    """State to stem and AI mark, as the painter receives them."""

    def test_every_state_and_status_maps_to_a_stem(self):
        from indicator import (ERROR_STATUS_LABELS, ERROR_STATUSES, SUCCESS_NOTICE_LABELS,
                               SUCCESS_STATUSES, orb_kind)

        for state in GuiState:
            if state is GuiState.HIDDEN:
                continue
            statuses = [None, "unknown_status"]
            if state is GuiState.SUCCESS:
                statuses += list(SUCCESS_STATUSES) + list(SUCCESS_NOTICE_LABELS)
            if state is GuiState.ERROR:
                statuses += list(ERROR_STATUSES) + list(ERROR_STATUS_LABELS)
            for status in statuses:
                with self.subTest(state=state.name, status=status):
                    self.assertIsInstance(hl.STEM_FOR_ORB[orb_kind(state, status)], hl.Stem)

    def test_the_widget_picks_the_stem_from_the_state(self):
        cases = [
            (dict(state=AppState.RECORDING), hl.Stem.LISTEN),
            (dict(state=AppState.PROCESSING), hl.Stem.WORK),
            (dict(state=AppState.PROCESSING, paste_delivered=True, last_status="success"), hl.Stem.DONE),
            (dict(state=AppState.PROCESSING, paste_delivered=True, last_status="ai_fallback"),
             hl.Stem.NOTICE),
            (dict(state=AppState.IDLE, last_status="empty"), hl.Stem.ERROR),
        ]
        for attrs, stem in cases:
            with self.subTest(stem=stem.name):
                _, hud = _hud(**attrs)
                try:
                    hud._sync_from_app()
                    self.assertIs(hud._stem, stem)
                    self.assertIs(hud._leaf_frame(time.monotonic()).stem, stem)
                finally:
                    _close(hud)

    def test_the_ai_mark_is_on_only_for_recording_and_processing_with_ai(self):
        expected = {GuiState.BOOTING: False, GuiState.RECORDING: True, GuiState.PROCESSING: True,
                    GuiState.SUCCESS: False, GuiState.ERROR: False, GuiState.RESET: False}
        for use_llm in (True, False):
            for state, with_ai in expected.items():
                with self.subTest(state=state.name, use_llm=use_llm):
                    _, hud = _hud(use_llm=use_llm)
                    try:
                        hud.gui_state = state
                        frame = hud._leaf_frame(time.monotonic())
                        self.assertIs(frame.ai, use_llm and with_ai)
                    finally:
                        _close(hud)

    def test_the_frame_carries_the_label_the_clock_and_the_window_geometry(self):
        _, hud = _hud(state=AppState.RECORDING, use_llm=True)
        try:
            hud._sync_from_app()
            _settle(hud)
            now = time.monotonic()
            frame = hud._leaf_frame(now)
            self.assertEqual(frame.label, "Listening")
            self.assertIs(frame.voice, hud._voice)
            self.assertEqual(frame.label_w, hud._label_target)
            self.assertEqual(frame.cx, hud._canvas_w / 2.0)
            self.assertEqual(frame.bottom, float(hud._canvas_h - _BLEED))
            self.assertEqual(frame.opening, hl.OPEN)
            self.assertEqual(frame.now, now)
            self.assertGreaterEqual(frame.since_enter, 10.0)
            self.assertEqual(frame.since_state, _STEM_SETTLE_S)
        finally:
            _close(hud)


class TestHudSize(unittest.TestCase):
    """HUD_SIZE grows the window and the canvas; the bottom offset and the centre stay."""

    def _sized(self, size, **attrs):
        with patch.object(Config, "HUD_SIZE", size):
            app, hud = _hud(**attrs)
        _settle(hud)
        return app, hud

    def test_the_style_carries_the_configured_size(self):
        for size in hl.HUD_SIZE_SCALE:
            _, hud = self._sized(size)
            try:
                self.assertEqual(hud._style.size, size)
                self.assertEqual(hud._scale, hl.HUD_SIZE_SCALE[size])
            finally:
                _close(hud)

    def test_the_window_holds_the_scaled_leaf_and_its_shadow(self):
        for size, scale in hl.HUD_SIZE_SCALE.items():
            for live in (False, True):
                app, hud = self._sized(size, state=AppState.RECORDING, live_active=live)
                try:
                    hud._sync_from_app()
                    _settle(hud)
                    hud._fit_canvas(shrink=True)
                    inner_h = (_LIVE_H if live else _COMPACT_H) * scale
                    inner_w = max(hl.leaf_width(hud._label_target),
                                  hl.CARD_MIN_W if live else 0) * scale
                    self.assertGreaterEqual(hud._bleed, 28 * scale, "the shadow reaches 28 design px")
                    self.assertGreaterEqual(hud._canvas_w, inner_w + 2 * hud._bleed, (size, live))
                    self.assertGreaterEqual(hud._canvas_h, inner_h + 2 * hud._bleed, (size, live))
                    self.assertEqual((hud._canvas_w % 4, hud._canvas_h % 4), (0, 0))
                    self.assertEqual((hud.width(), hud.height()), (hud._canvas_w, hud._canvas_h))
                finally:
                    _close(hud)

    def test_a_larger_size_makes_a_larger_window(self):
        sizes = []
        for size in hl.HUD_SIZE_SCALE:
            _, hud = self._sized(size, state=AppState.RECORDING, live_active=True)
            try:
                hud._sync_from_app()
                _settle(hud)
                hud._fit_canvas(shrink=True)
                sizes.append((hud.width(), hud.height()))
            finally:
                _close(hud)
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(len(set(sizes)), 3)

    def test_the_bottom_offset_and_the_centre_do_not_change_with_the_size(self):
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen().availableGeometry()
        for size in hl.HUD_SIZE_SCALE:
            for live in (False, True):
                _, hud = self._sized(size, state=AppState.RECORDING, live_active=live)
                try:
                    hud._sync_from_app()
                    _settle(hud)
                    hud._fit_canvas(shrink=True)
                    leaf_bottom = hud.y() + hud._canvas_h - hud._bleed
                    self.assertEqual(leaf_bottom, screen.y() + screen.height() - 64, (size, live))
                    self.assertLessEqual(abs(hud.x() + hud._canvas_w / 2 - (screen.x() + screen.width() / 2)), 0.5)
                finally:
                    _close(hud)

    def test_the_frame_is_in_design_pixels(self):
        for size, scale in hl.HUD_SIZE_SCALE.items():
            _, hud = self._sized(size, state=AppState.RECORDING)
            try:
                hud._sync_from_app()
                _settle(hud)
                frame = hud._leaf_frame(time.monotonic())
                self.assertAlmostEqual(frame.cx * scale, hud._canvas_w / 2.0, places=9)
                self.assertAlmostEqual(frame.bottom * scale, hud._canvas_h - hud._bleed, places=9)
            finally:
                _close(hud)

    def test_the_card_and_the_leaf_paint_inside_the_window_at_every_size(self):
        from PySide6.QtGui import QImage
        for size in hl.HUD_SIZE_SCALE:
            app, hud = self._sized(size, state=AppState.RECORDING, live_active=True,
                                   live_preview="Hello, this is a live caption long enough to need two rows.")
            try:
                hud._sync_from_app()
                _settle(hud)
                hud._fit_canvas(shrink=True)
                for _ in range(120):
                    hud._advance_caption(0.016)
                image = hud.grab().toImage()
                self.assertEqual((image.width(), image.height()), (hud.width(), hud.height()))
                lit = [(x, y) for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
                       if image.pixelColor(x, y).alpha() > 0]
                self.assertTrue(lit, size)
                # the shadow fades out before the window edge (a cut shadow shows a hard line)
                for x, y in lit:
                    self.assertTrue(2 <= x < image.width() - 2 and 2 <= y < image.height() - 2,
                                    (size, x, y))
            finally:
                _close(hud)

    def test_the_caption_scales_with_the_leaf(self):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        for size, scale in hl.HUD_SIZE_SCALE.items():
            _, hud = self._sized(size, state=AppState.RECORDING, live_active=True)
            try:
                hud._sync_from_app()
                rect = QRectF(10, 10, hud._card_width(), hl.CARD_H)
                image = QImage(int((rect.width() + 20) * scale) + 4, int((hl.CARD_H + 20) * scale) + 4,
                               QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(0)
                p = QPainter(image)
                hud._paint_caption(p, rect, 1.0)
                p.end()
                lit = [(x, y) for x in range(image.width()) for y in range(image.height())
                       if image.pixelColor(x, y).alpha() > 0]
                self.assertTrue(lit, size)
                # the prompt text sits inside the card, scaled with it
                self.assertGreaterEqual(min(y for _, y in lit), rect.top() * scale - 1)
                self.assertLessEqual(max(y for _, y in lit), rect.bottom() * scale + 1)
                self.assertGreaterEqual(min(x for x, _ in lit), (rect.left() + 18) * scale - 2)
                self.assertLessEqual(max(x for x, _ in lit), rect.right() * scale)
            finally:
                _close(hud)


class TestLeafStyle(unittest.TestCase):
    def test_every_config_choice_is_a_painter_style_with_the_same_default(self):
        import config

        self.assertEqual(
            config.HUD_CHOICES,
            {"HUD_" + field.upper(): values for field, values in hl.HUD_STYLE_CHOICES.items()})
        self.assertEqual(
            hl.HudStyle(),
            hl.HudStyle(**{f: config.ENV_DEFAULTS["HUD_" + f.upper()] for f in hl.HUD_STYLE_CHOICES}))
        self.assertEqual(hl.HudStyle().size, config.ENV_DEFAULTS["HUD_SIZE"])

    def _spy(self, seen):
        class Spy(hl.LeafPainter):
            def paint(self, p, frame, style):
                seen.append((frame, style))
                super().paint(p, frame, style)

        return Spy

    def test_hud_shape_capsule_reaches_the_painter(self):
        seen = []
        with patch("indicator.LeafPainter", self._spy(seen)), \
                patch.object(Config, "HUD_WAVEFORM", "reach"), \
                patch.object(Config, "HUD_TEXTURE", "veins"), \
                patch.object(Config, "HUD_EDGE", "rim"), \
                patch.object(Config, "HUD_SHAPE", "capsule"):
            _, hud = _hud(state=AppState.RECORDING)
            try:
                hud._sync_from_app()
                hud.grab()
            finally:
                _close(hud)
        self.assertTrue(seen, "the painter was never called")
        self.assertEqual(seen[-1][1], hl.HudStyle(shape="capsule"))

    def test_all_four_choices_reach_the_painter(self):
        seen = []
        with patch("indicator.LeafPainter", self._spy(seen)), \
                patch.object(Config, "HUD_WAVEFORM", "stream"), \
                patch.object(Config, "HUD_TEXTURE", "plain"), \
                patch.object(Config, "HUD_EDGE", "soft"), \
                patch.object(Config, "HUD_SHAPE", "capsule"):
            _, hud = _hud(state=AppState.RECORDING)
            try:
                hud.grab()
            finally:
                _close(hud)
        self.assertEqual(seen[-1][1], hl.HudStyle("stream", "plain", "soft", "capsule"))

    def test_an_unknown_value_logs_one_line_and_uses_the_default_style(self):
        seen = []
        out = io.StringIO()
        with patch("indicator.LeafPainter", self._spy(seen)), \
                patch.object(Config, "HUD_WAVEFORM", "reach"), \
                patch.object(Config, "HUD_TEXTURE", "veins"), \
                patch.object(Config, "HUD_EDGE", "rim"), \
                patch.object(Config, "HUD_SHAPE", "hexagon"), contextlib.redirect_stdout(out):
            _, hud = _hud(state=AppState.RECORDING)
            try:
                hud.grab()
            finally:
                _close(hud)
        self.assertEqual(seen[-1][1], hl.HudStyle())
        lines = [line for line in out.getvalue().splitlines() if "HUD style" in line]
        self.assertEqual(len(lines), 1, out.getvalue())

    def test_the_style_is_built_once(self):
        seen = []
        with patch("indicator.LeafPainter", self._spy(seen)):
            _, hud = _hud(state=AppState.RECORDING)
            try:
                hud.grab()
                first = seen[-1][1]
                hud.grab()
                self.assertIs(seen[-1][1], first)
            finally:
                _close(hud)

    def test_the_label_font_the_painter_gets_is_the_brand_family(self):
        from indicator import BRAND_FONT_FAMILY

        received = []

        class Spy(hl.LeafPainter):
            def __init__(self, font):
                received.append(font)
                super().__init__(font)

        with patch("indicator.LeafPainter", Spy):
            _, hud = _hud()
            try:
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0].family(), BRAND_FONT_FAMILY)
                self.assertEqual(received[0].pixelSize(), hl.LABEL_PX)
            finally:
                _close(hud)


class TestVoice(unittest.TestCase):
    def _recording(self, bands=None, raises=None, recorder=True):
        rec = MagicMock()
        if raises is not None:
            rec.get_bands.side_effect = raises
        else:
            rec.get_bands.side_effect = lambda n: list(bands)
        app, hud = _hud(state=AppState.RECORDING, recorder=rec if recorder else None)
        hud._sync_from_app()
        return app, hud, rec

    def test_a_high_band_only_lights_the_top_row(self):
        _, hud, rec = self._recording([0.0, 0.0, 0.0, 0.0, 1.0])
        try:
            hud._advance_voice(0.05)
            self.assertGreater(hud._voice.rows[0], 0.8)      # 1 - exp(-0.05 / 0.025) = 0.865
            self.assertEqual(hud._voice.rows[1:], [0.0] * 4)
            rec.get_bands.assert_called_with(5)
        finally:
            _close(hud)

    def test_a_low_band_only_lights_the_bottom_row(self):
        _, hud, _ = self._recording([1.0, 0.0, 0.0, 0.0, 0.0])
        try:
            hud._advance_voice(0.05)
            self.assertGreater(hud._voice.rows[4], 0.8)
            self.assertEqual(hud._voice.rows[:4], [0.0] * 4)
        finally:
            _close(hud)

    def test_a_recorder_that_raises_reads_as_silence(self):
        _, hud, _ = self._recording(raises=RuntimeError("stream gone"))
        try:
            hud._advance_voice(0.1)
            self.assertEqual(hud._voice.rows, [0.0] * 5)
            self.assertEqual(hud._voice.level, 0.0)
        finally:
            _close(hud)

    def test_a_wrong_length_or_a_missing_recorder_reads_as_silence(self):
        _, hud, _ = self._recording([1.0, 1.0, 1.0])
        try:
            hud._advance_voice(0.1)
            self.assertEqual(hud._voice.rows, [0.0] * 5)
        finally:
            _close(hud)
        _, hud, _ = self._recording([1.0] * 5, recorder=False)
        try:
            hud._advance_voice(0.1)
            self.assertEqual(hud._voice.rows, [0.0] * 5)
        finally:
            _close(hud)
        _, hud = _hud(state=AppState.RECORDING)   # a MagicMock recorder: not five numbers
        try:
            hud.app.recorder = MagicMock()
            hud._sync_from_app()
            hud._advance_voice(0.1)
            self.assertEqual(hud._voice.rows, [0.0] * 5)
        finally:
            _close(hud)

    def test_out_of_range_bands_are_clamped(self):
        _, hud, _ = self._recording([7.0, -3.0, 0.0, 0.0, 0.0])
        try:
            hud._advance_voice(1.0)
            self.assertLessEqual(max(hud._voice.rows), 1.0)
            self.assertGreaterEqual(min(hud._voice.rows), 0.0)
        finally:
            _close(hud)

    def test_outside_recording_the_voice_decays_to_exact_rest_and_a_new_recording_clears_it(self):
        app, hud, _ = self._recording([0.0, 0.2, 0.5, 0.8, 1.0])
        try:
            for _ in range(10):
                hud._advance_voice(0.05)
            self.assertGreater(hud._voice.level, 0.1)
            app.state = AppState.PROCESSING
            hud._sync_from_app()
            hud._advance_voice(0.05)
            self.assertGreater(hud._voice.rows[0], 0.0)   # decaying, not gone
            for _ in range(200):
                hud._advance_voice(0.05)
            self.assertEqual(hud._voice.rows, [0.0] * 5)  # exactly at rest
            self.assertEqual(hud._voice.level, 0.0)
            hud._voice.rows = [0.9] * 5
            hud._voice.level = 0.9
            app.state = AppState.RECORDING
            hud._sync_from_app()
            self.assertEqual(hud._voice.rows, [0.0] * 5)  # a new recording starts clean
            self.assertEqual(hud._voice.level, 0.0)
        finally:
            _close(hud)

    def test_a_stalled_tick_never_jumps_the_voice(self):
        _, hud, _ = self._recording([1.0] * 5)
        try:
            hud._last_tick = time.monotonic() - 5.0     # the timer stalled for 5 s
            hud._on_tick()
            ceiling = 1.0 - math.exp(-_DT_MAX / 0.025)  # one capped step
            self.assertLessEqual(hud._voice.rows[0], ceiling + 1e-9)
        finally:
            _close(hud)


class TestStemClock(unittest.TestCase):
    """The stem timeline restarts when the stem changes, whatever caused it."""

    def _open_done(self):
        app, hud = _hud(state=AppState.PROCESSING, paste_delivered=True, last_status="success")
        hud._sync_from_app()
        _settle(hud)
        self.assertIs(hud._stem_now(), hl.Stem.DONE)
        return app, hud

    def test_the_clock_restarts_only_when_the_stem_changes(self):
        app, hud = _hud(state=AppState.PROCESSING)
        try:
            hud._sync_from_app()
            hud._glyph_fade = 0.6
            hud._stem_now()
            self.assertEqual(hud._glyph_fade, 0.6)    # BOOTING and PROCESSING share the stem
            app.state = AppState.RECORDING
            hud._sync_from_app()
            self.assertIs(hud._stem, hl.Stem.LISTEN)
            self.assertEqual(hud._glyph_fade, 0.0)
        finally:
            _close(hud)

    def test_reset_flash_restarts_the_clock_from_done(self):
        _, hud = self._open_done()
        try:
            hud._do_flash_reset()
            self.assertIs(hud._stem_now(), hl.Stem.NOTICE)
            self.assertEqual(hud._glyph_fade, 0.0)
        finally:
            _close(hud)

    def test_a_notice_set_while_processing_still_restarts_the_clock(self):
        app, hud = self._open_done()
        try:
            app.last_status = "ai_fallback"  # main sets it before IDLE, with no notify
            self.assertIs(hud._stem_now(), hl.Stem.NOTICE)
            self.assertEqual(hud._glyph_fade, 0.0)
        finally:
            _close(hud)

    def test_the_clock_reaches_the_end_exactly_and_is_frame_rate_independent(self):
        results = {}
        for fps in (30, 120):
            _, hud = _hud(state=AppState.RECORDING)
            try:
                hud._sync_from_app()
                for _ in range(fps // 5):     # 0.2 s
                    hud._advance_stem(1.0 / fps)
                results[fps] = hud._glyph_fade
                for _ in range(fps * 3):
                    hud._advance_stem(1.0 / fps)
                self.assertEqual(hud._glyph_fade, 1.0)
                self.assertEqual(hud._leaf_frame(time.monotonic()).since_state, _STEM_SETTLE_S)
            finally:
                _close(hud)
        self.assertAlmostEqual(results[30], results[120], places=9)
        self.assertAlmostEqual(results[30], 0.2 / _STEM_SETTLE_S, places=9)

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


class TestErrorDroop(unittest.TestCase):
    def _droop_after(self, hud, fps, seconds):
        for _ in range(round(fps * seconds)):
            hud._advance_stem(1.0 / fps)
        return hud._droop_now()

    def test_the_leaf_droops_for_an_error_and_comes_back_exactly(self):
        mid = {}
        for fps in (30, 120):
            app, hud = _hud(state=AppState.RECORDING)
            try:
                hud._sync_from_app()
                _settle(hud)
                self.assertEqual(hud._droop_now(), hl.DROOP)
                app.state = AppState.IDLE
                app.last_status = "empty"
                hud._sync_from_app()
                self.assertIs(hud._stem, hl.Stem.ERROR)
                self.assertEqual(hud._droop_now(), hl.DROOP)       # starts where it was
                mid[fps] = self._droop_after(hud, fps, 0.2)
                self.assertGreater(mid[fps], hl.DROOP)
                self.assertLess(mid[fps], hl.DROOP_ERROR)
                self.assertEqual(self._droop_after(hud, fps, 3.0), hl.DROOP_ERROR)
                self.assertEqual(hud._leaf_frame(time.monotonic()).droop, hl.DROOP_ERROR)

                app.state = AppState.RECORDING
                app.last_status = None
                hud._sync_from_app()
                self.assertIs(hud._stem, hl.Stem.LISTEN)
                self.assertEqual(hud._droop_now(), hl.DROOP_ERROR)   # continuous at the turn
                self.assertEqual(self._droop_after(hud, fps, 3.0), hl.DROOP)
            finally:
                _close(hud)
        self.assertAlmostEqual(mid[30], mid[120], places=9)

    def test_a_change_in_the_middle_of_the_ease_continues_from_where_it_is(self):
        app, hud = _hud(state=AppState.RECORDING)
        try:
            hud._sync_from_app()
            _settle(hud)
            app.state = AppState.IDLE
            app.last_status = "empty"
            hud._sync_from_app()
            partway = self._droop_after(hud, 60, 0.15)
            app.state = AppState.RECORDING
            app.last_status = None
            hud._sync_from_app()
            self.assertEqual(hud._droop_now(), partway)
            self.assertEqual(self._droop_after(hud, 60, 3.0), hl.DROOP)
        finally:
            _close(hud)

    def test_a_fresh_entry_starts_at_the_droop_of_its_stem(self):
        app, hud = _hud(state=AppState.IDLE, last_status="empty")
        try:
            hud._sync_from_app()
            _settle(hud)
            self.assertEqual(hud._droop_now(), hl.DROOP_ERROR)
            hud._do_hide()
            hud._advance_phase(hud._phase_t0 + 10.0)
            self.assertEqual(hud._phase, "hidden")
            app.last_status = None
            app.state = AppState.RECORDING
            hud._sync_from_app()
            self.assertEqual(hud._phase, "entering")
            self.assertEqual(hud._droop_now(), hl.DROOP)
        finally:
            _close(hud)


class TestTick(unittest.TestCase):
    def _cases(self):
        return {
            "Done": dict(state=AppState.PROCESSING, paste_delivered=True, last_status="success"),
            "Notice": dict(state=AppState.PROCESSING, paste_delivered=True, last_status="ai_fallback"),
            "Error": dict(state=AppState.IDLE, last_status="empty"),
        }

    def test_a_settled_done_notice_or_error_uses_the_slow_tick(self):
        for name, attrs in self._cases().items():
            with self.subTest(name=name):
                _, hud = _hud(**attrs)
                try:
                    hud._sync_from_app()
                    self.assertEqual(hud._tick.interval(), 16)   # the entry animates
                    _settle(hud)
                    hud._on_tick()
                    self.assertEqual(hud._tick.interval(), 250)
                    hud._glyph_fade = 0.2     # the stem is still drawing its check or cross
                    hud._on_tick()
                    self.assertEqual(hud._tick.interval(), 16)
                finally:
                    _close(hud)

    def test_a_reset_notice_settles_to_the_slow_tick_too(self):
        _, hud = _hud()
        try:
            hud._do_flash_reset()
            _settle(hud)
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 250)
        finally:
            _close(hud)

    def test_recording_and_processing_never_use_the_slow_tick(self):
        for name, attrs in (("Listening", dict(state=AppState.RECORDING)),
                            ("Working", dict(state=AppState.PROCESSING))):
            with self.subTest(name=name):
                _, hud = _hud(**attrs)
                try:
                    hud._sync_from_app()
                    _settle(hud)
                    hud._on_tick()
                    self.assertEqual(hud._tick.interval(), 16)
                finally:
                    _close(hud)

    def test_a_done_leaf_keeps_the_fast_tick_until_the_seed_has_gone(self):
        _, hud = _hud(**self._cases()["Done"])
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._glyph_fade = 0.9
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 16)
            hud._glyph_fade = 1.0
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 250)
        finally:
            _close(hud)

    def test_a_voice_that_is_still_fading_keeps_the_fast_tick(self):
        _, hud = _hud(**self._cases()["Error"])
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._voice.rows = [0.3] * 5
            hud._voice.level = 0.3
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 16)
        finally:
            _close(hud)

    def test_the_entry_keeps_the_fast_tick_until_the_vein_sweep_is_over(self):
        _, hud = _hud(**self._cases()["Error"])
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._phase_t0 = time.monotonic() - hl.ENTER_S * 1.05
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 16)
            hud._phase_t0 = time.monotonic() - hl.ENTER_S * 1.2
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 250)
        finally:
            _close(hud)

    def test_the_slow_frames_are_identical_to_the_fast_ones(self):
        _, hud = _hud(**self._cases()["Error"])
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 250)
            first = hud.grab().toImage()
            for _ in range(40):  # what the 60 FPS timer would have drawn meanwhile
                hud._on_tick()
            second = hud.grab().toImage()
            self.assertEqual(first, second)
        finally:
            _close(hud)


class TestPainterCaches(unittest.TestCase):
    """The painter caches the settled layers; the widget must never serve a stale one."""

    def test_a_settled_leaf_repaints_without_rebuilding_and_a_moving_one_never_reuses(self):
        _, hud = _hud(state=AppState.IDLE, last_status="empty")
        try:
            hud._sync_from_app()
            _settle(hud)
            with patch.object(hl, "_build_shadow", wraps=hl._build_shadow) as build:
                hud.grab()
                built = build.call_count
                self.assertGreaterEqual(built, 1)
                hud.grab()
                hud.grab()
                self.assertEqual(build.call_count, built, "a settled frame was rebuilt")

                hud._w_tw.to(hud._label_target + 60, time.monotonic())   # a width change in flight
                for _ in range(3):
                    time.sleep(0.03)   # the Windows clock ticks every 15.6 ms
                    hud.grab()
                self.assertGreaterEqual(build.call_count, built + 3,
                                        "a moving frame reused a stale layer")

                _settle(hud)   # the move ends; the new size is built once and then reused
                hud.grab()
                again = build.call_count
                hud.grab()
                self.assertEqual(build.call_count, again)
        finally:
            _close(hud)


class TestBrandLook(unittest.TestCase):
    """The leaf shares the Odicto Mobile / setup page tokens and font."""

    def test_brand_font_loads_from_the_install_root(self):
        import tempfile
        from pathlib import Path

        from indicator import BRAND_FONT_FAMILY, load_brand_font

        _qt_app()
        self.assertEqual(load_brand_font(), BRAND_FONT_FAMILY)
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(load_brand_font(Path(empty)))  # missing file: system face

    def test_the_caption_paints_with_the_brand_font(self):
        from indicator import BRAND_FONT_FAMILY

        _, hud = _hud()
        try:
            self.assertEqual(hud._font.family(), BRAND_FONT_FAMILY)
            self.assertEqual(hud._font_caption.family(), BRAND_FONT_FAMILY)
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
        self.assertLess(hl.LEAVE_S, hl.ENTER_S)
        self.assertLessEqual(hl.ENTER_S, 0.7)


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


if __name__ == "__main__":
    unittest.main()
