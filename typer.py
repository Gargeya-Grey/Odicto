"""Clipboard helpers: paste injection and selected-text capture for AI context."""

from __future__ import annotations

import sys
import time
import uuid

import keyboard
import pyperclip

from config import Config

# Modifier names to clear before a synthetic Ctrl+C. AI mode holds ctrl+shift
# during capture; if those stay down, Ctrl+C becomes Ctrl+Shift+C and never copies.
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


def _force_release_modifiers() -> None:
    """Synthesize key-ups for modifiers that may still be physically held."""
    for key in _MODIFIER_KEYS:
        try:
            if keyboard.is_pressed(key):
                keyboard.release(key)
        except Exception:
            try:
                keyboard.release(key)
            except Exception:
                pass
    # Brief settle so the target app sees a clean key state before copy.
    time.sleep(0.04)


def _wm_copy_foreground() -> bool:
    """Ask the focused window to copy via WM_COPY (no synthetic keystrokes).

    Many Win32 / common controls honor this even when keyboard chords are messy.
    Returns True if the message was sent (not a guarantee the app copied).
    """
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


def _clipboard_read() -> str:
    try:
        value = pyperclip.paste()
        return value if isinstance(value, str) else ("" if value is None else str(value))
    except Exception:
        return ""


def _clipboard_write(text: str) -> bool:
    try:
        pyperclip.copy(text if text is not None else "")
        return True
    except Exception:
        return False


def get_selected_text(timeout: float = 0.50) -> str:
    """Copy the current selection and return it (restores clipboard after).

    Robust against the AI hold-to-talk chord:

    1. Write a unique **sentinel** to the clipboard so we detect a copy even when
       the selection already equals the previous clipboard contents.
    2. Release held Ctrl/Shift/Alt so a synthetic Ctrl+C is not polluted by the
       AI chord (Ctrl+Shift+` → release ` still leaves Ctrl+Shift down).
    3. Prefer ``WM_COPY`` to the foreground window (no keystrokes).
    4. Fall back to ``Ctrl+C`` if the clipboard still holds the sentinel.
    5. Always restore the user's original clipboard.

    Must **not** be called from inside a ``WH_KEYBOARD_LL`` hook callback —
    nested SendInput while the hook is still running often fails silently.
    Call from a worker thread after the hook returns.

    Args:
        timeout: Max seconds to wait for the clipboard to update after each copy attempt.

    Returns:
        Selected text, or ``""`` when nothing usable was captured.
    """
    original_clipboard = _clipboard_read()
    if original_clipboard is None:
        original_clipboard = ""

    # Unique sentinel (not valid user text) so equality with prior clipboard is fine.
    sentinel = f"\ufeffodicto-sel-{uuid.uuid4().hex}\ufeff"
    if not _clipboard_write(sentinel):
        # Can't control clipboard — best-effort Ctrl+C without sentinel.
        print("Warning: could not write clipboard sentinel; selection probe degraded", flush=True)
        return _get_selected_text_legacy(original_clipboard, timeout)

    selected = sentinel
    try:
        _force_release_modifiers()

        # Path A: WM_COPY (reliable for many native apps; no chord conflicts).
        if _wm_copy_foreground():
            selected = _poll_clipboard_change(sentinel, timeout=min(0.25, timeout))

        # Path B: synthetic Ctrl+C if WM_COPY did nothing.
        if selected == sentinel:
            _force_release_modifiers()
            try:
                # Explicit press/release sequence is more reliable than send("ctrl+c")
                # when the low-level hook stack is active.
                keyboard.press("ctrl")
                time.sleep(0.01)
                keyboard.press_and_release("c")
                time.sleep(0.01)
                keyboard.release("ctrl")
            except Exception as e:
                print(f"Error: Failed to send Ctrl+C for selection: {e}", flush=True)
                try:
                    keyboard.send("ctrl+c")
                except Exception as e2:
                    print(f"Error: keyboard.send Ctrl+C also failed: {e2}", flush=True)
            selected = _poll_clipboard_change(sentinel, timeout=timeout)

        # Path C: last-chance short Ctrl+C with a slightly longer wait.
        if selected == sentinel:
            try:
                keyboard.send("ctrl+c")
            except Exception:
                pass
            selected = _poll_clipboard_change(sentinel, timeout=min(0.20, timeout))
    finally:
        if not _clipboard_write(original_clipboard):
            print("Warning: Failed to restore original clipboard after selection probe", flush=True)

    if not selected or selected == sentinel:
        return ""
    if not selected.strip():
        return ""
    return selected


def _poll_clipboard_change(sentinel: str, timeout: float) -> str:
    """Poll until clipboard differs from sentinel, or timeout. Returns last read."""
    deadline = time.time() + max(0.05, float(timeout))
    last = sentinel
    while time.time() < deadline:
        time.sleep(0.03)
        cur = _clipboard_read()
        if cur != sentinel:
            # Allow multi-chunk clipboard writers a beat to finish.
            time.sleep(0.025)
            cur2 = _clipboard_read()
            return cur2 if cur2 != sentinel else cur
        last = cur
    return last


def _get_selected_text_legacy(original_clipboard: str, timeout: float) -> str:
    """Fallback when sentinel write fails: old change-vs-original logic."""
    selected = original_clipboard
    try:
        _force_release_modifiers()
        keyboard.send("ctrl+c")
        deadline = time.time() + max(0.08, float(timeout))
        while time.time() < deadline:
            time.sleep(0.04)
            cur = _clipboard_read()
            if cur != original_clipboard:
                selected = cur
                break
        else:
            selected = _clipboard_read()
    except Exception as e:
        print(f"Error: Failed to copy selection: {e}", flush=True)
        selected = original_clipboard
    finally:
        _clipboard_write(original_clipboard)

    if selected == original_clipboard or not (selected or "").strip():
        return ""
    return selected


def paste_text(text: str) -> None:
    """Inject text at the cursor via clipboard + Ctrl+V, then restore clipboard."""
    if not text:
        return

    original_clipboard = _clipboard_read()

    try:
        if not _clipboard_write(text):
            print("Error: Failed to write paste payload to clipboard", flush=True)
            return

        # Ensure modifiers from the AI chord are not still down (Ctrl+Shift+V ≠ paste).
        _force_release_modifiers()

        # Brief settle so the OS clipboard has the new payload before paste.
        time.sleep(0.02)
        try:
            keyboard.press("ctrl")
            time.sleep(0.01)
            keyboard.press_and_release("v")
            time.sleep(0.01)
            keyboard.release("ctrl")
        except Exception:
            keyboard.send("ctrl+v")

        # Give the focused app time to read clipboard before we restore it.
        delay = max(0.02, float(Config.PASTE_DELAY_SECONDS))
        time.sleep(delay)
    except Exception as e:
        print(f"Error: Failed to perform paste simulation: {e}", flush=True)
    finally:
        if not _clipboard_write(original_clipboard):
            print("Warning: Failed to restore original clipboard after paste", flush=True)
