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
import os
import sys
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


def status_label(state: GuiState, use_llm: bool = False, last_status: Optional[str] = None) -> str:
    """Pure label map (unit-testable without Qt paint)."""
    if state == GuiState.BOOTING:
        return "Starting"
    if state == GuiState.RECORDING:
        return "Listening"
    if state == GuiState.PROCESSING:
        return "Thinking" if use_llm else "Transcribing"
    if state == GuiState.SUCCESS:
        return "Done"
    if state == GuiState.ERROR:
        if last_status == "empty":
            return "No speech"
        return "Failed"
    if state == GuiState.RESET:
        return "Context cleared"
    return ""


def _show_ai_chip(state: GuiState, use_llm: bool) -> bool:
    return use_llm and state in (GuiState.RECORDING, GuiState.PROCESSING)


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
        status_label(GuiState.RESET),
    ]


class DictationIndicator(QWidget):
    """Bottom-center always-on-top glass HUD for the dictation service."""

    # Thread-safe wake-ups from keyboard / worker threads
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

        self.app = app
        self.gui_state: GuiState = GuiState.BOOTING
        self.last_app_state: Any = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_indicator)

        # Animation clock
        self._t: float = 0.0
        self._bars: list[float] = [0.16] * 5  # quiet equalizer
        self._level_smooth: float = 0.0
        self._appear: float = 0.0  # 0..1 opacity only (never scale)
        self._appear_target: float = 1.0
        self._rise: float = 0.0    # 0..1 rise offset for the entrance (no scale)
        self._check_progress: float = 0.0
        self._content_fade: float = 1.0
        self._glyph_fade: float = 1.0  # 0..1 cross-fade between state glyphs
        self._last_glyph_state: GuiState = GuiState.BOOTING

        # --- Geometry (8px rhythm) ---
        self._pad = 14                 # shadow bleed only (no glow halo)
        self._pill_h = 44
        self._radius = 22              # capsule ends
        self._inset_x = 14
        self._glyph_slot = 20
        self._gap_glyph_text = _S
        self._gap_text_chip = 8
        self._chip_h = 20
        self._chip_pad_x = 7
        self._text_optical_y = -0.5

        self._pill_w = 170
        self._canvas_w = self._pill_w + self._pad * 2
        self._canvas_h = self._pill_h + self._pad * 2

        self._setup_window()
        self._setup_font()
        self._fit_pill_to_labels()
        self._setup_tray()

        self._wake.connect(self._sync_from_app, Qt.ConnectionType.QueuedConnection)
        self._hide_req.connect(self._do_hide, Qt.ConnectionType.QueuedConnection)
        self._reset_flash.connect(
            self._do_flash_reset, Qt.ConnectionType.QueuedConnection
        )

        self._tick = QTimer(self)
        self._tick.setInterval(16)  # ~60 FPS
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self.last_app_state = getattr(app, "state", None)
        self._appear_target = 1.0
        self._appear = 0.0
        # Always paint during the appear fade-in, then pause once hidden.
        self._tick_active = True
        self.show()
        self._apply_win32_exstyles()
        self.raise_()

    # ------------------------------------------------------------------ window
    def _setup_tray(self) -> None:
        """Minimal tray/status menu for macOS/Linux background control.

        Kept deliberately light and optional — if the system has no tray
        (some Linux desktops), the HUD-only behavior is unchanged.
        """
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QIcon()
        pixmap = self._make_tray_icon()
        if pixmap.isNull():
            return
        icon.addPixmap(pixmap)
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

    def _make_tray_icon(self):
        from PySide6.QtGui import QPixmap

        pm = QPixmap(24, 24)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(108, 168, 128))
        painter.drawRoundedRect(QRectF(4, 4, 16, 16), 5, 5)
        painter.end()
        return pm

    def _open_setup_page(self) -> None:
        try:
            import subprocess
            import sys

            subprocess.Popen(
                [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "odicto.py"), "setup"],
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

    def _chip_width(self) -> float:
        fm = QFontMetrics(self._font_chip)
        return float(fm.horizontalAdvance("AI") + self._chip_pad_x * 2)

    def _fit_pill_to_labels(self) -> None:
        """Size pill so longest label (+ optional AI chip) fits with consistent chrome."""
        fm = QFontMetrics(self._font)
        max_text = 0
        for label in _all_status_labels():
            max_text = max(max_text, fm.horizontalAdvance(label))

        # [ inset | glyph | gap | text | (gap+chip)? | inset ]
        left = float(self._inset_x + self._glyph_slot + self._gap_glyph_text)
        right = float(self._inset_x)
        chip = self._gap_text_chip + self._chip_width()
        # Always reserve chip width so AI ↔ raw doesn't resize the capsule mid-session.
        # Shave 8% off the auto-fit width for a tighter, quieter capsule. The longest
        # label still fits because the 4px padding + glyph gap absorb the difference.
        self._pill_w = int(math.ceil((left + max_text + chip + right + 4.0) * 0.92))
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
        y = geo.y() + geo.height() - self._canvas_h - 48
        self.move(x, y)

    def _apply_win32_exstyles(self) -> None:
        """Apply platform-specific window styles (topmost/non-activating)."""
        apply_window_exstyles(self)

    # -------------------------------------------------------------- public API
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
        self.gui_state = GuiState.RESET
        self._appear_target = 1.0
        self._appear = max(self._appear, 0.9)
        self._content_fade = 0.8
        self._reposition_bottom_center()
        self.setWindowOpacity(1.0)
        self.show()
        self._apply_win32_exstyles()
        self.raise_()
        self._hide_timer.start(1200)
        self._sync_tick()
        print("[HUD] Context cleared", flush=True)

    def _do_hide(self) -> None:
        self._appear_target = 0.0
        self.gui_state = GuiState.HIDDEN
        self._sync_tick()
        try:
            from app_state import AppState

            if getattr(self.app, "state", None) == AppState.IDLE and getattr(
                self.app, "last_status", None
            ) in ("success", "error", "empty"):
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

        app_state = self.app.state
        last_status = getattr(self.app, "last_status", None)
        ready = bool(getattr(self.app, "ready", False))
        prev_app_state = self.last_app_state

        target: Optional[GuiState] = None

        if app_state == AppState.RECORDING:
            target = GuiState.RECORDING
        elif app_state == AppState.PROCESSING:
            target = GuiState.PROCESSING
        elif app_state == AppState.IDLE:
            came_from_processing = prev_app_state == AppState.PROCESSING
            if not ready and last_status is None:
                target = GuiState.BOOTING
            elif last_status == "success" and came_from_processing:
                target = GuiState.SUCCESS
            elif last_status in ("error", "empty") and (
                came_from_processing or not ready
            ):
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

        self.last_app_state = app_state
        self.update()

    def _transition_to(self, new_state: GuiState) -> None:
        self._hide_timer.stop()

        if new_state == GuiState.HIDDEN:
            self._appear_target = 0.0
            self.gui_state = GuiState.HIDDEN
            self._sync_tick()
            return

        prev = self.gui_state
        self.gui_state = new_state
        self._t = 0.0
        if new_state in (GuiState.SUCCESS, GuiState.ERROR):
            self._check_progress = 0.0
        if new_state == GuiState.RESET:
            self._hide_timer.start(1200)

        # Cross-fade the glyph when the drawn state changes (not on same-state noise).
        if new_state != prev:
            self._glyph_fade = 0.0
        self._rise = 0.0  # entrance rise runs on every transition in

        if new_state in (GuiState.RECORDING, GuiState.PROCESSING):
            self._appear = 1.0
            self._appear_target = 1.0
            self._content_fade = 0.7
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

        if new_state in (GuiState.SUCCESS, GuiState.ERROR):
            self._hide_timer.start(1200 if new_state == GuiState.SUCCESS else 1500)
        elif new_state == GuiState.RESET:
            self._hide_timer.start(1200)

        self._sync_tick()
        print(f"[HUD] -> {new_state.name}", flush=True)

    def _sync_tick(self) -> None:
        """Pause the 60 FPS paint timer while fully hidden; resume otherwise.

        The timer also stays active during a fade-out (HIDDEN with appear > 0)
        so the last frames render, then it stops at the actual hide().
        """
        active = (
            self.gui_state != GuiState.HIDDEN
            or self._appear > 0.02
            or self._appear_target > 0.0
        )
        if active and not self._tick.isActive():
            self._tick.start()
        elif not active and self._tick.isActive():
            self._tick.stop()

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        self._t += 0.016

        try:
            app_state = getattr(self.app, "state", None)
            if app_state is not None and app_state != self.last_app_state:
                self._sync_from_app()
        except Exception:
            pass

        self._appear += (self._appear_target - self._appear) * 0.35
        if self._appear_target == 0.0 and self._appear < 0.02:
            self._appear = 0.0
            if self.isVisible() and self.gui_state == GuiState.HIDDEN:
                self.hide()
            if self.gui_state == GuiState.HIDDEN:
                self._sync_tick()

        # Entrance rise — quick settle, no scale (avoids blur on translucent windows).
        self._rise = min(1.0, self._rise + 0.14)
        # Glyph cross-fade — ~150ms to full.
        self._glyph_fade = min(1.0, self._glyph_fade + 0.2)

        # Processing fill clock — a full ring represents one "working" sweep.
        if self.gui_state == GuiState.PROCESSING:
            self._proc_fill = (getattr(self, "_proc_fill", 0.0) + 0.016 / 2.5) % 1.0
            if self._proc_fill < 0.001:
                pass  # loop: indeterminate working sweep

        if self._content_fade < 1.0:
            self._content_fade = min(1.0, self._content_fade + 0.16)

        level = 0.0
        recorder = getattr(self.app, "recorder", None)
        if recorder is not None and self.gui_state == GuiState.RECORDING:
            try:
                level = float(recorder.get_level())
            except Exception:
                level = 0.0
        self._level_smooth += (level - self._level_smooth) * 0.28

        n = len(self._bars)
        for i in range(n):
            # Center bars taller; traveling phase keeps idle motion alive
            mid = abs(i - (n - 1) / 2.0) / max(1.0, (n - 1) / 2.0)
            shape = 1.0 - 0.35 * mid
            phase = self._t * 3.2 + i * 0.65
            ambient = 0.12 + 0.08 * abs(math.sin(phase))
            speech = self._level_smooth * (0.55 + 0.45 * abs(math.sin(phase * 1.05)))
            if self.gui_state == GuiState.RECORDING:
                target = min(1.0, (ambient * 0.4 + speech) * shape)
            else:
                target = (0.08 + 0.04 * abs(math.sin(self._t * 1.4 + i * 0.35))) * shape
            self._bars[i] += (target - self._bars[i]) * 0.26

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
        p.setOpacity(max(0.0, min(1.0, self._appear)))

        pill = QRectF(self._pad, self._pad, self._pill_w, self._pill_h)
        accent = self._accent_color()
        use_llm = bool(getattr(self.app, "use_llm", False))

        self._paint_shadow(p, pill)
        self._paint_surface(p, pill)
        self._paint_border(p, pill)
        self._paint_top_rim(p, pill)

        # Entrance rise: translate the content up as it fades in (no scale → no blur).
        rise_px = (1.0 - self._ease_out_cubic(self._rise)) * 4.0
        p.save()
        p.translate(0.0, rise_px)
        p.setOpacity(max(0.0, min(1.0, self._appear * self._content_fade)))
        self._paint_content(p, pill, accent, use_llm)
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
        if self.gui_state == GuiState.RESET:
            return _Theme.ai
        return _Theme.process

    def _paint_shadow(self, p: QPainter, pill: QRectF) -> None:
        """Neutral drop — slightly grounded so the capsule reads on bright screens."""
        for dy, expand, alpha in ((6, 3, 40), (3, 0, 52)):
            r = QRectF(pill).adjusted(-expand, dy * 0.35, expand, dy)
            path = QPainterPath()
            path.addRoundedRect(r, self._radius + 1, self._radius + 1)
            p.fillPath(path, QColor(0, 0, 0, alpha))

    def _paint_surface(self, p: QPainter, pill: QRectF) -> None:
        path = QPainterPath()
        path.addRoundedRect(pill, self._radius, self._radius)
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0.0, _Theme.surface_top)
        grad.setColorAt(1.0, _Theme.surface_bot)
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

    def _paint_top_rim(self, p: QPainter, pill: QRectF) -> None:
        """Faint top edge light — barely there."""
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(pill, self._radius, self._radius)
        p.setClipPath(clip)
        rim = QRectF(pill.left() + 10, pill.top() + 1.0, pill.width() - 20, 1.0)
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
        """[ inset | glyph | gap | label .............. | chip? | inset ]"""
        left = pill.x() + self._inset_x
        glyph_cx = left + self._glyph_slot * 0.5
        glyph_cy = pill.center().y() + self._text_optical_y

        if self._glyph_fade < 1.0:
            # Cross-fade: draw the previous state's glyph beneath at low opacity.
            p.save()
            p.setOpacity(max(0.0, 1.0 - self._glyph_fade))
            self._draw_glyph(p, self._last_glyph_state, glyph_cx, glyph_cy, accent)
            p.restore()
        self._draw_glyph(p, self.gui_state, glyph_cx, glyph_cy, accent)
        self._last_glyph_state = self.gui_state

        # Hairline divider
        div_x = left + self._glyph_slot + self._gap_glyph_text * 0.4
        mid = pill.center().y()
        div_pen = QPen(_Theme.divider)
        div_pen.setWidthF(1.0)
        p.setPen(div_pen)
        p.drawLine(QPointF(div_x, mid - 8), QPointF(div_x, mid + 8))

        last_status = getattr(self.app, "last_status", None)
        label = status_label(self.gui_state, use_llm, last_status)

        chip_on = _show_ai_chip(self.gui_state, use_llm)
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

    def _draw_ai_chip(
        self, p: QPainter, x: float, y: float, w: float, h: float
    ) -> None:
        """Neutral glass chip — no purple fill."""
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
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "AI")

    def _draw_eq_bars(self, p: QPainter, cx: float, cy: float, accent: QColor) -> None:
        """Slim rounded bars — calm, not nightclub equalizer."""
        n = len(self._bars)
        slot = self._glyph_slot - 2.0
        bar_w = 2.0
        gap = (slot - n * bar_w) / max(1, n - 1)
        x0 = cx - slot * 0.5
        max_h = 11.0

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
