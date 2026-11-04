"""The setup page's look: readable text, no network, one leaf painter.

The page is HTML and CSS, so these tests read assets/setup_template.html. The contrast test converts the
page's oklch tokens to sRGB itself (it does not import app/theme.py), blends each translucent panel over
the canvas the way a browser does, and checks every text colour against every surface it sits on.
"""

from __future__ import annotations

import math
import re
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parent.parent / "assets" / "setup_template.html").read_text(
    encoding="utf-8")

_TOKEN = re.compile(
    r"--([a-z0-9-]+):\s*oklch\(\s*([0-9.]+)\s+"
    r"(?:calc\(\s*([0-9.]+)\s*\*\s*var\(--k\)\s*\)|([0-9.]+))\s+"
    r"(?:var\(--h\)|([0-9.]+))(?:\s*/\s*([0-9.]+))?\s*\)\s*;")


def root_block() -> str:
    start = SOURCE.index(":root {")
    return SOURCE[start:SOURCE.index("\n}", start)]


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_srgb(lightness: float, chroma: float, hue: float):
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def oklch_to_srgb(lightness: float, chroma: float, hue: float):
    """Gamma-encoded sRGB in 0..1. A colour outside the gamut loses chroma, never lightness."""
    def fits(c):
        return all(-1e-6 <= v <= 1 + 1e-6 for v in _linear_srgb(lightness, c, hue))
    if not fits(chroma):
        lo, hi = 0.0, chroma
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid)
        chroma = lo
    return tuple(min(1.0, max(0.0, _gamma(v))) for v in _linear_srgb(lightness, chroma, hue))


def page_tokens(hue: float, k: float) -> dict:
    """Every oklch token of the page at one --h and --k, as name -> (rgb 0..1, alpha)."""
    tokens = {}
    for name, lightness, ck, cf, hf, alpha in _TOKEN.findall(root_block()):
        chroma = float(ck) * k if ck else float(cf)
        h = float(hf) if hf else hue
        tokens[name] = (oklch_to_srgb(float(lightness), chroma, h), float(alpha) if alpha else 1.0)
    return tokens


def _hex(value: str):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def over(fg, alpha: float, bg):
    """A browser blends in gamma-encoded sRGB."""
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(fg, bg))


def luminance(rgb) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def surfaces(tokens: dict) -> dict:
    """The surfaces text sits on, each blended over the canvas with the top light at its strongest."""
    def solid(name):
        return tokens[name][0]

    def tint(name, base):
        rgb, alpha = tokens[name]
        return over(rgb, alpha, base)

    canvas = solid("canvas")
    light = over(solid("accent"), 0.06, canvas)  # .app::before, at its centre
    glass = tint("glass", light)
    return {
        "canvas": light,
        "rail": solid("rail"),
        "glass": glass,
        "glass-hi": tint("glass-hi", glass),
        "well": tint("well", glass),
        "float": solid("float"),
        "accent-soft": tint("accent-soft", glass),
    }


def page_contrast_checks(hue: float, k: float) -> list:
    """(label, ratio, minimum) for every text colour on every surface it is used on."""
    tokens = page_tokens(hue, k)
    ground = surfaces(tokens)
    warn, danger = _hex("#f2c27a"), _hex("#f2a39b")
    rules = [
        ("text", tokens["text"][0], 4.5, ground),
        ("muted", tokens["muted"][0], 4.5, ground),
        ("faint", tokens["faint"][0], 3.0, {n: ground[n] for n in ("canvas", "rail", "glass", "well", "float")}),
        ("accent", tokens["accent"][0], 4.5, {n: ground[n] for n in ("canvas", "rail", "glass", "float", "accent-soft")}),
        ("warn", warn, 4.5, {n: ground[n] for n in ("canvas", "glass", "float", "well")}),
        ("danger", danger, 4.5, {n: ground[n] for n in ("canvas", "glass", "float", "well")}),
    ]
    checks = []
    for name, colour, minimum, on in rules:
        for surface, rgb in on.items():
            checks.append(("%s on %s" % (name, surface), contrast(colour, rgb), minimum))
    on_action = tokens["on-action"][0]
    for end in ("action-hi", "action"):
        checks.append(("on-action on %s" % end, contrast(on_action, tokens[end][0]), 4.5))
    return checks


def failing(checks: list) -> list:
    return ["%s: %.2f, needs %.1f" % (label, ratio, minimum) for label, ratio, minimum in checks if ratio < minimum]


class TestPageContrast(unittest.TestCase):
    def test_the_converter_matches_known_colours(self) -> None:
        for (lightness, chroma, hue), expected in (
                ((0.87, 0.088, 165), (156, 231, 198)), ((1.0, 0.0, 0.0), (255, 255, 255)),
                ((0.0, 0.0, 0.0), (0, 0, 0)), ((0.5, 0.0, 90.0), (99, 99, 99))):
            got = tuple(round(255 * c) for c in oklch_to_srgb(lightness, chroma, hue))
            for g, e in zip(got, expected):
                self.assertLessEqual(abs(g - e), 1, (lightness, chroma, hue, got))

    def test_forest_text_is_readable_on_every_surface(self) -> None:
        checks = page_contrast_checks(165, 1.0)
        self.assertGreaterEqual(len(checks), 30)  # a changed token table must not empty this test
        self.assertEqual(failing(checks), [])

    def test_the_token_table_has_every_surface_and_text_colour(self) -> None:
        tokens = page_tokens(165, 1.0)
        for name in ("canvas", "rail", "glass", "glass-hi", "well", "float", "text", "muted", "faint",
                     "accent", "accent-soft", "action", "action-hi", "on-action"):
            self.assertIn(name, tokens)

    def test_status_colours_and_the_ai_accent_are_fixed(self) -> None:
        root = root_block()
        for name, value in (("warn", "#f2c27a"), ("danger", "#f2a39b"), ("moon", "#b2d6f4")):
            self.assertRegex(root, r"--%s:\s*%s;" % (name, value))

    def test_the_logo_never_follows_the_theme(self) -> None:
        # The owner's Odicto mark keeps its own green (#8edcbb) in every theme.
        match = re.search(r"--logo-stem:\s*([^;]+);", root_block())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "#8edcbb")

    def test_every_oklch_token_has_a_hex_fallback(self) -> None:
        block = SOURCE[SOURCE.index("@supports not (color: oklch"):]
        block = block[:block.index("\n}\n}") if "\n}\n}" in block else block.index("html[data-motion")]
        fallback = set(re.findall(r"--([a-z0-9-]+):", block))
        self.assertEqual(set(page_tokens(165, 1.0)) - fallback, set())


