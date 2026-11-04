"""THEME_COLOR: the colour maths, the HUD palette, the config key, and the page's copy of the maths.

The page repeats the maths of app/theme.py in JavaScript (assets/setup_template.html), so the parity tests
read the page source and fail when a table, a rule or a matrix constant drifts from the Python.
"""

from __future__ import annotations

import importlib.util
import json
import io
import os
import random
import re
import shutil
import subprocess
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

import config
import hud_leaf as hl
import theme
from indicator import load_brand_font
from tests.test_setup_page_look import failing, page_contrast_checks

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "assets" / "setup_template.html").read_text(encoding="utf-8")
THEME_PY = (ROOT / "app" / "theme.py").read_text(encoding="utf-8")
CONFIG_PATH = str(ROOT / "app" / "config.py")

CUSTOM = ("#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ffffff", "#000000", "#808080", "#c06a8f")
ALL = tuple(theme.PRESETS) + CUSTOM

# The leaf colours as the painter drew them before themes existed. Forest must stay these, exactly.
FOREST_TODAY = {
    "jade": (142, 220, 187), "surface_top": (27, 64, 46), "surface_bottom": (9, 31, 22), "cool": (3, 18, 24),
    "vein": (205, 245, 222), "sheen": (225, 255, 238), "rim": (215, 250, 230), "card_top": (24, 58, 42),
    "card_bottom": (10, 32, 23), "glint": (240, 255, 248), "stem_hi": (244, 255, 250), "bead": (238, 255, 248),
    "warn": (242, 194, 122), "danger": (242, 163, 155), "ink": (231, 229, 223), "moon": (178, 214, 244),
}
FIXED = ("warn", "danger", "ink", "moon")


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _block(name: str) -> str:
    start = PAGE.index("// ------------------------------------------------------------------- theme")
    return PAGE[start:PAGE.index("// ----------------------------------------------------- appearance preview")]


def _channel_gap(a, b) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


class TestParse(unittest.TestCase):
    def test_a_preset_name_in_any_case_gives_the_lower_case_name(self) -> None:
        for name in theme.PRESETS:
            for text in (name, name.upper(), name.capitalize(), "  %s " % name):
                self.assertEqual(theme.parse_theme(text), name, text)

    def test_a_hex_colour_in_any_case_gives_lower_case_hex(self) -> None:
        self.assertEqual(theme.parse_theme("#C06A8F"), "#c06a8f")
        self.assertEqual(theme.parse_theme(" #c06A8f "), "#c06a8f")
        self.assertEqual(theme.parse_theme("#000000"), "#000000")

    def test_anything_else_is_unknown(self) -> None:
        for bad in ("", "   ", None, "teal", "#abc", "#abcd", "c06a8f", "#12345g", "#1234567", "##c06a8f",
                    "#c06a8f\n", "forest\nX=1", "#c06a8f\nX=1", "rgb(1,2,3)", 5, "forest ocean"):
            expected = theme.parse_theme(bad) if bad == "#c06a8f\n" else None
            self.assertEqual(theme.parse_theme(bad), expected, repr(bad))

    def test_a_newline_never_survives_in_a_value_that_is_accepted(self) -> None:
        for value in ("#c06a8f\n", "forest\n", "\nocean"):
            result = theme.parse_theme(value)
            self.assertTrue(result is None or "\n" not in result, repr(value))


