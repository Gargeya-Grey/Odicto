"""Render contact sheets of the leaf HUD offscreen, and time LeafPainter.paint.

No widget, no window, no Odicto process: QImage only. Look at the sheets by eye; no test
compares pixels (docs/engineering-notes.md, section 3.5).

    python tools/hud_snapshots.py --out <folder>
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication

import hud_leaf as hl
from indicator import BRAND_FONT_FAMILY, load_brand_font

CELL_W, CELL_H = 304, 96          # logical px; a multiple of 4 keeps 1.25x and 1.5x whole
DARK, LIGHT = "#16181b", "#f4f2ec"
BANDS = [0.92, 0.74, 0.5, 0.8, 0.28]       # a fixed voice pattern, top row first
SAMPLE = ("so the plan for next week is to ship the new pill first",
          "and then look at the live captions once the texture work has settled")


# --------------------------------------------------------------------------- scene
class Scene:
    def __init__(self, family: str) -> None:
        self.font = hl.label_font(family)
        self.family = family
        self.metrics = QFontMetricsF(self.font)
        self.painter = hl.LeafPainter(self.font)

    def voice(self, bands: Sequence[float] = BANDS, settle: float = 0.6) -> hl.VoiceEnvelope:
        v = hl.VoiceEnvelope()
        for _ in range(int(settle / 0.016)):
            v.advance(0.016, bands)
        for i in range(hl.ROWS):                          # a readable Stream history
            for j in range(hl.HIST):
                v.hist[i][j] = max(0.0, min(1.0, bands[i] * (0.55 + 0.45 * math.sin(j * 0.55 + i))))
        return v

    def frame(self, stem: hl.Stem, label: str, *, ai: bool = False, w: float = CELL_W,
              h: float = CELL_H, opening: hl.Opening = hl.OPEN, since_state: float = 5.0,
              since_enter: float = 5.0, now: float = 3.3, voice: hl.VoiceEnvelope | None = None,
              droop: float | None = None) -> hl.LeafFrame:
        label_w = hl.label_width(self.metrics, label, ai, stem)
        if droop is None:
            droop = hl.DROOP_ERROR if stem is hl.Stem.ERROR else hl.DROOP
        return hl.LeafFrame(cx=w / 2, bottom=h - 24, label_w=label_w, opening=opening, stem=stem,
                            ai=ai, label=label, since_state=since_state, since_enter=since_enter,
                            now=now, voice=voice or hl.VoiceEnvelope(), droop=droop)


def backdrop(p: QPainter, kind: str, w: float, h: float) -> None:
    if kind == "busy":
        p.fillRect(QRectF(0, 0, w, h), QColor("#2b2140"))
        for cx, cy, r, c in ((0.2, 0.3, 0.55, "#c47a3a"), (0.8, 0.6, 0.6, "#3a6fc4")):
            g = QRadialGradient(QPointF(w * cx, h * cy), max(w, h) * r)
            g.setColorAt(0.0, QColor(c))
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, 0, w, h), g)
        return
    p.fillRect(QRectF(0, 0, w, h), QColor(LIGHT if kind == "light" else DARK))


def cell(w: float, h: float, dpr: float, bg: str, draw: Callable[[QPainter], None]) -> QImage:
    """One rendered cell, in device pixels (its devicePixelRatio is reset to 1 for the sheet)."""
    img = QImage(round(w * dpr), round(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    backdrop(p, bg, w, h)
    sample_text(p, bg, w, h)
    draw(p)
    p.end()
    img.setDevicePixelRatio(1.0)
    return img


def sample_text(p: QPainter, bg: str, w: float, h: float) -> None:
    f = QFont(BRAND_FONT_FAMILY)
    f.setPixelSize(13)
    p.setFont(f)
    p.setPen(QColor("#2a2a28") if bg == "light" else QColor(255, 255, 255, 96 if bg == "busy" else 90))
    for n, y in enumerate((18, 38, 58)):
        p.drawText(QPointF(8, y), "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Integer" if n != 1
                   else "posuere erat a ante venenatis dapibus posuere velit aliquet. Cras mattis")


# --------------------------------------------------------------------------- sheets
def sheet(path: Path, cells: list[tuple[str, QImage]], cols: int, family: str,
          pad: int = 14, bg: str = "#0b0f0d") -> None:
    cw = max(img.width() for _, img in cells)
    ch = max(img.height() for _, img in cells)
    cap = 26
    rows = math.ceil(len(cells) / cols)
    out = QImage(pad + cols * (cw + pad), pad + rows * (ch + cap + pad), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor(bg))
    p = QPainter(out)
    font = QFont(family)
    font.setPixelSize(13)
    p.setFont(font)
    p.setPen(QColor("#9FB3A8"))
    for n, (caption, img) in enumerate(cells):
        x = pad + (n % cols) * (cw + pad)
        y = pad + (n // cols) * (ch + cap + pad)
        p.drawImage(QPointF(x, y), img)
        p.drawText(QPointF(x + 4, y + ch + 17), caption)
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(path))
    print("wrote", path)


def leaf_cell(scene: Scene, frame: hl.LeafFrame, style: hl.HudStyle, dpr: float,
              bg: str = "dark", w: float = CELL_W, h: float = CELL_H) -> QImage:
    return cell(w, h, dpr, bg, lambda p: scene.painter.paint(p, frame, style))


STATES = [
    ("Listening", hl.Stem.LISTEN, "Listening", False, 5.0),
    ("AI listening", hl.Stem.LISTEN, "Listening", True, 5.0),
    ("Transcribing", hl.Stem.WORK, "Transcribing", False, 5.0),
    ("AI thinking", hl.Stem.WORK, "Thinking", True, 5.0),
    ("Done (check + seed, 0.9 s)", hl.Stem.DONE, "Done", False, 0.9),
    ("Notice", hl.Stem.NOTICE, "Context cleared", False, 5.0),
    ("Error", hl.Stem.ERROR, "Mic unavailable", False, 5.0),
]


def sheet_states(out: Path, scene: Scene, dpr: float) -> None:
    v = scene.voice()
    cells = []
    for caption, stem, label, ai, since in STATES:
        f = scene.frame(stem, label, ai=ai, since_state=since, voice=v if stem is hl.Stem.LISTEN else None,
                        now=3.3 if stem is not hl.Stem.WORK else 3.1)
        cells.append((caption, leaf_cell(scene, f, hl.HudStyle(), dpr)))
    sheet(out / f"states_{dpr:g}x.png", cells, 2, scene.family)


def sheet_styles(out: Path, scene: Scene, dpr: float = 2.0) -> None:
    v = scene.voice()
    cells = []
    for wave, tex, edge, shape in itertools.product(*hl.HUD_STYLE_CHOICES.values()):
        style = hl.HudStyle(wave, tex, edge, shape)
        f = scene.frame(hl.Stem.LISTEN, "Listening", voice=v)
        cells.append((f"{wave} / {tex} / {edge} / {shape}", leaf_cell(scene, f, style, dpr)))
    sheet(out / "styles.png", cells, 4, scene.family)


def sheet_scales(out: Path, scene: Scene) -> None:
    v = scene.voice()
    cells = []
    for stem, label in ((hl.Stem.LISTEN, "Listening"), (hl.Stem.DONE, "Done"), (hl.Stem.ERROR, "Mic unavailable")):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            f = scene.frame(stem, label, voice=v if stem is hl.Stem.LISTEN else None,
                            since_state=0.9 if stem is hl.Stem.DONE else 5.0)
            cells.append((f"{stem.value} at {dpr:g}x", leaf_cell(scene, f, hl.HudStyle(), dpr)))
    sheet(out / "scales.png", cells, 4, scene.family)


def sheet_motion(out: Path, scene: Scene, dpr: float = 2.0) -> None:
    v = scene.voice()
    cells = []
    enter = [0.0, 0.08, 0.16, 0.22, 0.28, 0.34, 0.40, 0.46, 0.52, 0.62]
    for t in enter:
        f = scene.frame(hl.Stem.LISTEN, "Listening", opening=hl.enter_opening(t), since_enter=t,
                        since_state=t, voice=v)
        cells.append((f"enter {t:.2f} s", leaf_cell(scene, f, hl.HudStyle(), dpr)))
    for e in [0.0, 0.04, 0.08, 0.12, 0.16, 0.2, 0.26, 0.32, 0.38, 0.45]:
        f = scene.frame(hl.Stem.LISTEN, "Listening", opening=hl.leave_opening(e), voice=v)
        cells.append((f"exit {e:.2f} s", leaf_cell(scene, f, hl.HudStyle(), dpr)))
    sheet(out / "motion.png", cells, 4, scene.family)


def sheet_backdrops(out: Path, scene: Scene, dpr: float = 2.0) -> None:
    v = scene.voice()
    cells = []
    for bg in ("dark", "light", "busy"):
        for stem, label, ai in ((hl.Stem.LISTEN, "Listening", False), (hl.Stem.LISTEN, "Listening", True),
                                (hl.Stem.ERROR, "Mic unavailable", False)):
            f = scene.frame(stem, label, ai=ai, voice=v if stem is hl.Stem.LISTEN else None)
            cells.append((f"{bg}{' AI' if ai else ''} {stem.value}", leaf_cell(scene, f, hl.HudStyle(), dpr, bg)))
    sheet(out / "backdrops.png", cells, 3, scene.family)


def sheet_card(out: Path, scene: Scene, dpr: float = 2.0) -> None:
    v = scene.voice()
    w, h = 460.0, 168.0
    cells = []
    for bg in ("dark", "light"):
        f = scene.frame(hl.Stem.LISTEN, "Listening", ai=True, voice=v, w=w, h=h)

        def draw(p: QPainter, f: hl.LeafFrame = f) -> None:
            geo = hl.leaf_geometry(f)
            rect = hl.card_rect(geo, 1.0, p.device().devicePixelRatioF())
            scene.painter.paint_card(p, rect, hl.HudStyle(), 1.0)
            scene.painter.paint(p, f, hl.HudStyle())
            draw_card_text(p, rect, scene.family)

        cells.append((f"caption card, {bg}", cell(w, h, dpr, bg, draw)))
    sheet(out / "card.png", cells, 2, scene.family)


def draw_card_text(p: QPainter, rect: QRectF, family: str) -> None:
    f = QFont(family)
    f.setPixelSize(14)
    p.setFont(f)
    for row, line in enumerate(SAMPLE):
        p.setPen(QColor(231, 229, 223, 140 if row == 0 else 235))
        p.drawText(QPointF(rect.x() + 18, rect.y() + 12 + row * 20 + 10), line)


# --------------------------------------------------------------------------- timing
def median_paint_ms(scene: Scene, dpr: float = 1.5, n: int = 300) -> float:
    """Median time of paint() for a settled LISTEN frame whose voice moves every frame."""
    img = QImage(round(CELL_W * dpr), round(CELL_H * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(QColor(DARK))
    p = QPainter(img)
    voice = hl.VoiceEnvelope()
    frame = scene.frame(hl.Stem.LISTEN, "Listening", voice=voice)
    style = hl.HudStyle()
    scene.painter.paint(p, frame, style)             # warm the caches: the first paint builds them
    times = []
    for k in range(n):
        env = 0.5 + 0.5 * math.sin(k * 0.21)
        voice.advance(0.016, [env * (0.4 + 0.12 * ((k + i) % 5)) for i in range(hl.ROWS)])
        frame.now += 0.016
        t0 = time.perf_counter()
        scene.painter.paint(p, frame, style)
        times.append(time.perf_counter() - t0)
    p.end()
    return statistics.median(times) * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="folder for the PNG sheets")
    args = ap.parse_args()
    app = QApplication.instance() or QApplication([])
    family = load_brand_font() or BRAND_FONT_FAMILY
    scene = Scene(family)
    for dpr in (1.0, 2.0):
        sheet_states(args.out, scene, dpr)
    sheet_styles(args.out, scene)
    sheet_scales(args.out, scene)
    sheet_motion(args.out, scene)
    sheet_backdrops(args.out, scene)
    sheet_card(args.out, scene)
    ms = median_paint_ms(scene)
    print(f"median paint, settled LISTEN, dpr 1.5, 300 paints: {ms:.3f} ms (budget 3 ms)")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
