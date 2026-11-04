"""The leaf HUD painter: timeline, voice envelope, geometry, pixel grid, and a render smoke.

Behaviour only. No pixel hashes: Qt renders differently per platform, so the looks are
checked by eye with tools/hud_snapshots.py (docs/engineering-notes.md, section 3.5).
"""

from __future__ import annotations

import itertools
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter
from PySide6.QtWidgets import QApplication

import hud_leaf as hl
from indicator import (
    ERROR_STATUS_LABELS,
    SUCCESS_NOTICE_LABELS,
    GuiState,
    _Theme,
    ease_accelerate,
    ease_standard,
    load_brand_font,
    orb_kind,
)

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("rise", "grow", "open_h", "open_l", "open_r", "tip", "content", "fade")
DPRS = (1.0, 1.25, 1.5, 2.0)


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _font():
    return hl.label_font(load_brand_font() or "Segoe UI")


def _frame(stem=hl.Stem.LISTEN, ai=False, label="Listening", opening=hl.OPEN, label_w=90.0,
           droop=hl.DROOP, voice=None, cx=170.0, bottom=72.0, since=5.0):
    return hl.LeafFrame(cx=cx, bottom=bottom, label_w=label_w, opening=opening, stem=stem, ai=ai,
                        label=label, since_state=since, since_enter=5.0, now=3.3,
                        voice=voice or hl.VoiceEnvelope(), droop=droop)


class TestTimeline(unittest.TestCase):
    def test_ends_are_exact(self):
        self.assertEqual(hl.enter_opening(hl.ENTER_S), hl.OPEN)
        self.assertEqual(hl.enter_opening(hl.ENTER_S + 3.0), hl.OPEN)
        self.assertEqual(hl.leave_opening(hl.LEAVE_S).fade, 0.0)
        self.assertEqual(hl.leave_opening(hl.LEAVE_S + 1.0).fade, 0.0)
        self.assertEqual(hl.leave_opening(0.0), hl.OPEN)
        start = hl.enter_opening(0.0)
        self.assertTrue(all(getattr(start, f) == 0.0 for f in FIELDS if f != "fade"))

    def test_fields_are_monotonic(self):
        times = [i * 0.004 for i in range(0, 200)]
        for f in FIELDS:
            up = [getattr(hl.enter_opening(t), f) for t in times]
            down = [getattr(hl.leave_opening(t), f) for t in times]
            self.assertEqual(up, sorted(up), f"enter {f}")
            self.assertEqual(down, sorted(down, reverse=True), f"exit {f}")
            self.assertTrue(all(0.0 <= v <= 1.0 for v in up + down), f)

    def test_words_are_gone_before_the_leaf_closes(self):
        for e in (i * 0.002 for i in range(0, 260)):
            o = hl.leave_opening(e)
            if o.open_h < 1.0:
                self.assertEqual(o.content, 0.0, f"e={e}")

    def test_exit_from_a_half_open_leaf_never_grows(self):
        base = hl.enter_opening(0.3)
        for e in (0.0, 0.05, 0.2):
            o = hl.leave_opening(e, base)
            for f in FIELDS:
                self.assertLessEqual(getattr(o, f), getattr(base, f))

    def test_easing_matches_the_indicator(self):
        for t in (0.0, 0.1, 0.37, 0.5, 0.9, 1.0):
            self.assertAlmostEqual(hl._EASE(t), ease_standard(t), places=12)
            self.assertAlmostEqual(hl._ACCEL(t), ease_accelerate(t), places=12)


