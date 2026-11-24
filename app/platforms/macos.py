"""macOS platform backend.

Hotkeys and synthetic key chords are provided by ``pynput``. Selective
suppression uses pynput's ``darwin_intercept`` hook: callbacks fire first,
then the interceptor returns ``None`` to drop an event. We record the handler
verdict per event so the interceptor suppresses exactly the keys the app owns.

Modifier sides use native virtual keys and Quartz state. Character hotkeys
bind the unmodified keyboard layout so Option-generated symbols keep their primary.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from platforms._posix import (  # noqa: F401,F403
    acquire_lock,
    enumerate_odicto_pids,
    kill_other_odicto_processes,
    keep_responsive,
    kill_process_tree,
    lock_is_held,
    lock_refusal,
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


_MAIN_THREAD_RUNNER_CLS = None
_layout_lookup_tls = threading.local()


@contextlib.contextmanager
def _require_fresh_layout():
    prev = getattr(_layout_lookup_tls, "require_fresh", False)
    _layout_lookup_tls.require_fresh = True
    try:
        yield
    finally:
        _layout_lookup_tls.require_fresh = prev


@contextlib.contextmanager
def _preloaded_layout_map(mapping):
    prev = getattr(_layout_lookup_tls, "preloaded_map", None)
    _layout_lookup_tls.preloaded_map = mapping
    try:
        yield
    finally:
        _layout_lookup_tls.preloaded_map = prev


def _get_main_thread_runner_class():
    """Return a cached Objective-C NSObject subclass for main-thread dispatch."""
    global _MAIN_THREAD_RUNNER_CLS
    if _MAIN_THREAD_RUNNER_CLS is not None:
        return _MAIN_THREAD_RUNNER_CLS
    import objc
    from Foundation import NSObject

    try:
        _MAIN_THREAD_RUNNER_CLS = objc.lookUpClass("_OdictoMainThreadRunner")
        return _MAIN_THREAD_RUNNER_CLS
    except objc.nosuchclass_error:
        pass

    class _OdictoMainThreadRunner(NSObject):
        def initWithCallback_(self, fn):
            self = objc.super(_OdictoMainThreadRunner, self).init()
            if self is None:
                return None
            self._fn = fn
            return self

        def run_(self, _arg):
            self._fn()

    _MAIN_THREAD_RUNNER_CLS = _OdictoMainThreadRunner
    return _MAIN_THREAD_RUNNER_CLS


def _qt_app_instance():
    qt_core = sys.modules.get("PySide6.QtCore")
    if qt_core is None:
        return None
    qcore_app = getattr(qt_core, "QCoreApplication", None)
    return qcore_app.instance() if qcore_app is not None else None


def _run_on_main_thread(fn, timeout_s: float = 0.5):
    """Run ``fn`` on the Cocoa main runloop when Qt's event loop is active.

    Returns ``(True, result)`` on completion or ``(False, None)`` when no
    runloop is active or the bounded wait expires.
    """
    if _qt_app_instance() is None:
        return False, None
    try:
        from AppKit import NSApplication
        from Foundation import NSRunLoopCommonModes
        if not NSApplication.sharedApplication().isRunning():
            return False, None
        runner_cls = _get_main_thread_runner_class()
    except Exception:
        return False, None

    done = threading.Event()
    box: dict[str, object] = {}

    def _invoke():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    runner = runner_cls.alloc().initWithCallback_(_invoke)
    if runner is None:
        return False, None
    runner.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
        "run:", None, False, [NSRunLoopCommonModes]
    )
    if not done.wait(timeout_s) or "error" in box:
        return False, None
    return True, box.get("value")


def _install_pynput_main_thread_cache() -> None:
    """Keep pynput Carbon keyboard layout lookups on the main dispatch queue.

    On macOS 27+, HIToolbox enforces ``dispatch_assert_queue(main_queue)``
    inside ``TSMGetInputSourceProperty`` once AppKit/Qt shows a window.
    ``pynput.keyboard.Listener`` and ``Controller`` invoke ``keycode_context``
    from background threads, so we dispatch live lookups onto the main runloop
    (preserving layout switches such as U.S. -> Dvorak while running) and fall
    back to a main-thread-warmed cache when the runloop is not pumping.
    """
    if sys.platform != "darwin" or _pynput is None:
        return
    try:
        import contextlib
        import pynput._util.darwin as darwin_util
        import pynput.keyboard._darwin as darwin_kb
    except Exception:
        return

    if getattr(darwin_util, "_odicto_main_thread_safe", False):
        return

    orig_keycode_context = getattr(darwin_util, "keycode_context", None)
    orig_unicode_map = getattr(darwin_util, "get_unicode_to_keycode_map", None)
    if not callable(orig_keycode_context) or not callable(orig_unicode_map):
        return

    cache_lock = threading.Lock()
    cached: dict[str, object] = {"context": None, "map": None}

    def _read_context():
        with orig_keycode_context() as ctx:
            return ctx

    if threading.current_thread() is threading.main_thread():
        try:
            ctx = _read_context()
            if ctx is not None:
                cached["context"] = ctx
            mapping = orig_unicode_map()
            if isinstance(mapping, dict):
                cached["map"] = dict(mapping)
        except Exception:
            pass

    @contextlib.contextmanager
    def safe_keycode_context():
        if threading.current_thread() is threading.main_thread():
            with orig_keycode_context() as ctx:
                if ctx is not None:
                    with cache_lock:
                        cached["context"] = ctx
                yield ctx
            return
        ok, ctx = _run_on_main_thread(_read_context)
        if ok and ctx is not None:
            with cache_lock:
                cached["context"] = ctx
            yield ctx
            return
        if _qt_app_instance() is None:
            with orig_keycode_context() as ctx:
                if ctx is not None:
                    with cache_lock:
                        cached["context"] = ctx
                yield ctx
            return
        with cache_lock:
            ctx = cached["context"]
        yield ctx if ctx is not None else (None, None)

    def safe_get_unicode_to_keycode_map():
        preloaded = getattr(_layout_lookup_tls, "preloaded_map", None)
        if isinstance(preloaded, dict):
            return dict(preloaded)
        if threading.current_thread() is threading.main_thread():
            mapping = orig_unicode_map()
            if isinstance(mapping, dict):
                with cache_lock:
                    cached["map"] = dict(mapping)
                return dict(mapping)
            return mapping
        ok, mapping = _run_on_main_thread(orig_unicode_map)
        if ok and isinstance(mapping, dict):
            with cache_lock:
                cached["map"] = dict(mapping)
            return dict(mapping)
        if _qt_app_instance() is None:
            mapping = orig_unicode_map()
            if isinstance(mapping, dict):
                with cache_lock:
                    cached["map"] = dict(mapping)
                return dict(mapping)
            return mapping
        if getattr(_layout_lookup_tls, "require_fresh", False):
            raise RuntimeError(
                "Could not refresh macOS keyboard layout on main thread; "
                "shortcut not sent."
            )
        with cache_lock:
            mapping = cached["map"]
            ctx = cached["context"]
        if isinstance(mapping, dict):
            return dict(mapping)
        if isinstance(ctx, tuple) and len(ctx) == 2 and ctx[1] is not None:
            keycode_to_string = getattr(darwin_util, "keycode_to_string", None)
            if callable(keycode_to_string):
                return {keycode_to_string(ctx, kc): kc for kc in range(128)}
        return {}

    darwin_util.keycode_context = safe_keycode_context
    darwin_kb.keycode_context = safe_keycode_context
    darwin_util.get_unicode_to_keycode_map = safe_get_unicode_to_keycode_map
    darwin_kb.get_unicode_to_keycode_map = safe_get_unicode_to_keycode_map
    darwin_util._odicto_main_thread_safe = True


_install_pynput_main_thread_cache()

KEY_DOWN = "down"
KEY_UP = "up"

_KEY_ALIASES = {
    "`": "grave",
    "backtick": "grave",
    "back-tick": "grave",
    "back quote": "grave",
    "backquote": "grave",
    "page up": "page_up",
    "pageup": "page_up",
    "page down": "page_down",
    "pagedown": "page_down",
    "caps lock": "caps_lock",
    "capslock": "caps_lock",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
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
_event_tap = None
_listener_lock = threading.Lock()
_hooks: dict = {}
_pressed_tokens: set = set()
_synthetic_lock = threading.Lock()
_synthetic_depth = 0
_EVENT_TAP_DISABLED_TYPES = (0xFFFFFFFE, 0xFFFFFFFF)


@contextlib.contextmanager
def _suppress_self_events():
    """Ignore injected events emitted by Odicto's own synthetic keystrokes."""
    global _synthetic_depth
    with _synthetic_lock:
        _synthetic_depth += 1
    try:
        yield
    finally:
        with _synthetic_lock:
            _synthetic_depth = max(0, _synthetic_depth - 1)


