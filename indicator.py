"""
Premium floating status HUD for Odicto.

Quiet charcoal pill with a refined status language:
  - Left accent rail + smooth live waveform (recording)
  - Soft orbit dots (processing)
  - Opacity-only entrance (no scale — avoids tiny/blurry first frame)
  - Click-through, no focus steal, always on top, bottom-center
"""

from __future__ import annotations

import math
import sys
import ctypes
from enum import Enum, auto
from typing import Any, Optional

from PySide6.QtCore import (
    QPointF,
    QRectF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QLinearGradient,
    QPaintEvent,
)
from PySide6.QtWidgets import QApplication, QWidget


class GuiState(Enum):
    HIDDEN = auto()
    BOOTING = auto()
    RECORDING = auto()
    PROCESSING = auto()
    SUCCESS = auto()
    ERROR = auto()


# --------------------------------------------------------------------------- theme
class _Theme:
    """Quiet, adult UI — closer to macOS / Raycast than neon glassmorphism."""

    # Near-flat surface (almost no gradient)
    surface = QColor(28, 28, 30, 236)
    surface_edge = QColor(34, 34, 36, 240)  # barely lighter at top
    border = QColor(255, 255, 255, 18)
    text = QColor(235, 235, 240, 250)
    text_secondary = QColor(142, 142, 147, 255)  # system gray

    # Status accents — desaturated, used sparingly on glyphs only
    boot = QColor(174, 174, 178)       # neutral gray
    record = QColor(200, 80, 72)       # muted red
    process = QColor(152, 152, 157)    # cool gray (spinner)
    success = QColor(110, 168, 122)    # soft green
    error = QColor(190, 100, 96)       # soft red
    ai = QColor(168, 162, 178)         # cool muted lavender-gray


def status_label(state: GuiState, use_llm: bool = False, last_status: Optional[str] = None) -> str:
    """Pure label map (unit-testable without Qt paint)."""
    if state == GuiState.BOOTING:
        return "Starting"
    if state == GuiState.RECORDING:
        return "Listening · AI" if use_llm else "Listening"
    if state == GuiState.PROCESSING:
        return "Thinking" if use_llm else "Transcribing"
    if state == GuiState.SUCCESS:
        return "Done"
    if state == GuiState.ERROR:
        if last_status == "empty":
            return "No speech"
        return "Failed"
    return ""


def _all_status_labels() -> list[str]:
    """Every string the pill may show — used to size width to a perfect fit."""
    return [
        status_label(GuiState.BOOTING),
        status_label(GuiState.RECORDING, use_llm=False),
        status_label(GuiState.RECORDING, use_llm=True),
        status_label(GuiState.PROCESSING, use_llm=False),
        status_label(GuiState.PROCESSING, use_llm=True),
        status_label(GuiState.SUCCESS),
        status_label(GuiState.ERROR, last_status="empty"),
        status_label(GuiState.ERROR, last_status="error"),
    ]