class TestVoiceEnvelope(unittest.TestCase):
    def _run(self, steps, dt, bands, start=None):
        v = hl.VoiceEnvelope()
        if start:
            v.rows, v.level = list(start), sum(start) / hl.ROWS
        for _ in range(steps):
            v.advance(dt, bands)
        return v

    def test_frame_rate_independent(self):
        for bands, start in (([0.9, 0.5, 0.2, 0.7, 1.0], None),
                             ([0.0] * 5, [1.0, 0.8, 0.6, 0.4, 0.2])):
            one = self._run(1, 0.05, bands, start)
            five = self._run(5, 0.01, bands, start)
            for a, b in zip(one.rows, five.rows):
                self.assertAlmostEqual(a, b, delta=1e-9)
            self.assertAlmostEqual(one.level, five.level, delta=1e-9)

    def test_rises_faster_than_it_falls(self):
        rise = self._run(1, 0.05, [1.0] * 5).rows[0]
        fall = 1.0 - self._run(1, 0.05, [0.0] * 5, [1.0] * 5).rows[0]
        self.assertGreater(rise, fall)

    def test_history_shifts_once_per_step(self):
        def columns_filled(seconds_in_ms):
            v = hl.VoiceEnvelope()
            for _ in range(seconds_in_ms):
                v.advance(0.001, [1.0] * 5)
            return sum(1 for x in v.hist[0] if x > 0.0), v

        self.assertEqual(columns_filled(40)[0], 0)
        self.assertEqual(columns_filled(50)[0], 1)
        self.assertEqual(columns_filled(95)[0], 2)
        n, v = columns_filled(100)
        self.assertEqual(n, 2)
        self.assertLessEqual(v.hist[0][0], v.rows[0])    # column 0 is the newest sample
        self.assertGreater(v.hist[0][0], v.hist[0][1])

    def test_clear_and_input_check(self):
        v = self._run(30, 0.016, [1.0] * 5)
        v.clear()
        self.assertEqual(v.rows, [0.0] * hl.ROWS)
        self.assertEqual(v.level, 0.0)
        self.assertTrue(all(x == 0.0 for col in v.hist for x in col))
        with self.assertRaises(ValueError):
            v.advance(0.01, [0.5] * 3)


class TestStyleAndMaps(unittest.TestCase):
    def test_style_rejects_unknown_values(self):
        self.assertEqual(hl.HudStyle(), hl.HudStyle("reach", "veins", "rim", "leaf"))
        for field in hl.HUD_STYLE_CHOICES:
            with self.assertRaises(ValueError, msg=field):
                hl.HudStyle(**{field: "nope"})
        with self.assertRaises(ValueError):
            hl.HudStyle(shape="sharp")

    def test_every_orb_kind_maps_to_a_stem(self):
        statuses = {None, "success", "init_error", "preflight", "error", "polishing",
                    "finalizing", "unknown"} | set(SUCCESS_NOTICE_LABELS) | set(ERROR_STATUS_LABELS)
        for state, status in itertools.product(GuiState, statuses):
            kind = orb_kind(state, status)
            self.assertIn(kind, hl.STEM_FOR_ORB, (state, status))
        self.assertTrue(all(isinstance(s, hl.Stem) for s in hl.STEM_FOR_ORB.values()))

    def test_colours_equal_the_theme(self):
        def rgb(c: QColor):
            return (c.red(), c.green(), c.blue())

        self.assertEqual(hl.JADE, rgb(_Theme.accent))
        self.assertEqual(hl.WARN, rgb(_Theme.warn))
        self.assertEqual(hl.DANGER, rgb(_Theme.danger))
        self.assertEqual(hl.INK, rgb(_Theme.text))

    def test_label_width(self):
        m = QFontMetricsF(_font())
        word = m.horizontalAdvance("Thinking")
        self.assertAlmostEqual(hl.label_width(m, "Thinking", False, hl.Stem.WORK), word)
        self.assertAlmostEqual(hl.label_width(m, "Thinking", True, hl.Stem.WORK),
                               word + hl.AI_GLYPH_W + hl.AI_GAP)
        self.assertAlmostEqual(hl.label_width(m, "Thinking", False, hl.Stem.LISTEN),
                               word + hl.DOTS_W)
        self.assertAlmostEqual(hl.label_width(m, "Thinking", True, hl.Stem.LISTEN),
                               word + hl.AI_GLYPH_W + hl.AI_GAP + hl.DOTS_W)
        for stem in (hl.Stem.DONE, hl.Stem.NOTICE, hl.Stem.ERROR):
            self.assertAlmostEqual(hl.label_width(m, "Thinking", False, stem), word)
        self.assertEqual(hl.leaf_width(100.0), hl.VOICE_W + hl.TEXT_GAP + 100.0 + hl.RIGHT_INSET)


