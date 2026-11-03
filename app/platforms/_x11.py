"""Rootless X11 hotkeys using server-side grabs and explicit event replay.

Imports and connections are lazy. Importing this module never installs a hook.
Wayland is deliberately rejected: an Xwayland connection cannot supply global
shortcuts or inject into native Wayland applications reliably.
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

KEY_DOWN = "down"
KEY_UP = "up"

_ALIASES = {
    "ctrl": ("Control_L", "Control_R"), "control": ("Control_L", "Control_R"),
    "shift": ("Shift_L", "Shift_R"), "alt": ("Alt_L", "Alt_R"),
    "cmd": ("Super_L", "Super_R"), "win": ("Super_L", "Super_R"),
    "left ctrl": ("Control_L",), "right ctrl": ("Control_R",),
    "left control": ("Control_L",), "right control": ("Control_R",),
    "left shift": ("Shift_L",), "right shift": ("Shift_R",),
    "left alt": ("Alt_L",), "right alt": ("Alt_R",),
    "left windows": ("Super_L",), "right windows": ("Super_R",),
    "left cmd": ("Super_L",), "right cmd": ("Super_R",),
    "esc": ("Escape",), "escape": ("Escape",), "enter": ("Return",),
    "return": ("Return",), "backspace": ("BackSpace",), "tab": ("Tab",),
    "space": ("space",), "grave": ("grave",), "insert": ("Insert",),
    "delete": ("Delete",), "up": ("Up",), "down": ("Down",),
    "left": ("Left",), "right": ("Right",), "home": ("Home",),
    "end": ("End",), "page up": ("Prior",), "page down": ("Next",),
}


def _key_names(key):
    name = str(key).strip().lower()
    if name in _ALIASES:
        return _ALIASES[name]
    if name.startswith("f") and name[1:].isdigit():
        return (name.upper(),)
    return (name,)


def require_session():
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("Wayland is not supported by Odicto's X11 backend; log into an X11 session.")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("No X11 DISPLAY is available; start Odicto from your desktop session.")


def _down(bitmap, code):
    return bool(bitmap[code // 8] & (1 << (code % 8)))


class _Backend:
    def __init__(self, display, constants, keysyms):
        self.display, self.X, self.XK = display, constants, keysyms
        self.root = display.screen().root
        self.lock = threading.RLock()
        self.handlers = {}
        self.modifier_masks = {}
        self.pressed = {}
        self.suppressed = set()
        self.closing = threading.Event()
        self.local = threading.local()
        self.thread = None

    def codes(self, key):
        if isinstance(key, int):
            return (key,)
        found = []
        for name in _key_names(key):
            symbol = self.XK.string_to_keysym(name)
            code = self.display.keysym_to_keycode(symbol) if symbol else 0
            if code and code not in found:
                found.append(code)
        return tuple(found)

    def masks(self, modifier_sets):
        named = {"ctrl": self.X.ControlMask, "control": self.X.ControlMask,
                 "shift": self.X.ShiftMask, "alt": self.X.Mod1Mask,
                 "cmd": self.X.Mod4Mask, "win": self.X.Mod4Mask, "windows": self.X.Mod4Mask}
        ignored = {self.X.LockMask}
        mapping = self.display.get_modifier_mapping()
        for name in ("Num_Lock", "Scroll_Lock"):
            symbol = self.XK.string_to_keysym(name)
            code = self.display.keysym_to_keycode(symbol) if symbol else 0
            if code:
                ignored.update(1 << index for index, codes in enumerate(mapping) if code in codes)
        lock_variants = {0}
        for mask in ignored:
            lock_variants |= {value | mask for value in lock_variants}
        result = set()
        for modifiers in modifier_sets:
            mask = 0
            for modifier in modifiers:
                name = str(modifier).lower().removeprefix("left ").removeprefix("right ")
                if name not in named:
                    raise ValueError(f"Unknown X11 shortcut modifier: {modifier!r}")
                mask |= named[name]
            result.update(mask | locks for locks in lock_variants)
        if not result:
            raise ValueError("At least one X11 shortcut modifier combination is required")
        return result

    def bind(self, key, callback, suppress, modifier_sets=((),)):
        with self.lock:
            codes = self.codes(key)
            if not codes:
                raise ValueError(f"Unknown X11 shortcut key: {key!r}")
            if any(code in self.handlers for code in codes):
                raise ValueError(f"X11 shortcut key is already registered: {key!r}")
            masks = self.masks(modifier_sets)
            errors = []
            for code in codes:
                for mask in sorted(masks):
                    self.root.grab_key(code, mask, False,
                                       self.X.GrabModeAsync, self.X.GrabModeSync,
                                       onerror=lambda error, request: errors.append(error))
            self.display.sync()
            if errors:
                for code in codes:
                    self.root.ungrab_key(code, self.X.AnyModifier)
                self.display.sync()
                raise RuntimeError(f"Another application owns X11 shortcut {key!r}; choose another shortcut.")
            for code in codes:
                self.handlers[code] = (callback, suppress, str(key))
                self.modifier_masks[code] = masks
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, daemon=True, name="odicto-x11-hotkeys")
                self.thread.start()

    def is_pressed(self, key):
        with self.lock:
            state = getattr(self.local, "state", None)
            masks = {"ctrl": self.X.ControlMask, "control": self.X.ControlMask,
                     "shift": self.X.ShiftMask, "alt": self.X.Mod1Mask,
                     "cmd": self.X.Mod4Mask, "win": self.X.Mod4Mask}
            if state is not None and key in masks:
                return bool(state & masks[key])
            bitmap = self.display.query_keymap()
            return any(_down(bitmap, code) for code in self.codes(key))

    def _callback(self, code, kind, state=None):
        registered = self.handlers.get(code)
        if registered is None:
            return True
        callback, suppress, name = registered
        self.local.state = state
        try:
            verdict = callback(SimpleNamespace(event_type=kind, scan_code=code, name=name))
            return not suppress or verdict is not False
        finally:
            self.local.state = None

    def _replay(self, timestamp):
        self.display.allow_events(self.X.ReplayKeyboard, timestamp)
        if self.suppressed:
            reply = self.root.grab_keyboard(False, self.X.GrabModeAsync,
                                            self.X.GrabModeSync, self.X.CurrentTime)
            if reply != self.X.GrabSuccess:
                raise RuntimeError("X11 could not resume the held shortcut grab")
            self.display.allow_events(self.X.SyncKeyboard, self.X.CurrentTime)
        self.display.flush()

    def handle(self, event):
        if event.type not in (self.X.KeyPress, self.X.KeyRelease):
            return
        code = event.detail
        kind = KEY_DOWN if event.type == self.X.KeyPress else KEY_UP
        if (kind == KEY_DOWN and code not in self.suppressed and code in self.modifier_masks
                and event.state & 0xff not in self.modifier_masks[code]):
            self._replay(event.time)
            return
        if kind == KEY_UP and code in self.pressed and _down(self.display.query_keymap(), code):
            self.display.allow_events(self.X.SyncKeyboard, event.time)
            self.display.flush()
            return
        if kind == KEY_DOWN and code in self.handlers:
            self.pressed[code] = event.state
        if kind == KEY_UP and code not in self.pressed:
            self._replay(event.time)
            return
        allowed = self._callback(code, kind, event.state)
        if kind == KEY_UP:
            self.pressed.pop(code, None)
            self.suppressed.discard(code)
        elif not allowed:
            self.suppressed.add(code)
        if allowed:
            self._replay(event.time)
        elif kind == KEY_UP and not self.suppressed:
            self.display.allow_events(self.X.AsyncKeyboard, event.time)
            self.display.ungrab_keyboard(event.time)
            self.display.flush()
        else:
            self.display.allow_events(self.X.SyncKeyboard, event.time)
            self.display.flush()

    def poll_releases(self):
        if not self.pressed:
            return
        bitmap = self.display.query_keymap()
        if self.display.pending_events():
            return
        for code in list(self.pressed):
            if _down(bitmap, code):
                continue
            self.pressed.pop(code, None)
            self.suppressed.discard(code)
            self._callback(code, KEY_UP)
        if not self.suppressed:
            self.display.allow_events(self.X.AsyncKeyboard, self.X.CurrentTime)
            self.display.ungrab_keyboard(self.X.CurrentTime)
            self.display.flush()

    def run(self):
        try:
            while not self.closing.is_set():
                with self.lock:
                    while self.display.pending_events():
                        self.handle(self.display.next_event())
                    self.poll_releases()
                select.select([self.display.fileno()], [], [], 0.01)
        except Exception as error:
            if not self.closing.is_set():
                print(f"X11 hotkeys stopped: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        finally:
            self._release()

    def _release(self):
        with self.lock:
            try:
                self.root.ungrab_key(self.X.AnyKey, self.X.AnyModifier)
                self.display.allow_events(self.X.AsyncKeyboard, self.X.CurrentTime)
                self.display.ungrab_keyboard(self.X.CurrentTime)
                self.display.sync()
            except Exception:
                pass

    def close(self):
        self.closing.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        self._release()
        self.display.close()


_backend = None
_backend_lock = threading.Lock()


def _get_backend():
    global _backend
    with _backend_lock:
        if _backend is None:
            require_session()
            try:
                from Xlib import X, XK, display
            except ImportError as error:
                raise RuntimeError("python-xlib is missing; rerun the Odicto installer.") from error
            _backend = _Backend(display.Display(), X, XK)
        return _backend


def hook_key(key, handler, suppress=False, modifier_sets=((),)):
    _get_backend().bind(key, handler, suppress, modifier_sets)


def unhook_all():
    global _backend
    with _backend_lock:
        backend, _backend = _backend, None
    if backend is not None:
        backend.close()


def is_pressed(key):
    return _get_backend().is_pressed(key)


def side_exclusive_scan_codes(key):
    return _get_backend().codes(key)


def is_pressed_exclusive(key):
    key = str(key).strip().lower()
    if key.startswith("left "):
        other = "right " + key[5:]
    elif key.startswith("right "):
        other = "left " + key[6:]
    else:
        return is_pressed(key)
    return is_pressed(key) and not is_pressed(other)


def wait():
    threading.Event().wait()


def _xdotool(*arguments, timeout=5.0, input=None):
    require_session()
    executable = shutil.which("xdotool")
    if executable is None:
        raise RuntimeError("xdotool is missing; install it with your system package manager.")
    try:
        subprocess.run([executable, *arguments], check=True, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, input=input)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        raise RuntimeError("X11 input injection failed; refusing to repeat possibly partial input.") from error


def _chord(chord):
    return "+".join(_key_names(part)[0] for part in chord.split("+"))


def press(key):
    _xdotool("keydown", _key_names(key)[0])


def release(key):
    _xdotool("keyup", _key_names(key)[0])


def press_and_release(key):
    send(key)


def send(chord):
    _xdotool("key", "--clearmodifiers", _chord(chord))


def force_release_modifiers():
    _xdotool("keyup", "Control_L", "Control_R", "Shift_L", "Shift_R",
             "Alt_L", "Alt_R", "Super_L", "Super_R")


def send_copy():
    send("ctrl+c")


def send_copy_terminal():
    send("ctrl+shift+c")


def send_copy_ide():
    send("ctrl+insert")


def send_paste():
    send("ctrl+v")


def send_backspaces(n):
    count = min(max(0, int(n)), 4000)
    if count:
        _xdotool("key", "--clearmodifiers", "--repeat", str(count), "BackSpace",
                 timeout=max(5.0, count * 0.02))


def send_text(text):
    if len(text) > 256:
        return False
    return send_text_bulk(text)


def send_text_bulk(text):
    if not text:
        return True
    if any(not (" " <= char <= "~" or char in "\r\n") for char in text):
        from platforms.base import TextDeliveryUnavailable
        raise TextDeliveryUnavailable(
            "This text needs manual paste in this Linux target. Copy the complete text below, "
            "then paste it using the target application's paste command. Nothing was typed.")
    if shutil.which("xdotool") is None:
        return False
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if index:
            _xdotool("key", "--clearmodifiers", "Return")
        if line:
            _xdotool("type", "--clearmodifiers", "--delay", "2", "--file", "-",
                     timeout=max(5.0, len(line) * 0.004 + 5.0), input=line.encode("utf-8"))
    return True


def wm_copy_foreground():
    return False