class DictationIndicator(QWidget):
    """Bottom-center always-on-top glass HUD for the dictation service."""

    # Thread-safe wake-ups from keyboard / worker threads
    _wake = Signal()
    _hide_req = Signal()

    def __init__(self, app: Any) -> None:
        # Ensure a single QApplication exists before any QWidget.
        self._qt_app = QApplication.instance()
        if self._qt_app is None:
            # High-DPI before QApplication construction
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
            self._qt_app = QApplication(sys.argv if hasattr(sys, "argv") else [])
            self._qt_app.setApplicationName("Odicto")
            self._qt_app.setQuitOnLastWindowClosed(False)

        super().__init__(None)

        self.app = app
        self.gui_state: GuiState = GuiState.BOOTING
        self.last_app_state: Any = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_indicator)

        # Animation clock
        self._t: float = 0.0
        self._wave: list[float] = [0.12] * 24  # denser samples for smooth ribbon
        self._level_smooth: float = 0.0
        self._appear: float = 0.0  # 0..1 opacity only (never scale)
        self._appear_target: float = 1.0
        self._check_progress: float = 0.0
        self._content_fade: float = 1.0  # brief content settle after state change

        # Outer window padding (shadow bleed) + pill geometry
        self._pad = 16
        self._pill_h = 42
        self._radius = 11

        # Inner content rhythm (premium spacing scale)
        self._inset_x = 11          # left/right margin inside the pill
        self._rail_w = 2.0          # status accent rail on the left
        self._rail_gap = 8          # rail → glyph
        self._glyph_slot = 18       # fixed icon / waveform column
        self._glyph_text_gap = 8    # air between icon and label
        self._text_optical_y = -0.5 # fonts sit slightly heavy; nudge up

        # Width is measured from real font metrics so "Listening · AI" is a perfect fit
        self._pill_w = 160  # temporary until font is ready
        self._canvas_w = self._pill_w + self._pad * 2
        self._canvas_h = self._pill_h + self._pad * 2

        self._setup_window()
        self._setup_font()
        self._fit_pill_to_labels()

        self._wake.connect(self._sync_from_app, Qt.ConnectionType.QueuedConnection)
        self._hide_req.connect(self._do_hide, Qt.ConnectionType.QueuedConnection)

        self._tick = QTimer(self)
        self._tick.setInterval(16)  # ~60 FPS
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        # Boot visible
        self.last_app_state = getattr(app, "state", None)
        self._appear_target = 1.0
        self._appear = 0.0
        self.show()
        self._apply_win32_exstyles()
        self.raise_()

    # ------------------------------------------------------------------ window
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
        # Prefer modern system UI faces; fall back gracefully.
        families = set(QFontDatabase.families())
        for name in (
            "Segoe UI Variable Display",
            "Segoe UI Variable",
            "Segoe UI",
            "Inter",
            "SF Pro Display",
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
        # Slight tracking keeps short labels from feeling cramped
        self._font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 102)

        self._font_small = QFont(family, 9)
        self._font_small.setWeight(QFont.Weight.Normal)

    def _fit_pill_to_labels(self) -> None:
        """Size pill width so the longest label (e.g. Listening · AI) fits exactly.

        chrome = rail + glyph column + gaps + side insets
        pill_w = chrome + max(label widths) + 2px hairline breathing room
        """
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(self._font)
        max_text = 0
        for label in _all_status_labels():
            max_text = max(max_text, fm.horizontalAdvance(label))

        # Must match _paint_content layout math:
        # text starts at: 6 + rail_w + rail_gap + glyph_slot + glyph_text_gap
        # text ends at:   pill_w - inset_x
        left_chrome = 6.0 + self._rail_w + self._rail_gap + self._glyph_slot + self._glyph_text_gap
        right_chrome = float(self._inset_x)
        self._pill_w = int(math.ceil(left_chrome + max_text + right_chrome + 2.0))
        self._canvas_w = self._pill_w + self._pad * 2
        self._canvas_h = self._pill_h + self._pad * 2
        self.setFixedSize(self._canvas_w, self._canvas_h)
        self._reposition_bottom_center()

    def _reposition_bottom_center(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self._canvas_w) // 2
        y = geo.y() + geo.height() - self._canvas_h - 56
        self.move(x, y)

    def _apply_win32_exstyles(self) -> None:
        """Keep the HUD topmost and non-activating without breaking Qt alpha.

        Important: do NOT force WS_EX_LAYERED or WS_EX_TRANSPARENT here.
        Qt already owns layered/translucent composition; OR-ing those flags
        via SetWindowLong often makes the window fully invisible on Windows.
        """
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            user32 = ctypes.windll.user32

            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # Ensure no-activate + toolwindow; strip flags that fight Qt painting.
            style = (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_TRANSPARENT
            # Leave WS_EX_LAYERED alone if Qt set it; do not add it ourselves blindly.
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

            # Pin above other windows without activating / stealing focus.
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception as e:
            print(f"Warning: could not apply Win32 exstyles: {e}")

    # -------------------------------------------------------------- public API
    def notify_state_changed(self) -> None:
        """Thread-safe: request a UI refresh on the Qt main thread."""
        self._wake.emit()

    def hide_indicator(self) -> None:
        """Fade the HUD out (thread-safe; may be called from worker threads)."""
        self._hide_req.emit()

    def _do_hide(self) -> None:
        self._appear_target = 0.0
        self.gui_state = GuiState.HIDDEN
        # Consume one-shot outcome status so a later notify cannot re-flash Pasted/Error.
        try:
            from app_state import AppState

            if getattr(self.app, "state", None) == AppState.IDLE and getattr(
                self.app, "last_status", None
            ) in ("success", "error", "empty"):
                self.app.last_status = None
        except Exception:
            pass
        # Actual withdraw happens when appear ~ 0 in _on_tick

    def start(self) -> None:
        """Run the Qt event loop (blocks until quit)."""
        try:
            self._qt_app.exec()
        except KeyboardInterrupt:
            pass

    # ----------------------------------------------------------------- sync
    def _sync_from_app(self) -> None:
        # Import from app_state (NOT main) — see app_state.py docstring.
        from app_state import AppState

        app_state = self.app.state
        use_llm = bool(getattr(self.app, "use_llm", False))
        last_status = getattr(self.app, "last_status", None)
        ready = bool(getattr(self.app, "ready", False))
        prev_app_state = self.last_app_state

        target: Optional[GuiState] = None

        if app_state == AppState.RECORDING:
            target = GuiState.RECORDING
        elif app_state == AppState.PROCESSING:
            target = GuiState.PROCESSING
        elif app_state == AppState.IDLE:
            # Outcome flash only when the pipeline just finished (PROCESSING → IDLE),
            # not on every idle notify while last_status is still sticky.
            came_from_processing = prev_app_state == AppState.PROCESSING
            if not ready and last_status is None:
                target = GuiState.BOOTING
            elif last_status == "success" and came_from_processing:
                target = GuiState.SUCCESS
            elif last_status in ("error", "empty") and (
                came_from_processing or not ready
            ):
                # not-ready + error covers fatal init failures before first capture
                target = GuiState.ERROR
            else:
                target = GuiState.HIDDEN
        else:
            # Unknown / mismatched state type — compare by name as a safety net
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

        self.last_app_state = app_state
        self.update()

    def _transition_to(self, new_state: GuiState) -> None:
        self._hide_timer.stop()

        if new_state == GuiState.HIDDEN:
            self._appear_target = 0.0
            self.gui_state = GuiState.HIDDEN
            return

        prev = self.gui_state
        self.gui_state = new_state
        self._t = 0.0
        if new_state in (GuiState.SUCCESS, GuiState.ERROR):
            self._check_progress = 0.0

        # Hotkey states: full size immediately — no scale, no tiny first frame.
        if new_state in (GuiState.RECORDING, GuiState.PROCESSING):
            self._appear = 1.0
            self._appear_target = 1.0
            # Content starts mostly visible; only a whisper of fade (never undersized type)
            self._content_fade = 0.72
        elif new_state == GuiState.BOOTING:
            self._appear = max(self._appear, 0.9)
            self._appear_target = 1.0
            self._content_fade = 0.8
        elif prev == GuiState.HIDDEN or self._appear < 0.2:
            self._appear = 0.9
            self._appear_target = 1.0
            self._content_fade = 0.75
        else:
            self._appear_target = 1.0
            self._content_fade = 0.85

        self._reposition_bottom_center()
        self.setWindowOpacity(1.0)
        self.show()
        self._apply_win32_exstyles()
        self.raise_()

        if new_state == GuiState.SUCCESS:
            self._hide_timer.start(1200)
        elif new_state == GuiState.ERROR:
            self._hide_timer.start(1500)

        print(f"[HUD] → {new_state.name}", flush=True)

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        self._t += 0.016

        # Poll app state every frame as a reliability backup.
        # Keyboard hooks run off-thread; if a queued signal is delayed/missed,
        # this still keeps the HUD in lockstep with recording.
        try:
            app_state = getattr(self.app, "state", None)
            if app_state is not None and app_state != self.last_app_state:
                self._sync_from_app()
        except Exception:
            pass

        # Opacity only — never scale (scaling made the first frame look tiny/blurry)
        self._appear += (self._appear_target - self._appear) * 0.35
        if self._appear_target == 0.0 and self._appear < 0.02:
            self._appear = 0.0
            if self.isVisible() and self.gui_state == GuiState.HIDDEN:
                self.hide()

        # Content fade settles quickly (opacity), full size the whole time
        if self._content_fade < 1.0:
            self._content_fade = min(1.0, self._content_fade + 0.18)

        # Live mic level → waveform
        level = 0.0
        recorder = getattr(self.app, "recorder", None)
        if recorder is not None and self.gui_state == GuiState.RECORDING:
            try:
                level = float(recorder.get_level())
            except Exception:
                level = 0.0
        self._level_smooth += (level - self._level_smooth) * 0.32

        # Rolling waveform samples (smooth ribbon)
        n = len(self._wave)
        for i in range(n):
            # Traveling phase so the line feels alive even at low volume
            phase = self._t * 5.2 + i * (math.pi * 2.0 / n) * 1.8
            ambient = 0.14 + 0.08 * math.sin(phase)
            speech = self._level_smooth * (0.55 + 0.45 * abs(math.sin(phase * 1.1)))
            if self.gui_state == GuiState.RECORDING:
                target = min(1.0, ambient * 0.55 + speech)
            else:
                target = 0.08 + 0.04 * abs(math.sin(self._t * 1.6 + i * 0.2))
            self._wave[i] += (target - self._wave[i]) * 0.28

        if self.gui_state in (GuiState.SUCCESS, GuiState.ERROR):
            self._check_progress = min(1.0, self._check_progress + 0.08)

        if self._appear > 0.01 or self._appear_target > 0:
            self.update()

    # ----------------------------------------------------------------- paint
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._appear <= 0.001:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Opacity fade only — pill is always full size (crisp type from frame 1)
        p.setOpacity(max(0.0, min(1.0, self._appear)))

        pill = QRectF(self._pad, self._pad, self._pill_w, self._pill_h)
        accent = self._accent_color()

        self._paint_shadow(p, pill)
        self._paint_surface(p, pill)
        self._paint_accent_rail(p, pill, accent)
        self._paint_border(p, pill)

        # Content uses a separate soft fade (no geometry change)
        p.save()
        p.setOpacity(max(0.0, min(1.0, self._appear * self._content_fade)))
        self._paint_content(p, pill, accent)
        p.restore()

        p.end()

    def _ease_out_cubic(self, t: float) -> float:
        t = max(0.0, min(1.0, t))
        return 1.0 - (1.0 - t) ** 3

    def _accent_color(self) -> QColor:
        use_llm = bool(getattr(self.app, "use_llm", False))
        if self.gui_state == GuiState.BOOTING:
            return _Theme.boot
        if self.gui_state == GuiState.RECORDING:
            return _Theme.ai if use_llm else _Theme.record
        if self.gui_state == GuiState.PROCESSING:
            return _Theme.ai if use_llm else _Theme.process
        if self.gui_state == GuiState.SUCCESS:
            return _Theme.success
        if self.gui_state == GuiState.ERROR:
            return _Theme.error
        return _Theme.process

    def _paint_shadow(self, p: QPainter, pill: QRectF) -> None:
        # Soft neutral drop only — no colored bloom
        for dy, expand, alpha in ((6, 3, 28), (3, 1, 40), (1, 0, 50)):
            r = QRectF(pill).adjusted(-expand, dy * 0.3, expand, dy)
            path = QPainterPath()
            path.addRoundedRect(r, self._radius + 1, self._radius + 1)
            p.fillPath(path, QColor(0, 0, 0, alpha))

    def _paint_surface(self, p: QPainter, pill: QRectF) -> None:
        """Near-flat charcoal fill — barely any vertical shift."""
        path = QPainterPath()
        path.addRoundedRect(pill, self._radius, self._radius)

        # Almost solid; tiny top lift so it doesn't look dead-flat plastic
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0.0, _Theme.surface_edge)
        grad.setColorAt(1.0, _Theme.surface)
        p.fillPath(path, QBrush(grad))

    def _paint_border(self, p: QPainter, pill: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(
            pill.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius
        )
        pen = QPen(_Theme.border)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _paint_accent_rail(self, p: QPainter, pill: QRectF, accent: QColor) -> None:
        """Thin left status rail — quiet identity mark that reacts to mic level."""
        rail_x = pill.x() + 6.0
        rail_h = pill.height() - 14.0
        rail_y = pill.y() + (pill.height() - rail_h) * 0.5
        # Clip rail into rounded pill so ends stay soft
        clip = QPainterPath()
        clip.addRoundedRect(pill, self._radius, self._radius)
        p.save()
        p.setClipPath(clip)

        # Soft pulse from mic while recording; gentle breath otherwise
        if self.gui_state == GuiState.RECORDING:
            breath = 0.55 + 0.45 * self._level_smooth
        elif self.gui_state == GuiState.PROCESSING:
            breath = 0.55 + 0.25 * abs(math.sin(self._t * 2.4))
        else:
            breath = 0.7

        c = QColor(accent)
        c.setAlpha(int(70 + 130 * breath))
        rect = QRectF(rail_x, rail_y, self._rail_w, rail_h)
        path = QPainterPath()
        path.addRoundedRect(rect, self._rail_w / 2, self._rail_w / 2)
        p.fillPath(path, c)

        # Tiny highlight core
        core = QColor(255, 255, 255, int(18 + 22 * breath))
        core_rect = QRectF(rail_x + 0.6, rail_y + 2, max(0.8, self._rail_w - 1.2), rail_h - 4)
        core_path = QPainterPath()
        core_path.addRoundedRect(core_rect, 0.6, 0.6)
        p.fillPath(core_path, core)
        p.restore()

    def _paint_content(self, p: QPainter, pill: QRectF, accent: QColor) -> None:
        """Lay out icon + label with a consistent spacing rhythm.

        [ rail | rail_gap | glyph slot | gap | label .............. | inset ]
        """
        inset = self._inset_x
        slot = self._glyph_slot
        gap = self._glyph_text_gap
        left = pill.x() + 6.0 + self._rail_w + self._rail_gap

        glyph_cx = left + slot * 0.5
        glyph_cy = pill.center().y() + self._text_optical_y

        if self.gui_state == GuiState.RECORDING:
            self._draw_waveform(p, glyph_cx, glyph_cy, accent)
        elif self.gui_state == GuiState.PROCESSING:
            self._draw_orbit_dots(p, glyph_cx, glyph_cy, accent)
        elif self.gui_state == GuiState.BOOTING:
            self._draw_orbit_dots(p, glyph_cx, glyph_cy, accent, slow=True)
        elif self.gui_state == GuiState.SUCCESS:
            self._draw_check(p, glyph_cx, glyph_cy, accent)
        elif self.gui_state == GuiState.ERROR:
            self._draw_error(p, glyph_cx, glyph_cy, accent)
        else:
            self._draw_dot(p, glyph_cx, glyph_cy, accent)

        # Hairline divider between glyph and type
        div_x = left + slot + gap * 0.35
        div_pen = QPen(QColor(255, 255, 255, 16))
        div_pen.setWidthF(1.0)
        p.setPen(div_pen)
        mid = pill.center().y()
        p.drawLine(QPointF(div_x, mid - 7), QPointF(div_x, mid + 7))

        use_llm = bool(getattr(self.app, "use_llm", False))
        last_status = getattr(self.app, "last_status", None)
        label = status_label(self.gui_state, use_llm, last_status)

        p.setFont(self._font)
        p.setPen(_Theme.text)

        text_left = left + slot + gap
        text_right = pill.right() - inset
        fm = p.fontMetrics()
        cap = (
            float(fm.capHeight())
            if hasattr(fm, "capHeight")
            else float(fm.ascent() * 0.7)
        )
        text_cy = pill.center().y() + self._text_optical_y
        band = max(cap + 8.0, float(fm.height()))
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

    def _draw_waveform(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        """Smooth live ribbon — more expressive than discrete bars, still quiet."""
        n = len(self._wave)
        width = self._glyph_slot - 1.0
        x0 = cx - width * 0.5
        amp = 5.5 + 4.5 * self._level_smooth

        path = QPainterPath()
        for i, v in enumerate(self._wave):
            x = x0 + (i / max(1, n - 1)) * width
            # Blend sample with a traveling sine for fluid motion
            phase = self._t * 5.2 + i * 0.35
            y = cy + math.sin(phase) * amp * (0.35 + 0.65 * v)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        pen = QPen(accent)
        pen.setWidthF(1.35)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        # Soften alpha with level so quiet speech stays elegant
        c = QColor(accent)
        c.setAlpha(int(140 + 90 * self._level_smooth))
        pen.setColor(c)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # Very soft echo stroke underneath for depth
        echo = QPainterPath()
        for i, v in enumerate(self._wave):
            x = x0 + (i / max(1, n - 1)) * width
            phase = self._t * 5.2 + i * 0.35 + 0.4
            y = cy + math.sin(phase) * amp * 0.55 * (0.35 + 0.65 * v)
            if i == 0:
                echo.moveTo(x, y)
            else:
                echo.lineTo(x, y)
        echo_pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 40))
        echo_pen.setWidthF(1.0)
        p.setPen(echo_pen)
        p.drawPath(echo)

    def _draw_orbit_dots(
        self, p: QPainter, cx: float, cy: float, accent: QColor, slow: bool = False
    ) -> None:
        """Three orbiting dots — calmer and more distinctive than a thick spinner."""
        speed = 1.6 if slow else 2.8
        angle0 = self._t * speed
        radius = 6.2
        for i in range(3):
            a = angle0 + i * (math.pi * 2.0 / 3.0)
            x = cx + math.cos(a) * radius
            y = cy + math.sin(a) * radius
            # Leading dot slightly brighter
            alpha = 90 + int(120 * ((i + 1) / 3.0))
            c = QColor(accent)
            c.setAlpha(alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            r = 1.5 + (0.35 if i == 2 else 0.0)
            p.drawEllipse(QPointF(x, y), r, r)

    def _draw_check(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        t = self._ease_out_cubic(self._check_progress)
        pen = QPen(accent)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setColor(
            QColor(
                accent.red(),
                accent.green(),
                accent.blue(),
                int(240 * min(1.0, t * 1.2)),
            )
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if t > 0.15:
            prog = min(1.0, (t - 0.15) / 0.85)
            # Centered in the glyph slot
            p1 = QPointF(cx - 3.6, cy + 0.3)
            p2 = QPointF(cx - 0.9, cy + 2.9)
            p3 = QPointF(cx + 4.2, cy - 3.0)
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
            QColor(accent.red(), accent.green(), accent.blue(), int(230 * min(1.0, t)))
        )
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if t > 0.2:
            s = 3.1
            p.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            p.drawLine(QPointF(cx + s, cy - s), QPointF(cx - s, cy + s))

    def _draw_dot(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QPointF(cx, cy), 2.8, 2.8)