class TestGeometry(unittest.TestCase):
    def test_open_leaf_box_follows_label_width(self):
        geo = hl.leaf_geometry(_frame(label_w=120.0))
        self.assertAlmostEqual(geo.w, hl.leaf_width(120.0))
        self.assertAlmostEqual(geo.left + geo.w / 2, 170.0)
        self.assertAlmostEqual(geo.top + geo.h, 72.0)
        self.assertAlmostEqual(geo.h, hl.LEAF_H)
        self.assertEqual(geo.tip, 1.0)

    def test_capsule_is_symmetric(self):
        geo = hl.leaf_geometry(_frame())
        path = hl.leaf_path(geo, "capsule")
        mid = geo.left + geo.w / 2
        b = path.boundingRect()
        self.assertAlmostEqual(b.left() + b.width() / 2, mid, delta=0.01)
        for dx, dy in itertools.product(range(1, int(geo.w / 2), 7), range(0, int(geo.h), 3)):
            y = geo.top + dy
            self.assertEqual(path.contains(QPointF(mid - dx, y)), path.contains(QPointF(mid + dx, y)))

    def test_open_leaf_bounds_match_geometry(self):
        for droop in (hl.DROOP, hl.DROOP_ERROR):
            geo = hl.leaf_geometry(_frame(droop=droop))
            b = hl.leaf_path(geo, "leaf").boundingRect()
            self.assertAlmostEqual(b.left(), geo.left, delta=0.5)
            self.assertAlmostEqual(b.top(), geo.top, delta=0.5)
            self.assertAlmostEqual(b.right(), geo.left + geo.w, delta=0.5)
            self.assertAlmostEqual(b.bottom(), geo.top + geo.h, delta=0.5)

    def test_tip_droops_below_the_centre_line(self):
        for droop in (hl.DROOP, hl.DROOP_ERROR):
            geo = hl.leaf_geometry(_frame(droop=droop))
            path = hl.leaf_path(geo, "leaf")
            pts = [path.pointAtPercent(i / 4000.0) for i in range(4001)]
            min_x = min(p.x() for p in pts)
            tip = [p.y() for p in pts if p.x() <= min_x + 0.05]
            self.assertAlmostEqual(sum(tip) / len(tip), geo.cy + droop * geo.h, delta=0.5)

    def test_bud_is_a_capsule_end(self):
        geo = hl.leaf_geometry(_frame(opening=hl.Opening(1, 1, 1, 1, 1, 0.0, 1, 1)))
        self.assertEqual(geo.tip, 0.0)
        bud = hl.leaf_path(geo, "leaf").boundingRect()
        self.assertAlmostEqual(bud.width(), geo.w, delta=0.5)

    def test_unknown_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            hl.leaf_path(hl.leaf_geometry(_frame()), "sharp")


