"""Linux platform backend.

Reuses the ``keyboard``-based hotkey backend and POSIX process/lock helpers.
Global suppression requires root (or ``input`` group membership depending on
the distribution); see README for details.
"""

from __future__ import annotations

import subprocess

from platforms._keyboard import *  # noqa: F401,F403
from platforms._posix import (  # noqa: F401,F403
    acquire_lock,
    enumerate_odicto_pids,
    kill_other_odicto_processes,
    kill_process_tree,
    lock_is_held,
    release_lock,
    spawn_detached,
    terminate_process_tree,
)


def _x11_active_window_pid() -> int:
    """PID owning the active X11 window, or 0 when it cannot be determined.

    Prefers ``xdotool`` and falls back to ``xprop``. Wayland compositors do not
    expose the active window's PID to clients, so both return nothing there.
    """
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0 or "#" not in result.stdout:
            return 0
        window_id = result.stdout.split("#")[-1].strip().rstrip(",").split()[0]
        result = subprocess.run(
            ["xprop", "-id", window_id, "_NET_WM_PID"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0 or "=" not in result.stdout:
            return 0
        pid_text = result.stdout.split("=")[-1].strip().rstrip(",")
        return int(pid_text) if pid_text.isdigit() else 0
    except Exception:
        return 0


def foreground_is_terminal(extra=()) -> bool:
    """True when the focused window is a terminal emulator (X11 only).

    Returns False when the active window cannot be resolved (Wayland without
    ``xdotool``/``xprop``), which degrades to the normal paste chord rather than
    typing into a window that may not be a terminal.
    """
    from platforms.base import is_terminal_identifier

    try:
        pid = _x11_active_window_pid()
        if not pid:
            return False
        import psutil

        name = psutil.Process(pid).name()
    except Exception:
        return False
    return is_terminal_identifier((name,), extra)


def apply_window_exstyles(widget) -> None:
    # Qt window flags handle topmost/click-through on X11/Wayland.
    return None


def hotkey_backend_name() -> str:
    return "keyboard"