class TestColourMaths(unittest.TestCase):
    def test_a_round_trip_stays_within_one_step_for_1000_random_colours(self) -> None:
        rnd = random.Random(20261104)
        for _ in range(1000):
            rgb = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            back = theme.oklch_to_srgb(*theme.srgb_to_oklch(rgb))
            self.assertLessEqual(_channel_gap(rgb, back), 1, (rgb, back))

    def test_known_colours(self) -> None:
        lightness, chroma, _ = theme.srgb_to_oklch((255, 255, 255))
        self.assertAlmostEqual(lightness, 1.0, places=3)
        self.assertAlmostEqual(chroma, 0.0, places=3)
        self.assertEqual(theme.srgb_to_oklch((0, 0, 0))[:2], (0.0, 0.0))
        self.assertEqual(theme.oklch_to_srgb(1.0, 0.0, 0.0), (255, 255, 255))
        self.assertEqual(theme.oklch_to_srgb(0.0, 0.0, 0.0), (0, 0, 0))
        hue = theme.srgb_to_oklch((255, 0, 0))[2]
        self.assertTrue(25.0 < hue < 35.0, hue)   # red is near hue 29 in OKLCH

    def test_the_gamut_clamp_lowers_chroma_and_keeps_lightness(self) -> None:
        for lightness, hue in ((0.5, 140.0), (0.85, 260.0), (0.3, 30.0), (0.9, 100.0)):
            clamped = theme.gamut_clamp(lightness, 0.4, hue)
            self.assertLess(clamped, 0.4, (lightness, hue))
            rgb = theme.oklch_to_srgb(lightness, 0.4, hue)
            self.assertTrue(all(0 <= v <= 255 for v in rgb))
            self.assertAlmostEqual(theme.srgb_to_oklch(rgb)[0], lightness, delta=0.01)

    def test_a_colour_inside_the_gamut_is_not_changed_by_the_clamp(self) -> None:
        self.assertEqual(theme.gamut_clamp(0.6, 0.02, 200.0), 0.02)

    def test_chroma_zero_gives_a_grey(self) -> None:
        for lightness in (0.2, 0.5, 0.8):
            r, g, b = theme.oklch_to_srgb(lightness, 0.0, 77.0)
            self.assertEqual(r, g)
            self.assertEqual(g, b)


class TestPresetsAndSetting(unittest.TestCase):
    def test_the_preset_table(self) -> None:
        self.assertEqual(theme.PRESETS, {
            "forest": (165.0, 1.0), "ocean": (232.0, 1.0), "plum": (318.0, 0.95),
            "ember": (32.0, 1.0), "graphite": (230.0, 0.12)})
        self.assertEqual(theme.DEFAULT_THEME, "forest")
        self.assertEqual(config.ENV_DEFAULTS["THEME_COLOR"], theme.DEFAULT_THEME)

    def test_a_custom_colour_takes_its_own_hue_and_a_k_from_its_chroma(self) -> None:
        rnd = random.Random(5)
        for _ in range(200):
            rgb = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            hex_ = "#%02x%02x%02x" % rgb
            _, chroma, hue = theme.srgb_to_oklch(rgb)
            got_h, got_k = theme.theme_hue_k(hex_)
            self.assertAlmostEqual(got_h, hue, places=6)
            self.assertAlmostEqual(got_k, min(1.0, max(0.08, chroma / 0.13)), places=9)
            self.assertTrue(0.08 <= got_k <= 1.0)

    def test_the_k_rule_at_its_ends(self) -> None:
        self.assertEqual(theme.theme_hue_k("#808080")[1], 0.08)     # a grey is never fully grey
        self.assertEqual(theme.theme_hue_k("#ff0000")[1], 1.0)      # a vivid colour is capped
        self.assertTrue(0.08 < theme.theme_hue_k("#c06a8f")[1] < 1.0)

    def test_page_theme_gives_short_numbers(self) -> None:
        self.assertEqual(theme.page_theme("forest"), {"--h": "165", "--k": "1"})
        self.assertEqual(theme.page_theme("plum"), {"--h": "318", "--k": "0.95"})
        self.assertEqual(theme.page_theme("graphite"), {"--h": "230", "--k": "0.12"})
        custom = theme.page_theme("#c06a8f")
        self.assertRegex(custom["--h"], r"^\d+(\.\d{1,2})?$")
        self.assertRegex(custom["--k"], r"^0\.\d{1,3}$")

    def test_an_unknown_theme_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(ValueError):
            theme.theme_hue_k("teal")
        with self.assertRaises(ValueError):
            theme.hud_palette("teal")


