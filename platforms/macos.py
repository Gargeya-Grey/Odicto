"""macOS platform backend.

Hotkeys and synthetic key chords are provided by ``pynput``. Selective
suppression uses pynput's ``darwin_intercept`` hook: callbacks fire first,
then the interceptor returns ``None`` to drop an event. We record the handler
verdict per event so the interceptor suppresses exactly the keys the app owns.

Left/right modifier disambiguation (``side_exclusive_scan_codes``) is not
available through pynput's public API and degrades to ``is_pressed(key)``.
"""

from __future__ import annotations

import threading
import time
from typing import Dict

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

try:
    from pynput import keyboard as _pynput
except Exception as _exc:  # pragma: no cover - macOS only
    _pynput = None
    _PYNPUT_IMPORT_ERROR = _exc
else:
    _PYNPUT_IMPORT_ERROR = None

KEY_DOWN = "down"
KEY_UP = "up"

_KEY_ALIASES = {
    "`": "grave",
    "backtick": "grave",
    "back-tick": "grave",
    "back quote": "grave",
    "backquote": "grave",
    "control": "ctrl",
    "left ctrl": "ctrl",
    "right ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "left shift": "shift",
    "right shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "left alt": "alt",
    "right alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "option": "alt",
    "left option": "alt",
    "right option": "alt",
    "command": "cmd",
    "left command": "cmd",
    "right command": "cmd",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "super": "cmd",
}


def _canonical_token(key_name: str) -> str:
    return _KEY_ALIASES.get(key_name.strip().lower(), key_name.strip().lower())


class _Event:
    __slots__ = ("event_type", "key")

    def __init__(self, event_type: str, key: str) -> None:
        self.event_type = event_type
        self.key = key


_listener = None
_listener_lock = threading.Lock()
_hooks: Dict[str, Dict[str, Any]] = {}
_pressed_tokens: set = set()
_pending_suppress = False


def _require_pynput():
    if _pynput is None:
        raise RuntimeError(
            "pynput is required for macOS hotkeys but could not be imported: "
            f"{_PYNPUT_IMPORT_ERROR}"
        )
    return _pynput


def _key_token(key) -> str:
    if key is None:
        return ""
    if isinstance(key, _pynput.KeyCode):
        ch = key.char
        if not ch:
            return ""
        return _canonical_token(ch)
    name = getattr(key, "name", "")
    return _canonical_token(name)


def _on_press(key) -> None:
    global _pending_suppress
    token = _key_token(key)
    if token:
        _pressed_tokens.add(token)
    hook = _hooks.get(token)
    if hook is None:
        _pending_suppress = False
        return
    try:
        result = hook["handler"](_Event(KEY_DOWN, token))
    except Exception as e:
        print(f"Error in hotkey press handler: {e}", flush=True)
        result = True
    _pending_suppress = bool(hook["suppress"]) and result is False


def _on_release(key) -> None:
    global _pending_suppress
    token = _key_token(key)
    if token:
        _pressed_tokens.discard(token)
    hook = _hooks.get(token)
    if hook is None:
        _pending_suppress = False
        return
    try:
        result = hook["handler"](_Event(KEY_UP, token))
    except Exception as e:
        print(f"Error in hotkey release handler: {e}", flush=True)
        result = True
    _pending_suppress = bool(hook["suppress"]) and result is False


def _intercept(event_type, event):
    global _pending_suppress
    if _pending_suppress:
        _pending_suppress = False
        return None
    return event


def hook_key(key: str, handler, suppress: bool) -> None:
    global _listener

    pynput = _require_pynput()
    token = _canonical_token(key)

    with _listener_lock:
        _hooks[token] = {"handler": handler, "suppress": bool(suppress)}
        if _listener is None:
            _listener = pynput.Listener(
                on_press=_on_press,
                on_release=_on_release,
                darwin_intercept=_intercept,
            )
            _listener.daemon = True
            _listener.start()


def unhook_all() -> None:
    global _listener

    with _listener_lock:
        _hooks.clear()
        if _listener is not None:
            try:
                _listener.stop()
            except Exception:
                pass
            _listener = None


def is_pressed(key: str) -> bool:
    return _canonical_token(key) in _pressed_tokens


def side_exclusive_scan_codes(key: str):
    # pynput does not expose left/right scan-code disambiguation.
    return ()


def is_pressed_exclusive(key: str) -> bool:
    return is_pressed(key)


def wait() -> None:
    with _listener_lock:
        lst = _listener
    if lst is not None:
        try:
            lst.join()
        except Exception:
            pass


def force_release_modifiers() -> None:
    pynput = _require_pynput()
    controller = pynput.Controller()
    for token in ("ctrl", "shift", "alt", "cmd"):
        if token in _pressed_tokens:
            try:
                controller.release(getattr(pynput.Key, token))
            except Exception:
                pass
    time.sleep(0.04)


def wm_copy_foreground() -> bool:
    return False


def copy_chord() -> None:
    pynput = _require_pynput()
    controller = pynput.Controller()
    cmd = pynput.Key.cmd
    controller.press(cmd)
    time.sleep(0.01)
    controller.press("c")
    controller.release("c")
    time.sleep(0.01)
    controller.release(cmd)


def paste_chord() -> None:
    pynput = _require_pynput()
    controller = pynput.Controller()
    cmd = pynput.Key.cmd
    controller.press(cmd)
    time.sleep(0.01)
    controller.press("v")
    controller.release("v")
    time.sleep(0.01)
    controller.release(cmd)


def send(chord: str) -> None:
    # Simple chord parser used only as a fallback; translate Ctrl to Cmd on macOS.
    parts = [p.strip().lower() for p in chord.split("+")]
    if "c" in parts:
        return copy_chord()
    if "v" in parts:
        return paste_chord()
    if parts:
        pynput = _require_pynput()
        controller = pynput.Controller()
        try:
            controller.press(parts[-1])
            controller.release(parts[-1])
        except Exception:
            pass


def send_copy() -> None:
    try:
        copy_chord()
    except Exception:
        send("cmd+c")


def send_paste() -> None:
    try:
        paste_chord()
    except Exception:
        send("cmd+v")


def apply_window_exstyles(widget) -> None:
    # Qt window flags already handle topmost/click-through on macOS.
    return None


def hotkey_backend_name() -> str:
    return "pynput"
