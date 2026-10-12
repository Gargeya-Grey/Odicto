"""
Odicto floating status HUD — quiet glass capsule.

Design language:
  - Near-flat charcoal glass, soft neutral shadow only (no colored halos)
  - Consistent 8px spacing + optical vertical alignment
  - Restrained EQ bars / dual-ring spinner; accents on glyphs only
  - AI mode: subtle chip, no purple body wash
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
from typing import Any, Optional

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
    """Quiet charcoal glass — restrained, adult, no neon or colored blooms."""

    # Near-flat surface (barely any gradient)
    surface_top = QColor(36, 36, 38, 244)
    surface_bot = QColor(24, 24, 26, 250)
    rim = QColor(255, 255, 255, 22)
    border = QColor(255, 255, 255, 16)

    text = QColor(236, 236, 240, 250)
    chip_fill = QColor(255, 255, 255, 12)
    chip_border = QColor(255, 255, 255, 22)
    chip_text = QColor(200, 200, 210, 240)
    divider = QColor(255, 255, 255, 14)

    # Desaturated status accents — glyphs only
    boot = QColor(160, 160, 166)
    record = QColor(196, 92, 86)
    process = QColor(140, 148, 168)
    success = QColor(108, 168, 128)
    error = QColor(186, 108, 104)
    ai = QColor(148, 142, 168)


# Spacing scale (px) — keep all layout math on this rhythm
_S = 8

# Compact layout chrome: [ inset | glyph | gap | label | (gap + chip)? | inset ]
_INSET_X = 14
_GLYPH_SLOT = 20
_GAP_TEXT_CHIP = 8
_FIT_SLACK = 4.0           # absorbs paint-time metric rounding (HiDPI)
_MIN_PILL_W = 96
_COMPACT_H = 44
_RECORDING_BARS = 20
_RECORDING_BAR_W = 2.0
_RECORDING_BAR_GAP = 1.85
# The slot is the bars themselves, so Listening has no empty track beside them.
_RECORDING_WAVE_W = (
    _RECORDING_BARS * _RECORDING_BAR_W
    + (_RECORDING_BARS - 1) * _RECORDING_BAR_GAP
)
_LIVE_MIN_W = 440
_LIVE_H = 86

# Motion, in seconds. Every move uses ease_in_out_cubic (slow start, slow end).
_RISE_S = 0.28             # droplet rises from below bottom-center
_SPREAD_AT_S = 0.22        # spread overlaps the end of the rise
_SPREAD_S = 0.42           # droplet spreads left and right to the capsule
_RESIZE_S = 0.38           # width/height change while the capsule is open
# The capsule moves this long after the new words are fully in, so a shrink
# cannot clip the longer label while that label is still on screen.
_RESIZE_AFTER_TEXT_S = 0.0003
_WORDS_OUT_S = 0.14        # exit: words fade first
_COLLAPSE_AT_S = 0.10
_COLLAPSE_S = 0.42         # exit: capsule contracts to the droplet
_DROP_AT_S = _COLLAPSE_AT_S + _COLLAPSE_S - 0.06
_DROP_S = 0.28             # exit: droplet drops and fades
_ENTER_S = _SPREAD_AT_S + _SPREAD_S
_LEAVE_S = _DROP_AT_S + _DROP_S
_DROP_PX = 10.0
_DROPLET_MIN = 0.6         # droplet diameter at the start of the rise / end of the drop
_SUCCESS_HOLD_MS = 1200
# Paint interval while the pill animates, and while it only holds a finished,
# unchanging frame (a standing error such as "Mic unavailable").
_ANIM_TICK_MS = 16
_STATIC_TICK_MS = 250


def ease_in_out_cubic(t: float) -> float:
    """Cubic ease-in-out on 0..1: slow start, slow end."""
    t = 0 if t < 0 else 1 if t > 1 else t
    if t < 0.5:
        return 4 * t * t * t
    return 1 - ((-2 * t + 2) ** 3) / 2


def fit_pill_width(text_w: float, chip_w: float = 0.0) -> int:
    """Compact capsule width that shows ``text_w`` px of label (+ chip) unclipped."""
    w = _INSET_X + _GLYPH_SLOT + _S + text_w + _INSET_X + _FIT_SLACK
    if chip_w:
        w += _GAP_TEXT_CHIP + chip_w
    return max(_MIN_PILL_W, int(math.ceil(w)))


def fit_recording_width(text_w: float, chip_w: float = 0.0) -> int:
    """Recording capsule width: waveform, divider, label and chip. No reserved margin."""
    w = _INSET_X + _RECORDING_WAVE_W + 18.0 + text_w + _INSET_X + _FIT_SLACK
    if chip_w:
        w += _GAP_TEXT_CHIP + chip_w
    return max(_MIN_PILL_W, int(math.ceil(w)))


class _Tween:
    """One eased value; retargeting starts from wherever the value is now."""

    __slots__ = ("a", "b", "t0", "dur")

    def __init__(self, value: float) -> None:
        self.a = self.b = float(value)
        self.t0 = 0.0
        self.dur = 0.0

    def value(self, now: float) -> float:
        if self.dur <= 0.0:
            return self.b
        return self.a + (self.b - self.a) * ease_in_out_cubic((now - self.t0) / self.dur)

    def done(self, now: float) -> bool:
        return self.dur <= 0.0 or now - self.t0 >= self.dur

    def to(self, target: float, now: float, dur: float) -> None:
        self.a, self.b, self.t0, self.dur = self.value(now), float(target), now, dur

    def snap(self, value: float) -> None:
        self.a = self.b = float(value)
        self.dur = 0.0


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

        # Animation clock
        self._t: float = 0.0
        self._bars: list[float] = [0.16] * 5  # quiet equalizer; grows in live layout
        self._appear: float = 0.0  # 0..1 shape opacity, derived from the phase timeline
        self._appear_target: float = 1.0
        # Droplet timeline: hidden -> entering -> open -> leaving -> hidden.
        # The window never scales; the painted shape does (scaling blurs layered HWNDs).
        self._phase: str = "hidden"
        self._phase_t0: float = 0.0
        self._check_progress: float = 0.0
        self._content_fade: float = 1.0
        self._glyph_fade: float = 1.0  # 0..1 cross-fade between state glyphs
        self._last_glyph_state: GuiState = GuiState.BOOTING
        self._shown_state: GuiState = GuiState.BOOTING  # painted while leaving
        self._label_cache: str = ""
        self._success_since: float = 0.0
        self._resize_hold: Optional[tuple[float, float]] = None
        self._text_in_at: Optional[float] = None

        # --- Geometry (8px rhythm) ---
        self._pad = 14                 # shadow bleed only (no glow halo)
        self._pill_h = _COMPACT_H
        self._radius = 22              # capsule ends
        self._paint_radius = 22.0
        self._inset_x = _INSET_X
        self._glyph_slot = _GLYPH_SLOT
        self._gap_glyph_text = _S
        self._gap_text_chip = _GAP_TEXT_CHIP
        self._chip_h = 20
        self._chip_pad_x = 7
        self._text_optical_y = -0.5

        # _pill_w/_pill_h are the layout target; the tweens are what is painted.
        self._pill_w = 170
        self._compact_h = _COMPACT_H
        self._canvas_w = self._pill_w + self._pad * 2
        self._canvas_h = self._pill_h + self._pad * 2
        self._w_tw = _Tween(self._pill_w)
        self._h_tw = _Tween(self._pill_h)

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
        self._tick_active = True
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
        """The Odicto logo; the old plain green square only if no logo file loads."""
        icon = logo_icon()
        if not icon.isNull():
            return icon
        from PySide6.QtGui import QPixmap

        pm = QPixmap(24, 24)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(108, 168, 128))
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
        families = set(QFontDatabase.families())
        for name in (
            "Segoe UI Variable Display",
            "Segoe UI Variable",
            "Segoe UI",
            "SF Pro Display",
            "Inter",
            "Ubuntu",
            "Noto Sans",
            "DejaVu Sans",
        ):
            if name in families:
                family = name
                break
        else:
            family = self.font().family()

        self._font = QFont(family, 11)
        self._font.setWeight(QFont.Weight.Medium)
        self._font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self._font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 102)

        self._font_chip = QFont(family, 8)
        self._font_chip.setWeight(QFont.Weight.DemiBold)
        self._font_chip.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)

    def _chip_width(self, label: str = "AI") -> float:
        fm = QFontMetrics(self._font_chip)
        return float(fm.horizontalAdvance(label) + self._chip_pad_x * 2)

    def _target_width(self, label: str, chip_label: str = "") -> int:
        """Compact width that fits ``label`` (and an optional chip) unclipped."""
        text_w = QFontMetrics(self._font).horizontalAdvance(label)
        return fit_pill_width(text_w, self._chip_width(chip_label) if chip_label else 0.0)

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

    def _is_live_layout(self) -> bool:
        return (getattr(self.app, "live_active", False) is True or
                (self.gui_state == GuiState.PROCESSING and bool(getattr(self.app, "live_preview", ""))))

    def _layout_target(self) -> tuple[int, int, int]:
        """(width, height, bar count) that fits what the current state shows."""
        state = self.gui_state
        use_llm = bool(getattr(self.app, "use_llm", False))
        label = self._label_for(state, use_llm)
        if state == GuiState.RECORDING:
            live_on = getattr(self.app, "live_active", False) is True
            chip = "Live" if live_on else ("AI" if _show_ai_chip(state, use_llm) else "")
            text_w = QFontMetrics(self._font).horizontalAdvance(label)
            w = fit_recording_width(text_w, self._chip_width(chip) if chip else 0.0)
            n_bars = _RECORDING_BARS
        else:
            w = self._target_width(label, "AI" if _show_ai_chip(state, use_llm) else "")
            n_bars = 7
        h = self._compact_h
        if self._is_live_layout():
            w, h = max(w, _LIVE_MIN_W), _LIVE_H
        return w, h, n_bars

    def _sync_geometry(self) -> None:
        """Retarget the capsule to the current words.

        From hidden, the droplet opens at the new size. While the capsule is
        already open, the painted size waits until the new words are fully in,
        then starts 0.3 ms later, so a shrink cannot clip the longer label.
        """
        if self.gui_state == GuiState.HIDDEN:
            return  # keep the shown size while the exit plays
        w, h, n_bars = self._layout_target()
        if len(self._bars) != n_bars:
            self._bars = [0.16] * n_bars
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
        self._w_tw.to(w, now, _RESIZE_S)
        self._h_tw.to(h, now, _RESIZE_S)
        self._resize_hold = None
        self._text_in_at = None

    def _fit_canvas(self, w: float, h: float, shrink: bool = False) -> None:
        """Resize the window to hold a ``w`` x ``h`` capsule (one resize, not per frame)."""
        cw = int(math.ceil(w)) + self._pad * 2
        cw += cw % 2  # even width keeps the painted center on a whole pixel
        ch = int(math.ceil(h)) + self._pad * 2
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
            rise = ease_in_out_cubic(el / _RISE_S)
            spread_raw = max(0.0, min(1.0, (el - _SPREAD_AT_S) / _SPREAD_S))
            spread = ease_in_out_cubic(spread_raw)
            size = d * (_DROPLET_MIN + (1.0 - _DROPLET_MIN) * rise)
            words = ease_in_out_cubic((spread_raw - 0.5) / 0.5)
            return (size + (W - size) * spread, size + (H - size) * spread,
                    (1.0 - rise) * _DROP_PX, rise, words)
        if self._phase == "leaving":
            words = 1.0 - ease_in_out_cubic(el / _WORDS_OUT_S)
            collapse = ease_in_out_cubic((el - _COLLAPSE_AT_S) / _COLLAPSE_S)
            drop = ease_in_out_cubic((el - _DROP_AT_S) / _DROP_S)
            shrink = 1.0 - (1.0 - _DROPLET_MIN) * drop
            return ((W + (d - W) * collapse) * shrink, (H + (d - H) * collapse) * shrink,
                    drop * _DROP_PX, 1.0 - drop, words)
        if self._phase == "open":
            return W, H, 0.0, 1.0, 1.0
        return W, H, 0.0, 0.0, 0.0

    def _begin_show(self, now: float) -> None:
        """Play the droplet entrance from hidden; reopen in place if the exit is early."""
        if self._phase == "leaving" and now - self._phase_t0 < _DROP_AT_S:
            # Not yet a droplet: ease back out from the painted size.
            w, h, _, _, _ = self._frame(now)
            self._w_tw.snap(w)
            self._h_tw.snap(h)
            self._w_tw.to(self._pill_w, now, _RESIZE_S)
            self._h_tw.to(self._pill_h, now, _RESIZE_S)
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
            self._appear = ease_in_out_cubic(el / _RISE_S)
            if el >= _ENTER_S:
                self._phase = "open"
        elif self._phase == "open":
            self._appear = 1.0
            # Shrink the canvas once, after a shrink animation has finished.
            if (self._resize_hold is None
                    and self._w_tw.done(now) and self._h_tw.done(now)):
                self._fit_canvas(self._pill_w, self._pill_h, shrink=True)
        elif self._phase == "leaving":
            self._appear = 1.0 - ease_in_out_cubic((el - _DROP_AT_S) / _DROP_S)
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
            self._glyph_fade = 0.0
        self.gui_state = self._shown_state = GuiState.RESET
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
        if kept_success and self._label_cache != shown_label:
            self._content_fade = 0.8
        if not kept_success:
            self._t = 0.0
            if new_state in (GuiState.SUCCESS, GuiState.ERROR):
                self._check_progress = 0.0
            if new_state == GuiState.SUCCESS:
                self._success_since = now
            # Cross-fade the glyph when the drawn state changes (not on same-state noise).
            if new_state != prev:
                self._glyph_fade = 0.0
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
            self._tick.start()
        elif not active and self._tick.isActive():
            self._tick.stop()
        if active:
            self._wake_tick()

    def _wake_tick(self) -> None:
        if self._tick.interval() != _ANIM_TICK_MS:
            self._tick.setInterval(_ANIM_TICK_MS)

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
        self._t += 0.016

        try:
            app_state = getattr(self.app, "state", None)
            if app_state is not None and app_state != self.last_app_state:
                self._sync_from_app()
        except Exception:
            pass

        self._advance_phase(time.monotonic())

        # Glyph cross-fade — ~150ms to full.
        self._glyph_fade = min(1.0, self._glyph_fade + 0.2)

        # Processing fill clock — a full ring represents one "working" sweep.
        if self.gui_state == GuiState.PROCESSING:
            self._proc_fill = (getattr(self, "_proc_fill", 0.0) + 0.016 / 2.5) % 1.0

        if self._content_fade < 1.0:
            self._content_fade = min(1.0, self._content_fade + 0.16)
        self._release_resize(time.monotonic())

        n = len(self._bars)
        recorder = getattr(self.app, "recorder", None)
        wave: list[float] = []
        if recorder is not None and self.gui_state == GuiState.RECORDING:
            try:
                wave = recorder.get_waveform(n)
            except Exception:
                wave = []
        for i in range(n):
            if self.gui_state == GuiState.RECORDING and wave:
                target = float(wave[i]) if i < len(wave) else 0.0
                # Fast attack / slower release so consonants pop, vowels settle.
                blend = 0.55 if target > self._bars[i] else 0.22
            else:
                mid = abs(i - (n - 1) / 2.0) / max(1.0, (n - 1) / 2.0)
                shape = 1.0 - 0.35 * mid
                target = (0.08 + 0.04 * abs(math.sin(self._t * 1.4 + i * 0.35))) * shape
                blend = 0.26
            self._bars[i] += (target - self._bars[i]) * blend

        if self.gui_state in (GuiState.SUCCESS, GuiState.ERROR):
            self._check_progress = min(1.0, self._check_progress + 0.08)

        if self._phase != "hidden" or self._appear_target > 0:
            self.update()

        # A finished, unchanging pill (a standing "Mic unavailable") needs no
        # 60 FPS repaint. Any app change or state change wakes the tick again.
        interval = _STATIC_TICK_MS if self._is_static(time.monotonic()) else _ANIM_TICK_MS
        if self._tick.interval() != interval:
            self._tick.setInterval(interval)

    # ----------------------------------------------------------------- paint
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        now = time.monotonic()
        w, h, dy, alpha, words = self._frame(now)
        if alpha <= 0.001:
            return

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
        accent = self._accent_color()
        use_llm = bool(getattr(self.app, "use_llm", False))

        self._paint_shadow(p, pill)
        self._paint_surface(p, pill)
        self._paint_border(p, pill)
        self._paint_top_rim(p, pill)

        # Words are laid out at full size for the current width and revealed by
        # the capsule clip; glyphs and text never stretch with the width.
        content_alpha = alpha * self._content_fade * words
        if content_alpha > 0.001:
            W, H = self._w_tw.value(now), self._h_tw.value(now)
            layout = QRectF(cx - W * 0.5, bottom - H + dy, W, H)
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(pill, self._paint_radius, self._paint_radius)
            p.setClipPath(clip)
            p.setOpacity(max(0.0, min(1.0, content_alpha)))
            self._paint_content(p, layout, accent, use_llm)
            p.restore()

        p.end()

    def _ease_out_cubic(self, t: float) -> float:
        t = max(0.0, min(1.0, t))
        return 1.0 - (1.0 - t) ** 3

    def _accent_color(self) -> QColor:
        use_llm = bool(getattr(self.app, "use_llm", False))
        state = self._vis_state()
        if state == GuiState.BOOTING:
            return _Theme.boot
        if state == GuiState.RECORDING:
            return _Theme.ai if use_llm else _Theme.record
        if state == GuiState.PROCESSING:
            return _Theme.ai if use_llm else _Theme.process
        if state == GuiState.SUCCESS:
            return _Theme.success
        if state == GuiState.ERROR:
            return _Theme.error
        if state == GuiState.RESET:
            return _Theme.ai
        return _Theme.process

    def _paint_shadow(self, p: QPainter, pill: QRectF) -> None:
        """Neutral drop — slightly grounded so the capsule reads on bright screens."""
        for dy, expand, alpha in ((6, 3, 40), (3, 0, 52)):
            r = QRectF(pill).adjusted(-expand, dy * 0.35, expand, dy)
            path = QPainterPath()
            path.addRoundedRect(r, self._paint_radius + 1, self._paint_radius + 1)
            p.fillPath(path, QColor(0, 0, 0, alpha))

    def _paint_surface(self, p: QPainter, pill: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(pill, self._paint_radius, self._paint_radius)
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0.0, _Theme.surface_top)
        grad.setColorAt(1.0, _Theme.surface_bot)
        p.fillPath(path, QBrush(grad))

    def _paint_border(self, p: QPainter, pill: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(
            pill.adjusted(0.5, 0.5, -0.5, -0.5), self._paint_radius, self._paint_radius
        )
        pen = QPen(_Theme.border)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _paint_top_rim(self, p: QPainter, pill: QRectF) -> None:
        """Faint top edge light — barely there."""
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(pill, self._paint_radius, self._paint_radius)
        p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        rim_w = pill.width() - 20
        if rim_w <= 0:
            p.restore()
            return
        rim = QRectF(pill.left() + 10, pill.top() + 1.0, rim_w, 1.0)
        g = QLinearGradient(rim.left(), rim.top(), rim.right(), rim.top())
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.25, _Theme.rim)
        g.setColorAt(0.75, _Theme.rim)
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(rim, QBrush(g))
        p.restore()

    def _paint_content(
        self, p: QPainter, pill: QRectF, accent: QColor, use_llm: bool
    ) -> None:
        """Compact glyph+label or expanded recording waveform."""
        if self._is_live_layout():
            header = QRectF(pill.x(), pill.y(), pill.width(), 44)
            if self._vis_state() == GuiState.RECORDING:
                self._paint_recording_content(p, header, accent, use_llm)
            else:
                self._paint_compact_content(p, header, accent, use_llm)
            p.setFont(self._font)
            p.setPen(_Theme.text)
            preview = getattr(self.app, "live_preview", "") or "Speak, then tap F7 to insert"
            preview = " ".join(preview.split())[-250:]
            preview = p.fontMetrics().elidedText(preview, Qt.TextElideMode.ElideLeft, int(pill.width() - 28))
            p.drawText(QRectF(pill.x() + 14, pill.y() + 44, pill.width() - 28, 30), Qt.AlignmentFlag.AlignVCenter, preview)
            return
        if self._vis_state() == GuiState.RECORDING:
            self._paint_recording_content(p, pill, accent, use_llm)
            return
        self._paint_compact_content(p, pill, accent, use_llm)

    def _paint_compact_content(
        self, p: QPainter, pill: QRectF, accent: QColor, use_llm: bool
    ) -> None:
        """[ inset | glyph | gap | label .............. | chip? | inset ]"""
        state = self._vis_state()
        left = pill.x() + self._inset_x
        glyph_cx = left + self._glyph_slot * 0.5
        glyph_cy = pill.center().y() + self._text_optical_y

        if self._glyph_fade < 1.0:
            # Cross-fade: draw the previous state's glyph beneath at low opacity.
            p.save()
            p.setOpacity(max(0.0, 1.0 - self._glyph_fade))
            self._draw_glyph(p, self._last_glyph_state, glyph_cx, glyph_cy, accent)
            p.restore()
        self._draw_glyph(p, state, glyph_cx, glyph_cy, accent)
        self._last_glyph_state = state

        # Hairline divider
        div_x = left + self._glyph_slot + self._gap_glyph_text * 0.4
        mid = pill.center().y()
        div_pen = QPen(_Theme.divider)
        div_pen.setWidthF(1.0)
        p.setPen(div_pen)
        p.drawLine(QPointF(div_x, mid - 8), QPointF(div_x, mid + 8))

        label = self._label_for(state, use_llm)

        chip_on = _show_ai_chip(state, use_llm)
        chip_w = self._chip_width() if chip_on else 0.0
        text_left = left + self._glyph_slot + self._gap_glyph_text
        text_right = pill.right() - self._inset_x
        if chip_on:
            text_right -= chip_w + self._gap_text_chip

        p.setFont(self._font)
        p.setPen(_Theme.text)
        fm = p.fontMetrics()
        cap = (
            float(fm.capHeight())
            if hasattr(fm, "capHeight")
            else float(fm.ascent() * 0.7)
        )
        text_cy = pill.center().y() + self._text_optical_y
        band = max(cap + 10.0, float(fm.height()))
        text_rect = QRectF(
            text_left,
            text_cy - band * 0.5,
            max(0.0, text_right - text_left),
            band,
        )
        p.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            label,
        )

        if chip_on:
            self._draw_ai_chip(
                p,
                pill.right() - self._inset_x - chip_w,
                pill.center().y() - self._chip_h * 0.5,
                chip_w,
                self._chip_h,
            )

    def _paint_recording_content(
        self, p: QPainter, pill: QRectF, accent: QColor, use_llm: bool
    ) -> None:
        """Expanded waveform on the left, status text on the right."""
        state = self._vis_state()
        left = pill.x() + self._inset_x
        wave_w = _RECORDING_WAVE_W
        wave_cx = left + wave_w * 0.5
        wave_cy = pill.center().y()
        self._draw_eq_bars(p, wave_cx, wave_cy, accent, slot=wave_w, max_h=16.0, bar_w=2.0)

        div_x = left + wave_w + 8.0
        mid = pill.center().y()
        div_pen = QPen(_Theme.divider)
        div_pen.setWidthF(1.0)
        p.setPen(div_pen)
        p.drawLine(QPointF(div_x, mid - 9), QPointF(div_x, mid + 9))

        label = self._label_for(state, use_llm)
        live_on = getattr(self.app, "live_active", False) is True
        chip_on = _show_ai_chip(state, use_llm)
        chip_label = "Live" if live_on else ("AI" if chip_on else "")
        chip_w = (
            float(QFontMetrics(self._font_chip).horizontalAdvance(chip_label) + self._chip_pad_x * 2)
            if chip_label
            else 0.0
        )
        text_left = div_x + 10.0
        text_right = pill.right() - self._inset_x
        if chip_w:
            text_right -= chip_w + self._gap_text_chip

        p.setFont(self._font)
        p.setPen(_Theme.text)
        fm = p.fontMetrics()
        cap = (
            float(fm.capHeight())
            if hasattr(fm, "capHeight")
            else float(fm.ascent() * 0.7)
        )
        band = max(cap + 10.0, float(fm.height()))
        text_rect = QRectF(
            text_left,
            pill.center().y() + self._text_optical_y - band * 0.5,
            max(0.0, text_right - text_left),
            band,
        )
        p.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            label,
        )
        if chip_label:
            self._draw_named_chip(
                p,
                pill.right() - self._inset_x - chip_w,
                pill.center().y() - self._chip_h * 0.5,
                chip_w,
                self._chip_h,
                chip_label,
            )

    def _draw_named_chip(
        self, p: QPainter, x: float, y: float, w: float, h: float, label: str
    ) -> None:
        rect = QRectF(x, y, w, h)
        path = QPainterPath()
        path.addRoundedRect(rect, h * 0.5, h * 0.5)
        p.fillPath(path, _Theme.chip_fill)
        pen = QPen(_Theme.chip_border)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setFont(self._font_chip)
        p.setPen(_Theme.chip_text)
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)

    def _draw_ai_chip(
        self, p: QPainter, x: float, y: float, w: float, h: float
    ) -> None:
        """Neutral glass chip (no purple fill) — the named chip, labelled "AI"."""
        self._draw_named_chip(p, x, y, w, h, "AI")

    def _draw_eq_bars(
        self,
        p: QPainter,
        cx: float,
        cy: float,
        accent: QColor,
        slot: Optional[float] = None,
        max_h: float = 11.0,
        bar_w: float = 2.0,
    ) -> None:
        """Slim rounded bars — calm, not nightclub equalizer."""
        n = len(self._bars)
        if slot is None:
            slot = self._glyph_slot - 2.0
        raw_gap = (slot - n * bar_w) / max(1, n - 1)
        gap = min(1.85, max(0.85, raw_gap))
        used = n * bar_w + max(0, n - 1) * gap
        x0 = cx - min(used, slot) * 0.5

        for i, v in enumerate(self._bars):
            h = max(2.2, max_h * (0.2 + 0.8 * v))
            x = x0 + i * (bar_w + gap)
            y = cy - h * 0.5
            rect = QRectF(x, y, bar_w, h)
            path = QPainterPath()
            path.addRoundedRect(rect, bar_w * 0.5, bar_w * 0.5)
            c = QColor(accent)
            c.setAlpha(int(100 + 100 * v))
            p.fillPath(path, c)

    def _draw_glyph(
        self, p: QPainter, state: GuiState, cx: float, cy: float, accent: QColor
    ) -> None:
        """Route a GuiState to its glyph draw function (used for cross-fades too)."""
        if state == GuiState.RECORDING:
            self._draw_eq_bars(p, cx, cy, accent)
        elif state == GuiState.PROCESSING:
            self._draw_fill_ring(p, cx, cy, accent)
        elif state == GuiState.BOOTING:
            self._draw_dual_ring(p, cx, cy, accent, slow=True)
        elif state in (GuiState.SUCCESS, GuiState.RESET):
            self._draw_check(p, cx, cy, accent)
        elif state == GuiState.ERROR:
            self._draw_error(p, cx, cy, accent)
        else:
            self._draw_dot(p, cx, cy, accent)

    def _draw_fill_ring(
        self, p: QPainter, cx: float, cy: float, accent: QColor
    ) -> None:
        """Indeterminate 'working' ring: a fine arc fills over ~2.5s, then loops.

        More informative than a decorative spinner — you feel the pipeline work.
        """
        radius = 6.4
        width = 1.35
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        track = QPen(QColor(255, 255, 255, 16))
        track.setWidthF(width)
        p.setPen(track)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 200))
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        sweep = getattr(self, "_proc_fill", 0.0)
        start = int(math.degrees(-90.0) * 16)
        p.drawArc(rect, start, int(-360.0 * 16 * sweep))

    def _draw_dual_ring(
        self, p: QPainter, cx: float, cy: float, accent: QColor, slow: bool = False
    ) -> None:
        """Single fine arc on a faint track — less busy than dual neon rings."""
        speed = 1.3 if slow else 2.2
        a = self._t * speed
        radius = 6.4
        width = 1.35
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        track = QPen(QColor(255, 255, 255, 16))
        track.setWidthF(width)
        p.setPen(track)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        pen = QPen(
            QColor(accent.red(), accent.green(), accent.blue(), 200)
        )
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        start = int(math.degrees(a) * 16)
        p.drawArc(rect, start, int(-100 * 16))

    def _draw_check(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        t = self._ease_out_cubic(self._check_progress)
        pen = QPen(accent)
        pen.setWidthF(1.55)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setColor(
            QColor(
                accent.red(),
                accent.green(),
                accent.blue(),
                int(235 * min(1.0, t * 1.2)),
            )
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if t > 0.12:
            prog = min(1.0, (t - 0.12) / 0.88)
            p1 = QPointF(cx - 3.5, cy + 0.3)
            p2 = QPointF(cx - 0.7, cy + 2.8)
            p3 = QPointF(cx + 4.0, cy - 2.9)
            path = QPainterPath()
            path.moveTo(p1)
            if prog < 0.4:
                u = prog / 0.4
                path.lineTo(
                    QPointF(
                        p1.x() + (p2.x() - p1.x()) * u,
                        p1.y() + (p2.y() - p1.y()) * u,
                    )
                )
            else:
                path.lineTo(p2)
                u = (prog - 0.4) / 0.6
                path.lineTo(
                    QPointF(
                        p2.x() + (p3.x() - p2.x()) * u,
                        p2.y() + (p3.y() - p2.y()) * u,
                    )
                )
            p.drawPath(path)

    def _draw_error(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        t = self._ease_out_cubic(self._check_progress)
        pen = QPen(
            QColor(accent.red(), accent.green(), accent.blue(), int(225 * min(1.0, t)))
        )
        pen.setWidthF(1.55)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if t > 0.18:
            s = 3.0
            p.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            p.drawLine(QPointF(cx + s, cy - s), QPointF(cx - s, cy + s))

    def _draw_dot(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QPointF(cx, cy), 2.4, 2.4)