class TestPixelGrid(unittest.TestCase):
    def test_voice_cells_sit_on_whole_device_pixels(self):
        geos = [hl.leaf_geometry(_frame(droop=d)) for d in (hl.DROOP, hl.DROOP_ERROR)]
        geos.append(hl.LeafGeometry(40.3, 20.7, 250.9, 36.0, 124.37, 38.71, 1.0, hl.DROOP))
        for dpr, geo in itertools.product(DPRS, geos):
            cells = hl.voice_cells(geo, dpr)
            self.assertEqual(len(cells), hl.ROWS * hl._MAX_COLS)
            dev = []
            for _, _, r in cells:
                x0, y0, w, h = r.x() * dpr, r.y() * dpr, r.width() * dpr, r.height() * dpr
                for v in (x0, y0, w, h):
                    self.assertAlmostEqual(v, round(v), delta=1e-6, msg=f"dpr {dpr}")
                dev.append((round(x0), round(y0), round(x0 + w), round(y0 + h)))
            self.assertEqual(len({(c[2] - c[0], c[3] - c[1]) for c in dev}), 1)
            for a, b in itertools.combinations(dev, 2):
                apart = (a[2] + 1 <= b[0] or b[2] + 1 <= a[0] or a[3] + 1 <= b[1] or b[3] + 1 <= a[1])
                self.assertTrue(apart, f"dpr {dpr}: {a} and {b} are closer than 1 device px")

    def test_top_row_is_row_zero(self):
        cells = hl.voice_cells(hl.leaf_geometry(_frame()), 2.0)
        ys = {i: r.y() for i, j, r in cells if j == 0}
        self.assertEqual(sorted(ys, key=ys.get), list(range(hl.ROWS)))

    def test_small_glyph_rects_have_whole_edges(self):
        for dpr in DPRS:
            r = hl._dev_rect(10.3, 7.7, 12.3, 9.7, dpr)
            for v in (r.x(), r.y(), r.width(), r.height()):
                self.assertAlmostEqual(v * dpr, round(v * dpr), delta=1e-6)
            self.assertGreaterEqual(r.width() * dpr, 1.0 - 1e-6)

    def test_grain_is_a_faint_fixed_tile(self):
        img = hl._grain_base()
        alphas = [img.pixelColor(x, y).alpha() for x in range(0, 128, 3) for y in range(0, 128, 3)]
        self.assertLessEqual(max(alphas), 16)
        self.assertGreater(max(alphas), 0)
        self.assertEqual(hl._grain_base().pixelColor(5, 9), img.pixelColor(5, 9))

    def test_plain_grain_is_forty_percent_of_the_veins_grain(self):
        self.assertEqual(hl.grain_strength("veins"), 1.0)
        self.assertAlmostEqual(hl.grain_strength("plain"), 0.4 * hl.grain_strength("veins"))
        with self.assertRaises(ValueError):
            hl.grain_strength("nope")

    def test_the_surface_draws_the_grain_at_the_texture_strength(self):
        geo = hl.leaf_geometry(_frame())
        for texture in hl.HUD_STYLE_CHOICES["texture"]:
            drawn, q = [], MagicMock()
            q.setOpacity.side_effect = lambda a: drawn.append(("opacity", a))
            q.drawTiledPixmap.side_effect = lambda *a: drawn.append(("grain",))
            hl._over_layer(q, geo, texture, 1.0)
            at = drawn.index(("grain",))
            self.assertEqual(drawn[at - 1], ("opacity", hl.grain_strength(texture)), texture)
            self.assertEqual(drawn[-1], ("opacity", 1.0), texture)
            self.assertEqual(texture == "veins", q.drawPath.called, texture)


SIZES = tuple(hl.HUD_SIZE_SCALE)