@dataclass
class _KeyPress:
    token: str
    handler: Callable | None = None
    suppress: bool = False
    failed: bool = False


_pressed_keys: dict[tuple[str, int | str], _KeyPress] = {}
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
        ch = key.char
        is_media = bool(getattr(key, "_is_media", False))
        # Passively refresh a hooked character's virtual_key if the user switched
        # keyboard layouts while Odicto is running (zero Carbon calls on tap thread).
        if (
            virtual_key is not None
            and not is_media
            and isinstance(ch, str)
            and len(ch) == 1
            and ch.isprintable()
        ):
            canonical_ch = _canonical_token(ch)
            hook = _hooks.get(canonical_ch)
            if hook is not None and hook.get("virtual_key") != virtual_key:
                hook["virtual_key"] = virtual_key
        if virtual_key is not None and not is_media:
            for token, hook in _hooks.copy().items():
                if hook.get("virtual_key") == virtual_key:
                    return token
        if not ch:
            return ""
        # macOS Ctrl+A..Z emits ASCII control chars 0x01..0x1A when layout lookup missed.
        if len(ch) == 1 and 1 <= ord(ch) <= 26:
            return chr(ord(ch) + 96)
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


def _on_press(key, injected: bool = False) -> None:
    global _pending_suppress
    if injected and _synthetic_depth > 0:
        _pending_suppress = False
        return
    identity = _key_identity(key)
    press = _pressed_keys.get(identity)
    if press is not None:
        if press.handler is not None and not press.failed:
            try:
                press.handler(_Event(KEY_DOWN, press.token))
            except Exception as error:
                print(f"Error in hotkey repeat handler: {error}", flush=True)
                press.failed = True
        _pending_suppress = press.suppress
        return
    token = _key_token(key)
    press = _KeyPress(token)
    _pressed_keys[identity] = press
    if token:
        _pressed_tokens.add(token)
    side = _modifier_side(key)
    if side:
        _pressed_tokens.add(side)
    candidates = [token]
    if isinstance(key, _pynput.KeyCode):
        character = _canonical_token(key.char or "")
        if character != token and character in _hooks:
            if _hooks.get(token, {}).get("can_decline", False):
                candidates.append(character)
            else:
                candidates.insert(0, character)
    for candidate in candidates:
        hook = _hooks.get(candidate)
        if hook is None:
            continue
        result, failed = True, False
        try:
            result = hook["handler"](_Event(KEY_DOWN, candidate))
        except Exception as e:
            print(f"Error in hotkey press handler: {e}", flush=True)
            failed = True
        if failed or result is False or not hook["can_decline"]:
            _pressed_tokens.discard(token)
            _pressed_tokens.add(candidate)
            press.token, press.handler = candidate, hook["handler"]
            press.suppress = bool(hook["suppress"]) and result is False
            press.failed = failed
            break
    _pending_suppress = press.suppress


