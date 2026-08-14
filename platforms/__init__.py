"""Cross-platform dispatch facade.

``main.py``, ``typer.py`` and ``indicator.py`` import platform behavior from
here instead of branching on ``sys.platform`` directly. The active backend is
selected once at import time.
"""

from __future__ import annotations

import sys

from platforms.base import clipboard_read, clipboard_write

if sys.platform == "win32":
    from platforms.windows import *  # noqa: F401,F403
    from platforms.windows import hotkey_backend_name
elif sys.platform == "darwin":
    from platforms.macos import *  # noqa: F401,F403
    from platforms.macos import hotkey_backend_name
else:
    from platforms.linux import *  # noqa: F401,F403
    from platforms.linux import hotkey_backend_name

__all__ = [
    "hotkey_backend_name",
    "clipboard_read",
    "clipboard_write",
    "is_pressed",
    "is_pressed_exclusive",
    "side_exclusive_scan_codes",
    "hook_key",
    "unhook_all",
    "wait",
    "send",
    "send_copy",
    "send_paste",
    "force_release_modifiers",
    "wm_copy_foreground",
    "apply_window_exstyles",
    "acquire_lock",
    "release_lock",
    "lock_is_held",
    "kill_other_odicto_processes",
    "spawn_detached",
    "terminate_process_tree",
]