class TestSize(unittest.TestCase):
    """HUD_SIZE: every geometry number stays in design pixels and the painter scales it."""

    def test_the_size_is_a_style_choice_with_an_exact_scale(self):
        self.assertEqual(hl.HUD_STYLE_CHOICES["size"], ("1x", "1.3x", "1.5x"))
        self.assertEqual(hl.HudStyle().size, "1x")
        self.assertEqual([hl.HudStyle(size=s).scale for s in SIZES], [1.0, 1.3, 1.5])
        with self.assertRaises(ValueError):
            hl.HudStyle(size="2x")

    def test_the_pixel_ratio_is_the_device_ratio_times_the_scale(self):
        for size, dpr in itertools.product(SIZES, DPRS):
            self.assertEqual(hl.pixel_ratio(dpr, hl.HudStyle(size=size)),
                             dpr * hl.HUD_SIZE_SCALE[size])

    def test_voice_cells_land_on_whole_device_pixels_at_every_size_and_ratio(self):
        geos = [hl.leaf_geometry(_frame(droop=d)) for d in (hl.DROOP, hl.DROOP_ERROR)]
        geos.append(hl.LeafGeometry(40.3, 20.7, 250.9, 36.0, 124.37, 38.71, 1.0, hl.DROOP))
        for size, dpr, geo in itertools.product(SIZES[1:], DPRS, geos):
            style = hl.HudStyle(size=size)
            ratio = hl.pixel_ratio(dpr, style)
            dev = []
            for _, _, r in hl.voice_cells(geo, ratio):
                # design px -> widget px (x scale) -> device px (x dpr)
                x0, y0 = r.x() * style.scale * dpr, r.y() * style.scale * dpr
                w, h = r.width() * style.scale * dpr, r.height() * style.scale * dpr
                for v in (x0, y0, w, h):
                    self.assertAlmostEqual(v, round(v), delta=1e-6, msg=f"{size} dpr {dpr}")
                dev.append((round(x0), round(y0), round(x0 + w), round(y0 + h)))
            self.assertEqual(len({(c[2] - c[0], c[3] - c[1]) for c in dev}), 1)
            for a, b in itertools.combinations(dev, 2):
                apart = (a[2] + 1 <= b[0] or b[2] + 1 <= a[0] or a[3] + 1 <= b[1] or b[3] + 1 <= a[1])
                self.assertTrue(apart, f"{size} dpr {dpr}: {a} and {b} touch or overlap")

    def _recorded(self, size, dpr, painter=None):
        """Paint a settled frame; return what the painter drew: (scale, ratio, point, image)."""
        drawn = []

        class Spy(QPainter):
            def drawImage(self, *args):
                t = self.worldTransform()
                if len(args) == 2 and isinstance(args[1], QImage):
                    drawn.append((t.m11(), t.m22(), args[0], args[1]))
                return super().drawImage(*args)

        style = hl.HudStyle(size=size)
        img = QImage(round(520 * dpr), round(160 * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        p = Spy(img)
        try:
            (painter or hl.LeafPainter(_font())).paint(p, _frame(), style)
        finally:
            p.end()
        return drawn, img

    def test_the_painter_scales_all_geometry_by_exactly_the_size(self):
        for size, dpr in itertools.product(SIZES, DPRS):
            drawn, _ = self._recorded(size, dpr)
            scale = hl.HUD_SIZE_SCALE[size]
            self.assertTrue(drawn, (size, dpr))
            for m11, m22, at, image in drawn:
                self.assertEqual((m11, m22), (scale, scale), (size, dpr))
                # A layer image holds dpr x scale device pixels per design pixel, so under the
                # scale it maps to the device 1:1 and starts on a whole device pixel.
                self.assertEqual(image.devicePixelRatio(), dpr * scale)
                for v in (at.x(), at.y()):
                    self.assertAlmostEqual(v * scale * dpr, round(v * scale * dpr), delta=1e-6)

    def test_the_voice_is_built_for_the_scaled_ratio(self):
        seen = []
        real = hl.voice_cells
        for size, dpr in ((s, d) for s in SIZES for d in (1.0, 1.25, 2.0)):
            with unittest.mock.patch.object(
                    hl, "voice_cells", lambda geo, ratio: (seen.append(ratio), real(geo, ratio))[1]):
                self._recorded(size, dpr)
            self.assertEqual(seen[-1], dpr * hl.HUD_SIZE_SCALE[size], (size, dpr))

    def test_layer_caches_key_on_the_size(self):
        # Compare the layer images, not the whole frame: macOS glyph rasterisation can differ
        # by a pixel between two painters, and the label is not a cached layer.
        def layers(drawn):
            return [(image.devicePixelRatio(), image) for *_, image in drawn]

        shared = hl.LeafPainter(_font())
        for dpr in (1.0, 1.5):
            self._recorded("1x", dpr, shared)
            reused, _ = self._recorded("1.3x", dpr, shared)
            fresh, _ = self._recorded("1.3x", dpr)
            self.assertEqual(layers(reused), layers(fresh),
                             f"a layer from another size was reused at dpr {dpr}")
            back, _ = self._recorded("1x", dpr, shared)
            first, _ = self._recorded("1x", dpr)
            self.assertEqual(layers(back), layers(first),
                             f"a layer from another size was reused at dpr {dpr}")


class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.painter = hl.LeafPainter(_font())
        cls.metrics = QFontMetricsF(_font())

    def _image(self, dpr, w=340, h=100):
        img = QImage(round(w * dpr), round(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        return img

    def _paint(self, img, frame, style):
        p = QPainter(img)
        try:
            self.painter.paint(p, frame, style)
        finally:
            p.end()

    def test_every_stem_ai_style_and_ratio_paints(self):
        styles = [hl.HudStyle(*c) for c in itertools.product(*hl.HUD_STYLE_CHOICES.values())]
        self.assertEqual(len(styles), 48)
        voice = hl.VoiceEnvelope()
        for _ in range(30):
            voice.advance(0.016, [0.9, 0.6, 0.4, 0.8, 0.3])
        for dpr in DPRS:
            for stem, ai, style in itertools.product(hl.Stem, (False, True), styles):
                label = "Listening"
                lw = hl.label_width(self.metrics, label, ai, stem)
                frame = _frame(stem, ai, label, label_w=lw, voice=voice,
                               droop=hl.DROOP_ERROR if stem is hl.Stem.ERROR else hl.DROOP)
                img = self._image(dpr, w=520, h=160)
                self._paint(img, frame, style)
                geo = hl.leaf_geometry(frame)
                px = img.pixelColor(round(geo.stem_x * style.scale * dpr),
                                    round(geo.cy * style.scale * dpr))
                self.assertGreater(px.alpha(), 0, (dpr, stem, ai, style))

    def test_animating_frames_paint_and_leave_no_residue(self):
        style = hl.HudStyle()
        lw = hl.label_width(self.metrics, "Listening", False, hl.Stem.LISTEN)
        for t in (0.0, 0.1, 0.25, 0.4, 0.55):
            img = self._image(1.5)
            self._paint(img, _frame(label_w=lw, opening=hl.enter_opening(t)), style)
        for e in (0.0, 0.2, 0.35):
            img = self._image(1.5)
            self._paint(img, _frame(label_w=lw, opening=hl.leave_opening(e)), style)
        gone = self._image(1.5)
        self._paint(gone, _frame(label_w=lw, opening=hl.leave_opening(hl.LEAVE_S)), style)
        self.assertTrue(all(gone.pixelColor(x, y).alpha() == 0
                            for x in range(0, gone.width(), 5) for y in range(0, gone.height(), 5)))

    def test_painter_opacity_is_restored(self):
        img = self._image(1.0)
        p = QPainter(img)
        p.setOpacity(0.5)
        self.painter.paint(p, _frame(), hl.HudStyle())
        self.assertEqual(p.opacity(), 0.5)
        p.end()

    def test_caption_card_paints_inside_its_rect(self):
        geo = hl.leaf_geometry(_frame())
        for dpr in DPRS:
            img = self._image(dpr, 460, 170)
            rect = hl.card_rect(geo, 1.0, dpr)
            self.assertGreaterEqual(rect.width(), hl.CARD_MIN_W)
            self.assertEqual(rect.height(), hl.CARD_H)
            p = QPainter(img)
            self.painter.paint_card(p, rect, hl.HudStyle(), 1.0)
            self.painter.paint_card(p, rect, hl.HudStyle("reach", "veins", "soft", "leaf"), 0.5)
            p.end()
            c = rect.center()
            self.assertGreater(img.pixelColor(round(c.x() * dpr), round(c.y() * dpr)).alpha(), 0)

    def test_module_does_not_import_the_app(self):
        code = ("import sys; sys.path.insert(0, 'app'); import hud_leaf; "
                "bad = {'indicator', 'config', 'main', 'recorder'} & set(sys.modules); "
                "sys.exit(1 if bad else 0)")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                              env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