class TestPalette(unittest.TestCase):
    def test_forest_is_exactly_todays_constants(self) -> None:
        pal = theme.hud_palette("forest")
        self.assertIs(pal, theme.FOREST)
        self.assertEqual({k: getattr(pal, k) for k in FOREST_TODAY}, FOREST_TODAY)
        for name, constant in (("jade", hl.JADE), ("moon", hl.MOON), ("warn", hl.WARN), ("danger", hl.DANGER),
                               ("ink", hl.INK)):
            self.assertEqual(getattr(pal, name), constant)

    def test_the_four_meaning_colours_never_change(self) -> None:
        for name in ALL:
            pal = theme.hud_palette(name)
            for field in FIXED:
                self.assertEqual(getattr(pal, field), FOREST_TODAY[field], (name, field))

    def test_every_themed_colour_keeps_the_lightness_of_the_forest_colour(self) -> None:
        worst = 0.0
        for name in ALL:
            pal = theme.hud_palette(name)
            for field in theme._THEMED:
                want = theme.srgb_to_oklch(FOREST_TODAY[field])[0]
                got = theme.srgb_to_oklch(getattr(pal, field))[0]
                worst = max(worst, abs(want - got))
                self.assertAlmostEqual(got, want, delta=0.03, msg=(name, field))
        self.assertLess(worst, 0.03)

    def test_a_preset_has_its_own_hue_where_the_colour_has_chroma(self) -> None:
        for name in ("ocean", "plum", "ember"):
            hue = theme.PRESETS[name][0]
            lightness, chroma, got = theme.srgb_to_oklch(theme.hud_palette(name).jade)
            self.assertGreater(chroma, 0.03, name)
            self.assertLess(min(abs(got - hue), 360 - abs(got - hue)), 4.0, name)

    def test_graphite_is_nearly_grey(self) -> None:
        jade = theme.hud_palette("graphite").jade
        self.assertLessEqual(max(jade) - min(jade), 16)

    def test_the_palette_is_cached_and_hashable(self) -> None:
        self.assertIs(theme.hud_palette("ocean"), theme.hud_palette("ocean"))
        self.assertIsInstance(hash(theme.hud_palette("#c06a8f")), int)


# --------------------------------------------------------------------------- the HUD
def _label_contrast_checks(name: str) -> list:
    """The label on the leaf body, as ratios. The label sits on the vertical middle of the body, where the
    top and bottom colours meet (the sheen has faded by then), over black: the lightest backdrop that is
    still the leaf's own colour."""
    from tests.test_setup_page_look import contrast, over

    def unit(rgb):
        return tuple(c / 255 for c in rgb)

    pal = theme.hud_palette(name)
    top = over(unit(pal.surface_top), 227 / 255, (0.0, 0.0, 0.0))
    bottom = over(unit(pal.surface_bottom), 240 / 255, (0.0, 0.0, 0.0))
    body = tuple((a + b) / 2 for a, b in zip(top, bottom))
    checks = []
    for label, rgb, alpha in (("Listening", pal.ink, 0.78), ("Done", pal.ink, 0.92),
                              ("notice", pal.warn, 0.92), ("error", pal.danger, 0.92)):
        checks.append((label, contrast(over(unit(rgb), alpha, body), body), 4.5))
    return checks


class TestHudContrast(unittest.TestCase):
    def test_the_label_is_readable_on_the_leaf_body_in_every_theme(self) -> None:
        for name in ALL:
            self.assertEqual(failing(_label_contrast_checks(name)), [], name)