def _key_identity(key):
    value = getattr(key, "value", key)
    virtual_key = getattr(value, "vk", None)
    namespace = "media" if getattr(value, "_is_media", False) else "keyboard"
    code = virtual_key if virtual_key is not None else str(getattr(key, "name", None) or getattr(key, "char", ""))
    return namespace, code


def _on_release(key, injected: bool = False) -> None:
    global _pending_suppress
    if injected and _synthetic_depth > 0:
        _pending_suppress = False
        return
    press = _pressed_keys.pop(_key_identity(key), None)
    token = press.token if press is not None else _key_token(key)
    side = _modifier_side(key)
    if side:
        _pressed_tokens.discard(side)
    if token and not any(modifier in _pressed_tokens for modifier in (
        "left " + ("windows" if token == "cmd" else token),
        "right " + ("windows" if token == "cmd" else token),
    )):
        _pressed_tokens.discard(token)
    if press is not None and press.handler is not None:
        try:
            press.handler(_Event(KEY_UP, token))
        except Exception as e:
            print(f"Error in hotkey release handler: {e}", flush=True)
    _pending_suppress = press.suppress if press is not None else False


def _intercept(event_type, event):
    global _pending_suppress
    if event_type in _EVENT_TAP_DISABLED_TYPES:
        _pending_suppress = False
        if _event_tap is not None:
            try:
                from Quartz import CGEventTapEnable
                CGEventTapEnable(_event_tap, True)
            except Exception:
                pass
        return event
    if _pending_suppress:
        _pending_suppress = False
        return None
    return event


