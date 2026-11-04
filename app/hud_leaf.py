"""
The Odicto "leaf" HUD: pure painter, no widget, no timers.

Ported from the approved HTML canvas prototype (kept outside the repository). A
caller (indicator.py) owns the clock and the state, builds a LeafFrame each tick and
calls LeafPainter.paint. Everything here is a function of that frame, so a frame can be
rendered offscreen and checked by eye (docs/engineering-notes.md, section 3.5).

Rules kept from the HUD paint notes: every rate is per second, and every animated
value reaches its end value exactly (enter_opening == OPEN from ENTER_S on,
leave_opening fade == 0.0 from LEAVE_S on).

Imports PySide6, numpy and the standard library only (indicator imports this module).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from typing import Callable, Optional, Sequence

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

# --------------------------------------------------------------------------- layout
LEAF_H = 36
STEM_H = 32
STEM_W = 2.0
VOICE_W = 84
STEM_GAP = 8
TIP_LEN = 58
TEXT_GAP = 12
RIGHT_INSET = 18
ROWS = 5
PIX = 2
PITCH = 3
ROW_PITCH = 4
HIST = 24
AI_GLYPH_W = 14
AI_GAP = 10
AI_ALPHA = 0.74
DOTS_W = 17
RISE_PX = 12.0
DROOP = 0.06
DROOP_ERROR = 0.11
CARD_MIN_W = 420
CARD_H = 58
CARD_GAP = 6
CARD_RADIUS = 14
ENTER_S = 0.62
LEAVE_S = 0.5
LABEL_PX = 13

JADE = (142, 220, 187)
MOON = (178, 214, 244)
WARN = (242, 194, 122)
DANGER = (242, 163, 155)
INK = (231, 229, 223)

_TIP_ROUND = 4.0            # radius of the soft tip
_MAX_COLS = (VOICE_W - STEM_GAP - 6) // PITCH   # 23 pixel columns per row
_FALL = tuple(1.0 - 0.62 * (j / _MAX_COLS) for j in range(_MAX_COLS))
_BODY_MARGIN = 3.0          # image bleed around the body (the rim and AA fringe)
_SHADOW_MARGIN = 28.0       # widest shadow: 2.4 sigma + drop + 2 px
_GRAIN_TILE = 128
_GRAIN_ALPHA_MAX = 16 / 255
PLAIN_GRAIN = 0.4           # Plain keeps this share of the Veins grain strength


class Stem(Enum):
    LISTEN = "listen"
    WORK = "work"
    DONE = "done"
    NOTICE = "notice"
    ERROR = "error"


# indicator.orb_kind() results.
STEM_FOR_ORB: dict[str, Stem] = {
    "listen": Stem.LISTEN,
    "wait": Stem.WORK,
    "done": Stem.DONE,
    "notice": Stem.NOTICE,
    "reset": Stem.NOTICE,
    "error": Stem.ERROR,
}

HUD_STYLE_CHOICES: dict[str, tuple[str, ...]] = {
    "waveform": ("reach", "stream"),
    "texture": ("veins", "plain"),
    "edge": ("rim", "soft"),
    "shape": ("leaf", "capsule"),
    "size": ("1x", "1.3x", "1.5x"),
}
HUD_SIZE_SCALE = {"1x": 1.0, "1.3x": 1.3, "1.5x": 1.5}


@dataclass(frozen=True)
class HudStyle:
    """The owner choices. The first value of each is the default."""

    waveform: str = "reach"
    texture: str = "veins"
    edge: str = "rim"
    shape: str = "leaf"
    size: str = "1x"

    def __post_init__(self) -> None:
        for name, choices in HUD_STYLE_CHOICES.items():
            value = getattr(self, name)
            if value not in choices:
                raise ValueError(f"HudStyle.{name} must be one of {choices}, got {value!r}")

    @property
    def scale(self) -> float:
        return HUD_SIZE_SCALE[self.size]


def pixel_ratio(device_dpr: float, style: HudStyle) -> float:
    """Device pixels per design pixel. All geometry is in design pixels and the painter draws
    under QPainter.scale(style.scale); every snap and every layer image uses this ratio, so
    the pixel voice still lands on whole device pixels at any size."""
    return device_dpr * style.scale


# --------------------------------------------------------------------------- easing
def _cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """A CSS cubic-bezier easing on 0..1, clamped and exact at both ends (same as indicator's)."""

    def curve(t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        lo, hi = 0.0, 1.0
        u = t
        for _ in range(24):
            u = (lo + hi) * 0.5
            x = 3 * (1 - u) * (1 - u) * u * x1 + 3 * (1 - u) * u * u * x2 + u * u * u
            if x < t:
                lo = u
            else:
                hi = u
        return 3 * (1 - u) * (1 - u) * u * y1 + 3 * (1 - u) * u * u * y2 + u * u * u

    return curve


_EASE = _cubic_bezier(0.2, 0.0, 0.0, 1.0)
_ACCEL = _cubic_bezier(0.3, 0.0, 1.0, 1.0)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _seg(t: float, start: float, span: float) -> float:
    return _clamp((t - start) / span)


def _smooth(x: float) -> float:
    return x * x * (3.0 - 2.0 * x)


# --------------------------------------------------------------------------- timeline
@dataclass(frozen=True)
class Opening:
    """How far each part of the entry or exit has come, each 0..1."""

    rise: float
    grow: float
    open_h: float
    open_l: float
    open_r: float
    tip: float
    content: float
    fade: float


OPEN = Opening(1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)


def enter_opening(t: float) -> Opening:
    """Seed rises, stem grows, bud opens into the leaf, the tip forms, words fade in."""
    if t >= ENTER_S:
        return OPEN
    u = max(0.0, t) / ENTER_S
    return Opening(
        rise=_EASE(_seg(u, 0.0, 0.22)),
        grow=_EASE(_seg(u, 0.14, 0.2)),
        open_h=_EASE(_seg(u, 0.26, 0.26)),
        open_l=_EASE(_seg(u, 0.32, 0.4)),
        open_r=_EASE(_seg(u, 0.3, 0.34)),
        tip=_EASE(_seg(u, 0.5, 0.32)),
        content=_EASE(_seg(u, 0.42, 0.3)),
        fade=1.0,
    )


def leave_opening(e: float, base: Opening = OPEN) -> Opening:
    """The exit in reverse order: words, tip, sides, height, stem, rise, then a short fade.

    Each field is min(base, exit curve), so an exit that starts mid-entry never grows.
    """
    e = max(0.0, e)

    def out(start: float, span: float) -> float:
        return 1.0 - _ACCEL(_seg(e, start, span))

    fade = 0.0 if e >= LEAVE_S else min(base.fade, 1.0 - _seg(e, 0.3, 0.1))
    return Opening(
        rise=min(base.rise, out(0.26, 0.14)),
        grow=min(base.grow, out(0.22, 0.1)),
        open_h=min(base.open_h, out(0.14, 0.14)),
        open_l=min(base.open_l, out(0.06, 0.18)),
        open_r=min(base.open_r, out(0.06, 0.18)),
        tip=min(base.tip, out(0.02, 0.12)),
        content=min(base.content, out(0.0, 0.1)),
        fade=fade,
    )


# --------------------------------------------------------------------------- voice
_ATTACK_S = 0.025
_RELEASE_S = 0.14
_LEVEL_ATTACK_S = 0.03
_LEVEL_RELEASE_S = 0.2
_HIST_STEP_S = 0.045


def _follow(value: float, goal: float, dt: float, attack: float, release: float) -> float:
    """One exponential step per second: the result does not depend on how dt is split."""
    tau = attack if goal > value else release
    return value + (goal - value) * (1.0 - math.exp(-dt / tau))


class VoiceEnvelope:
    """Smoothed band levels for the five pixel rows. Pure Python, no Qt.

    rows[0] is the TOP row. hist[row][0] is the newest column (Stream mode).
    """

    def __init__(self) -> None:
        self.rows: list[float] = [0.0] * ROWS
        self.hist: list[list[float]] = [[0.0] * HIST for _ in range(ROWS)]
        self.level: float = 0.0
        self._hist_t: float = 0.0

    def clear(self) -> None:
        self.rows = [0.0] * ROWS
        self.hist = [[0.0] * HIST for _ in range(ROWS)]
        self.level = 0.0
        self._hist_t = 0.0

    def advance(self, dt: float, bands: Sequence[float]) -> None:
        if len(bands) != ROWS:
            raise ValueError(f"bands must hold {ROWS} values, got {len(bands)}")
        if dt <= 0.0:
            return
        goals = [_clamp(float(b)) for b in bands]
        self.rows = [_follow(v, g, dt, _ATTACK_S, _RELEASE_S) for v, g in zip(self.rows, goals)]
        self.level = _follow(self.level, sum(goals) / ROWS, dt, _LEVEL_ATTACK_S, _LEVEL_RELEASE_S)
        self._shift_history(dt)

    def _shift_history(self, dt: float) -> None:
        self._hist_t += dt
        steps = int(self._hist_t / _HIST_STEP_S)
        if not steps:
            return
        self._hist_t -= steps * _HIST_STEP_S
        for _ in range(min(steps, HIST)):
            for row, column in zip(self.rows, self.hist):
                column.insert(0, row)
                column.pop()


# --------------------------------------------------------------------------- geometry
@dataclass(frozen=True)
class LeafGeometry:
    left: float
    top: float
    w: float
    h: float
    stem_x: float
    cy: float
    tip: float
    droop: float


@dataclass
class LeafFrame:
    cx: float                    # centre x of the open leaf's box (logical px)
    bottom: float                # bottom edge of the open leaf
    label_w: float               # current label width; the caller animates it
    opening: Opening
    stem: Stem
    ai: bool                     # moon accent and the pixel "AI" glyph
    label: str                   # exact text
    since_state: float           # seconds since `stem` last changed
    since_enter: float           # seconds since the entry began
    now: float                   # monotonic seconds
    voice: VoiceEnvelope
    droop: float = DROOP


def leaf_width(label_w: float) -> float:
    return VOICE_W + TEXT_GAP + label_w + RIGHT_INSET


def leaf_geometry(frame: LeafFrame) -> LeafGeometry:
    o = frame.opening
    right_span = TEXT_GAP + frame.label_w + RIGHT_INSET
    stem_x = frame.cx - (right_span - VOICE_W) / 2.0
    cy = frame.bottom + (1.0 - o.rise) * RISE_PX - LEAF_H / 2.0
    h = 5.0 + (LEAF_H - 5.0) * o.open_h
    left = stem_x - 2.5 - (VOICE_W - 2.5) * o.open_l
    right = stem_x + 2.5 + (right_span - 2.5) * o.open_r
    return LeafGeometry(left, cy - h / 2.0, right - left, h, stem_x, cy, o.tip, frame.droop)


def leaf_path(geo: LeafGeometry, shape: str) -> QPainterPath:
    """The outline: a round label end on the right, and on the left a bud that opens
    into a drooping, soft-pointed leaf tip (geo.tip 0 = bud, 1 = full tip)."""
    if shape not in HUD_STYLE_CHOICES["shape"]:
        raise ValueError(f"unknown shape {shape!r}")
    x, y, w, h = geo.left, geo.top, geo.w, geo.h
    path = QPainterPath()
    if w <= 0.0 or h <= 0.0:
        return path
    r = min(h / 2.0, w / 2.0)
    if shape == "capsule":
        path.addRoundedRect(QRectF(x, y, w, h), r, r)
        return path
    tf = geo.tip
    ee = r + max(0.0, min(TIP_LEN, w * 0.4) - r) * tf
    ty = y + h * (0.5 + geo.droop * tf)
    k = 0.5523
    rr = _TIP_ROUND * tf

    def lerp(a: tuple[float, float], b: tuple[float, float]) -> QPointF:
        return QPointF(a[0] + (b[0] - a[0]) * tf, a[1] + (b[1] - a[1]) * tf)

    b1 = lerp((x + ee - ee * k, y + h), (x + ee * 0.62, y + h))
    b0 = lerp((x, ty + (y + h - ty) * k), (x + ee * 0.3, ty + (y + h - ty) * 0.5))
    t0 = lerp((x, ty - (ty - y) * k), (x + ee * 0.3, ty - (ty - y) * 0.5))
    t1 = lerp((x + ee - ee * k, y), (x + ee * 0.62, y))
    path.moveTo(x + ee, y)
    path.lineTo(x + w - r, y)
    path.arcTo(QRectF(x + w - 2 * r, y, 2 * r, 2 * r), 90, -90)
    path.lineTo(x + w, y + h - r)
    path.arcTo(QRectF(x + w - 2 * r, y + h - 2 * r, 2 * r, 2 * r), 0, -90)
    path.lineTo(x + ee, y + h)
    path.cubicTo(b1, b0, QPointF(x + rr, ty + rr * 0.45))
    if rr > 0.0:
        # The control point sits rr left of the box, so the curve's extreme is exactly
        # geo.left: the path's bounds equal the geometry.
        path.quadTo(QPointF(x - rr, ty), QPointF(x + rr, ty - rr * 0.45))
    path.cubicTo(t0, t1, QPointF(x + ee, y))
    path.closeSubpath()
    return path


# --------------------------------------------------------------------------- pixel grid
def _snap(v: float, dpr: float) -> float:
    return math.floor(v * dpr + 0.5) / dpr


def _dev(v: float, dpr: float) -> int:
    return math.floor(v * dpr + 0.5)


def _dev_rect(x0: float, y0: float, x1: float, y1: float, dpr: float) -> QRectF:
    """A rect whose four edges sit on whole device pixels (never thinner than one)."""
    dx0, dy0 = _dev(x0, dpr), _dev(y0, dpr)
    dx1, dy1 = max(_dev(x1, dpr), dx0 + 1), max(_dev(y1, dpr), dy0 + 1)
    return QRectF(dx0 / dpr, dy0 / dpr, (dx1 - dx0) / dpr, (dy1 - dy0) / dpr)


def _cell_dev(dpr: float) -> int:
    """Device size of a voice pixel: PIX logical px, rounded down so columns keep a gap."""
    return max(1, int(PIX * dpr + 1e-9))


def _row_shift(d: float, geo: LeafGeometry) -> tuple[float, float]:
    """(share s of the convergence at distance d from the stem, drop of the tip centre line)."""
    s = _smooth(_clamp((d - 24.0) / (VOICE_W - 26.0)))
    return s, geo.droop * geo.h * geo.tip * s


def voice_cells(geo: LeafGeometry, dpr: float) -> list[tuple[int, int, QRectF]]:
    """All 5 x 23 pixel cells as (row, column, rect). Row 0 is the top row.

    The five rows are the leaf's parallel veins: level near the stem, bending together
    toward the tip. Every rect edge lies on a whole device pixel, every cell has the
    same device size, and neighbours keep at least one device pixel between them (rows
    stop converging where they would touch).
    """
    cell = _cell_dev(dpr)
    cells: list[tuple[int, int, QRectF]] = []
    for j in range(_MAX_COLS):
        x_dev = _dev(geo.stem_x - STEM_GAP - (j + 1) * PITCH + (PITCH - PIX), dpr)
        d = geo.stem_x - x_dev / dpr - PIX / 2.0
        s, tip_y = _row_shift(d, geo)
        pitch = max(cell + 1, _dev(ROW_PITCH * (1.0 - 0.62 * s), dpr))
        mid = math.floor((geo.cy + tip_y) * dpr - cell / 2.0 + 0.5)
        for i in range(ROWS):
            y_dev = mid + (i - (ROWS - 1) // 2) * pitch
            cells.append((i, j, QRectF(x_dev / dpr, y_dev / dpr, cell / dpr, cell / dpr)))
    return cells


# --------------------------------------------------------------------------- text
def label_font(family: str) -> QFont:
    font = QFont(family)
    font.setPixelSize(LABEL_PX)
    font.setWeight(QFont.Weight.Medium)
    # Full hinting rounds advances, so paint and fit metrics would disagree.
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def label_width(metrics: QFontMetricsF, label: str, ai: bool, stem: Stem) -> float:
    w = metrics.horizontalAdvance(label)
    if ai:
        w += AI_GLYPH_W + AI_GAP
    if stem is Stem.LISTEN:
        w += DOTS_W
    return float(w)


# --------------------------------------------------------------------------- colours
def _qc(c: tuple[int, int, int], a: float = 1.0) -> QColor:
    return QColor(c[0], c[1], c[2], int(_clamp(a) * 255.0 + 0.5))


def _accent(stem: Stem, ai: bool) -> tuple[int, int, int]:
    if stem is Stem.NOTICE:
        return WARN
    if stem is Stem.ERROR:
        return DANGER
    if stem is Stem.DONE:
        return JADE
    return MOON if ai else JADE


def _lcg(seed: int):
    while True:
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        yield seed / 4294967296.0


@lru_cache(maxsize=1)
def _grain_base() -> QImage:
    """128 x 128 noise: light flecks and dark flecks, alpha 0..15 of 255, fixed seed."""
    rnd = _lcg(3)
    n = _GRAIN_TILE * _GRAIN_TILE
    buf = bytearray(n * 4)
    for k in range(n):
        v = next(rnd)
        a = int(abs(v - 0.5) * 2.0 * 16.0)
        if v > 0.5 and a:
            buf[k * 4:k * 4 + 4] = bytes((225 * a // 255, 245 * a // 255, 215 * a // 255, a))
        else:
            buf[k * 4 + 3] = a
    img = QImage(bytes(buf), _GRAIN_TILE, _GRAIN_TILE, _GRAIN_TILE * 4,
                 QImage.Format.Format_ARGB32_Premultiplied)
    return img.copy()


_grain_tiles: dict[float, QPixmap] = {}


def _grain_tile(dpr: float) -> QPixmap:
    """The noise tile at device resolution; its logical size is always 128 px."""
    tile = _grain_tiles.get(dpr)
    if tile is None:
        size = max(1, round(_GRAIN_TILE * dpr))
        scaled = _grain_base().scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.FastTransformation)
        tile = QPixmap.fromImage(scaled)
        tile.setDevicePixelRatio(dpr)
        _grain_tiles[dpr] = tile
    return tile


def _new_image(w_dev: int, h_dev: int, dpr: float) -> QImage:
    img = QImage(max(1, w_dev), max(1, h_dev), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    return img


def _begin(img: QImage) -> QPainter:
    q = QPainter(img)
    q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return q


def _coverage(path: QPainterPath, w_dev: int, h_dev: int, dpr: float) -> QImage:
    """An image that is opaque inside path (anti-aliased edge) and clear outside."""
    img = _new_image(w_dev, h_dev, dpr)
    q = _begin(img)
    q.fillPath(path, QColor(0, 0, 0, 255))
    q.end()
    return img


def _mask(q: QPainter, coverage: QImage) -> None:
    """Keep only what lies inside the coverage. A clip path is hard-edged, and a
    composition mode only touches the pixels a shape covers, so the mask is a full image."""
    q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    q.drawImage(0, 0, coverage)
    q.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


@dataclass(frozen=True)
class _Region:
    """An image's place in logical space, on whole device pixels."""

    ox: float
    oy: float
    w_dev: int
    h_dev: int

    @staticmethod
    def around(left: float, top: float, w: float, h: float, margin: float, dpr: float) -> "_Region":
        x0, y0 = math.floor((left - margin) * dpr), math.floor((top - margin) * dpr)
        x1, y1 = math.ceil((left + w + margin) * dpr), math.ceil((top + h + margin) * dpr)
        return _Region(x0 / dpr, y0 / dpr, x1 - x0, y1 - y0)


def _local(geo: LeafGeometry, reg: _Region) -> LeafGeometry:
    return replace(geo, left=geo.left - reg.ox, top=geo.top - reg.oy,
                   stem_x=geo.stem_x - reg.ox, cy=geo.cy - reg.oy)


def _geo_key(geo: LeafGeometry) -> tuple[float, ...]:
    return tuple(round(v, 3) for v in (geo.left, geo.top, geo.w, geo.h, geo.stem_x, geo.cy,
                                       geo.tip, geo.droop))


# --------------------------------------------------------------------------- shadow
_SHADOW_RES = 4.0   # the big blur is computed at this fraction of the logical size


def _box_radii(sigma: float, n: int = 3) -> list[int]:
    """Radii of n box blurs that together approximate a Gaussian of this sigma."""
    ideal = math.sqrt(12.0 * sigma * sigma / n + 1.0)
    low = int(ideal)
    low -= 1 if low % 2 == 0 else 0
    low = max(1, low)
    k = round((12.0 * sigma * sigma - n * low * low - 4 * n * low - 3 * n) / (-4.0 * low - 4.0))
    return [((low if i < k else low + 2) - 1) // 2 for i in range(n)]


def _box(grid: np.ndarray, r: int, axis: int) -> np.ndarray:
    """Mean over a window of 2r + 1 cells along axis; cells outside the grid count as 0."""
    n, m = grid.shape[axis], 2 * r + 1
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r + 1, r)
    acc = np.cumsum(np.pad(grid, pad), axis=axis)
    return (np.take(acc, np.arange(m, m + n), axis=axis) - np.take(acc, np.arange(n), axis=axis)) / m


def _blur_grid(grid: np.ndarray, sigma: float) -> np.ndarray:
    for r in _box_radii(sigma):
        if r > 0:
            grid = _box(_box(grid, r, 1), r, 0)
    return grid


def _gaussian_shadow(path: QPainterPath, reg: _Region, dpr: float,
                     sigma: float, alpha: float) -> QImage:
    """A black shadow of path, Gaussian-blurred (sigma in logical px) at low resolution.

    Stacked outlines make a narrow tip as dark as a broad edge; a real blur does not.
    The mask is blurred on a grid 1/4 the size, then scaled up smoothly.
    """
    w_log, h_log = reg.w_dev / dpr, reg.h_dev / dpr
    w4, h4 = math.ceil(w_log / _SHADOW_RES), math.ceil(h_log / _SHADOW_RES)
    mask = QImage(w4, h4, QImage.Format.Format_Grayscale8)
    mask.fill(0)
    q = QPainter(mask)
    q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    q.scale(w4 / w_log, h4 / h_log)
    q.fillPath(path, QColor(255, 255, 255))
    q.end()
    raw = np.frombuffer(bytes(mask.constBits()), np.uint8).reshape(h4, mask.bytesPerLine())
    blurred = _blur_grid(raw[:, :w4] / 255.0, sigma * w4 / w_log)
    pixels = np.zeros((h4, w4, 4), np.uint8)
    pixels[..., 3] = np.minimum(255, (blurred * alpha * 255.0 + 0.5).astype(np.int32))
    data = pixels.tobytes()
    small = QImage(data, w4, h4, w4 * 4, QImage.Format.Format_ARGB32_Premultiplied)
    img = small.scaled(reg.w_dev, reg.h_dev, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    return img


@lru_cache(maxsize=8)
def _shadow_stack(sigma: float, alpha: float) -> tuple[tuple[float, float], ...]:
    """(grow, layer alpha) from the outermost layer in, for a small blur.

    Hard-edged outlines whose alphas compose to the Gaussian edge profile.
    """
    step = max(0.9, sigma * 0.2)
    count = max(2, math.ceil(3.6 * sigma / step))
    layers: list[tuple[float, float]] = []
    covered = 0.0
    for m in range(count):
        grow = 2.4 * sigma - m * step
        target = alpha * 0.5 * (1.0 - math.erf((grow - step / 2.0) / (sigma * math.sqrt(2.0))))
        a = 1.0 - (1.0 - target) / (1.0 - covered)
        layers.append((grow, max(0.0, a)))
        covered = target
    return tuple(layers)


def _fill_stack(q: QPainter, build: Callable[[float], QPainterPath],
                sigma: float, alpha: float) -> None:
    for grow, a in _shadow_stack(sigma, alpha):
        path = build(grow)
        if not path.isEmpty():
            q.fillPath(path, QColor.fromRgbF(0.0, 0.0, 0.0, a))


def _grown(geo: LeafGeometry, grow: float, dy: float) -> LeafGeometry:
    g = max(grow, -(min(geo.w, geo.h) / 2.0 - 0.5))
    return replace(geo, left=geo.left - g, top=geo.top - g + dy, w=geo.w + 2 * g, h=geo.h + 2 * g)


def _build_shadow(geo: LeafGeometry, shape: str, reg: _Region, dpr: float) -> QImage:
    local = _local(geo, reg)
    # A canvas shadowBlur of 16 is a Gaussian with sigma 8, and 3 one with sigma 1.5.
    img = _gaussian_shadow(leaf_path(_grown(local, 0.0, 5.0), shape), reg, dpr, 8.0, 0.5)
    q = _begin(img)
    _fill_stack(q, lambda g: leaf_path(_grown(local, g, 1.0), shape), 1.5, 0.32)
    q.end()
    return img


# --------------------------------------------------------------------------- surface layers
_VEIN = (205, 245, 222)


def _vein_y(i: float, d: float, geo: LeafGeometry) -> float:
    s, tip_y = _row_shift(d, geo)
    return (i - (ROWS - 1) / 2.0) * ROW_PITCH * (1.0 - 0.62 * s) + tip_y


def _outer_y(sign: int, d: float, geo: LeafGeometry) -> float:
    s = _smooth(_clamp((d - 10.0) / (VOICE_W - 12.0)))
    return sign * 12.5 * (1.0 - 0.8 * s) + geo.droop * geo.h * geo.tip * s


def _vein_pen(geo: LeafGeometry, alpha: float, fall: float, width: float, dmax: float) -> QPen:
    """Alpha falls linearly with distance d from the stem: alpha * (1 - fall * d / dmax).

    One gradient pen per vein replaces ~200 per-segment pens; a rebuild runs on every
    frame of the entry and of a width change.
    """
    g = QLinearGradient(geo.stem_x, 0.0, geo.stem_x - dmax, 0.0)
    g.setColorAt(0.0, _qc(_VEIN, alpha))
    g.setColorAt(1.0, _qc(_VEIN, alpha * (1.0 - fall)))
    return QPen(QBrush(g), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin)


def _polyline(points: Sequence[QPointF]) -> QPainterPath:
    path = QPainterPath(points[0])
    for pt in points[1:]:
        path.lineTo(pt)
    return path


def _draw_veins(q: QPainter, geo: LeafGeometry) -> None:
    """Five parallel veins that bend into the tip, two edge veins and a few cross veins."""
    dmax, n = VOICE_W - 2.0, 24
    sx, cy = geo.stem_x, geo.cy
    ds = [3.0 + s * (dmax - 3.0) / n for s in range(n + 1)]
    q.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(ROWS):
        mid = i == (ROWS - 1) // 2
        q.setPen(_vein_pen(geo, 0.2 if mid else 0.12, 0.7, 0.9 if mid else 0.6, dmax))
        q.drawPath(_polyline([QPointF(sx - d, cy + _vein_y(i, d, geo)) for d in ds]))
    q.setPen(_vein_pen(geo, 0.07, 0.6, 0.5, dmax))
    for sign in (-1, 1):
        q.drawPath(_polyline([QPointF(sx - d, cy + _outer_y(sign, d, geo)) for d in ds]))
    cross = QPainterPath()
    rnd = _lcg(9)
    for _ in range(30):
        i = int(next(rnd) * (ROWS - 1))
        d = 5.0 + next(rnd) * (dmax - 14.0)
        dd = d + 2.0 + next(rnd) * 4.0
        cross.moveTo(sx - d, cy + _vein_y(i, d, geo))
        cross.lineTo(sx - dd, cy + _vein_y(i + 1, dd, geo))
    q.drawPath(cross)


def _vertical(top: float, h: float, stops: Sequence[tuple[float, QColor]]) -> QLinearGradient:
    g = QLinearGradient(0.0, top, 0.0, top + h)
    for at, color in stops:
        g.setColorAt(at, color)
    return g


def _under_layer(q: QPainter, geo: LeafGeometry, tint: Optional[str]) -> None:
    box = QRectF(geo.left - 1, geo.top - 1, geo.w + 2, geo.h + 2)
    q.fillRect(box, _vertical(geo.top, geo.h, [(0.0, QColor(27, 64, 46, 227)),
                                               (1.0, QColor(9, 31, 22, 240))]))
    cool = QLinearGradient(geo.left, 0.0, geo.stem_x, 0.0)   # the tip is cooler and darker
    cool.setColorAt(0.0, QColor(3, 18, 24, 107))
    cool.setColorAt(1.0, QColor(3, 18, 24, 0))
    q.fillRect(QRectF(box.left(), box.top(), geo.stem_x - box.left(), box.height()), cool)
    if tint:
        q.fillRect(box, QColor(130, 95, 20, 15) if tint == "notice" else QColor(120, 45, 45, 15))


def _pool_layer(q: QPainter, geo: LeafGeometry, col: tuple[int, int, int]) -> None:
    """Soft light on the voice side. Drawn at full strength; the caller sets the opacity."""
    centre = QPointF(geo.stem_x - 16.0, geo.cy)
    g = QRadialGradient(centre, 52.0)
    g.setColorAt(0.0, _qc(col, 1.0))
    g.setColorAt(1.0, _qc(col, 0.0))
    q.fillRect(QRectF(geo.left - 1, geo.top - 1, geo.w + 2, geo.h + 2), g)


def grain_strength(texture: str) -> float:
    """How strongly the leaf's grain shows: 1.0 with veins, PLAIN_GRAIN with plain."""
    if texture not in HUD_STYLE_CHOICES["texture"]:
        raise ValueError(f"unknown texture {texture!r}")
    return 1.0 if texture == "veins" else PLAIN_GRAIN


def _over_layer(q: QPainter, geo: LeafGeometry, texture: str, dpr: float) -> None:
    box = QRectF(geo.left - 1, geo.top - 1, geo.w + 2, geo.h + 2)
    if texture == "veins":
        _draw_veins(q, geo)
    q.setOpacity(grain_strength(texture))
    q.drawTiledPixmap(box, _grain_tile(dpr))
    q.setOpacity(1.0)
    q.fillRect(box, _vertical(geo.top, geo.h, [(0.0, QColor(225, 255, 238, 22)),
                                               (0.38, QColor(225, 255, 238, 0)),
                                               (0.7, QColor(0, 0, 0, 0)),
                                               (1.0, QColor(0, 0, 0, 51))]))   # sheen above, shade below


def _rim(q: QPainter, geo: LeafGeometry, shape: str, edge: str) -> None:
    ra = 0.18 if edge == "rim" else 0.07
    rim = _vertical(geo.top, geo.h, [(0.0, _qc((215, 250, 230), ra)),
                                     (0.55, _qc((215, 250, 230), ra * 0.3)),
                                     (1.0, _qc((215, 250, 230), ra * 0.12))])
    inner = replace(geo, left=geo.left + 0.5, top=geo.top + 0.5, w=geo.w - 1, h=geo.h - 1)
    q.setPen(QPen(QBrush(rim), 1.0))
    q.setBrush(Qt.BrushStyle.NoBrush)
    q.drawPath(leaf_path(inner, shape))


@dataclass
class _Body:
    key: tuple
    ox: float
    oy: float
    under: QImage
    pool: QImage
    over: QImage


def _build_body(geo: LeafGeometry, style: HudStyle, tint: Optional[str],
                col: tuple[int, int, int], reg: _Region, dpr: float) -> list[QImage]:
    local = _local(geo, reg)
    coverage = _coverage(leaf_path(local, style.shape), reg.w_dev, reg.h_dev, dpr)
    layers = []
    for paint in (lambda q: _under_layer(q, local, tint),
                  lambda q: _pool_layer(q, local, col),
                  lambda q: _over_layer(q, local, style.texture, dpr)):
        img = _new_image(reg.w_dev, reg.h_dev, dpr)
        q = _begin(img)
        paint(q)
        _mask(q, coverage)
        q.end()
        layers.append(img)
    q = _begin(layers[2])
    _rim(q, local, style.shape, style.edge)   # the rim is the top layer
    q.end()
    return layers


# --------------------------------------------------------------------------- caption card
def card_rect(geo: LeafGeometry, content: float = 1.0, dpr: float = 1.0) -> QRectF:
    """Where the caption card sits: centred above the leaf, wide enough for its text."""
    cw = max(float(CARD_MIN_W), geo.w)
    x = _snap(geo.left + geo.w / 2.0 - cw / 2.0, dpr)
    y = _snap(geo.top - CARD_GAP - CARD_H + (1.0 - content) * 6.0, dpr)
    return QRectF(x, y, cw, float(CARD_H))


def _card_path(rect: QRectF, grow: float = 0.0, dy: float = 0.0) -> QPainterPath:
    r = rect.adjusted(-grow, -grow + dy, grow, grow + dy)
    radius = _clamp(CARD_RADIUS + grow, 0.0, min(r.width(), r.height()) / 2.0)
    path = QPainterPath()
    if r.width() > 0.0 and r.height() > 0.0:
        path.addRoundedRect(r, radius, radius)
    return path


# --------------------------------------------------------------------------- painter
_GLYPH = {"A": ("010", "101", "111", "101", "101"), "I": ("111", "010", "010", "010", "111")}


class LeafPainter:
    """Paints one LeafFrame. Keeps small caches so a settled frame costs a few blits."""

    def __init__(self, font: QFont) -> None:
        _grain_base()   # built once here, so the first entry animation does not pay for it
        self._font = font
        self._metrics = QFontMetricsF(font)
        self._widths: dict[str, float] = {}
        self._shadow: Optional[tuple[tuple, _Region, QImage]] = None
        self._body: Optional[_Body] = None
        self._cells: Optional[tuple[tuple, list[tuple[int, int, QRectF]]]] = None
        self._card: Optional[tuple[tuple, _Region, QImage, QImage]] = None

    # ------------------------------------------------------------------ public
    def paint(self, p: QPainter, frame: LeafFrame, style: HudStyle) -> None:
        """Paint one frame. The frame is in design pixels; the style's size scales it."""
        o = frame.opening
        if o.fade <= 0.0:
            return
        dpr = pixel_ratio(p.device().devicePixelRatioF(), style)
        geo = self._geometry(frame, style, dpr)
        col = _accent(frame.stem, frame.ai)
        p.save()
        p.scale(style.scale, style.scale)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        base = p.opacity()
        p.setOpacity(base * o.fade)
        settled = o == OPEN
        if o.open_h > 0.02:
            self._paint_shadow(p, geo, style, o.open_h, dpr, settled)
            self._paint_body(p, geo, style, frame, col, dpr, settled)
        if o.content > 0.001:
            p.setOpacity(base * o.fade * o.content)
            self._paint_content(p, frame, geo, style, col, dpr, settled)
            p.setOpacity(base * o.fade)
        self._paint_stem(p, frame, geo, col)
        self._paint_seed(p, frame, geo)
        p.restore()

    def paint_card(self, p: QPainter, rect: QRectF, style: HudStyle, opacity: float) -> None:
        if opacity <= 0.0:
            return
        dpr = pixel_ratio(p.device().devicePixelRatioF(), style)
        shadow, surface, reg = self._card_images(rect, style, dpr)
        p.save()
        p.scale(style.scale, style.scale)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setOpacity(p.opacity() * opacity)
        p.drawImage(QPointF(reg.ox, reg.oy), shadow)
        p.drawImage(QPointF(reg.ox, reg.oy), surface)
        p.restore()

    # ------------------------------------------------------------------ geometry
    def _geometry(self, frame: LeafFrame, style: HudStyle, dpr: float) -> LeafGeometry:
        geo = leaf_geometry(frame)
        sx = _snap(geo.stem_x, dpr)
        geo = replace(geo, left=geo.left + (sx - geo.stem_x), stem_x=sx)
        return replace(geo, tip=0.0) if style.shape == "capsule" else geo

    def _text_width(self, text: str) -> float:
        w = self._widths.get(text)
        if w is None:
            w = self._widths[text] = float(self._metrics.horizontalAdvance(text))
        return w

    # ------------------------------------------------------------------ shadow and body
    def _paint_shadow(self, p: QPainter, geo: LeafGeometry, style: HudStyle, strength: float,
                      dpr: float, settled: bool) -> None:
        reg = _Region.around(geo.left, geo.top, geo.w, geo.h, _SHADOW_MARGIN, dpr)
        key = (dpr, style.size, style.shape, _geo_key(_local(geo, reg)), reg.w_dev, reg.h_dev)
        if self._shadow is not None and self._shadow[0] == key:
            img = self._shadow[2]
        else:
            img = _build_shadow(geo, style.shape, reg, dpr)
            if settled:
                self._shadow = (key, reg, img)
        base = p.opacity()
        p.setOpacity(base * strength)
        p.drawImage(QPointF(reg.ox, reg.oy), img)
        p.setOpacity(base)

    def _paint_body(self, p: QPainter, geo: LeafGeometry, style: HudStyle, frame: LeafFrame,
                    col: tuple[int, int, int], dpr: float, settled: bool) -> None:
        reg = _Region.around(geo.left, geo.top, geo.w, geo.h, _BODY_MARGIN, dpr)
        tint = frame.stem.value if frame.stem in (Stem.NOTICE, Stem.ERROR) else None
        key = (dpr, style, tint, col, _geo_key(_local(geo, reg)), reg.w_dev, reg.h_dev)   # style holds the size
        if self._body is not None and self._body.key == key:
            body = self._body
        else:
            under, pool, over = _build_body(geo, style, tint, col, reg, dpr)
            body = _Body(key, reg.ox, reg.oy, under, pool, over)
            if settled:
                self._body = body
        at = QPointF(body.ox, body.oy)
        base = p.opacity()
        p.drawImage(at, body.under)
        p.setOpacity(base * (0.08 + 0.07 * _clamp(frame.voice.level)))
        p.drawImage(at, body.pool)
        p.setOpacity(base)
        p.drawImage(at, body.over)

    # ------------------------------------------------------------------ content
    def _paint_content(self, p: QPainter, frame: LeafFrame, geo: LeafGeometry, style: HudStyle,
                       col: tuple[int, int, int], dpr: float, settled: bool) -> None:
        text_end = geo.stem_x + TEXT_GAP + label_width(self._metrics, frame.label, frame.ai,
                                                       frame.stem)
        # Nothing can leak out of a settled leaf; while it opens, closes or resizes it can.
        clip = not settled or text_end > geo.left + geo.w + 0.5
        p.save()
        if clip:
            p.setClipPath(leaf_path(geo, style.shape))
        self._paint_voice(p, frame, geo, style, col, dpr)
        self._paint_label(p, frame, geo, col, dpr)
        p.restore()

    def _voice_cells(self, geo: LeafGeometry, dpr: float) -> list[tuple[int, int, QRectF]]:
        key = (dpr, geo.stem_x, geo.cy, geo.h, geo.droop, geo.tip)
        if self._cells is None or self._cells[0] != key:
            self._cells = (key, voice_cells(geo, dpr))
        return self._cells[1]

    def _paint_voice(self, p: QPainter, frame: LeafFrame, geo: LeafGeometry, style: HudStyle,
                     col: tuple[int, int, int], dpr: float) -> None:
        stem, now, voice = frame.stem, frame.now, frame.voice
        u = frame.since_enter / ENTER_S
        sweep = _seg(u, 0.6, 0.42) * (_MAX_COLS + 4) - 2.0 if u < 1.1 else None
        stream = style.waveform == "stream"
        color = QColor(col[0], col[1], col[2])
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)   # voice pixels stay crisp
        for i, j, rect in self._voice_cells(geo, dpr):
            fall = _FALL[j]
            a = 0.0
            if stem is Stem.LISTEN or stem is Stem.DONE:
                level = voice.hist[i][j] * 1.6 if stream else voice.rows[i] * _MAX_COLS - j
                a = _clamp(level) * fall
                if stem is Stem.LISTEN and a < 0.02 and j == 0:
                    a = 0.26 + 0.1 * math.sin(now * 2.2 + i * 0.9)   # the idle first pixel
            elif stem is Stem.WORK:
                phase = (now * 1.1 + i * 0.23 + j * 0.055) % 1.0
                a = max(0.0, 1.0 - abs(j - (1.0 - phase) * _MAX_COLS) / 1.4) * 0.6 * fall
            if sweep is not None:
                a = max(a, 0.6 * math.exp(-((sweep - j) ** 2) / 2.0) * fall)   # sap fills the veins
            if a > 0.01:
                color.setAlpha(int(_clamp(a) * 255.0 + 0.5))
                p.fillRect(rect, color)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _paint_ai_glyph(self, p: QPainter, x: float, base: float,
                        col: tuple[int, int, int], dpr: float) -> None:
        color = _qc(col, AI_ALPHA)
        x = _snap(x, dpr)
        top = base - 10.0
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for ch in "AI":
            for r, bits in enumerate(_GLYPH[ch]):
                for c, bit in enumerate(bits):
                    if bit == "1":
                        x0, y0 = x + c * PIX, top + r * PIX
                        p.fillRect(_dev_rect(x0, y0, x0 + PIX, y0 + PIX, dpr), color)
            x += 8.0
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _paint_label(self, p: QPainter, frame: LeafFrame, geo: LeafGeometry,
                     col: tuple[int, int, int], dpr: float) -> None:
        stem, now = frame.stem, frame.now
        x = _snap(geo.stem_x + TEXT_GAP - (1.0 - frame.opening.content) * 4.0, dpr)
        base = _snap(geo.cy + 4.5, dpr)
        if frame.ai:
            self._paint_ai_glyph(p, x, base, col, dpr)
            x += AI_GLYPH_W + AI_GAP
        ink = DANGER if stem is Stem.ERROR else WARN if stem is Stem.NOTICE else INK
        p.setFont(self._font)
        p.setPen(_qc(ink, 0.78 if stem in (Stem.LISTEN, Stem.WORK) else 0.92))
        p.drawText(QPointF(x, base), frame.label)
        word_w = self._text_width(frame.label)
        if stem is Stem.WORK:
            self._paint_shimmer(p, frame.label, x, base, word_w, now, col)
        elif stem is Stem.LISTEN:
            self._paint_dots(p, x + word_w, base, now, col, dpr)

    def _paint_shimmer(self, p: QPainter, label: str, x: float, base: float, word_w: float,
                       now: float, col: tuple[int, int, int]) -> None:
        span = word_w + 50.0
        at = ((now * 0.6) % 1.5) / 1.5 * span - 25.0 + x
        g = QLinearGradient(at - 18.0, 0.0, at + 18.0, 0.0)
        g.setColorAt(0.0, _qc(col, 0.0))
        g.setColorAt(0.5, QColor(240, 255, 248, 255))
        g.setColorAt(1.0, _qc(col, 0.0))
        p.setPen(QPen(QBrush(g), 1.0))
        p.drawText(QPointF(x, base), label)

    def _paint_dots(self, p: QPainter, word_end: float, base: float, now: float,
                    col: tuple[int, int, int], dpr: float) -> None:
        lit = int(now * 4.0) % 4
        x = _snap(word_end + 5.0, dpr)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for d in range(3):
            x0 = _snap(x + d * 4.0, dpr)
            p.fillRect(_dev_rect(x0, base - 2.0, x0 + 2.0, base, dpr),
                       _qc(col, 0.8 if d < lit else 0.22))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # ------------------------------------------------------------------ stem and seed
    def _paint_stem(self, p: QPainter, frame: LeafFrame, geo: LeafGeometry,
                    col: tuple[int, int, int]) -> None:
        stem, x, cy = frame.stem, geo.stem_x, geo.cy
        grow = frame.opening.grow
        since = frame.since_state
        morph = _EASE(_seg(since, 0.0, 0.28))
        glyph = stem is Stem.DONE or stem is Stem.ERROR
        wd = STEM_W + (1.0 - grow) * 2.5
        hh = max(2.5, STEM_H * grow * (1.0 - morph * 0.62 if glyph else 1.0))
        top = cy - hh / 2.0
        halo = 0.5 + 0.5 * _clamp(frame.voice.level) if stem is Stem.LISTEN else 0.6
        p.setPen(Qt.PenStyle.NoPen)
        for i in (2, 1):
            g = i * 1.6
            p.setBrush(_qc(col, 0.12 * halo * (1.0 - i / 3.0)))
            p.drawRoundedRect(QRectF(x - wd / 2 - g, top - g, wd + 2 * g, hh + 2 * g),
                              (wd + 2 * g) / 2, (wd + 2 * g) / 2)
        if stem is Stem.NOTICE:
            self._stem_bang(p, x, top, wd, hh, col)
        elif stem is Stem.WORK:
            self._stem_bead(p, x, top, wd, hh, frame.now, col)
        else:
            p.setBrush(_qc(col, 0.95 * (1.0 - morph) if glyph else 0.94))
            p.drawRoundedRect(QRectF(x - wd / 2, top, wd, hh), wd / 2, wd / 2)
            if not glyph:
                p.setBrush(QColor(244, 255, 250, 128))
                p.drawRect(QRectF(x - 0.5, top + 3.0, 1.0, max(0.0, hh - 6.0)))
            else:
                self._stem_glyph(p, stem, x, cy, since, col)

    @staticmethod
    def _stem_bang(p: QPainter, x: float, top: float, wd: float, hh: float,
                   col: tuple[int, int, int]) -> None:
        p.setBrush(_qc(col, 0.95))
        p.drawRoundedRect(QRectF(x - wd / 2, top, wd, max(0.0, hh - 7.0)), wd / 2, wd / 2)
        p.drawEllipse(QPointF(x, top + hh - 1.5), 1.6, 1.6)

    @staticmethod
    def _stem_bead(p: QPainter, x: float, top: float, wd: float, hh: float, now: float,
                   col: tuple[int, int, int]) -> None:
        p.setBrush(_qc(col, 0.45))
        p.drawRoundedRect(QRectF(x - wd / 2, top, wd, hh), wd / 2, wd / 2)
        by = top + 3.0 + (hh - 10.0) * (0.5 + 0.5 * math.sin(now * math.pi * 1.4))
        p.setBrush(QColor(238, 255, 248, 242))
        p.drawRoundedRect(QRectF(x - wd / 2, by, wd, 5.0), wd / 2, wd / 2)

    @staticmethod
    def _stem_glyph(p: QPainter, stem: Stem, x: float, cy: float, since: float,
                    col: tuple[int, int, int]) -> None:
        """The check or the cross draws on after the stem has shrunk."""
        d = _EASE(_seg(since, 0.08, 0.3))
        path = QPainterPath()
        if stem is Stem.DONE:
            l1, l2 = math.hypot(3.3, 3.5), math.hypot(6.2, 8.5)
            run = d * (l1 + l2)
            path.moveTo(x - 4.5, cy + 0.5)
            if run <= l1:
                q = run / l1
                path.lineTo(x - 4.5 + 3.3 * q, cy + 0.5 + 3.5 * q)
            else:
                q = (run - l1) / l2
                path.lineTo(x - 1.2, cy + 4.0)
                path.lineTo(x - 1.2 + 6.2 * q, cy + 4.0 - 8.5 * q)
        else:
            a, b, s = min(1.0, d * 2.0), max(0.0, d * 2.0 - 1.0), 4.0
            path.moveTo(x - s, cy - s)
            path.lineTo(x - s + 2 * s * a, cy - s + 2 * s * a)
            if b > 0.0:
                path.moveTo(x + s, cy - s)
                path.lineTo(x + s - 2 * s * b, cy - s + 2 * s * b)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_qc(col, 0.98), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)

    def _paint_seed(self, p: QPainter, frame: LeafFrame, geo: LeafGeometry) -> None:
        """Done: the leaf lets a seed go."""
        if frame.stem is not Stem.DONE:
            return
        prog = _seg(frame.since_state, 0.32, 1.0)
        if prog <= 0.0 or prog >= 1.0:
            return
        y = geo.cy - 6.0 - _EASE(prog) * 22.0
        sx = geo.stem_x + math.sin(prog * 6.0) * 1.5
        a = (1.0 - prog * prog) * min(1.0, prog * 8.0)
        g = QRadialGradient(QPointF(sx, y), 6.0)
        g.setColorAt(0.0, _qc(JADE, 0.45 * a))
        g.setColorAt(1.0, _qc(JADE, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRect(QRectF(sx - 6.0, y - 6.0, 12.0, 12.0))
        p.setBrush(_qc((240, 255, 248), a))
        p.drawEllipse(QPointF(sx, y), 1.3, 1.3)

    # ------------------------------------------------------------------ caption card
    def _card_images(self, rect: QRectF, style: HudStyle,
                     dpr: float) -> tuple[QImage, QImage, _Region]:
        reg = _Region.around(rect.x(), rect.y(), rect.width(), rect.height(), _SHADOW_MARGIN, dpr)
        local = QRectF(rect.x() - reg.ox, rect.y() - reg.oy, rect.width(), rect.height())
        key = (dpr, style.size, style.edge, round(local.x(), 3), round(local.y(), 3),
               round(local.width(), 3), reg.w_dev, reg.h_dev)
        if self._card is None or self._card[0] != key:
            self._card = (key, reg, _build_card_shadow(local, reg, dpr),
                          _build_card_surface(local, style.edge, reg, dpr))
        return self._card[2], self._card[3], self._card[1]


def _build_card_shadow(local: QRectF, reg: _Region, dpr: float) -> QImage:
    return _gaussian_shadow(_card_path(local, 0.0, 4.0), reg, dpr, 7.0, 0.4)


def _build_card_surface(local: QRectF, edge: str, reg: _Region, dpr: float) -> QImage:
    img = _new_image(reg.w_dev, reg.h_dev, dpr)
    q = _begin(img)
    q.fillRect(local, _vertical(local.top(), local.height(), [(0.0, QColor(24, 58, 42, 230)),
                                                              (1.0, QColor(10, 32, 23, 240))]))
    q.drawTiledPixmap(local, _grain_tile(dpr))
    q.fillRect(local, _vertical(local.top(), local.height(), [(0.0, QColor(225, 255, 238, 18)),
                                                              (0.3, QColor(225, 255, 238, 0))]))
    _mask(q, _coverage(_card_path(local), reg.w_dev, reg.h_dev, dpr))
    q.setPen(QPen(_qc((215, 250, 230), 0.15 if edge == "rim" else 0.06), 1.0))
    q.setBrush(Qt.BrushStyle.NoBrush)
    q.drawPath(_card_path(local.adjusted(0.5, 0.5, -0.5, -0.5)))
    q.end()
    return img
