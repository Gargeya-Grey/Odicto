"""
Odicto floating status HUD: the "leaf".

The look is one pure painter, hud_leaf.LeafPainter. This module owns what the painter
does not: the state (GuiState and the stem it maps to), the clock, the label width spring,
the voice envelope, the F7 caption text and the window.

  - Each tick the widget builds one hud_leaf.LeafFrame and the painter draws all of it.
    With F7 live captions a card sits above the leaf; the window grows upward, so the
    leaf's bottom edge never moves.
  - Instrument Sans (shipped in assets/fonts), Segoe UI Variable as fallback
  - Motion: the label width follows a critically damped spring (no bounce); the entry and
    the exit are the painter's timelines; every rate is per second, so the look is the
    same at any frame interval
  - Click-through, no focus steal, always on top, bottom-center
"""

from __future__ import annotations

import dataclasses
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
    QPaintEvent,
    QFontMetrics,
    QFontMetricsF,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

import hud_leaf as hl
from config import Config
from hud_leaf import STEM_FOR_ORB, HudStyle, LeafFrame, LeafPainter, VoiceEnvelope
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

    The setup page carries the same values; TestBrandLook keeps the two equal, and
    tests/test_hud_leaf.py keeps the painter's greens (hud_leaf.JADE, MOON, WARN, DANGER, INK)
    equal to them. The widget itself uses ``text``, ``muted`` and ``action`` (the caption card
    and the tray fallback icon); the rest are the shared tokens, kept so the three stay equal.
    """

    text = QColor(0xE7, 0xE5, 0xDF)
    muted = QColor(0xB2, 0xB5, 0xB5)
    accent = QColor(0x8E, 0xDC, 0xBB)           # pale jade
    action = QColor(0x2A, 0x74, 0x5D)           # jade action fill
    action_hi = QColor(0x2C, 0x78, 0x60)
    on_action = QColor(0xF4, 0xF6, 0xF1)
    raised = QColor(0x30, 0x34, 0x38)
    ink = QColor(0x17, 0x19, 0x1C)              # dark glyph on a light surface
    warn = QColor(0xF2, 0xC2, 0x7A)
    danger = QColor(0xF2, 0xA3, 0x9B)


# Window. The leaf is drawn inside a fixed canvas. Outside the leaf (and the card) the
# painter draws its shadow up to 28 px away, and the entry starts the leaf 12 px low.
_BLEED = 32                 # canvas margin on every side (a multiple of 4)
_BOTTOM_GAP = 64            # work area bottom to the leaf's bottom edge, as the pill had it
_COMPACT_H = hl.LEAF_H
_LIVE_H = hl.LEAF_H + hl.CARD_GAP + hl.CARD_H   # the leaf and the caption card above it

# Live captions (F7): two wrapped lines on the card, newest at the bottom. The card is
# drawn by the painter; the text is laid out here.
_CAPTION_PX = 14
_CAPTION_LINE = 20         # line pitch
_CAPTION_LINES = 2
_CAPTION_PAD = 18          # left and right
_CAPTION_ALPHA = 0.92      # the ink of the newest line
_CAPTION_OLD_ALPHA = 0.55  # the older line recedes
_CAPTION_WORDS_PER_S = 9.0 # at least this fast, so a chunk of words never lags behind speech
_CAPTION_CATCHUP_S = 0.18  # a larger backlog is caught up faster: ~63 % of it in this time
_CAPTION_SCROLL_TAU = 0.07 # a new line slides up into place

# Motion, in seconds. The entry and the exit are hud_leaf.ENTER_S and LEAVE_S.
_SIZE_OMEGA = 20.5         # size spring, critically damped: within 1 % in ~0.33 s
_STEM_SETTLE_S = 1.4       # the stem's timeline: morph 0.28 s, glyph to 0.38 s, seed to 1.32 s
_DROOP_S = 0.5             # the tip droop eases over this long after a stem change
_SWEEP_END_S = hl.ENTER_S * 1.1   # the painter's vein sweep lasts until here
_VOICE_REST = 1e-3         # below this a fading voice is cleared (alpha under 1/255 per pixel)
_DT_MAX = 0.05             # a stalled or slowed tick never jumps an animation
_SUCCESS_HOLD_MS = 1200
# Paint interval while the leaf animates, and while it only holds a finished,
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


def wrap_words(widths: list[float], space: float, room: float) -> list[tuple[int, int]]:
    """Greedy word wrap: one ``(first, end)`` word range per line.

    A word wider than ``room`` takes a line of its own (the painter elides it).
    """
    lines: list[tuple[int, int]] = []
    start, used = 0, 0.0
    for i, width in enumerate(widths):
        if i == start:
            used = width
        elif used + space + width > room:
            lines.append((start, i))
            start, used = i, width
        else:
            used += space + width
    if start < len(widths):
        lines.append((start, len(widths)))
    return lines


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
    "polish_ready": "Polish ready - review result",
    "copy_ready": "Text ready - copy to insert",
    "ai_fallback": "AI failed · raw text",
    "ai_timeout": "AI timed out · raw text",
    "stt_fallback": "Cloud slow · local speech",
    "mic_gap": "Mic gap · check text",
    "clipboard_not_restored": "Clipboard not restored",
}
SUCCESS_STATUSES = ("success",) + tuple(SUCCESS_NOTICE_LABELS)
# Nothing was inserted. "init_error" stays up until init succeeds.
ERROR_STATUS_LABELS = {
    "polish_fallback": "Polish unavailable - text kept",
    "polish_no_selection": "Select text to polish",
    "polish_target": "Selection changed - text kept",
    "polish_unchanged": "Already polished - text kept",
    "empty": "No speech",
    "cancelled": "Cancelled",
    "stt_timeout": "Speech timed out",
}
ERROR_STATUSES = ("error", "init_error", "preflight") + tuple(ERROR_STATUS_LABELS)
# Shown while recording, when the F7 live captions cannot run. The leaf says only that the
# captions are off; the caption card says why and that the text still arrives.
LIVE_FAILURE_LABELS = {
    "live_key_rejected": "Captions off",
    "live_unavailable": "Captions off",
}
# What serves the text when the captions are off (Config.live_key_rejected_stt_provider).
_SPEECH_SERVICE_NAMES = {"groq": "Groq", "openrouter": "OpenRouter", "whisper": "local Whisper"}


def live_failure_message(status: Optional[str], provider: str) -> str:
    """The caption card's text for a failed Live call; empty for any other status."""
    if status == "live_key_rejected":
        service = _SPEECH_SERVICE_NAMES.get(provider, _SPEECH_SERVICE_NAMES["whisper"])
        return f"Gemini key rejected. Your text still goes through {service} when you tap F7."
    if status == "live_unavailable":
        return "Live captions failed. Your text still arrives when you tap F7."
    return ""
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
        # Live captions failed. Recording goes on and the text is made at the end.
        return LIVE_FAILURE_LABELS.get(last_status, "Listening")
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
    """Which of the six kinds a state shows; hud_leaf.STEM_FOR_ORB maps each to a stem."""
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
    """The AI mark (the pixel "AI" and the moon accent): recording and processing with AI."""
    return use_llm and state in (GuiState.RECORDING, GuiState.PROCESSING)