def hook_key(key: str, handler, suppress: bool, *, can_decline: bool = False) -> None:
    """Only opt in when True guarantees the handler performed no action."""
    global _listener, _event_tap

    pynput = _require_pynput()
    token = _canonical_token(key)

    with _listener_lock:
        _hooks[token] = {"handler": handler, "suppress": bool(suppress),
                         "virtual_key": _primary_virtual_key(token), "can_decline": can_decline}
        if _listener is None:
            listener = pynput.Listener(
                on_press=_on_press,
                on_release=_on_release,
                darwin_intercept=_intercept,
            )
            orig_create_tap = getattr(listener, "_create_event_tap", None)
            if callable(orig_create_tap):
                def _tracked_create_tap():
                    global _event_tap
                    tap = orig_create_tap()
                    _event_tap = tap
                    return tap
                listener._create_event_tap = _tracked_create_tap
            _listener = listener
            _listener.daemon = True
            _listener.start()


def unhook_all() -> None:
    global _listener, _event_tap

    with _listener_lock:
        _hooks.clear()
        _pressed_tokens.clear()
        _pressed_keys.clear()
        _event_tap = None
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


_SIDE_RELEASE_KEYS = (
    ("left ctrl", "ctrl", "ctrl_l"),
    ("right ctrl", "ctrl", "ctrl_r"),
    ("left shift", "shift", "shift_l"),
    ("right shift", "shift", "shift_r"),
    ("left alt", "alt", "alt_l"),
    ("right alt", "alt", "alt_r"),
    ("left windows", "cmd", "cmd_l"),
    ("right windows", "cmd", "cmd_r"),
)


def force_release_modifiers() -> None:
    pynput = _require_pynput()
    # Give a just-released shortcut finger up to ~450ms to physically lift so
    # WindowServer does not merge Option/Ctrl/Shift into the upcoming Cmd+V.
    for _ in range(45):
        if not any(_modifier_key_state(side_token) for side_token, _, _ in _SIDE_RELEASE_KEYS):
            break
        time.sleep(0.01)
    controller = pynput.Controller()
    with _suppress_self_events():
        for side_token, family, key_attr in _SIDE_RELEASE_KEYS:
            native = _modifier_key_state(side_token)
            should_release = (
                bool(native)
                if native is not None
                else (side_token in _pressed_tokens or family in _pressed_tokens)
            )
            if should_release:
                key_obj = getattr(pynput.Key, key_attr, None) or getattr(pynput.Key, family, None)
                if key_obj is not None:
                    try:
                        controller.release(key_obj)
                    except Exception:
                        pass
    time.sleep(0.04)


def wm_copy_foreground() -> bool:
    return False


def _fresh_controller(required_char: str | None = None):
    """Instantiate a pynput Controller with a fresh active keyboard layout map."""
    pynput = _require_pynput()
    mapping = None
    try:
        from pynput._util.darwin import get_unicode_to_keycode_map
    except ImportError:
        get_unicode_to_keycode_map = None
    if callable(get_unicode_to_keycode_map):
        with _require_fresh_layout():
            mapping = get_unicode_to_keycode_map()
        if not isinstance(mapping, dict) or not mapping:
            raise RuntimeError("Could not obtain a fresh macOS keyboard layout map.")
        if required_char is not None and required_char not in mapping:
            raise RuntimeError(
                f"Fresh macOS keyboard layout map is missing key {required_char!r}."
            )
    with _preloaded_layout_map(mapping):
        return pynput.Controller()


