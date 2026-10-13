"""
Odicto floating status HUD — the Odicto Mobile voice bubble, at desktop size.

Design language (shared with Odicto Mobile and the setup page):
  - Graphite capsule, 1 px hairline, soft neutral shadow; no coloured body wash
  - Pale jade accent (#8EDCBB) for waveform, spinner, halo and chips only
  - A status disc at the left end, the phone's disc language: jade action fill while
    listening, a raised disc with a jade arc while working, pale jade / amber / soft
    red with a dark glyph for done / done-with-notice / error
  - Instrument Sans (shipped in assets/fonts), Segoe UI Variable as fallback
  - Motion: sizes follow a critically damped spring (no bounce); entries use
    cubic-bezier(0.2, 0, 0, 1), exits accelerate and finish sooner than entries;
    every rate is per second, so the look is the same at any frame interval
  - Click-through, no focus steal, always on top, bottom-center
"""

from __future__ import annotations

import math
import threading
import os
import sys
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import (
    QPointF,
    QRectF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QLinearGradient,
    QPaintEvent,
    QFontMetrics,
    QFontMetricsF,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from platforms import apply_window_exstyles
from paths import ROOT


# The Odicto logo, tried in order: the vector first, then the raster copy.
_LOGO_FILES = ("odicto-icon.svg", "odicto-icon-256.png")


def logo_icon(root: Optional[Path] = None) -> QIcon:
    """The Odicto logo as a QIcon, read from ``<install root>/assets``.

    The root is ``paths.ROOT`` (never the working directory). A file counts only
    if it really renders: a missing Qt SVG plugin or a damaged file moves on to
    the next one. Returns a null icon when none loads. Needs a QGuiApplication.
    """
    base = Path(ROOT if root is None else root) / "assets"
    for name in _LOGO_FILES:
        path = base / name
        try:
            if not path.is_file():
                continue
            icon = QIcon(str(path))
            if not icon.isNull() and not icon.pixmap(32, 32).isNull():
                return icon
        except Exception:
            continue
    return QIcon()


# The brand face, shared with the setup page and Odicto Mobile.
BRAND_FONT_FAMILY = "Instrument Sans"
_BRAND_FONT_FILE = Path("assets") / "fonts" / "InstrumentSans-VF.ttf"
# Used, in order, when the brand font cannot be loaded.
_FALLBACK_FAMILIES = (
    "Segoe UI Variable Display",
    "Segoe UI Variable",
    "Segoe UI",
    "SF Pro Display",
    "Inter",
    "Ubuntu",
    "Noto Sans",
    "DejaVu Sans",
)


def load_brand_font(root: Optional[Path] = None) -> Optional[str]:
    """Register Instrument Sans from ``<install root>/assets/fonts``; return its family.

    Returns None when the file is missing or Qt rejects it, so the caller falls
    back to a system face. Registering the same file again is harmless. The
    variable font registers real 400/500/600/700 instances (not faux bold).
    Needs a QGuiApplication.
    """
    path = Path(ROOT if root is None else root) / _BRAND_FONT_FILE
    try:
        if not path.is_file():
            return None
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            return None
        families = QFontDatabase.applicationFontFamilies(font_id)
    except Exception:
        return None
    if BRAND_FONT_FAMILY in families:
        return BRAND_FONT_FAMILY
    return families[0] if families else None


class GuiState(Enum):
    HIDDEN = auto()
    BOOTING = auto()
    RECORDING = auto()
    PROCESSING = auto()
    SUCCESS = auto()
    ERROR = auto()
    RESET = auto()


# --------------------------------------------------------------------------- theme
class _Theme:
    """Odicto Mobile tokens (Odicto-Mobile repo: apps/mobile/src/voice.css, VoiceControlView.kt).

    The setup page carries the same values; TestBrandLook keeps the two equal.
    """

    # Capsule: the phone bubble (#212326) split into a faint top-to-bottom fall.
    surface_top = QColor(0x26, 0x29, 0x2C, 247)
    surface_bot = QColor(0x1D, 0x1F, 0x22, 247)
    hairline = QColor(0xE7, 0xE5, 0xDF, 31)      # text colour at 12 %
    top_light = QColor(255, 255, 255, 18)       # 1 px light along the top edge

    text = QColor(0xE7, 0xE5, 0xDF)
    muted = QColor(0xB2, 0xB5, 0xB5)
    accent = QColor(0x8E, 0xDC, 0xBB)           # pale jade
    action = QColor(0x2A, 0x74, 0x5D)           # jade action fill
    action_hi = QColor(0x2C, 0x78, 0x60)
    on_action = QColor(0xF4, 0xF6, 0xF1)
    raised = QColor(0x30, 0x34, 0x38)
    ink = QColor(0x17, 0x19, 0x1C)              # dark glyph on a light disc
    warn = QColor(0xF2, 0xC2, 0x7A)
    danger = QColor(0xF2, 0xA3, 0x9B)

    chip_fill = QColor(0x8E, 0xDC, 0xBB, 33)    # jade tint, 13 %
    chip_border = QColor(0x8E, 0xDC, 0xBB, 77)  # 30 %
    disc_track = QColor(0xE7, 0xE5, 0xDF, 26)


# Compact layout: [ inset | disc | gap | (waveform | gap)? | label | (gap + chip)? | inset ]
_COMPACT_H = 44
_ORB = 30                                  # status disc diameter
_ORB_INSET = (_COMPACT_H - _ORB) // 2      # 7: the disc is concentric with the capsule end
_GAP_ORB_TEXT = 10
_INSET_R = 16
_GAP_TEXT_CHIP = 8
_FIT_SLACK = 4.0           # absorbs paint-time metric rounding (HiDPI)
_MIN_PILL_W = 96
_CHIP_H = 20
_CHIP_PAD_X = 8
_RECORDING_BARS = 13
_RECORDING_BAR_W = 2.0
# A 4 px pitch lands bar edges on whole device pixels at 1.25x, 1.5x and 2x.
_RECORDING_BAR_GAP = 2.0
# The slot is the bars themselves, so Listening has no empty track beside them.
_RECORDING_WAVE_W = (
    _RECORDING_BARS * _RECORDING_BAR_W
    + (_RECORDING_BARS - 1) * _RECORDING_BAR_GAP
)
_GAP_WAVE_TEXT = 12
_WAVE_MIN_H = 3.0          # a silent bar is a dot, never gone
_WAVE_MAX_H = 20.0
_LIVE_MIN_W = 440
_LIVE_H = 88
_PAD = 16                  # shadow bleed around the capsule (a multiple of 4)
# (grow px, drop px, alpha): ten faint layers falling off, then a tight contact shadow.
_SHADOW_LAYERS = tuple(
    (i * 1.1, 1.5 + i * 0.6, int(16 * (1.0 - (i - 1) / 11.0) ** 1.6)) for i in range(10, 0, -1)
) + ((0.0, 0.75, 34),)

# Motion, in seconds. Entries ease with cubic-bezier(0.2, 0, 0, 1); exits accelerate.
_RISE_S = 0.20             # droplet rises from below bottom-center
_SPREAD_AT_S = 0.08        # spread overlaps the rise
_SPREAD_S = 0.30           # droplet spreads left and right to the capsule
# The capsule moves this long after the new words are fully in, so a shrink
# cannot clip the longer label while that label is still on screen.
_RESIZE_AFTER_TEXT_S = 0.0003
_WORDS_OUT_S = 0.10        # exit: words fade first
_COLLAPSE_AT_S = 0.04
_COLLAPSE_S = 0.20         # exit: capsule contracts to the droplet
_DROP_AT_S = _COLLAPSE_AT_S + _COLLAPSE_S - 0.04
_DROP_S = 0.14             # exit: droplet drops and fades
_ENTER_S = _SPREAD_AT_S + _SPREAD_S
_LEAVE_S = _DROP_AT_S + _DROP_S
_DROP_PX = 8.0
_DROPLET_MIN = 0.8         # droplet diameter at the start of the rise / end of the drop
_SIZE_OMEGA = 20.5         # size spring, critically damped: within 1 % in ~0.33 s
_WORDS_IN_S = 0.18         # new words fade in (and rise _WORDS_RISE_PX)
_WORDS_RISE_PX = 3.0
_GLYPH_S = 0.30            # disc change: cross-fade in the first half, pop settles by the end
_POP_FROM = 0.86           # the new disc starts at this scale
_POP_OMEGA = 26.0
_POP_ZETA = 0.7            # a small, single overshoot on the disc only
_CHECK_S = 0.30            # check / cross stroke draws on
_SPIN_REV_S = 1.0          # working arc: one turn per second, as on the phone
_WAVE_ATTACK_S = 0.025     # level rises fast...
_WAVE_RELEASE_S = 0.14     # ...and falls slowly
_WAVE_IDLE_S = 0.12
_DT_MAX = 0.05             # a stalled or slowed tick never jumps an animation
_SUCCESS_HOLD_MS = 1200
# Paint interval while the pill animates, and while it only holds a finished,
# unchanging frame (a standing error such as "Mic unavailable").
_ANIM_TICK_MS = 16
_STATIC_TICK_MS = 250


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """A CSS ``cubic-bezier(x1, y1, x2, y2)`` easing on 0..1, clamped, exact at the ends."""

    def curve(t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        lo, hi = 0.0, 1.0
        u = t
        for _ in range(24):  # bisection on x(u) = t: 2^-24 is far below a pixel
            u = (lo + hi) * 0.5
            x = 3 * (1 - u) * (1 - u) * u * x1 + 3 * (1 - u) * u * u * x2 + u * u * u
            if x < t:
                lo = u
            else:
                hi = u
        return 3 * (1 - u) * (1 - u) * u * y1 + 3 * (1 - u) * u * u * y2 + u * u * u

    return curve


# The Odicto easing (setup page and phone: --ease) and Material's standard accelerate.
ease_standard = cubic_bezier(0.2, 0.0, 0.0, 1.0)
ease_accelerate = cubic_bezier(0.3, 0.0, 1.0, 1.0)


def pop_scale(progress: float) -> float:
    """Disc scale for a glyph change at ``progress`` (0..1 of _GLYPH_S): one soft overshoot.

    The step response of an underdamped spring, so it rises past 1 once and
    settles. Exactly 1.0 at progress >= 1, so a finished frame is static.
    """
    if progress >= 1.0:
        return 1.0
    t = max(0.0, progress) * _GLYPH_S
    wd = _POP_OMEGA * math.sqrt(1.0 - _POP_ZETA * _POP_ZETA)
    decay = math.exp(-_POP_ZETA * _POP_OMEGA * t)
    resp = 1.0 - decay * (math.cos(wd * t) + (_POP_ZETA * _POP_OMEGA / wd) * math.sin(wd * t))
    return _POP_FROM + (1.0 - _POP_FROM) * resp


def fit_pill_width(text_w: float, chip_w: float = 0.0) -> int:
    """Compact capsule width that shows ``text_w`` px of label (+ chip) unclipped."""
    w = _ORB_INSET + _ORB + _GAP_ORB_TEXT + text_w + _INSET_R + _FIT_SLACK
    if chip_w:
        w += _GAP_TEXT_CHIP + chip_w
    return max(_MIN_PILL_W, int(math.ceil(w)))


def fit_recording_width(text_w: float, chip_w: float = 0.0) -> int:
    """Recording capsule width: disc, waveform, label and chip. No reserved margin."""
    return fit_pill_width(_RECORDING_WAVE_W + _GAP_WAVE_TEXT + text_w, chip_w)


def _wave_profile(n: int) -> list[float]:
    """Centre weighting: the middle bar moves fully, the end bars about a third."""
    c = (n - 1) / 2.0
    return [0.35 + 0.65 * math.cos(math.pi / 2 * (i - c) / max(1.0, c)) ** 2 for i in range(n)]


_WAVE_PROFILE = _wave_profile(_RECORDING_BARS)


class _Spring:
    """One value that follows its target as a damped spring, in closed form.

    Exact for any time step (a 16 ms tick, timer jitter, or one 250 ms jump all
    land on the same value), keeps velocity when retargeted mid-flight, and
    snaps to the target once within a fraction of a pixel, so ``done`` becomes
    exactly true and a finished frame is static. The API matches the old tween:
    ``value(now)``, ``done(now)``, ``to(target, now)``, ``snap(value)``, ``b``, ``t0``.
    """

    __slots__ = ("x", "v", "b", "t", "t0", "w", "z")

    _EPS = 0.05        # px
    _EPS_V = 2.0       # px per second

    def __init__(self, value: float, omega: float = _SIZE_OMEGA, zeta: float = 1.0) -> None:
        self.x = self.b = float(value)
        self.v = 0.0
        self.t: Optional[float] = None
        self.t0 = 0.0
        self.w = omega
        self.z = zeta

    def _advance(self, now: float) -> None:
        if self.t is None:
            self.t = now
        dt = now - self.t
        if dt <= 0.0:
            return  # never rewinds; repeated calls at one instant agree
        self.t = now
        d = self.x - self.b
        if d == 0.0 and self.v == 0.0:
            return
        w, z, v = self.w, self.z, self.v
        if z >= 1.0 - 1e-6:
            e = math.exp(-w * dt)
            c = v + w * d
            self.x = self.b + e * (d + c * dt)
            self.v = e * (v - w * c * dt)
        else:
            wd = w * math.sqrt(1.0 - z * z)
            e = math.exp(-z * w * dt)
            cs, sn = math.cos(wd * dt), math.sin(wd * dt)
            c2 = (v + z * w * d) / wd
            self.x = self.b + e * (d * cs + c2 * sn)
            self.v = e * ((-z * w * d + wd * c2) * cs + (-z * w * c2 - wd * d) * sn)
        if abs(self.x - self.b) < self._EPS and abs(self.v) < self._EPS_V:
            self.x, self.v = self.b, 0.0

    def value(self, now: float) -> float:
        self._advance(now)
        return self.x

    def done(self, now: float) -> bool:
        self._advance(now)
        return self.x == self.b and self.v == 0.0

    def to(self, target: float, now: float) -> None:
        self._advance(now)
        self.b = float(target)
        self.t0 = now

    def snap(self, value: float) -> None:
        self.x = self.b = float(value)
        self.v = 0.0


# Text was inserted, with a caveat the user should see.
SUCCESS_NOTICE_LABELS = {
    "ai_fallback": "AI failed · raw text",
    "polish_fallback": "Polish skipped · raw text",
    "ai_timeout": "AI timed out · raw text",
    "stt_fallback": "Cloud slow · local speech",
    "mic_gap": "Mic gap · check text",
    "clipboard_not_restored": "Clipboard not restored",
}
SUCCESS_STATUSES = ("success",) + tuple(SUCCESS_NOTICE_LABELS)
# Nothing was inserted. "init_error" stays up until init succeeds.
ERROR_STATUS_LABELS = {
    "empty": "No speech",
    "cancelled": "Cancelled",
    "stt_timeout": "Speech timed out",
}
ERROR_STATUSES = ("error", "init_error", "preflight") + tuple(ERROR_STATUS_LABELS)
# Short reasons for "init_error" / "preflight" (app.status_detail holds the code).
INIT_REASON_LABELS = {
    "mic": "Mic unavailable",
    "speech": "Speech engine failed",
    "ai": "AI setup failed",
    "hotkeys": "Hotkeys failed",
    "linux_not_root": "Needs root · see log",
    "linux_missing_clipboard_tool": "Clipboard tool missing",
    "linux_missing_portaudio": "PortAudio missing",
    "macos_accessibility": "Allow Accessibility",
    "macos_input_monitoring": "Allow Input Monitoring",
}
_INIT_REASON_DEFAULT = "Not ready · see log"


def status_label(state: GuiState, use_llm: bool = False, last_status: Optional[str] = None,
                 detail: Optional[str] = None) -> str:
    """Pure label map (unit-testable without Qt paint)."""
    if state == GuiState.BOOTING:
        return "Starting"
    if state == GuiState.RECORDING:
        return "Listening"
    if state == GuiState.PROCESSING:
        if last_status in ("polishing", "finalizing"):
            return last_status.capitalize()
        return "Thinking" if use_llm else "Transcribing"
    if state == GuiState.SUCCESS:
        return SUCCESS_NOTICE_LABELS.get(last_status, "Done")
    if state == GuiState.ERROR:
        if last_status in ("init_error", "preflight"):
            return INIT_REASON_LABELS.get(detail, _INIT_REASON_DEFAULT) if isinstance(
                detail, str) else _INIT_REASON_DEFAULT
        return ERROR_STATUS_LABELS.get(last_status, "Failed")
    if state == GuiState.RESET:
        return "Context cleared"
    return ""


def orb_kind(state: GuiState, last_status: Optional[str] = None) -> str:
    """Which status disc a state shows (pure; the phone uses the same five)."""
    if state == GuiState.RECORDING:
        return "listen"
    if state in (GuiState.BOOTING, GuiState.PROCESSING):
        return "wait"
    if state == GuiState.SUCCESS:
        return "notice" if last_status in SUCCESS_NOTICE_LABELS else "done"
    if state == GuiState.ERROR:
        return "error"
    if state == GuiState.RESET:
        return "reset"
    return "wait"


def _show_ai_chip(state: GuiState, use_llm: bool) -> bool:
    return use_llm and state in (GuiState.RECORDING, GuiState.PROCESSING)


class DictationIndicator(QWidget):
    """Bottom-center always-on-top glass HUD for the dictation service."""

    # Thread-safe wake-ups from keyboard / worker threads
    _image_request = Signal(object)
    _wake = Signal()
    _hide_req = Signal()
    _reset_flash = Signal()

    def __init__(self, app: Any) -> None:
        # Ensure a single QApplication exists before any QWidget.
        self._qt_app = QApplication.instance()
        if self._qt_app is None:
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
            self._qt_app = QApplication(sys.argv if hasattr(sys, "argv") else [])
            self._qt_app.setApplicationName("Odicto")
            self._qt_app.setQuitOnLastWindowClosed(False)

        super().__init__(None)

        self._image_request.connect(self._read_clipboard_image)
        self.app = app
        self.gui_state: GuiState = GuiState.BOOTING
        self.last_app_state: Any = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_indicator)

        # Animation clock (seconds, advanced by the real time between ticks)
        self._t: float = 0.0
        self._last_tick: Optional[float] = None
        self._bars: list[float] = [0.0] * _RECORDING_BARS
        self._level: float = 0.0   # smoothed voice envelope, drives the disc halo
        self._appear: float = 0.0  # 0..1 shape opacity, derived from the phase timeline
        self._appear_target: float = 1.0
        # Droplet timeline: hidden -> entering -> open -> leaving -> hidden.
        # The window never scales; the painted shape does (scaling blurs layered HWNDs).
        self._phase: str = "hidden"
        self._phase_t0: float = 0.0
        self._check_progress: float = 0.0
        self._content_fade: float = 1.0
        self._glyph_fade: float = 1.0  # 0..1 over _GLYPH_S: cross-fade, then the disc pop
        self._last_orb: str = "wait"
        self._shown_orb: str = "wait"
        self._shown_state: GuiState = GuiState.BOOTING  # painted while leaving
        self._label_cache: str = ""
        self._dpr: float = 1.0          # device pixel ratio, read once per paint
        # The shadow of a settled capsule, drawn once: (size key, pixmap).
        self._shadow_cache: Optional[tuple[tuple[float, ...], QPixmap]] = None
        self._success_since: float = 0.0
        self._resize_hold: Optional[tuple[float, float]] = None
        self._text_in_at: Optional[float] = None

        # --- Geometry ---
        self._pad = _PAD                # shadow bleed only (no glow halo)
        self._pill_h = _COMPACT_H
        self._radius = 22              # capsule ends; the live card keeps this corner
        self._paint_radius = 22.0

        # _pill_w/_pill_h are the layout target; the springs are what is painted.
        self._pill_w = 170
        self._compact_h = _COMPACT_H
        self._canvas_w = self._pill_w + self._pad * 2
        self._canvas_h = self._pill_h + self._pad * 2
        self._w_tw = _Spring(self._pill_w)
        self._h_tw = _Spring(self._pill_h)

        self._setup_window()
        self._setup_font()
        self._sync_geometry()
        self._setup_tray()

        self._wake.connect(self._sync_from_app, Qt.ConnectionType.QueuedConnection)
        self._hide_req.connect(self._do_hide, Qt.ConnectionType.QueuedConnection)
        self._reset_flash.connect(
            self._do_flash_reset, Qt.ConnectionType.QueuedConnection
        )

        self._tick = QTimer(self)
        self._tick.setInterval(_ANIM_TICK_MS)  # ~60 FPS
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self.last_app_state = getattr(app, "state", None)
        # Always paint during the entrance, then pause once hidden.
        self._begin_show(time.monotonic())

    # ------------------------------------------------------------------ window
    def _setup_tray(self) -> None:
        """Minimal tray/status menu for macOS/Linux background control.

        Kept deliberately light and optional — if the system has no tray
        (some Linux desktops), the HUD-only behavior is unchanged.
        """
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self._make_tray_icon()
        if icon.isNull():
            return
        self._tray = QSystemTrayIcon(icon, self)
        menu = QMenu()

        setup_action = QAction("Setup…", self)
        setup_action.triggered.connect(self._open_setup_page)
        menu.addAction(setup_action)

        menu.addSeparator()

        quit_action = QAction("Quit Odicto", self)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.setToolTip("Odicto")
        self._tray.show()

    def _make_tray_icon(self) -> QIcon:
        """The Odicto logo; a plain jade square only if no logo file loads."""
        icon = logo_icon()
        if not icon.isNull():
            return icon
        pm = QPixmap(24, 24)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_Theme.action)
        painter.drawRoundedRect(QRectF(4, 4, 16, 16), 5, 5)
        painter.end()
        fallback = QIcon()
        fallback.addPixmap(pm)
        return fallback

    def _open_setup_page(self) -> None:
        try:
            import subprocess
            import sys

            subprocess.Popen(
                [sys.executable, os.path.join(str(ROOT), "odicto.py"), "setup"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"Warning: could not open setup page: {e}", flush=True)

    def _quit_app(self) -> None:
        self._tray.hide() if self._tray is not None else None
        self._qt_app.quit()

    def _setup_window(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(self._canvas_w, self._canvas_h)
        self._reposition_bottom_center()

    def _setup_font(self) -> None:
        family = load_brand_font()
        if family is None:
            families = set(QFontDatabase.families())
            family = next((name for name in _FALLBACK_FAMILIES if name in families),
                          self.font().family())
        # Pixel sizes: the label is 14 px on every DPI, like the setup page's body text.
        self._font = QFont(family)
        self._font.setPixelSize(14)
        self._font.setWeight(QFont.Weight.Medium)
        # Full hinting rounds advances and makes paint and fit metrics disagree.
        self._font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)

        self._font_caption = QFont(self._font)
        self._font_caption.setWeight(QFont.Weight.Normal)

        self._font_chip = QFont(family)
        self._font_chip.setPixelSize(11)
        self._font_chip.setWeight(QFont.Weight.DemiBold)
        self._font_chip.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        self._font_chip.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self._font_chip.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)

        # Metrics are fixed per font: build them once, not on every frame.
        self._fm = QFontMetrics(self._font)
        self._fm_chip = QFontMetrics(self._font_chip)
        self._fm_caption = QFontMetrics(self._font_caption)
        self._fmf_chip = QFontMetricsF(self._font_chip)
        self._half_cap = {
            id(f): QFontMetricsF(f).capHeight() * 0.5
            for f in (self._font, self._font_chip, self._font_caption)
        }

    def _chip_width(self, label: str = "AI") -> float:
        return float(self._fm_chip.horizontalAdvance(label) + _CHIP_PAD_X * 2)

    def _target_width(self, label: str, chip_label: str = "", recording: bool = False) -> int:
        """Width that fits ``label``, an optional chip and, when recording, the waveform."""
        text_w = self._fm.horizontalAdvance(label)
        chip_w = self._chip_width(chip_label) if chip_label else 0.0
        return (fit_recording_width if recording else fit_pill_width)(text_w, chip_w)

    def _label_for(self, state: GuiState, use_llm: bool) -> str:
        """The label painted now. While leaving, the last shown label stays."""
        if self.gui_state == GuiState.HIDDEN:
            return self._label_cache
        label = status_label(state, use_llm, getattr(self.app, "last_status", None),
                             getattr(self.app, "status_detail", None))
        self._label_cache = label
        return label

    def _vis_state(self) -> GuiState:
        """The state to draw: the last shown state while the exit plays."""
        return self._shown_state if self.gui_state == GuiState.HIDDEN else self.gui_state

    def _orb_now(self) -> str:
        """The disc to draw; it stays the last shown disc while the exit plays.

        Any change of disc starts the cross-fade and pop here, whatever caused it: a
        state change, the reset flash, or a notice status set while still processing.
        From fully hidden, _begin_show cancels it: the droplet entrance is enough.
        """
        if self.gui_state != GuiState.HIDDEN:
            kind = orb_kind(self.gui_state, getattr(self.app, "last_status", None))
            if kind != self._shown_orb:
                self._last_orb = self._shown_orb
                self._shown_orb = kind
                self._glyph_fade = 0.0
                self._wake_tick()
        return self._shown_orb

    def _chip_label(self, state: GuiState, use_llm: bool) -> str:
        if state == GuiState.RECORDING and getattr(self.app, "live_active", False) is True:
            return "Live"
        return "AI" if _show_ai_chip(state, use_llm) else ""

    def _is_live_layout(self) -> bool:
        return (getattr(self.app, "live_active", False) is True or
                (self.gui_state == GuiState.PROCESSING and bool(getattr(self.app, "live_preview", ""))))

    def _layout_target(self) -> tuple[int, int]:
        """(width, height) that fits what the current state shows."""
        state = self.gui_state
        use_llm = bool(getattr(self.app, "use_llm", False))
        label = self._label_for(state, use_llm)
        chip = self._chip_label(state, use_llm)
        w = self._target_width(label, chip, recording=state == GuiState.RECORDING)
        h = self._compact_h
        if self._is_live_layout():
            w, h = max(w, _LIVE_MIN_W), _LIVE_H
        return w, h

    def _sync_geometry(self) -> None:
        """Retarget the capsule to the current words.

        From hidden, the droplet opens at the new size. While the capsule is
        already open, the painted size waits until the new words are fully in,
        then starts 0.3 ms later, so a shrink cannot clip the longer label.
        """
        if self.gui_state == GuiState.HIDDEN:
            return  # keep the shown size while the exit plays
        w, h = self._layout_target()
        if w == self._pill_w and h == self._pill_h:
            return
        self._pill_w, self._pill_h = w, h
        # Grow the canvas once, before a grow animation; shrink happens after.
        self._fit_canvas(w, h)
        if self._phase == "hidden":
            self._w_tw.snap(w)
            self._h_tw.snap(h)
            self._resize_hold = None
            self._text_in_at = None
        else:
            self._resize_hold = (float(w), float(h))
            self._text_in_at = None

    def _release_resize(self, now: float) -> None:
        """Start a held size change 0.3 ms after the new words are fully in."""
        if self._resize_hold is None:
            return
        if self._content_fade < 1.0:
            self._text_in_at = None
            return
        if self._text_in_at is None:
            self._text_in_at = now
            return
        if now < self._text_in_at + _RESIZE_AFTER_TEXT_S:
            return
        w, h = self._resize_hold
        self._w_tw.to(w, now)
        self._h_tw.to(h, now)
        self._resize_hold = None
        self._text_in_at = None

    def _fit_canvas(self, w: float, h: float, shrink: bool = False) -> None:
        """Resize the window to hold a ``w`` x ``h`` capsule (one resize, not per frame).

        Sizes round up to a multiple of 4 px, so the centre falls on a whole
        device pixel at 1x, 1.25x, 1.5x and 2x.
        """
        cw = int(math.ceil(w)) + self._pad * 2
        cw = -(-cw // 4) * 4
        ch = int(math.ceil(h)) + self._pad * 2
        ch = -(-ch // 4) * 4
        if not shrink:
            cw, ch = max(cw, self._canvas_w), max(ch, self._canvas_h)
        if (cw, ch) == (self._canvas_w, self._canvas_h):
            return
        self._canvas_w, self._canvas_h = cw, ch
        self.setFixedSize(cw, ch)
        self._reposition_bottom_center()

    def _frame(self, now: float) -> tuple[float, float, float, float, float]:
        """Painted capsule for this instant: (width, height, drop px, shape alpha, words alpha)."""
        W = self._w_tw.value(now)
        H = self._h_tw.value(now)
        d = float(self._compact_h)
        el = now - self._phase_t0
        if self._phase == "entering":
            rise = ease_standard(el / _RISE_S)
            spread_raw = max(0.0, min(1.0, (el - _SPREAD_AT_S) / _SPREAD_S))
            spread = ease_standard(spread_raw)
            size = d * (_DROPLET_MIN + (1.0 - _DROPLET_MIN) * rise)
            words = ease_standard((spread_raw - 0.35) / 0.65)
            return (size + (W - size) * spread, size + (H - size) * spread,
                    (1.0 - rise) * _DROP_PX, rise, words)
        if self._phase == "leaving":
            words = 1.0 - ease_accelerate(el / _WORDS_OUT_S)
            collapse = ease_accelerate((el - _COLLAPSE_AT_S) / _COLLAPSE_S)
            drop = ease_accelerate((el - _DROP_AT_S) / _DROP_S)
            shrink = 1.0 - (1.0 - _DROPLET_MIN) * drop
            return ((W + (d - W) * collapse) * shrink, (H + (d - H) * collapse) * shrink,
                    drop * _DROP_PX, 1.0 - drop, words)
        if self._phase == "open":
            return W, H, 0.0, 1.0, 1.0
        return W, H, 0.0, 0.0, 0.0

    def _begin_show(self, now: float) -> None:
        """Play the droplet entrance from hidden; reopen in place if the exit is early."""
        if self._phase == "leaving" and now - self._phase_t0 < _DROP_AT_S:
            # Not yet a droplet: spring back out from the painted size.
            w, h, _, _, _ = self._frame(now)
            self._w_tw.snap(w)
            self._h_tw.snap(h)
            self._w_tw.to(self._pill_w, now)
            self._h_tw.to(self._pill_h, now)
            self._phase, self._phase_t0 = "open", now
            self._content_fade = 0.0
        elif self._phase in ("hidden", "leaving"):
            self._w_tw.snap(self._pill_w)
            self._h_tw.snap(self._pill_h)
            self._fit_canvas(self._pill_w, self._pill_h, shrink=True)
            self._phase, self._phase_t0 = "entering", now
            self._content_fade = 1.0
            self._glyph_fade = 1.0  # nothing to cross-fade from
        self._appear_target = 1.0
        self._reposition_bottom_center()
        self.setWindowOpacity(1.0)
        self.show()
        self._apply_win32_exstyles()
        self.raise_()

    def _begin_leave(self, now: float) -> None:
        """Words fade, the capsule contracts to a droplet, the droplet drops and fades."""
        if self._phase in ("hidden", "leaving"):
            return
        if self._phase == "entering":
            w, h, _, _, _ = self._frame(now)
            self._w_tw.snap(w)
            self._h_tw.snap(h)
        else:
            self._w_tw.snap(self._w_tw.value(now))
            self._h_tw.snap(self._h_tw.value(now))
        self._phase, self._phase_t0 = "leaving", now

    def _advance_phase(self, now: float) -> None:
        el = now - self._phase_t0
        if self._phase == "entering":
            self._appear = ease_standard(el / _RISE_S)
            if el >= _ENTER_S:
                self._phase = "open"
        elif self._phase == "open":
            self._appear = 1.0
            # Shrink the canvas once, after a shrink animation has finished.
            if (self._resize_hold is None
                    and self._w_tw.done(now) and self._h_tw.done(now)):
                self._fit_canvas(self._pill_w, self._pill_h, shrink=True)
        elif self._phase == "leaving":
            self._appear = 1.0 - ease_accelerate((el - _DROP_AT_S) / _DROP_S)
            if el >= _LEAVE_S:
                self._phase = "hidden"
                self._appear = 0.0
                if self.isVisible() and self.gui_state == GuiState.HIDDEN:
                    self.hide()
                self._sync_tick()
        else:
            self._appear = 0.0

    def _reposition_bottom_center(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self._canvas_w) // 2
        y = geo.y() + geo.height() - self._canvas_h - 48
        self.move(x, y)

    def _apply_win32_exstyles(self) -> None:
        """Apply platform-specific window styles (topmost/non-activating)."""
        apply_window_exstyles(self)

    # -------------------------------------------------------------- public API
    def capture_clipboard_image(self):
        """Worker request; Qt clipboard access executes only on the GUI thread."""
        request = {"done": threading.Event(), "image": None}
        self._image_request.emit(request)
        return request["image"] if request["done"].wait(0.35) else None

    def _read_clipboard_image(self, request) -> None:
        from typer import get_clipboard_image
        try:
            request["image"] = get_clipboard_image()
        finally:
            request["done"].set()

    def notify_state_changed(self) -> None:
        """Thread-safe: request a UI refresh on the Qt main thread."""
        self._wake.emit()

    def hide_indicator(self) -> None:
        """Fade the HUD out (thread-safe; may be called from worker threads)."""
        self._hide_req.emit()

    def flash_reset_notice(self) -> None:
        """Thread-safe: briefly show a 'Context cleared' notice (hotkey reset)."""
        self._reset_flash.emit()

    def _do_flash_reset(self) -> None:
        if self.gui_state != GuiState.RESET:
            self._check_progress = 0.0
        self.gui_state = self._shown_state = GuiState.RESET
        self._orb_now()  # cross-fades from the disc on screen, not a stale one
        self._sync_geometry()
        self._content_fade = 0.8
        self._begin_show(time.monotonic())
        self._hide_timer.start(1200)
        self._sync_tick()
        print("[HUD] Context cleared", flush=True)

    def _do_hide(self) -> None:
        self._appear_target = 0.0
        self.gui_state = GuiState.HIDDEN
        self._begin_leave(time.monotonic())
        self._sync_tick()
        try:
            from app_state import AppState

            last_status = getattr(self.app, "last_status", None)
            if getattr(self.app, "state", None) == AppState.IDLE and last_status in (
                "success",) + ERROR_STATUSES and last_status != "init_error":
                self.app.last_status = None
        except Exception:
            pass

    def start(self) -> None:
        """Run the Qt event loop (blocks until quit)."""
        try:
            self._qt_app.exec()
        except KeyboardInterrupt:
            pass

    # ----------------------------------------------------------------- sync
    def _sync_from_app(self) -> None:
        from app_state import AppState

        self._wake_tick()  # the app changed something: animate at full speed again
        app_state = self.app.state
        last_status = getattr(self.app, "last_status", None)
        ready = bool(getattr(self.app, "ready", False))
        prev_app_state = self.last_app_state

        target: Optional[GuiState] = None

        if app_state == AppState.RECORDING:
            target = GuiState.RECORDING
        elif app_state == AppState.PROCESSING:
            # The text is in the target app; only the clipboard wait remains.
            # `is True`: a MagicMock app attribute is truthy but never True.
            delivered = getattr(self.app, "paste_delivered", False) is True
            target = GuiState.SUCCESS if delivered else GuiState.PROCESSING
        elif app_state == AppState.IDLE:
            came_from_processing = prev_app_state == AppState.PROCESSING
            if not ready and last_status is None:
                target = GuiState.BOOTING
            elif last_status in SUCCESS_STATUSES and came_from_processing:
                target = GuiState.SUCCESS
            elif last_status in ERROR_STATUSES:
                target = GuiState.ERROR
            else:
                target = GuiState.HIDDEN
        else:
            name = getattr(app_state, "name", None)
            if name == "RECORDING":
                target = GuiState.RECORDING
            elif name == "PROCESSING":
                target = GuiState.PROCESSING
            elif name == "IDLE":
                target = GuiState.HIDDEN if ready else GuiState.BOOTING

        if target is None:
            print(f"[HUD] sync skipped; unrecognized state={app_state!r}", flush=True)
            return

        if target != self.gui_state or app_state != prev_app_state:
            self._transition_to(target)
        else:
            self._sync_geometry()

        self.last_app_state = app_state
        self.update()

    def _transition_to(self, new_state: GuiState) -> None:
        self._hide_timer.stop()
        now = time.monotonic()

        if new_state == GuiState.HIDDEN:
            self._appear_target = 0.0
            self.gui_state = GuiState.HIDDEN
            self._begin_leave(now)
            self._sync_tick()
            return

        prev = self.gui_state
        # Done shown at delivery stays put when the cycle reaches IDLE: no
        # replayed check, only an eased width change if a notice replaces it.
        kept_success = new_state == prev == GuiState.SUCCESS
        shown_label = self._label_cache
        self.gui_state = self._shown_state = new_state
        self._sync_geometry()
        self._orb_now()
        if kept_success and self._label_cache != shown_label:
            self._content_fade = 0.8
        if not kept_success:
            self._t = 0.0
            if new_state in (GuiState.SUCCESS, GuiState.ERROR):
                self._check_progress = 0.0
            if new_state == GuiState.SUCCESS:
                self._success_since = now
            self._content_fade = 0.7 if new_state in (
                GuiState.RECORDING, GuiState.PROCESSING) else 0.8
        # The droplet plays only from hidden; an open capsule stays one capsule.
        self._begin_show(now)

        last_status = getattr(self.app, "last_status", None)
        app_state = getattr(self.app, "state", None)
        still_processing = getattr(app_state, "name", None) == "PROCESSING"
        if new_state == GuiState.ERROR and last_status == "init_error":
            pass  # not ready: stay visible until init succeeds (no auto-hide)
        elif new_state == GuiState.SUCCESS:
            # Done shows at delivery; hiding waits for IDLE (the clipboard wait
            # still runs), then keeps Done up ~1.2 s in total.
            if not still_processing:
                hold_ms = _SUCCESS_HOLD_MS - (now - self._success_since) * 1000.0
                if last_status != "success":
                    hold_ms = _SUCCESS_HOLD_MS  # a notice that just replaced Done
                self._hide_timer.start(max(0, int(hold_ms)))
        elif new_state == GuiState.ERROR:
            # A preflight problem gets time to be read; it explains silent hotkeys.
            self._hide_timer.start(6000 if last_status == "preflight" else 1500)
        elif new_state == GuiState.RESET:
            self._hide_timer.start(1200)

        self._sync_tick()
        print(f"[HUD] -> {new_state.name}", flush=True)

    def _sync_tick(self) -> None:
        """Pause the 60 FPS paint timer while fully hidden; resume otherwise.

        The timer also stays active during the exit (HIDDEN while leaving)
        so the last frames render, then it stops at the actual hide().
        """
        active = (
            self.gui_state != GuiState.HIDDEN
            or self._phase != "hidden"
            or self._appear_target > 0.0
        )
        if active and not self._tick.isActive():
            self._last_tick = None  # the paused time is not animation time
            self._tick.start()
        elif not active and self._tick.isActive():
            self._tick.stop()
        if active:
            self._wake_tick()

    def _wake_tick(self) -> None:
        if self._tick.interval() != _ANIM_TICK_MS:
            self._tick.setInterval(_ANIM_TICK_MS)
            self._last_tick = None  # the wait at the slow interval is not animation time

    def _is_static(self, now: float) -> bool:
        """True when every following frame would be pixel-identical to this one.

        Only a finished check, cross or notice qualifies: its glyph, words and
        size have stopped moving and nothing in it is driven by the clock.
        """
        return (
            self._phase == "open"
            and self.gui_state in (GuiState.SUCCESS, GuiState.ERROR, GuiState.RESET)
            and self._check_progress >= 1.0
            and self._glyph_fade >= 1.0
            and self._content_fade >= 1.0
            and self._resize_hold is None
            and self._w_tw.done(now)
            and self._h_tw.done(now)
        )

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        now = time.monotonic()
        # Real time since the last tick, capped so a stall never jumps an animation.
        dt = 0.016 if self._last_tick is None else min(max(now - self._last_tick, 0.0), _DT_MAX)
        self._last_tick = now
        self._t += dt

        try:
            app_state = getattr(self.app, "state", None)
            if app_state is not None and app_state != self.last_app_state:
                self._sync_from_app()
        except Exception:
            pass

        self._advance_phase(now)

        self._glyph_fade = min(1.0, self._glyph_fade + dt / _GLYPH_S)

        if self._content_fade < 1.0:
            self._content_fade = min(1.0, self._content_fade + dt / _WORDS_IN_S)
        self._release_resize(now)

        self._advance_wave(dt)

        if self.gui_state in (GuiState.SUCCESS, GuiState.ERROR, GuiState.RESET):
            self._check_progress = min(1.0, self._check_progress + dt / _CHECK_S)

        if self._phase != "hidden" or self._appear_target > 0:
            self.update()

        # A finished, unchanging pill (a standing "Mic unavailable") needs no
        # 60 FPS repaint. Any app change or state change wakes the tick again.
        interval = _STATIC_TICK_MS if self._is_static(now) else _ANIM_TICK_MS
        if self._tick.interval() != interval:
            self._tick.setInterval(interval)

    def _advance_wave(self, dt: float) -> None:
        """Smooth the voice envelope and the bars: fast attack, slow release.

        ``recorder.get_waveform`` gives bucket RMS of the newest 64 ms block. The
        buckets are shorter than a pitch period, so their mean is the honest
        loudness; each bucket only adds a little texture to its bar.
        """
        n = len(self._bars)
        raw: list[float] = []
        recorder = getattr(self.app, "recorder", None)
        if recorder is not None and self.gui_state == GuiState.RECORDING:
            try:
                raw = [max(0.0, min(1.0, float(x))) for x in recorder.get_waveform(n)]
            except Exception:
                raw = []
        mean = sum(raw) / len(raw) if raw else 0.0
        env = min(1.0, mean * 1.5) ** 0.7 if mean > 0.0 else 0.0

        def follow(cur: float, target: float, rise_s: float, fall_s: float) -> float:
            tau = rise_s if target > cur else fall_s
            return cur + (target - cur) * (1.0 - math.exp(-dt / tau))

        self._level = follow(self._level, env, _WAVE_ATTACK_S, _WAVE_RELEASE_S)
        for i in range(n):
            if raw:
                ratio = min(1.6, raw[i] / (mean + 1e-4)) if i < len(raw) else 1.0
                target = env * _WAVE_PROFILE[i % len(_WAVE_PROFILE)] * (0.7 + 0.3 * ratio / 1.6)
                self._bars[i] = follow(self._bars[i], target, _WAVE_ATTACK_S, _WAVE_RELEASE_S)
            else:
                self._bars[i] = follow(self._bars[i], 0.0, _WAVE_IDLE_S, _WAVE_IDLE_S)

    # ----------------------------------------------------------------- paint
    def _snap(self, v: float) -> float:
        """Round to a whole device pixel (crisp edges and baselines at any scale)."""
        return round(v * self._dpr) / self._dpr

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        now = time.monotonic()
        w, h, dy, alpha, words = self._frame(now)
        if alpha <= 0.001:
            return

        self._dpr = self.devicePixelRatioF() or 1.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setOpacity(max(0.0, min(1.0, alpha)))

        # The painted shape animates inside a fixed canvas: centered, bottom-aligned
        # (the window never scales, so nothing blurs).
        cx = self._canvas_w * 0.5
        bottom = float(self._canvas_h - self._pad)
        pill = QRectF(cx - w * 0.5, bottom - h + dy, w, h)
        self._paint_radius = min(float(self._radius), h * 0.5, w * 0.5)

        shape = QPainterPath()  # one capsule path per frame: fill, rim clip, content clip
        shape.addRoundedRect(pill, self._paint_radius, self._paint_radius)
        settled = (self._phase == "open" and dy == 0.0
                   and self._w_tw.done(now) and self._h_tw.done(now))
        self._paint_shadow(p, pill, settled)
        self._paint_surface(p, pill, shape)
        self._paint_border(p, pill)
        self._paint_top_rim(p, pill, shape)

        # Content rides the painted capsule: the disc sits in its left end and the
        # words follow it, revealed by the capsule clip. Nothing stretches.
        p.save()
        p.setClipPath(shape)
        self._paint_orb(p, pill, alpha)
        content_alpha = alpha * ease_standard(self._content_fade) * words
        if content_alpha > 0.001:
            p.setOpacity(max(0.0, min(1.0, content_alpha)))
            rise = (1.0 - min(words, ease_standard(self._content_fade))) * _WORDS_RISE_PX
            self._paint_content(p, pill, rise)
        p.restore()

        p.end()

    def _row(self, pill: QRectF) -> tuple[float, float]:
        """(row height, row centre y): the top 44 px row, or the droplet itself."""
        row_h = min(pill.height(), float(self._compact_h))
        return row_h, pill.top() + row_h * 0.5

    def _paint_shadow(self, p: QPainter, pill: QRectF, settled: bool) -> None:
        """Soft neutral drop: many faint layers stand in for a blur (few strong ones band).

        The widest layer stays inside ``_PAD``: 11 px to the sides, 13 px below.
        The layers are 40 % of a frame, so a settled capsule draws them once into a
        pixmap and then only blits it; a capsule that changes size draws them live.
        """
        if not settled:
            self._shadow_layers(p, pill)
            return
        m = float(self._pad)
        ox, oy = math.floor(pill.left()), math.floor(pill.top())
        local = QRectF(m + pill.left() - ox, m + pill.top() - oy, pill.width(), pill.height())
        key = (local.x(), local.y(), local.width(), local.height(), self._paint_radius, self._dpr)
        if self._shadow_cache is None or self._shadow_cache[0] != key:
            pm = QPixmap(int(math.ceil((local.right() + m) * self._dpr)),
                         int(math.ceil((local.bottom() + m) * self._dpr)))
            pm.setDevicePixelRatio(self._dpr)
            pm.fill(Qt.GlobalColor.transparent)
            sp = QPainter(pm)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._shadow_layers(sp, local)
            sp.end()
            self._shadow_cache = (key, pm)
        p.drawPixmap(QPointF(ox - m, oy - m), self._shadow_cache[1])

    def _shadow_layers(self, p: QPainter, pill: QRectF) -> None:
        for grow, dy, alpha in _SHADOW_LAYERS:
            r = QRectF(pill).adjusted(-grow, dy - grow * 0.5, grow, dy + grow * 0.5)
            path = QPainterPath()
            path.addRoundedRect(r, self._paint_radius + grow, self._paint_radius + grow)
            p.fillPath(path, QColor(0, 0, 0, alpha))

    def _paint_surface(self, p: QPainter, pill: QRectF, shape: QPainterPath) -> None:
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0.0, _Theme.surface_top)
        grad.setColorAt(1.0, _Theme.surface_bot)
        p.fillPath(shape, QBrush(grad))

    def _paint_border(self, p: QPainter, pill: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(
            pill.adjusted(0.5, 0.5, -0.5, -0.5), self._paint_radius, self._paint_radius
        )
        pen = QPen(_Theme.hairline)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _paint_top_rim(self, p: QPainter, pill: QRectF, shape: QPainterPath) -> None:
        """Faint light along the top edge, faded at both ends."""
        inset = self._paint_radius * 0.6
        rim_w = pill.width() - inset * 2
        if rim_w <= 0:
            return
        p.save()
        p.setClipPath(shape, Qt.ClipOperation.IntersectClip)
        rim = QRectF(pill.left() + inset, pill.top() + 1.0, rim_w, 1.0)
        g = QLinearGradient(rim.left(), rim.top(), rim.right(), rim.top())
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.3, _Theme.top_light)
        g.setColorAt(0.7, _Theme.top_light)
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(rim, QBrush(g))
        p.restore()

    # ----------------------------------------------------------------- disc
    def _paint_orb(self, p: QPainter, pill: QRectF, alpha: float) -> None:
        """The status disc in the capsule's left end; cross-fades and pops on change."""
        row_h, cy = self._row(pill)
        cx = pill.left() + row_h * 0.5
        base_r = _ORB * 0.5 * (row_h / float(self._compact_h))
        kind = self._orb_now()
        fade = min(1.0, self._glyph_fade * 2.0)  # the cross-fade takes the first half
        if fade < 1.0 and self._last_orb != kind:
            p.save()
            p.setOpacity(alpha * (1.0 - fade))
            self._draw_orb(p, self._last_orb, cx, cy, base_r, glyph=1.0)
            p.restore()
        p.save()
        p.setOpacity(alpha * fade)
        self._draw_orb(p, kind, cx, cy, base_r * pop_scale(self._glyph_fade),
                       glyph=ease_standard(self._check_progress))
        p.restore()

    def _draw_orb(self, p: QPainter, kind: str, cx: float, cy: float, r: float,
                  glyph: float) -> None:
        if r <= 0.5:
            return
        p.setPen(Qt.PenStyle.NoPen)
        if kind == "listen":
            # The phone's listening disc: a jade halo that grows with the voice.
            lvl = self._level
            halo_r = r + 2.0 + 4.0 * lvl
            halo = QRadialGradient(QPointF(cx, cy), halo_r + 3.0)
            c0 = QColor(_Theme.accent)
            c0.setAlpha(int(60 + 120 * lvl))
            c1 = QColor(_Theme.accent)
            c1.setAlpha(0)
            halo.setColorAt(r * 0.6 / (halo_r + 3.0), c0)
            halo.setColorAt(1.0, c1)
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(cx, cy), halo_r + 3.0, halo_r + 3.0)
            disc_r = r - (1.0 - lvl) * 1.5 * (r / (_ORB * 0.5))
            fill = QLinearGradient(QPointF(cx, cy - disc_r), QPointF(cx, cy + disc_r))
            fill.setColorAt(0.0, _Theme.action_hi)
            fill.setColorAt(1.0, _Theme.action)
            p.setBrush(QBrush(fill))
            p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)
            ring = QPen(QColor(_Theme.accent.red(), _Theme.accent.green(), _Theme.accent.blue(), 150))
            ring.setWidthF(1.5)
            p.setPen(ring)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), disc_r - 0.75, disc_r - 0.75)
            self._draw_mic(p, cx, cy, disc_r / (_ORB * 0.5), _Theme.on_action)
            return
        fills = {"done": _Theme.accent, "notice": _Theme.warn, "error": _Theme.danger}
        p.setBrush(fills.get(kind, _Theme.raised))
        p.drawEllipse(QPointF(cx, cy), r, r)
        scale = r / (_ORB * 0.5)
        if kind in ("wait", "reset"):
            edge = QPen(_Theme.hairline)
            edge.setWidthF(1.0)
            p.setPen(edge)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r - 0.5, r - 0.5)
        if kind == "wait":
            self._draw_spinner(p, cx, cy, scale)
        elif kind in ("done", "notice"):
            self._draw_check(p, cx, cy, scale, _Theme.ink, glyph)
        elif kind == "reset":
            self._draw_check(p, cx, cy, scale, _Theme.accent, glyph)
        elif kind == "error":
            self._draw_error(p, cx, cy, scale, _Theme.ink, glyph)

    def _draw_mic(self, p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
        """A small microphone: capsule head, cradle arc, stem."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        head = QRectF(cx - 2.6 * s, cy - 6.2 * s, 5.2 * s, 8.4 * s)
        p.drawRoundedRect(head, 2.6 * s, 2.6 * s)
        pen = QPen(color)
        pen.setWidthF(1.6 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = 5.0 * s
        arc = QRectF(cx - rr, cy - 1.6 * s - rr, rr * 2, rr * 2)
        p.drawArc(arc, int(-20 * 16), int(-140 * 16))
        p.drawLine(QPointF(cx, cy + 3.4 * s), QPointF(cx, cy + 6.2 * s))

    def _draw_spinner(self, p: QPainter, cx: float, cy: float, s: float) -> None:
        """The phone's "wait" disc: a 270° jade arc, one turn per second, on a faint track."""
        radius = 7.5 * s
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        track = QPen(_Theme.disc_track)
        track.setWidthF(2.0 * s)
        p.setPen(track)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)
        pen = QPen(_Theme.accent)
        pen.setWidthF(2.0 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        start = 90.0 - 360.0 * ((self._t / _SPIN_REV_S) % 1.0)
        p.drawArc(rect, int(start * 16), int(-270 * 16))

    def _draw_check(self, p: QPainter, cx: float, cy: float, s: float, color: QColor,
                    t: float) -> None:
        if t <= 0.0:
            return
        pen = QPen(color)
        pen.setWidthF(2.0 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p1 = QPointF(cx - 4.6 * s, cy + 0.2 * s)
        p2 = QPointF(cx - 1.2 * s, cy + 3.6 * s)
        p3 = QPointF(cx + 5.0 * s, cy - 3.6 * s)
        path = QPainterPath()
        path.moveTo(p1)
        if t < 0.4:
            u = t / 0.4
            path.lineTo(QPointF(p1.x() + (p2.x() - p1.x()) * u, p1.y() + (p2.y() - p1.y()) * u))
        else:
            path.lineTo(p2)
            u = (t - 0.4) / 0.6
            path.lineTo(QPointF(p2.x() + (p3.x() - p2.x()) * u, p2.y() + (p3.y() - p2.y()) * u))
        p.drawPath(path)

    def _draw_error(self, p: QPainter, cx: float, cy: float, s: float, color: QColor,
                    t: float) -> None:
        if t <= 0.0:
            return
        pen = QPen(color)
        pen.setWidthF(2.0 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        k = 3.8 * s
        a, b = min(1.0, t * 2.0), max(0.0, t * 2.0 - 1.0)
        p.drawLine(QPointF(cx - k, cy - k), QPointF(cx - k + 2 * k * a, cy - k + 2 * k * a))
        if b > 0.0:
            p.drawLine(QPointF(cx + k, cy - k), QPointF(cx + k - 2 * k * b, cy - k + 2 * k * b))

    # ----------------------------------------------------------------- words
    def _paint_content(self, p: QPainter, pill: QRectF, rise: float) -> None:
        """[ disc | (waveform)? | label | (chip)? ], then the live caption line."""
        state = self._vis_state()
        use_llm = bool(getattr(self.app, "use_llm", False))
        _, cy = self._row(pill)
        cy += rise
        x = pill.left() + _ORB_INSET + _ORB + _GAP_ORB_TEXT
        if state == GuiState.RECORDING:
            self._draw_wave(p, x, cy)
            x += _RECORDING_WAVE_W + _GAP_WAVE_TEXT

        label = self._label_for(state, use_llm)
        p.setFont(self._font)
        p.setPen(_Theme.text)
        p.drawText(QPointF(self._snap(x), self._baseline(self._font, cy)), label)

        chip = self._chip_label(state, use_llm)
        if chip:
            chip_w = self._chip_width(chip)
            self._draw_named_chip(p, pill.right() - _INSET_R - chip_w, cy, chip_w, chip)

        if self._is_live_layout() and pill.height() > self._compact_h + 1:
            p.setFont(self._font_caption)
            said = " ".join((getattr(self.app, "live_preview", "") or "").split())[-250:]
            text = said or "Speak, then tap F7 to insert"
            room = int(pill.width() - 32)
            text = self._fm_caption.elidedText(text, Qt.TextElideMode.ElideLeft, room)
            p.setPen(_Theme.text if said else _Theme.muted)
            caption_cy = pill.top() + self._compact_h + (pill.height() - self._compact_h) * 0.5 - 4 + rise
            p.drawText(QPointF(self._snap(pill.left() + 16), self._baseline(self._font_caption, caption_cy)),
                       text)

    def _baseline(self, font: QFont, cy: float) -> float:
        """Baseline that centres capital letters on ``cy`` (the font box is not symmetric)."""
        return self._snap(cy + self._half_cap[id(font)])

    def _draw_named_chip(self, p: QPainter, x: float, cy: float, w: float, label: str) -> None:
        """Jade tint chip, the setup page's selected-option look."""
        rect = QRectF(x, cy - _CHIP_H * 0.5, w, _CHIP_H)
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), _CHIP_H * 0.5, _CHIP_H * 0.5)
        p.fillPath(path, _Theme.chip_fill)
        pen = QPen(_Theme.chip_border)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setFont(self._font_chip)
        p.setPen(_Theme.accent)
        advance = self._fmf_chip.horizontalAdvance(label)
        p.drawText(QPointF(self._snap(rect.center().x() - advance * 0.5),
                           self._baseline(self._font_chip, cy)), label)

    def _draw_wave(self, p: QPainter, x: float, cy: float) -> None:
        """Jade bars mirrored on the centre line; a silent bar is a dot, never gone."""
        p.setPen(Qt.PenStyle.NoPen)
        breathe_on = self._level < 0.05
        for i, v in enumerate(self._bars):
            if breathe_on:
                # Quiet breathing (0.4 Hz) says "still listening" during a pause.
                v = max(v, 0.06 * _WAVE_PROFILE[i] * (0.5 + 0.5 * math.sin(
                    2 * math.pi * 0.4 * self._t - i * 0.45)))
            h = _WAVE_MIN_H + (_WAVE_MAX_H - _WAVE_MIN_H) * min(1.0, v)
            h = max(_WAVE_MIN_H, self._snap(h))
            bx = self._snap(x + i * (_RECORDING_BAR_W + _RECORDING_BAR_GAP))
            c = QColor(_Theme.accent)
            c.setAlphaF(0.5 + 0.5 * min(1.0, v * 1.5))
            p.setBrush(c)
            p.drawRoundedRect(QRectF(bx, cy - h * 0.5, _RECORDING_BAR_W, h),
                              _RECORDING_BAR_W * 0.5, _RECORDING_BAR_W * 0.5)