def _opening_max(a: hl.Opening, b: hl.Opening) -> hl.Opening:
    """Field by field, the larger of two openings (the entry that resumes a closing leaf)."""
    return hl.Opening(**{f.name: max(getattr(a, f.name), getattr(b, f.name))
                         for f in dataclasses.fields(hl.Opening)})


class DictationIndicator(QWidget):
    """Bottom-center always-on-top HUD for the dictation service."""

    # Thread-safe wake-ups from keyboard / worker threads
    _polish_result = Signal(str, str)
    _copy_result = Signal(str, str)
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

        self._polish_result.connect(self._show_polish_result)
        self._copy_result.connect(self._show_copy_result)
        self._polish_dialog = None
        self._result_dialogs = set()
        self._image_request.connect(self._read_clipboard_image)
        self.app = app
        self.gui_state: GuiState = GuiState.BOOTING
        self.last_app_state: Any = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_indicator)
        self._last_tick: Optional[float] = None

        # The four style choices, read once. Config checks them; a bad one still must not
        # stop the HUD.
        try:
            self._style = HudStyle(waveform=Config.HUD_WAVEFORM, texture=Config.HUD_TEXTURE,
                                   edge=Config.HUD_EDGE, shape=Config.HUD_SHAPE)
        except ValueError as e:
            print(f"[HUD] HUD style not used, default leaf drawn: {e}", flush=True)
            self._style = HudStyle()

        # Live caption: the words arrive in chunks; they are revealed smoothly.
        self._cap_words: list[str] = []
        self._cap_shown = 0.0                  # words revealed so far (the fraction fades one in)
        self._cap_lines: list[str] = []        # laid-out lines of the revealed words, oldest first
        self._cap_fading = ""                  # the word that is fading in at the end of the last line
        self._cap_fade = 0.0
        self._cap_last_first: Optional[int] = None  # index of the newest line's first word
        self._cap_prev_len: Optional[int] = None  # words on screen in the last frame
        self._cap_visible_from = 0             # index of the first word on the older of the two read lines
        self._cap_scroll = 0.0                 # px the lines are still below their place
        self._cap_widths: dict[str, int] = {}
        self._fail_text: tuple[Optional[str], str] = (None, "")   # (status, card message)
        self._voice = VoiceEnvelope()          # smoothed band levels; the painter reads it
        self._appear_target: float = 1.0
        # Timeline: hidden -> entering -> open -> leaving -> hidden. The window never
        # scales; the painter draws the leaf's opening (enter_opening / leave_opening).
        # _phase_t0 is when the entry began (entering, open) or the exit began (leaving).
        self._phase: str = "hidden"
        self._phase_t0: float = 0.0
        self._enter_at: float = 0.0            # when the last entry began (read during the exit)
        # Entry-clock time already gone when this entry began: 0 from hidden; when a show
        # resumes a closing leaf, the time since its first entry, so the vein sweep never
        # plays twice. The painter's since_enter is now - (_phase_t0 - _entry_offset).
        self._entry_offset: float = 0.0
        self._enter_base: Optional[hl.Opening] = None  # set when a show resumes a closing leaf
        self._leave_base: hl.Opening = hl.OPEN  # the opening the exit started from
        # The stem (hud_leaf.Stem) the state maps to, and its timeline: 0..1 over
        # _STEM_SETTLE_S, restarted whenever the stem changes. It is also the clock of the tip
        # droop. (The name is from the old status disc; tests/test_resource_use.py sets it.)
        self._stem: hl.Stem = hl.Stem.WORK
        self._glyph_fade: float = 1.0
        self._droop_from: float = hl.DROOP
        self._shown_state: GuiState = GuiState.BOOTING  # painted while leaving
        self._label_cache: str = ""
        self._dpr: float = 1.0          # device pixel ratio, read once per paint
        self._success_since: float = 0.0

        # --- Geometry ---
        # _label_target / _pill_h are the layout target; the springs are what is painted.
        self._label_target: float = 90.0     # label width (hud_leaf.label_width)
        self._compact_h = _COMPACT_H
        self._live_h = _LIVE_H
        self._pill_h = self._compact_h       # leaf, or leaf and caption card
        self._w_tw = _Spring(self._label_target)
        self._h_tw = _Spring(self._pill_h)   # its share between compact and live is the card
        self._canvas_w, self._canvas_h = self._canvas_size(self._label_target, self._pill_h)

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
        self._stem_now()

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
        # Pixel sizes: the label is 13 px on every DPI, like the setup page's body text.
        self._font = hl.label_font(family)
        self._painter = LeafPainter(self._font)

        self._font_caption = QFont(family)
        self._font_caption.setPixelSize(_CAPTION_PX)
        self._font_caption.setWeight(QFont.Weight.Normal)
        # Full hinting rounds advances and makes paint and fit metrics disagree.
        self._font_caption.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        self._font_caption.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)

        # Metrics are fixed per font: build them once, not on every frame.
        self._fm = QFontMetricsF(self._font)
        self._fm_caption = QFontMetrics(self._font_caption)
        self._half_cap = QFontMetricsF(self._font_caption).capHeight() * 0.5

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

    def _stem_now(self) -> hl.Stem:
        """The stem to draw; it stays the last shown stem while the exit plays.

        Any change of stem restarts its timeline, whatever caused it: a state change, the
        reset flash, or a notice status set while still processing. The tip droop carries on
        from where it is. From fully hidden, _begin_show restarts it anyway.
        """
        if self.gui_state != GuiState.HIDDEN:
            stem = STEM_FOR_ORB[orb_kind(self.gui_state, getattr(self.app, "last_status", None))]
            if stem is not self._stem:
                self._droop_from = self._droop_now()
                self._stem = stem
                self._glyph_fade = 0.0
                self._wake_tick()
        return self._stem

    def _droop_goal(self) -> float:
        return hl.DROOP_ERROR if self._stem is hl.Stem.ERROR else hl.DROOP

    def _droop_now(self) -> float:
        """The tip droop: it eases from where it was to the stem's value over _DROOP_S.

        A function of the stem's timeline, so it is the same at any frame rate and equals
        the end value exactly once the timeline is past _DROOP_S.
        """
        goal = self._droop_goal()
        k = min(1.0, self._glyph_fade) * _STEM_SETTLE_S / _DROOP_S
        if k >= 1.0:
            return goal
        return self._droop_from + (goal - self._droop_from) * ease_standard(k)

    def _is_live_layout(self) -> bool:
        return (getattr(self.app, "live_active", False) is True or
                (self.gui_state == GuiState.PROCESSING and bool(getattr(self.app, "live_preview", ""))))

    def _layout_target(self) -> tuple[float, float]:
        """(label width, layout height) that fits what the current state shows."""
        state = self.gui_state
        use_llm = bool(getattr(self.app, "use_llm", False))
        label = self._label_for(state, use_llm)
        stem = STEM_FOR_ORB[orb_kind(state, getattr(self.app, "last_status", None))]
        w = hl.label_width(self._fm, label, _show_ai_chip(state, use_llm), stem)
        h = self._live_h if self._is_live_layout() else self._compact_h
        return w, float(h)

    @staticmethod
    def _canvas_size(label_w: float, h: float) -> tuple[int, int]:
        """Window size that holds the leaf (and the card) with the painter's shadow around it.

        Sizes round up to a multiple of 4 px, so the centre falls on a whole device pixel
        at 1x, 1.5x and 2x. At 1.25x it can fall on a half pixel; the painter snaps the
        stem and the label to whole device pixels, so nothing blurs.
        """
        inner = hl.leaf_width(label_w)
        if h > _COMPACT_H:
            inner = max(inner, float(hl.CARD_MIN_W))
        cw = -(-(int(math.ceil(inner)) + _BLEED * 2) // 4) * 4
        ch = -(-(int(math.ceil(h)) + _BLEED * 2) // 4) * 4
        return cw, ch

    def _sync_geometry(self) -> None:
        """Retarget the label width and the layout height to the current state.

        From hidden, the leaf opens at the new size. While it is open, the springs ease
        the change; the window is resized once, up front, when it must grow, and once
        more after the springs have settled when it can shrink.
        """
        if self.gui_state == GuiState.HIDDEN:
            return  # keep the shown size while the exit plays
        w, h = self._layout_target()
        if w == self._label_target and h == self._pill_h:
            return
        now = time.monotonic()
        hidden = self._phase == "hidden"
        if w != self._label_target:
            self._label_target = w
            if hidden:
                self._w_tw.snap(w)
            else:
                self._w_tw.to(w, now)
        if h != self._pill_h:
            self._pill_h = h
            if hidden:
                self._h_tw.snap(h)
            else:
                self._h_tw.to(h, now)
        self._fit_canvas()

    def _fit_canvas(self, shrink: bool = False) -> None:
        """Resize the window to hold the target layout (one resize, not per frame)."""
        cw, ch = self._canvas_size(self._label_target, self._pill_h)
        if not shrink:
            cw, ch = max(cw, self._canvas_w), max(ch, self._canvas_h)
        if (cw, ch) == (self._canvas_w, self._canvas_h):
            return
        self._canvas_w, self._canvas_h = cw, ch
        self.setFixedSize(cw, ch)
        self._reposition_bottom_center()

    def _opening(self, now: float) -> hl.Opening:
        """How far the entry or the exit has come at ``now`` (the painter's timelines)."""
        if self._phase in ("entering", "open"):
            elapsed = now - self._phase_t0
            opening = hl.enter_opening(elapsed)
            base = self._enter_base
            if base is not None:   # a show that resumed a closing leaf never goes back down
                opening = dataclasses.replace(
                    _opening_max(base, opening), fade=min(1.0, base.fade + max(0.0, elapsed) / 0.1))
            return opening
        if self._phase == "leaving":
            return hl.leave_opening(now - self._phase_t0, self._leave_base)
        return hl.leave_opening(hl.LEAVE_S)

    def _begin_show(self, now: float) -> None:
        """Play the entry from hidden; from an exit in progress, open again from where it is."""
        if self._phase in ("hidden", "leaving"):
            if self._phase == "leaving":
                self._enter_base = self._opening(now)
                self._entry_offset = now - self._enter_at
            else:
                self._enter_base = None
                self._entry_offset = 0.0
                self._glyph_fade = 0.0
                self._droop_from = self._droop_goal()
                self._w_tw.snap(self._label_target)
                self._h_tw.snap(self._pill_h)
                self._fit_canvas(shrink=True)
            self._phase, self._phase_t0 = "entering", now
        self._appear_target = 1.0
        self._reposition_bottom_center()
        self.setWindowOpacity(1.0)
        self.show()
        self._apply_win32_exstyles()
        self.raise_()

    def _begin_leave(self, now: float) -> None:
        """Play the exit from the opening on screen, so an exit in the entry never grows."""
        if self._phase in ("hidden", "leaving"):
            return
        self._leave_base = self._opening(now)
        self._enter_at = self._phase_t0 - self._entry_offset
        self._enter_base = None
        self._phase, self._phase_t0 = "leaving", now

    def _advance_phase(self, now: float) -> None:
        el = now - self._phase_t0
        if self._phase == "entering":
            if el >= hl.ENTER_S:
                self._phase = "open"
                self._enter_base = None
        elif self._phase == "open":
            # Shrink the window once, after a shrink animation has finished.
            if self._w_tw.done(now) and self._h_tw.done(now):
                self._fit_canvas(shrink=True)
        elif self._phase == "leaving":
            if el >= hl.LEAVE_S:
                self._phase = "hidden"
                if self.isVisible() and self.gui_state == GuiState.HIDDEN:
                    self.hide()
                self._sync_tick()

    def _reposition_bottom_center(self) -> None:
        """Bottom-centre of the work area. The leaf's bottom edge keeps its screen position
        whatever the window height is, so the window grows upward for the caption card."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self._canvas_w) // 2
        y = geo.y() + geo.height() - _BOTTOM_GAP - (self._canvas_h - _BLEED)
        self.move(x, y)

    def _apply_win32_exstyles(self) -> None:
        """Apply platform-specific window styles (topmost/non-activating)."""
        apply_window_exstyles(self)

    # -------------------------------------------------------------- public API
    def show_polish_result(self, original, polished):
        self._polish_result.emit(original, polished)

    def _show_polish_result(self, original, polished):
        from polish_result import PolishResultDialog
        self._present_result(PolishResultDialog(original, polished))

    def _present_result(self, dialog):
        self._result_dialogs.add(dialog)
        self._polish_dialog = dialog
        dialog.finished.connect(lambda _: self._clear_polish_dialog(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _clear_polish_dialog(self, dialog):
        self._result_dialogs.discard(dialog)
        if self._polish_dialog is dialog:
            self._polish_dialog = next(iter(self._result_dialogs), None)

    def show_copy_result(self, text, explanation):
        self._copy_result.emit(text, explanation)

    def _show_copy_result(self, text, explanation):
        from polish_result import PolishResultDialog
        dialog = PolishResultDialog(None, text, title="Odicto · Text ready", explanation=explanation)
        self._present_result(dialog)

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
        self.gui_state = self._shown_state = GuiState.RESET
        self._stem_now()  # restarts the stem's timeline from the stem on screen
        self._sync_geometry()
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
        self.gui_state = self._shown_state = new_state
        self._sync_geometry()
        self._stem_now()
        if new_state == GuiState.RECORDING and prev != GuiState.RECORDING:
            self._voice.clear()   # a new recording starts with a clean voice
        if new_state == GuiState.SUCCESS and not kept_success:
            self._success_since = now
        # The entry plays only from hidden; an open leaf stays one leaf.
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

        Only a finished check, cross or notice qualifies: the entry (and its vein sweep),
        the stem's timeline (check, cross, seed), the width and height springs and the
        voice have all stopped, and nothing in the frame is driven by the clock. The tip
        droop is a function of the stem's timeline, so it has stopped too.
        """
        return (
            self._phase == "open"
            and self.gui_state in (GuiState.SUCCESS, GuiState.ERROR, GuiState.RESET)
            and now - self._phase_t0 + self._entry_offset >= _SWEEP_END_S
            and self._glyph_fade >= 1.0
            and self._w_tw.done(now)
            and self._h_tw.done(now)
            and self._voice_peak() == 0.0
        )

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        now = time.monotonic()
        # Real time since the last tick, capped so a stall never jumps an animation.
        dt = 0.016 if self._last_tick is None else min(max(now - self._last_tick, 0.0), _DT_MAX)
        self._last_tick = now

        try:
            app_state = getattr(self.app, "state", None)
            if app_state is not None and app_state != self.last_app_state:
                self._sync_from_app()
        except Exception:
            pass

        self._advance_phase(now)
        # The label can change with no state change (Finalizing -> Transcribing): refit
        # the width, or the longer word is clipped. An equal target returns at once.
        if self._phase in ("entering", "open"):
            self._sync_geometry()

        self._stem_now()
        self._advance_stem(dt)
        self._advance_voice(dt)
        self._advance_caption(dt)

        if self._phase != "hidden" or self._appear_target > 0:
            self.update()

        # A finished, unchanging leaf (a standing "Mic unavailable") needs no
        # 60 FPS repaint. Any app change or state change wakes the tick again.
        interval = _STATIC_TICK_MS if self._is_static(now) else _ANIM_TICK_MS
        if self._tick.interval() != interval:
            self._tick.setInterval(interval)

    def _advance_stem(self, dt: float) -> None:
        self._glyph_fade = min(1.0, self._glyph_fade + dt / _STEM_SETTLE_S)

    def _advance_voice(self, dt: float) -> None:
        """Feed the voice envelope the five spectral bands of the newest audio.

        ``recorder.get_bands`` lists the lowest band first; row 0 is the top pixel row and
        shows the highest band, so the list is reversed. A recorder that is missing,
        raises or returns another length reads as silence, and so does anything outside
        recording: the envelope then decays, and a voice too faint to see is cleared, so a
        finished frame can go static.
        """
        bands = [0.0] * hl.ROWS
        recorder = getattr(self.app, "recorder", None)
        recording = self.gui_state == GuiState.RECORDING
        if recorder is not None and recording:
            try:
                got = list(recorder.get_bands(hl.ROWS))
                if len(got) == hl.ROWS:
                    bands = list(reversed([max(0.0, min(1.0, float(x))) for x in got]))
            except Exception:
                bands = [0.0] * hl.ROWS
        self._voice.advance(dt, bands)
        peak = self._voice_peak()
        if not recording and 0.0 < peak < _VOICE_REST:
            self._voice.clear()

    def _voice_peak(self) -> float:
        """The largest value the painter could still show: the level, the rows, the history."""
        v = self._voice
        return max(v.level, *v.rows, *(x for column in v.hist for x in column))

    def _card_width(self) -> float:
        """Width of the caption card: its minimum, or the leaf's width when that is larger."""
        return max(float(hl.CARD_MIN_W), hl.leaf_width(self._label_target))

    def _caption_room(self) -> float:
        return self._card_width() - 2 * _CAPTION_PAD - 4

    def _card_progress(self, now: float) -> float:
        """0 with the leaf alone, 1 with the caption card in: the height spring's share."""
        span = float(self._live_h - self._compact_h)
        return max(0.0, min(1.0, (self._h_tw.value(now) - self._compact_h) / span))

    def _word_width(self, word: str) -> int:
        width = self._cap_widths.get(word)
        if width is None:
            if len(self._cap_widths) > 4000:
                self._cap_widths.clear()
            width = self._cap_widths[word] = self._fm_caption.horizontalAdvance(word)
        return width

    def _live_failure_text(self) -> str:
        status = getattr(self.app, "last_status", None)
        if self._fail_text[0] != status:
            message = live_failure_message(status, Config.live_key_rejected_stt_provider())
            self._fail_text = (status, message)
        return self._fail_text[1]

    def _card_notice(self, room: int) -> list[str]:
        """The muted lines for a card with no captions: why the captions are off, or the prompt.

        One or two lines, wrapped to ``room``, centred as a block in the card's two caption rows.
        What does not fit on the second line is elided.
        """
        text = self._live_failure_text() or "Speak, then tap F7 to insert"
        words = text.split()
        spans = wrap_words([self._word_width(w) for w in words], self._word_width(" "), room)
        lines = [" ".join(words[a:b]) for a, b in spans[:_CAPTION_LINES]]
        if len(spans) > _CAPTION_LINES:
            lines[-1] = " ".join(words[spans[_CAPTION_LINES - 1][0]:])
        lines = [self._fm_caption.elidedText(line, Qt.TextElideMode.ElideRight, room)
                 for line in lines]
        return lines

    def _reset_caption(self) -> None:
        self._cap_words, self._cap_lines, self._cap_fading = [], [], ""
        self._cap_shown = self._cap_fade = self._cap_scroll = 0.0
        self._cap_last_first = None
        self._cap_prev_len = None
        self._cap_visible_from = 0

    @staticmethod
    def _preview_head_dropped(old: list[str], new: list[str]) -> int:
        """How many words the preview lost at its front (0 if it did not slide).

        A service that keeps only its newest words sends ``old[d:]`` plus more. Without
        this, the revealed count would point d words too far on.
        """
        if not old or not new or new[:len(old)] == old:  # nothing changed, or only a longer text
            return 0
        for d in range(1, len(old)):
            if old[d] != new[0]:
                continue
            n = min(len(old) - d, len(new))
            if n >= 3 and old[d:d + n] == new[:n]:  # three words in a row: not by chance
                return d
        return 0

    def _advance_caption(self, dt: float) -> None:
        """Reveal the live words a little at a time, and lay out the revealed lines.

        The service sends a few words about once a second, so showing each update at
        once makes the caption jump. The words appear at a steady pace instead, and a
        bigger backlog is caught up faster, so the caption never trails the speech.
        """
        preview = getattr(self.app, "live_preview", "")
        words = preview.split() if isinstance(preview, str) else []
        if not words or not self._is_live_layout():
            if self._cap_words or self._cap_lines:
                self._reset_caption()
            return
        old = self._cap_words
        if words != old:
            dropped = self._preview_head_dropped(old, words)
            if dropped:  # the preview slid: the words already shown moved to the front
                old = old[dropped:]
                self._cap_shown = max(0.0, self._cap_shown - dropped)
                self._cap_visible_from = max(0, self._cap_visible_from - dropped)
                if self._cap_prev_len is not None:
                    self._cap_prev_len = max(0, self._cap_prev_len - dropped)
            # Find the changed stretch: the words before it and after it are the same in
            # both. A change that ends above the two lines on screen cannot be seen, so the
            # caption keeps its place, moved by the words added or removed there. A change
            # that reaches the lines on screen is revealed again from where it is visible.
            # A longer text just carries on.
            limit = min(len(old), len(words))
            head = 0
            while head < limit and old[head] == words[head]:
                head += 1
            tail = 0
            while tail < limit - head and old[-1 - tail] == words[-1 - tail]:
                tail += 1
            if head < len(old) and len(old) - tail <= self._cap_visible_from:
                moved = len(words) - len(old)
                self._cap_shown = max(0.0, self._cap_shown + moved)
                self._cap_visible_from = max(0, self._cap_visible_from + moved)
                if self._cap_prev_len is not None:
                    self._cap_prev_len = max(0, self._cap_prev_len + moved)
            else:
                self._cap_shown = min(self._cap_shown, float(max(head, self._cap_visible_from)))
            self._cap_words = words
        total = float(len(words))
        step = max(_CAPTION_WORDS_PER_S * dt,
                   (total - self._cap_shown) * (1.0 - math.exp(-dt / _CAPTION_CATCHUP_S)))
        self._cap_shown = min(total, self._cap_shown + step)
        whole = int(self._cap_shown)
        fraction = self._cap_shown - whole
        fading = words[whole] if fraction > 0.0 and whole < len(words) else ""
        shown = words[:whole] + ([fading] if fading else [])
        # Wrap from the first word, so a line keeps its place while later words arrive; a
        # window that slid would re-flow every line and fake a new one.
        spans = wrap_words([self._word_width(w) for w in shown], self._word_width(" "),
                           self._caption_room())
        kept = spans[-(_CAPTION_LINES + 1):]
        lines = [" ".join(shown[a:b - 1 if (fading and b == len(shown)) else b]) for a, b in kept]
        self._cap_lines = lines
        # The oldest kept line only leaves the screen; the two after it are what is read.
        self._cap_visible_from = spans[-_CAPTION_LINES][0] if len(spans) >= _CAPTION_LINES else 0
        self._cap_fading, self._cap_fade = fading, fraction
        first = spans[-1][0] if spans else 0
        # A line is new when it starts at a word that was not on screen in the last frame.
        # A line that merely moved (words dropped from the front, a wider card) is not.
        if self._cap_prev_len is not None and first > 0 and first >= self._cap_prev_len:
            # Everything slides up. One row is all the painter shows, so a burst of
            # lines (a big backlog caught up) does not stack past it.
            self._cap_scroll = min(self._cap_scroll + _CAPTION_LINE, float(_CAPTION_LINE))
        self._cap_prev_len = len(shown)
        self._cap_last_first = first
        self._cap_scroll *= math.exp(-dt / _CAPTION_SCROLL_TAU)
        if self._cap_scroll < 0.05:
            self._cap_scroll = 0.0

    # ----------------------------------------------------------------- paint
    def _snap(self, v: float) -> float:
        """Round to a whole device pixel (crisp baselines at any scale)."""
        return round(v * self._dpr) / self._dpr

    def _leaf_frame(self, now: float) -> LeafFrame:
        """Everything the painter needs for this instant."""
        state = self._vis_state()
        use_llm = bool(getattr(self.app, "use_llm", False))
        if self._phase in ("entering", "open"):
            entered = self._phase_t0 - self._entry_offset
        else:
            entered = self._enter_at
        return LeafFrame(
            cx=self._canvas_w / 2.0,
            bottom=float(self._canvas_h - _BLEED),
            label_w=self._w_tw.value(now),
            opening=self._opening(now),
            stem=self._stem,
            ai=_show_ai_chip(state, use_llm),
            label=self._label_for(state, use_llm),
            since_state=min(1.0, self._glyph_fade) * _STEM_SETTLE_S,
            since_enter=max(0.0, now - entered),
            now=now,
            voice=self._voice,
            droop=self._droop_now(),
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        now = time.monotonic()
        frame = self._leaf_frame(now)
        opening = frame.opening
        if opening.fade <= 0.0:
            return

        self._dpr = self.devicePixelRatioF() or 1.0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # The caption card first, so the leaf's own shadow lies over its lower edge.
        progress = self._card_progress(now)
        if progress > 0.0:
            rect = hl.card_rect(hl.leaf_geometry(frame), progress, self._dpr)
            card_opacity = progress * opening.content * opening.fade
            self._painter.paint_card(p, rect, self._style, card_opacity)
            if card_opacity > 0.0:
                self._paint_caption(p, rect, card_opacity)
        self._painter.paint(p, frame, self._style)
        p.end()

    def _baseline(self, cy: float) -> float:
        """Baseline that centres capital letters on ``cy`` (the font box is not symmetric)."""
        return self._snap(cy + self._half_cap)

    def _paint_caption(self, p: QPainter, rect: QRectF, opacity: float = 1.0) -> None:
        """Two caption rows inside the card, newest at the bottom.

        The older row recedes; a new row slides up. ``rect`` is the card the painter drew.
        """
        block_h = _CAPTION_LINES * _CAPTION_LINE
        block = QRectF(rect.left(), rect.top() + (rect.height() - block_h) / 2.0,
                       rect.width(), block_h)
        left = rect.left() + _CAPTION_PAD
        room = int(rect.width() - 2 * _CAPTION_PAD)
        font = self._font_caption
        p.save()
        p.setFont(font)
        p.setClipRect(block, Qt.ClipOperation.IntersectClip)
        if not self._cap_lines:
            if self._is_live_layout():
                p.setOpacity(opacity)
                p.setPen(_Theme.muted)
                lines = self._card_notice(room)
                for index, text in enumerate(lines):
                    row_cy = block.top() + ((_CAPTION_LINES - len(lines)) / 2.0 + index + 0.5) * _CAPTION_LINE
                    p.drawText(QPointF(self._snap(left), self._baseline(row_cy)), text)
            p.restore()
            return
        slide = min(1.0, self._cap_scroll / _CAPTION_LINE)
        p.setPen(QColor(*hl.INK))
        count = len(self._cap_lines)
        for index, line in enumerate(self._cap_lines):
            row = index - count + 2  # the newest line is row 1; the one before it, row 0
            if row == 1:
                alpha = 1.0
            elif row == 0:
                alpha = _CAPTION_OLD_ALPHA + (1.0 - _CAPTION_OLD_ALPHA) * slide
            else:
                alpha = _CAPTION_OLD_ALPHA * slide  # leaving at the top
            if alpha <= 0.01 or row < -1:
                continue
            row_cy = block.top() + (row + 0.5) * _CAPTION_LINE + self._cap_scroll
            baseline = self._baseline(row_cy)
            ink = opacity * _CAPTION_ALPHA * alpha
            p.setOpacity(ink)
            if row == 1 and self._cap_fading:
                # The newest words are the ones that matter, so what does not fit loses its
                # beginning. The fading word keeps its own room; a word wider than the whole
                # row shows its end.
                fm = self._fm_caption
                fade_w = fm.horizontalAdvance(self._cap_fading)
                fade_text = self._cap_fading
                head, lead = "", 0
                if fade_w >= room:
                    fade_text = fm.elidedText(fade_text, Qt.TextElideMode.ElideLeft, room)
                elif line:
                    head = fm.elidedText(line, Qt.TextElideMode.ElideLeft,
                                         room - fade_w - fm.horizontalAdvance(" "))
                    lead = fm.horizontalAdvance(head + " ") if head else 0
                if head:
                    p.drawText(QPointF(self._snap(left), baseline), head)
                p.setOpacity(ink * max(0.0, min(1.0, self._cap_fade)))
                p.drawText(QPointF(self._snap(left + lead), baseline), fade_text)
            else:
                # A line wider than the row is one oversized word (the wrap sees to that), and
                # its end is what the speaker just said. Left elision keeps it on either row.
                p.drawText(QPointF(self._snap(left), baseline),
                           self._fm_caption.elidedText(line, Qt.TextElideMode.ElideLeft, room))
        p.restore()