class TestPageLoadsNothingFromTheNetwork(unittest.TestCase):
    def _is_network(self, value: str) -> bool:
        value = value.strip().strip("'\"")
        return bool(re.match(r"(?i)(https?:)?//", value))

    def test_no_src_stylesheet_font_or_css_url_reaches_the_network(self) -> None:
        offenders = []
        for match in re.finditer(r"""\ssrc\s*=\s*(["'])(.*?)\1""", SOURCE, re.S):
            if self._is_network(match.group(2)):
                offenders.append(("src", match.group(2)))
        for tag in re.finditer(r"<link\b[^>]*>", SOURCE, re.S):
            href = re.search(r"""\shref\s*=\s*(["'])(.*?)\1""", tag.group(0), re.S)
            if href and self._is_network(href.group(2)):
                offenders.append(("link href", href.group(2)))
        for match in re.finditer(r"url\(\s*([^)]*?)\s*\)", SOURCE):
            if self._is_network(match.group(1)):
                offenders.append(("css url", match.group(1)))
        for match in re.finditer(r"@import\s+([^;]+);", SOURCE):
            offenders.append(("css import", match.group(1)))
        self.assertEqual(offenders, [])

    def test_the_script_makes_no_cross_origin_request(self) -> None:
        script = SOURCE[SOURCE.index("<script>"):]
        for pattern in (r"fetch\(\s*['\"`]https?:", r"XMLHttpRequest", r"new\s+WebSocket", r"importScripts",
                        r"\.src\s*=\s*['\"`]https?:", r"sendBeacon\(\s*['\"`]https?:"):
            self.assertIsNone(re.search(pattern, script), pattern)

    def test_the_font_comes_from_the_install(self) -> None:
        self.assertIn("url('/assets/fonts/InstrumentSans-VF.ttf')", SOURCE)


class TestOneLeafPainter(unittest.TestCase):
    def test_the_hero_and_the_preview_share_one_painter(self) -> None:
        for name in ("leafDraw", "leafSurface", "leafVoice", "leafLabel", "leafStem"):
            self.assertEqual(len(re.findall(r"function %s\(" % name, SOURCE)), 1, name)
        self.assertIn("leafDraw(ctx, h.inst, now, LEAF_HERO.W / 2, LEAF_HERO.BOTTOM)", SOURCE)
        self.assertIn("leafDraw(ctx, LEAF.inst, now, LEAF_VIEW.W / 2 / size, LEAF_VIEW.BOTTOM / size)", SOURCE)

    def test_the_hero_leaf_has_a_canvas_on_home(self) -> None:
        home = SOURCE[SOURCE.index('id="view-overview"'):SOURCE.index('id="view-connections"')]
        self.assertIn('<canvas id="hero_leaf"', home)


class TestPageShell(unittest.TestCase):
    def test_motion_levels_and_the_reduced_motion_preference_still_stop_animation(self) -> None:
        self.assertRegex(SOURCE, r'html\[data-motion="off"\] \*[^{]*\{\s*animation: none !important;\s*transition: none !important;')
        self.assertRegex(SOURCE, r'html\[data-motion="reduced"\] \{ --t-fast: 60ms')
        start = SOURCE.index("@media (prefers-reduced-motion: reduce)")
        self.assertIn("animation: none !important", SOURCE[start:start + 200])

    def test_the_rail_keeps_icons_only_below_900px(self) -> None:
        narrow = SOURCE[SOURCE.index("@media (max-width: 900px)"):]
        narrow = narrow[:narrow.index("@media (max-width: 720px)")]
        self.assertIn("--rail-w: 64px", narrow)
        self.assertIn(".nav-label", narrow)

    def test_the_ground_has_a_top_light_and_a_grain(self) -> None:
        self.assertRegex(SOURCE, r"\.app::before \{[^}]*radial-gradient")
        self.assertRegex(SOURCE, r"\.app::after \{[^}]*var\(--grain\)[^}]*opacity: 0\.045")

    def test_the_brand_row_keeps_the_version_label(self) -> None:
        self.assertIn('<span class="brand-ver" title="Odicto version">v__APP_VERSION__</span>', SOURCE)
        # The rail shows the original Odicto mark (assets/odicto-mark.svg), not a leaf.
        self.assertIn('<svg class="brand-mark" viewBox="18 18 72 72"', SOURCE)
        self.assertIn('<line x1="65.00" y1="54.00" x2="83.00" y2="54.00"/>', SOURCE)


if __name__ == "__main__":
    unittest.main()
