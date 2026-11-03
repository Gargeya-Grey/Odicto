"""macOS platform backend.

Hotkeys and synthetic key chords are provided by ``pynput``. Selective
suppression uses pynput's ``darwin_intercept`` hook: callbacks fire first,
then the interceptor returns ``None`` to drop an event. We record the handler
verdict per event so the interceptor suppresses exactly the keys the app owns.

Modifier sides use native virtual keys and Quartz state. Character hotkeys
bind the unmodified keyboard layout so Option-generated symbols keep their primary.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Dict

from platforms._posix import (  # noqa: F401,F403
    acquire_lock,
    enumerate_odicto_pids,
    kill_other_odicto_processes,
    keep_responsive,
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
_pressed_key_tokens: dict = {}
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
        virtual_key = getattr(key, "vk", None)
        if virtual_key is not None:
            for token, hook in _hooks.copy().items():
                if hook.get("pressed_virtual_key") == virtual_key:
                    return token
            for token, hook in _hooks.copy().items():
                if hook.get("virtual_key") == virtual_key:
                    return token
        ch = key.char
        if not ch:
            return ""
        return _canonical_token(ch)
    name = getattr(key, "name", "")
    return _canonical_token(name)


def _primary_virtual_key(token: str):
    character = "`" if token == "grave" else token
    if len(character) != 1:
        return None
    try:
        from pynput._util.darwin import get_unicode_to_keycode_map
        return get_unicode_to_keycode_map().get(character)
    except Exception:
        return None


_MODIFIER_VIRTUAL_KEYS = {"ctrl": (59, 62), "shift": (56, 60), "alt": (58, 61), "cmd": (55, 54)}


def _modifier_side(key) -> str:
    value = getattr(key, "value", key)
    virtual_key = getattr(value, "vk", None)
    for family, codes in _MODIFIER_VIRTUAL_KEYS.items():
        if virtual_key in codes:
            return ("left " if virtual_key == codes[0] else "right ") + ("windows" if family == "cmd" else family)
    name = getattr(key, "name", "")
    if name.endswith(("_l", "_r")):
        from config import normalize_modifier_name
        try:
            return normalize_modifier_name(("left " if name.endswith("_l") else "right ") + name[:-2])
        except ValueError:
            pass
    return ""


def _modifier_key_state(token):
    from config import normalize_modifier_name
    side, family = "", token
    if token.startswith(("left ", "right ")):
        side, family = token.split(" ", 1)
    family = normalize_modifier_name(family)
    codes = _MODIFIER_VIRTUAL_KEYS[family]
    if side:
        codes = (codes[0 if side == "left" else 1],)
    try:
        from Quartz import CGEventSourceKeyState, kCGEventSourceStateCombinedSessionState
        return any(bool(CGEventSourceKeyState(kCGEventSourceStateCombinedSessionState, code)) for code in codes)
    except Exception:
        return None


def _on_press(key) -> None:
    global _pending_suppress
    token = _key_token(key)
    virtual_key = getattr(key, "vk", None) if isinstance(key, _pynput.KeyCode) else None
    if virtual_key is not None:
        _pressed_tokens.discard(_pressed_key_tokens.get(virtual_key))
        _pressed_key_tokens[virtual_key] = token
    if token:
        _pressed_tokens.add(token)
    side = _modifier_side(key)
    if side:
        _pressed_tokens.add(side)
    candidates = [token]
    if isinstance(key, _pynput.KeyCode):
        character = _canonical_token(key.char or "")
        already_claimed = virtual_key is not None and _hooks.get(token, {}).get("pressed_virtual_key") == virtual_key
        if not already_claimed and character != token and character in _hooks:
            if _hooks.get(token, {}).get("can_decline", False):
                candidates.append(character)
            else:
                candidates.insert(0, character)
    for candidate in candidates:
        hook = _hooks.get(candidate)
        if hook is None:
            continue
        result, claimed = True, True
        try:
            result = hook["handler"](_Event(KEY_DOWN, candidate))
            claimed = result is False or not hook["can_decline"]
        except Exception as e:
            print(f"Error in hotkey press handler: {e}", flush=True)
        if claimed:
            _pressed_tokens.discard(token)
            _pressed_tokens.add(candidate)
            if virtual_key is not None:
                hook["pressed_virtual_key"] = virtual_key
                _pressed_key_tokens[virtual_key] = candidate
            _pending_suppress = bool(hook["suppress"]) and result is False
            return
    _pending_suppress = False


def _on_release(key) -> None:
    global _pending_suppress
    token = _key_token(key)
    if isinstance(key, _pynput.KeyCode):
        _pressed_tokens.discard(_pressed_key_tokens.pop(getattr(key, "vk", None), None))
    side = _modifier_side(key)
    if side:
        _pressed_tokens.discard(side)
    if token and not any(modifier in _pressed_tokens for modifier in (
        "left " + ("windows" if token == "cmd" else token),
        "right " + ("windows" if token == "cmd" else token),
    )):
        _pressed_tokens.discard(token)
    hook = _hooks.get(token)
    if hook is None:
        _pending_suppress = False
        return
    hook.pop("pressed_virtual_key", None)
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


def hook_key(key: str, handler, suppress: bool, *, can_decline: bool = False) -> None:
    """Only opt in when True guarantees the handler performed no action."""
    global _listener

    pynput = _require_pynput()
    token = _canonical_token(key)

    with _listener_lock:
        _hooks[token] = {"handler": handler, "suppress": bool(suppress),
                         "virtual_key": _primary_virtual_key(token), "can_decline": can_decline}
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
        _pressed_tokens.clear()
        _pressed_key_tokens.clear()
        if _listener is not None:
            try:
                _listener.stop()
            except Exception:
                pass
            _listener = None


def is_pressed(key: str) -> bool:
    from config import normalize_modifier_name
    try:
        token = normalize_modifier_name(key)
    except ValueError:
        token = _canonical_token(key)
    else:
        pressed = _modifier_key_state(token)
        if pressed is not None:
            return pressed
    return token in _pressed_tokens


def side_exclusive_scan_codes(key: str):
    # pynput does not expose left/right scan-code disambiguation.
    return ()


def is_pressed_exclusive(key: str) -> bool:
    from config import normalize_modifier_name
    try:
        token = normalize_modifier_name(key)
    except ValueError:
        return is_pressed(key)
    if token.startswith(("left ", "right ")):
        side, family = token.split(" ", 1)
        opposite = ("right " if side == "left" else "left ") + family
        return is_pressed(token) and not is_pressed(opposite)
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


def send_copy_terminal() -> None:
    """Copy chord for a focused terminal. Cmd+C is already terminal-safe here."""
    send_copy()


def send_copy_ide() -> None:
    """Copy chord for an IDE host. Cmd+C is never SIGINT on macOS."""
    send_copy()


def foreground_is_ide_host(extra=()) -> bool:
    """Always False on macOS: Cmd+C copies and never interrupts a process."""
    return False


def send_paste() -> None:
    try:
        paste_chord()
    except Exception:
        send("cmd+v")


def send_backspaces(n: int) -> None:
    n = min(max(0, int(n)), 4000)
    if n <= 0:
        return
    pynput = _require_pynput()
    from pynput.keyboard import Key

    controller = pynput.Controller()
    for _ in range(n):
        controller.tap(Key.backspace)


def send_text(text: str) -> bool:
    """Type ``text`` at the caret without touching the clipboard."""
    if not text:
        return True
    if len(text) > 256:
        return False
    try:
        pynput = _require_pynput()
        pynput.Controller().type(text)
        return True
    except Exception:
        return False


def send_text_bulk(text: str) -> bool:
    """Type arbitrary-length text at the caret, without touching the clipboard.

    Used for terminals, where no paste chord is dependable. Newlines are sent
    as-is — callers accept that a shell treats them as Enter.
    """
    if not text:
        return True
    try:
        pynput = _require_pynput()
        pynput.Controller().type(text)
        return True
    except Exception:
        return False


def foreground_is_terminal(extra=()) -> bool:
    """True when the frontmost macOS app is a terminal emulator."""
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return False
        identifiers = [app.bundleIdentifier() or "", app.localizedName() or ""]
    except Exception:
        return False
    from platforms.base import is_terminal_identifier

    return is_terminal_identifier(identifiers, extra)


def apply_window_exstyles(widget) -> None:
    # Qt window flags already handle topmost/click-through on macOS.
    return None


def hotkey_backend_name() -> str:
    return "pynput"

def foreground_window_id():
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.processIdentifier() if app else None
    except Exception:
        return None
