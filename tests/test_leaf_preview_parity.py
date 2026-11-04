"""The setup page's Appearance preview draws the same leaf as the HUD painter.

The preview is JavaScript in assets/setup_template.html, so it cannot import app/hud_leaf.py.
These tests read the preview's numbers and fail when one drifts from the painter. Each table
must map every key it has, so a new preview number cannot slip in unchecked.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import hud_leaf
from indicator import load_brand_font

SOURCE = (Path(__file__).resolve().parent.parent / "assets" / "setup_template.html").read_text(
    encoding="utf-8")

# Preview key -> painter constant.
GEOMETRY = {
    "H": "LEAF_H", "STEM_H": "STEM_H", "STEM_W": "STEM_W", "VOICE_W": "VOICE_W",
    "STEM_GAP": "STEM_GAP", "TIP": "TIP_LEN", "TEXT_GAP": "TEXT_GAP",
    "RIGHT_INSET": "RIGHT_INSET", "ROWS": "ROWS", "PIX": "PIX", "PITCH": "PITCH",
    "ROW_PITCH": "ROW_PITCH", "HIST": "HIST",
}
COLOURS = {"jade": "JADE", "warn": "WARN", "danger": "DANGER", "ink": "INK"}
TIMES = {"ENTER": "ENTER_S", "EXIT": "LEAVE_S"}
# How long the preview's demo loop holds each state. The HUD follows the real pipeline.
PREVIEW_ONLY_TIMES = {"LISTEN", "WORK", "DONE", "GAP"}


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _table(name: str) -> str:
    match = re.search(r"const %s = \{(.*?)\};" % name, SOURCE)
    if match is None:
        raise AssertionError("the preview has no %s table; update this test" % name)
    return match.group(1)


def _numbers(name: str) -> dict:
    return {k: float(v) for k, v in re.findall(r"(\w+)\s*:\s*(-?[0-9.]+)", _table(name))}


def _literal(pattern: str, what: str) -> float:
    match = re.search(pattern, SOURCE)
    if match is None:
        raise AssertionError("the preview no longer writes the %s as %r; update this test"
                             % (what, pattern))
    return float(match.group(1))


class TestLeafPreviewParity(unittest.TestCase):
    def test_the_preview_geometry_is_the_painter_geometry(self) -> None:
        self.assertEqual(
            _numbers("LEAF_K"),
            {k: float(getattr(hud_leaf, v)) for k, v in GEOMETRY.items()})

    def test_the_preview_colours_are_the_painter_colours(self) -> None:
        found = {k: (int(r), int(g), int(b)) for k, r, g, b in re.findall(
            r"(\w+)\s*:\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]", _table("LEAF_C"))}
        self.assertEqual(found, {k: tuple(getattr(hud_leaf, v)) for k, v in COLOURS.items()})

    def test_the_preview_opens_and_closes_in_the_painter_times(self) -> None:
        times = _numbers("LEAF_TIME")
        self.assertEqual(set(times), set(TIMES) | PREVIEW_ONLY_TIMES)
        self.assertEqual({k: times[k] for k in TIMES},
                         {k: float(getattr(hud_leaf, v)) for k, v in TIMES.items()})

    def test_the_preview_tip_droop_and_rise_are_the_painter_values(self) -> None:
        self.assertEqual(_literal(r"droop:\s*([0-9.]+)", "tip droop"), hud_leaf.DROOP)
        self.assertEqual(_literal(r"\(1 - rise\) \* ([0-9.]+)", "seed rise"), hud_leaf.RISE_PX)

    def test_the_preview_label_font_is_the_painter_label_font(self) -> None:
        match = re.search(r"const LEAF_FONT = '(\d+) (\d+)px \"([^\"]+)\"", SOURCE)
        self.assertIsNotNone(match, "the preview has no LEAF_FONT; update this test")
        family = load_brand_font()
        self.assertIsNotNone(family, "the brand font file is missing from assets/fonts")
        font = hud_leaf.label_font(family)
        self.assertEqual((int(match.group(1)), int(match.group(2)), match.group(3)),
                         (font.weight().value, font.pixelSize(), family))


if __name__ == "__main__":
    unittest.main()