class TestHudStyleTheme(unittest.TestCase):
    def test_the_style_takes_a_theme_and_defaults_to_forest(self) -> None:
        self.assertEqual(hl.HudStyle().theme, "forest")
        self.assertEqual(hl.HudStyle(theme="ocean").palette, theme.hud_palette("ocean"))
        self.assertEqual(hl.HudStyle(theme="#c06a8f").palette, theme.hud_palette("#c06a8f"))

    def test_the_style_rejects_a_theme_that_is_not_canonical(self) -> None:
        for bad in ("Forest", "#C06A8F", "teal", "", "#abc"):
            with self.assertRaises(ValueError, msg=bad):
                hl.HudStyle(theme=bad)

    def test_the_theme_is_not_a_hud_style_choice(self) -> None:
        self.assertNotIn("theme", hl.HUD_STYLE_CHOICES)


def _render(painter: hl.LeafPainter, frame: hl.LeafFrame, style: hl.HudStyle, card: bool = False) -> bytes:
    img = QImage(360, 160, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    p = QPainter(img)
    if card:
        painter.paint_card(p, hl.card_rect(hl.leaf_geometry(frame), 1.0, 1.0), style, 1.0)
    painter.paint(p, frame, style)
    p.end()
    return bytes(img.constBits())


class TestHudPainterTheme(unittest.TestCase):
    def setUp(self) -> None:
        family = load_brand_font()
        self.font = hl.label_font(family)
        self.painter_font = self.font

    def _frame(self, stem=hl.Stem.LISTEN, label="Listening") -> hl.LeafFrame:
        from PySide6.QtGui import QFontMetricsF
        metrics = QFontMetricsF(self.font)
        v = hl.VoiceEnvelope()
        for _ in range(40):
            v.advance(0.016, [0.9, 0.7, 0.5, 0.8, 0.3])
        return hl.LeafFrame(cx=180.0, bottom=120.0, label_w=hl.label_width(metrics, label, False, stem),
                            opening=hl.OPEN, stem=stem, ai=False, label=label, since_state=5.0, since_enter=5.0,
                            now=3.3, voice=v, droop=hl.DROOP)

    def test_a_theme_changes_the_leaf_and_the_caption_card(self) -> None:
        frame = self._frame()
        forest = _render(hl.LeafPainter(self.font), frame, hl.HudStyle(), card=True)
        for name in ("ocean", "#c06a8f"):
            self.assertNotEqual(_render(hl.LeafPainter(self.font), frame, hl.HudStyle(theme=name), card=True),
                                forest, name)

    def test_one_painter_follows_a_theme_change_and_never_shows_a_stale_layer(self) -> None:
        frame = self._frame()
        shared = hl.LeafPainter(self.font)
        sequence = ("forest", "ocean", "forest", "#c06a8f", "ocean")
        for name in sequence:
            style = hl.HudStyle(theme=name)
            fresh = _render(hl.LeafPainter(self.font), frame, style, card=True)
            self.assertEqual(_render(shared, frame, style, card=True), fresh, name)
            self.assertEqual(_render(shared, frame, style, card=True), fresh, name)   # the settled, cached frame

    def test_the_default_style_and_an_explicit_forest_paint_the_same(self) -> None:
        frame = self._frame(hl.Stem.WORK, "Transcribing")
        self.assertEqual(_render(hl.LeafPainter(self.font), frame, hl.HudStyle()),
                         _render(hl.LeafPainter(self.font), frame, hl.HudStyle(theme="forest")))

    def test_the_notice_and_error_accents_do_not_follow_the_theme(self) -> None:
        for stem in (hl.Stem.NOTICE, hl.Stem.ERROR):
            for name in ALL:
                pal = theme.hud_palette(name)
                self.assertEqual(hl._accent(stem, False, pal), hl._accent(stem, False, theme.FOREST))
        self.assertEqual(hl._accent(hl.Stem.LISTEN, True, theme.hud_palette("ocean")), hl.MOON)
        self.assertEqual(hl._accent(hl.Stem.LISTEN, False, theme.hud_palette("ocean")),
                         theme.hud_palette("ocean").jade)


# --------------------------------------------------------------------------- config
_KEYS = ("THEME_COLOR",)


def _reload_with(env: dict):
    """A private copy of config.py under a patched environment, without a real .env."""
    spec = importlib.util.spec_from_file_location("_config_theme_copy", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    clean = {k: v for k, v in os.environ.items() if k not in _KEYS}
    with mock.patch.dict(os.environ, clean, clear=True), \
            mock.patch.dict(os.environ, env), \
            mock.patch("dotenv.load_dotenv", lambda *a, **k: False), \
            redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class TestThemeConfig(unittest.TestCase):
    def test_default_when_unset(self) -> None:
        cfg = _reload_with({})
        self.assertEqual(cfg.Config.THEME_COLOR, "forest")
        self.assertEqual(cfg.Config.CONFIG_WARNINGS, [])

    def test_a_value_is_stripped_and_lowercased(self) -> None:
        for raw, want in (("  Ocean ", "ocean"), ("PLUM", "plum"), ("#C06A8F", "#c06a8f"), (" #FFFF00", "#ffff00")):
            cfg = _reload_with({"THEME_COLOR": raw})
            self.assertEqual((cfg.Config.THEME_COLOR, cfg.Config.CONFIG_WARNINGS), (want, []), raw)

    def test_blank_reads_as_the_default_without_a_warning(self) -> None:
        for raw in ("", "   "):
            cfg = _reload_with({"THEME_COLOR": raw})
            self.assertEqual((cfg.Config.THEME_COLOR, cfg.Config.CONFIG_WARNINGS), ("forest", []), repr(raw))

    def test_an_unknown_value_warns_once_and_uses_forest(self) -> None:
        for bad in ("teal", "#abc", "c06a8f", "#12345g", "#1234567", "rgb(1,2,3)"):
            cfg = _reload_with({"THEME_COLOR": bad})
            self.assertEqual(cfg.Config.THEME_COLOR, "forest", bad)
            self.assertEqual(len(cfg.CONFIG_WARNINGS), 1, bad)
            self.assertIn("THEME_COLOR", cfg.CONFIG_WARNINGS[0])
            for preset in theme.PRESETS:
                self.assertIn(preset, cfg.CONFIG_WARNINGS[0])
            self.assertIn("#rrggbb", cfg.CONFIG_WARNINGS[0])

    def test_explain_lists_the_colour(self) -> None:
        self.assertIn("Visual HUD colour", repr(config.Config.explain()))

    def test_the_key_is_not_in_the_import_snapshot(self) -> None:
        self.assertNotIn("THEME_COLOR", config._IMPORT_SNAPSHOT)

    def test_the_example_file_documents_the_default(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^THEME_COLOR=forest$")


# --------------------------------------------------------------------------- the page's copy of the maths
class TestNeutralCustomColour(unittest.TestCase):
    def test_a_grey_takes_graphite_hue_and_the_lowest_k(self) -> None:
        for grey in ("#000000", "#010101", "#808080", "#ffffff"):
            self.assertEqual(theme.theme_hue_k(grey), (theme.PRESETS["graphite"][0], theme.HEX_K_MIN), grey)

    def test_every_grey_gives_the_same_palette(self) -> None:
        self.assertEqual(len({theme.hud_palette(g) for g in ("#000000", "#808080", "#ffffff")}), 1)

    def test_a_coloured_pick_keeps_its_own_hue(self) -> None:
        self.assertNotEqual(theme.theme_hue_k("#c06a8f")[0], theme.PRESETS["graphite"][0])


class TestPageParity(unittest.TestCase):
    def test_the_preset_table_is_equal(self) -> None:
        match = re.search(r"const THEME_PRESETS = \{(.*?)\};", PAGE)
        self.assertIsNotNone(match)
        found = {n: (float(h), float(k)) for n, h, k in re.findall(r"(\w+):\[([0-9.]+),\s*([0-9.]+)\]", match.group(1))}
        self.assertEqual(found, theme.PRESETS)

    def test_the_k_rule_is_equal(self) -> None:
        match = re.search(r"const THEME_HEX_K = \{DIVISOR:([0-9.]+), MIN:([0-9.]+), MAX:([0-9.]+), "
                          r"NEUTRAL:([0-9.]+)\};", PAGE)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(float(v) for v in match.groups()),
                         (theme.HEX_K_DIVISOR, theme.HEX_K_MIN, theme.HEX_K_MAX, theme.HEX_NEUTRAL_CHROMA))

    def test_the_matrix_constants_are_equal(self) -> None:
        pattern = r"\d\.\d{9,10}"
        self.assertEqual(Counter(re.findall(pattern, _block("theme"))), Counter(re.findall(pattern, THEME_PY)))

    def test_the_gamut_search_runs_the_same_number_of_steps(self) -> None:
        self.assertIn("for (var i = 0; i < 24; i++)", _block("theme"))
        self.assertIn("for _ in range(24)", THEME_PY)

    def test_the_curve_and_gamma_constants_are_equal(self) -> None:
        for literal in ("0.04045", "12.92", "0.055", "1.055", "2.4", "0.0031308", "1e-6"):
            self.assertIn(literal, _block("theme"), literal)
            self.assertIn(literal, THEME_PY, literal)

    def test_the_forest_leaf_colours_are_equal(self) -> None:
        match = re.search(r"const THEME_FOREST = \{(.*?)\};", PAGE, re.S)
        self.assertIsNotNone(match)
        found = {n: (int(r), int(g), int(b)) for n, r, g, b in
                 re.findall(r"(\w+):\[(\d+),(\d+),(\d+)\]", match.group(1))}
        self.assertEqual(found, {f: getattr(theme.FOREST, f) for f in theme._THEMED})

    def test_the_preview_draws_with_the_palette_and_keeps_no_forest_literal(self) -> None:
        start = PAGE.index("function leafVeins(")
        painter = PAGE[start:PAGE.index("function leafDraw(")]
        painter += PAGE[PAGE.index("function leafDraw("):PAGE.index("function leafDraw(") + 400]
        for literal in ("205,245,222", "27,64,46", "9,31,22", "3,18,24", "225,255,238", "215,250,230",
                        "240,255,248", "244,255,250", "238,255,248", "LEAF_C.jade"):
            self.assertNotIn(literal, painter, literal)
        self.assertIn("LEAF.pal.jade", painter)

    def test_the_page_maths_gives_the_python_values_when_run(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available for the theme maths run")
        themes = list(theme.PRESETS) + ["#000000", "#010101", "#808080", "#ffffff", "#ff0000",
                                        "#00ff00", "#0000ff", "#ffff00", "#c06a8f", "#123456"]
        script = _block("theme") + (
            "\nconst out = {};\n"
            "for (const t of %s) out[t] = {hk: themeHueK(t), pal: themePalette(t)};\n"
            "process.stdout.write(JSON.stringify(out));\n" % json.dumps(themes))
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        for name in themes:
            hue, k = theme.theme_hue_k(name)
            self.assertAlmostEqual(got[name]["hk"][0], hue, delta=1e-9, msg=name)
            self.assertAlmostEqual(got[name]["hk"][1], k, delta=1e-9, msg=name)
            pal = theme.hud_palette(name)
            for field in theme._THEMED:
                self.assertEqual(tuple(got[name]["pal"][field]), getattr(pal, field), (name, field))

    def test_the_page_contrast_holds_for_every_preset_and_custom_colour(self) -> None:
        for name in ALL:
            hue, k = theme.theme_hue_k(name)
            checks = page_contrast_checks(hue, k)
            self.assertGreaterEqual(len(checks), 30)
            self.assertEqual(failing(checks), [], name)


if __name__ == "__main__":
    unittest.main()
