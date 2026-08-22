"""Shared hotkey/clipboard backend built on the ``keyboard`` library.

Used by the Windows and Linux backends. macOS uses ``pynput`` instead and
therefore does not import this module.
"""

from __future__ import annotations

import sys
import time
from typing import Tuple

try:
    import keyboard
except ImportError:  # Linux requires root/input group; keep import safe for tests
    keyboard = None

KEY_DOWN = keyboard.KEY_DOWN if keyboard is not None else "down"
KEY_UP = keyboard.KEY_UP if keyboard is not None else "up"


def _require_keyboard():
    if keyboard is None:
        raise RuntimeError(
            "keyboard library is unavailable on this platform without root or "
            "input-group access. On Linux, run as root or add your user to the "
            "'input' group."
        )
    return keyboard

# The `keyboard` library aliases side-specific modifiers onto both sides
# (e.g. "right ctrl" scan codes include left ctrl's 29). That makes
# is_pressed("right ctrl") true whenever *either* Ctrl is down.
_SIDE_COUNTERPARTS = {
    "right ctrl": "left ctrl",
    "right control": "left ctrl",
    "left ctrl": "right ctrl",
    "left control": "right ctrl",
    "right shift": "left shift",
    "left shift": "right shift",
    "right alt": "left alt",
    "left alt": "right alt",
}

_MODIFIER_KEYS = (
    "ctrl",
    "shift",
    "alt",
    "left ctrl",
    "right ctrl",
    "left shift",
    "right shift",
    "left alt",
    "right alt",
    "left windows",
    "right windows",
)


def is_pressed(key: str) -> bool:
    kb = keyboard
    if kb is None:
        return False
    try:
        return bool(kb.is_pressed(key))
    except Exception:
        return False


def side_exclusive_scan_codes(key: str) -> Tuple[int, ...]:
    kb = keyboard
    if kb is None:
        return ()
    key_n = key.strip().lower()
    try:
        codes = set(kb.key_to_scan_codes(key_n))
    except Exception:
        return ()
    other = _SIDE_COUNTERPARTS.get(key_n)
    if other is None:
        return tuple(codes)
    try:
        other_codes = set(kb.key_to_scan_codes(other))
    except Exception:
        return tuple(codes)
    exclusive = codes - other_codes
    return tuple(exclusive if exclusive else codes)


def is_pressed_exclusive(key: str) -> bool:
    try:
        codes = side_exclusive_scan_codes(key)
        if not codes:
            return is_pressed(key)
        return any(is_pressed(code) for code in codes)
    except Exception:
        return is_pressed(key)


def hook_key(key: str, handler, suppress: bool) -> None:
    _require_keyboard().hook_key(key, handler, suppress=suppress)


def unhook_all() -> None:
    try:
        if keyboard is not None:
            keyboard.unhook_all()
    except Exception:
        pass


def wait() -> None:
    _require_keyboard().wait()


def press(key: str) -> None:
    _require_keyboard().press(key)


def release(key: str) -> None:
    _require_keyboard().release(key)


def press_and_release(key: str) -> None:
    _require_keyboard().press_and_release(key)


def send(chord: str) -> None:
    _require_keyboard().send(chord)


def force_release_modifiers() -> None:
    """Synthesize key-ups for modifiers that may still be physically held."""
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # Neutralize Windows Alt menu activation (SC_KEYMENU) so the active window
            # does not steal focus to the File/Edit menu bar when Alt is released.
            VK_NONAME = 0xFC
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_NONAME, 0, 0, 0)
            user32.keybd_event(VK_NONAME, 0, KEYEVENTF_KEYUP, 0)

            # Direct Win32 keybd_event key-up events for all modifier virtual keys.
            # Instantaneous (<0.05ms) and clears physical/logical modifier states.
            VK_MODIFIERS = (
                0x11,  # VK_CONTROL
                0xA2,  # VK_LCONTROL
                0xA3,  # VK_RCONTROL
                0x10,  # VK_SHIFT
                0xA0,  # VK_LSHIFT
                0xA1,  # VK_RSHIFT
                0x12,  # VK_MENU (Alt)
                0xA4,  # VK_LMENU (Left Alt)
                0xA5,  # VK_RMENU (Right Alt)
                0x5B,  # VK_LWIN (Left Windows/Cmd)
                0x5C,  # VK_RWIN (Right Windows/Cmd)
            )
            for vk in VK_MODIFIERS:
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.01)
            return
        except Exception:
            pass

    kb = keyboard
    if kb is None:
        return
    for key in _MODIFIER_KEYS:
        try:
            if kb.is_pressed(key):
                kb.release(key)
        except Exception:
            try:
                kb.release(key)
            except Exception:
                pass
    time.sleep(0.02)


def wm_copy_foreground() -> bool:
    """Windows WM_COPY path; a no-op on Linux."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        WM_COPY = 0x0301
        user32.SendMessageW(hwnd, WM_COPY, 0, 0)
        return True
    except Exception:
        return False


def copy_chord() -> None:
    press("ctrl")
    time.sleep(0.01)
    press_and_release("c")
    time.sleep(0.01)
    release("ctrl")


def paste_chord() -> None:
    press("ctrl")
    time.sleep(0.01)
    press_and_release("v")
    time.sleep(0.01)
    release("ctrl")


def send_copy() -> None:
    try:
        copy_chord()
    except Exception:
        send("ctrl+c")


def send_paste() -> None:
    try:
        paste_chord()
    except Exception:
        send("ctrl+v")
