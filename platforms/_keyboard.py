"""Shared hotkey/clipboard backend built on the ``keyboard`` library.

Used by the Windows and Linux backends. macOS uses ``pynput`` instead and
therefore does not import this module.
"""

from __future__ import annotations

import os
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


def _win_foreground_window_info() -> Tuple[str, str]:
    """(window class, process image name) for the foreground window, lowercased."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""

    class_buf = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(hwnd, class_buf, 256):
        return "", ""
    window_class = class_buf.value.lower()

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return window_class, ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return window_class, ""

    try:
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if query is None:
            return window_class, ""
        query.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(1024)
        path_buf = ctypes.create_unicode_buffer(size.value)
        if not query(handle, 0, path_buf, ctypes.byref(size)):
            return window_class, ""
        return window_class, os.path.basename(path_buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def foreground_is_terminal(extra=()) -> bool:
    """True when the focused window is a terminal emulator.

    Terminals have no shared paste chord and treat Ctrl+C as SIGINT, so callers
    type the text instead. Linux overrides this in ``platforms.linux`` with an
    X11 lookup; ``extra`` is the user's EXTRA_TERMINAL_APPS list.
    """
    if sys.platform != "win32":
        return False
    try:
        info = _win_foreground_window_info()
    except Exception:
        return False
    from platforms.base import is_terminal_identifier

    return is_terminal_identifier(info, extra)


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


def send_copy_terminal() -> None:
    """Copy chord for a focused terminal — Ctrl+Shift+C, never plain Ctrl+C.

    Plain Ctrl+C is SIGINT: sending it would interrupt whatever is running
    instead of copying the terminal's selection.
    """
    try:
        press("ctrl")
        time.sleep(0.01)
        press("shift")
        time.sleep(0.01)
        press_and_release("c")
        time.sleep(0.01)
        release("shift")
        release("ctrl")
    except Exception:
        send("ctrl+shift+c")


def send_paste() -> None:
    try:
        paste_chord()
    except Exception:
        send("ctrl+v")


def send_backspaces(n: int) -> None:
    """Delete ``n`` characters before the caret. Batched on Windows."""
    n = min(max(0, int(n)), 4000)
    if n <= 0:
        return
    if sys.platform == "win32" and _win_send_backspaces(n):
        return
    kb = _require_keyboard()
    for _ in range(n):
        kb.press_and_release("backspace")


def send_text(text: str) -> bool:
    """Type ``text`` at the caret without touching the clipboard.

    Returns True if key events were injected. Callers must fall back to a
    clipboard paste when this returns False — some apps ignore Unicode SendInput.
    """
    if not text:
        return True
    if len(text) > 256:
        return False
    if sys.platform == "win32" and _win_send_text(text):
        return True
    try:
        kb = _require_keyboard()
        kb.write(text, delay=0, restore_state_after=False)
        return True
    except Exception:
        return False


def send_text_bulk(text: str) -> bool:
    """Type arbitrary-length text at the caret, without touching the clipboard.

    Used for terminals, where no paste chord is dependable. Windows batches the
    whole string through SendInput; the ``keyboard`` backend types with a small
    inter-character delay because a terminal drops characters written at zero
    delay. Newlines are sent as-is — callers accept that a shell treats them as
    Enter.
    """
    if not text:
        return True
    if sys.platform == "win32" and _win_send_text(text):
        return True
    try:
        kb = _require_keyboard()
        kb.write(text, delay=0.002, restore_state_after=False)
        return True
    except Exception:
        return False


def _win_send_backspaces(n: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class INPUT(ctypes.Structure):
            class _I(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("i",)
            _fields_ = [("type", wintypes.DWORD), ("i", _I)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        VK_BACK = 0x08
        batch = 256
        remaining = n
        while remaining > 0:
            count = min(batch, remaining)
            arr = (INPUT * (count * 2))()
            for i in range(count):
                arr[i * 2].type = INPUT_KEYBOARD
                arr[i * 2].ki.wVk = VK_BACK
                arr[i * 2 + 1].type = INPUT_KEYBOARD
                arr[i * 2 + 1].ki.wVk = VK_BACK
                arr[i * 2 + 1].ki.dwFlags = KEYEVENTF_KEYUP
            sent = ctypes.windll.user32.SendInput(
                count * 2, ctypes.byref(arr), ctypes.sizeof(INPUT)
            )
            if sent != count * 2:
                return False
            remaining -= count
        return True
    except Exception:
        return False


def _win_send_text(text: str) -> bool:
    """Inject UTF-16 code units via SendInput KEYEVENTF_UNICODE."""
    try:
        import ctypes
        from ctypes import wintypes

        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class INPUT(ctypes.Structure):
            class _I(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("i",)
            _fields_ = [("type", wintypes.DWORD), ("i", _I)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        units = [ord(ch) for ch in text]
        if any(u > 0xFFFF for u in units):
            encoded = text.encode("utf-16-le")
            units = [
                encoded[i] | (encoded[i + 1] << 8) for i in range(0, len(encoded), 2)
            ]
        batch = 128
        remaining = units
        while remaining:
            chunk = remaining[:batch]
            remaining = remaining[batch:]
            arr = (INPUT * (len(chunk) * 2))()
            for i, code in enumerate(chunk):
                arr[i * 2].type = INPUT_KEYBOARD
                arr[i * 2].ki.wScan = code
                arr[i * 2].ki.dwFlags = KEYEVENTF_UNICODE
                arr[i * 2 + 1].type = INPUT_KEYBOARD
                arr[i * 2 + 1].ki.wScan = code
                arr[i * 2 + 1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            sent = ctypes.windll.user32.SendInput(
                len(chunk) * 2, ctypes.byref(arr), ctypes.sizeof(INPUT)
            )
            if sent != len(chunk) * 2:
                return False
        return True
    except Exception:
        return False
