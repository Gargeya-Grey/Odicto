"""Linux platform backend.

Reuses the ``keyboard``-based hotkey backend and POSIX process/lock helpers.
Global suppression requires root (or ``input`` group membership depending on
the distribution); see README for details.
"""

from __future__ import annotations

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


def apply_window_exstyles(widget) -> None:
    # Qt window flags handle topmost/click-through on X11/Wayland.
    return None


def hotkey_backend_name() -> str:
    return "keyboard"