def _quartz_cmd_chord(vk: int) -> bool:
    """Post a clean Cmd+<vk> via a private CGEventSource on kCGSessionEventTap."""
    if sys.platform != "darwin":
        return False
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            CGEventSourceCreate,
            kCGEventFlagMaskCommand,
            kCGEventSourceStatePrivate,
            kCGSessionEventTap,
        )
    except Exception:
        return False
    source = CGEventSourceCreate(kCGEventSourceStatePrivate)
    down = CGEventCreateKeyboardEvent(source, vk, True)
    up = CGEventCreateKeyboardEvent(source, vk, False)
    if down is None or up is None:
        return False
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    with _suppress_self_events():
        CGEventPost(kCGSessionEventTap, down)
        time.sleep(0.02)
        CGEventPost(kCGSessionEventTap, up)
    return True


def _send_cmd_chord(char: str) -> None:
    pynput = _require_pynput()
    controller = _fresh_controller(char)
    mapping = getattr(controller, "_mapping", None)
    vk = mapping.get(char) if isinstance(mapping, dict) else None
    is_real_pynput = getattr(type(controller), "__module__", "").startswith("pynput.")
    if is_real_pynput and isinstance(vk, int) and _quartz_cmd_chord(vk):
        return
    cmd = pynput.Key.cmd
    with _suppress_self_events():
        controller.press(cmd)
        time.sleep(0.01)
        controller.press(char)
        time.sleep(0.02)
        controller.release(char)
        time.sleep(0.01)
        controller.release(cmd)


def copy_chord() -> None:
    _send_cmd_chord("c")


def paste_chord() -> None:
    _send_cmd_chord("v")


def send(chord: str) -> None:
    # Simple chord parser used only as a fallback; translate Ctrl to Cmd on macOS.
    parts = [p.strip().lower() for p in chord.split("+")]
    if "c" in parts:
        return copy_chord()
    if "v" in parts:
        return paste_chord()
    if parts:
        key_char = parts[-1]
        controller = _fresh_controller(key_char if len(key_char) == 1 else None)
        try:
            with _suppress_self_events():
                controller.press(key_char)
                controller.release(key_char)
        except Exception:
            pass


def send_copy() -> None:
    copy_chord()


def send_copy_terminal() -> None:
    """Copy chord for a focused terminal. Cmd+C is already terminal-safe here."""
    send_copy()


def send_copy_ide() -> None:
    """Copy chord for an IDE host. Cmd+C is never SIGINT on macOS."""
    send_copy()


def foreground_is_ide_host(extra=()) -> bool:
    """Identify editors that may copy the whole line when the caret is collapsed."""
    try:
        from AppKit import NSWorkspace
        from platforms.base import is_ide_host_identifier
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return is_ide_host_identifier((app.bundleIdentifier(), app.localizedName()), extra)
    except Exception:
        return False


def send_paste() -> None:
    paste_chord()


def send_backspaces(n: int) -> None:
    n = min(max(0, int(n)), 4000)
    if n <= 0:
        return
    pynput = _require_pynput()
    from pynput.keyboard import Key

    controller = pynput.Controller()
    with _suppress_self_events():
        for _ in range(n):
            controller.tap(Key.backspace)


def send_text(text: str) -> bool:
    """Type ``text`` at the caret without touching the clipboard."""
    if not text:
        return True
    if len(text) > 256:
        return False
    try:
        with _suppress_self_events():
            _fresh_controller().type(text)
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
        with _suppress_self_events():
            _fresh_controller().type(text)
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
    """Configure the macOS HUD NSWindow as a transparent, non-activating floating overlay."""
    if sys.platform != "darwin" or widget is None:
        return None
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return None
        import objc
        from AppKit import NSColor
        win_id = int(widget.winId())
        if not win_id:
            return None
        view = objc.objc_object(c_void_p=win_id)
        nswin = view.window() if view is not None else None
        if nswin is None:
            return None
        # Keep the frameless backing completely transparent (do not mutate styleMask,
        # which causes macOS to paint an opaque rectangular border around QNSPanel).
        nswin.setOpaque_(False)
        nswin.setBackgroundColor_(NSColor.clearColor())
        nswin.setHasShadow_(False)
        nswin.setHidesOnDeactivate_(False)
        nswin.setIgnoresMouseEvents_(True)
        nswin.setLevel_(25)  # NSStatusWindowLevel
        nswin.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 8))
        nswin.orderFrontRegardless()
    except Exception:
        pass
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


# ---- Consent (TCC) helpers -------------------------------------------------------
#
# Used by platforms.preflight: the microphone probe reads the status, and a refused start
# asks macOS to show its prompts and opens the matching System Settings pane. Every function
# is best effort and never raises: a failure means "nothing happened", and the caller still
# prints its own message. Calls go through ctypes (the Objective-C runtime and the system
# frameworks), so no extra package is needed. Nothing here binds a hook.

# AVAuthorizationStatus
AUTH_NOT_DETERMINED, AUTH_RESTRICTED, AUTH_DENIED, AUTH_AUTHORIZED = 0, 1, 2, 3

_PRIVACY_PANES = {
    "accessibility": "Privacy_Accessibility",
    "input_monitoring": "Privacy_ListenEvent",
    "microphone": "Privacy_Microphone",
}


def privacy_pane_url(pane: str) -> str:
    return "x-apple.systempreferences:com.apple.preference.security?" + _PRIVACY_PANES[pane]


def _load(name: str, path: str):
    import ctypes
    import ctypes.util

    return ctypes.cdll.LoadLibrary(ctypes.util.find_library(name) or path)


def _objc():
    """The Objective-C runtime, with Foundation loaded so NSString and NSDictionary exist."""
    import ctypes

    objc = _load("objc", "/usr/lib/libobjc.A.dylib")
    _load("Foundation", "/System/Library/Frameworks/Foundation.framework/Foundation")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(objc, restype, argtypes, receiver, selector: bytes, *args):
    """objc_msgSend with an explicit signature; ``selector`` is the method name."""
    import ctypes

    objc.objc_msgSend.restype = restype
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
    return objc.objc_msgSend(receiver, objc.sel_registerName(selector), *args)


def _nsstring(objc, text: str):
    import ctypes

    return _send(objc, ctypes.c_void_p, [ctypes.c_char_p], objc.objc_getClass(b"NSString"),
                 b"stringWithUTF8String:", text.encode("utf-8"))


def audio_authorization_status():
    """AVFoundation's microphone status (``AUTH_*``), or None when it cannot be read.

    Reading the status never shows a prompt; the first capture does that.
    """
    try:
        import ctypes

        objc = _objc()
        _load("AVFoundation", "/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        device = objc.objc_getClass(b"AVCaptureDevice")
        media_type = _nsstring(objc, "soun")  # AVMediaTypeAudio
        if not device or not media_type:
            return None
        return int(_send(objc, ctypes.c_long, [ctypes.c_void_p], device,
                         b"authorizationStatusForMediaType:", media_type))
    except Exception:
        return None


def request_accessibility_prompt() -> bool:
    """AXIsProcessTrustedWithOptions with the prompt option: macOS shows its consent dialog."""
    try:
        import ctypes

        services = _load("ApplicationServices",
                         "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        objc = _objc()
        key = _nsstring(objc, "AXTrustedCheckOptionPrompt")  # kAXTrustedCheckOptionPrompt
        yes = _send(objc, ctypes.c_void_p, [ctypes.c_int], objc.objc_getClass(b"NSNumber"),
                    b"numberWithBool:", 1)
        options = _send(objc, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p],
                        objc.objc_getClass(b"NSDictionary"), b"dictionaryWithObject:forKey:", yes, key)
        if not options:
            return False
        services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(services.AXIsProcessTrustedWithOptions(options))
    except Exception:
        return False


def request_input_monitoring_prompt() -> bool:
    """IOHIDRequestAccess(listen events): macOS shows its Input Monitoring dialog."""
    try:
        import ctypes

        iokit = _load("IOKit", "/System/Library/Frameworks/IOKit.framework/IOKit")
        request = iokit.IOHIDRequestAccess
        request.restype = ctypes.c_bool
        request.argtypes = [ctypes.c_uint32]
        return bool(request(1))  # kIOHIDRequestTypeListenEvent
    except Exception:
        return False


def open_privacy_pane(pane: str) -> bool:
    """Open System Settings at one Privacy & Security pane. True when ``open`` succeeded."""
    try:
        result = subprocess.run(["open", privacy_pane_url(pane)], check=False, timeout=5,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def responsible_program_name() -> str:
    """How to name the program whose switch the person must turn on."""
    try:
        name = os.path.basename(os.path.realpath(sys.executable))
    except Exception:
        name = ""
    if name:
        return f'"{name}" (or the terminal app that started Odicto)'
    return "your terminal app or Python"
