"""Clipboard helpers: paste injection and selected-text capture for AI context."""

from __future__ import annotations

import threading
import time
import uuid

from config import Config
from platforms import (
    clipboard_read,
    clipboard_write,
    force_release_modifiers,
    is_pressed,
    send_backspaces,
    send_copy,
    send_paste,
    send_text,
    wm_copy_foreground,
)

# Live paste, F7 clipboard restore, and AI selection copy must not interleave.
_CLIPBOARD_LOCK = threading.Lock()

_MODIFIER_POLL_KEYS = (
    "ctrl",
    "shift",
    "alt",
    "cmd",
    "win",
    "left ctrl",
    "right ctrl",
    "left shift",
    "right shift",
    "left alt",
    "right alt",
)


def _clipboard_read() -> str:
    try:
        return clipboard_read()
    except Exception:
        return ""


def _clipboard_write(text: str) -> bool:
    try:
        return clipboard_write(text)
    except Exception:
        return False


def clipboard_snapshot() -> str:
    """Read the clipboard under the process-wide clipboard lock."""
    with _CLIPBOARD_LOCK:
        return _clipboard_read()


def clipboard_restore(text: str) -> None:
    """Write the clipboard under the process-wide clipboard lock."""
    with _CLIPBOARD_LOCK:
        _clipboard_write(text or "")


def _wait_modifiers_up(timeout: float = 0.08) -> None:
    """Release modifiers, then poll until they are actually up (or timeout)."""
    force_release_modifiers()
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        held = False
        for key in _MODIFIER_POLL_KEYS:
            try:
                if is_pressed(key):
                    held = True
                    break
            except Exception:
                continue
        if not held:
            return
        time.sleep(0.01)


def get_selected_text(timeout: float = 0.35) -> str:
    """Copy the current selection and return it (restores clipboard after).

    Robust against the AI hold-to-talk chord:

    1. Write a unique **sentinel** to the clipboard so we detect a copy even when
       the selection already equals the previous clipboard contents.
    2. Release held modifiers so a synthetic copy chord is not polluted by the
       AI chord.
    3. Check native foreground copy (WM_COPY) or trigger platform copy chord.
    4. Poll clipboard with low latency for the captured selection.
    5. Always restore the user's original clipboard.

    Must **not** be called from inside a keyboard-hook callback — nested synthetic
    input while the hook is still running often fails silently. Call from a worker
    thread after the hook returns.

    Args:
        timeout: Max seconds to wait for the clipboard to update after each copy attempt.

    Returns:
        Selected text, or ``""`` when nothing usable was captured.
    """
    with _CLIPBOARD_LOCK:
        return _get_selected_text_locked(timeout)


def _get_selected_text_locked(timeout: float) -> str:
    original_clipboard = _clipboard_read()
    if original_clipboard is None:
        original_clipboard = ""

    sentinel = f"\ufeffodicto-sel-{uuid.uuid4().hex}\ufeff"
    if not _clipboard_write(sentinel):
        print("Warning: could not write clipboard sentinel; selection probe degraded", flush=True)
        return _get_selected_text_legacy(original_clipboard, timeout)

    selected = sentinel
    path = "empty"
    try:
        _wait_modifiers_up(0.08)

        # Path A: fast native foreground-copy if supported and immediate.
        # SendMessageW WM_COPY is synchronous: if the control handles it, the
        # clipboard updates immediately before the call returns.
        if wm_copy_foreground():
            cur = _clipboard_read()
            if cur != sentinel and cur.strip():
                selected = cur
                path = "WM_COPY"

        # Path B: synthetic copy chord (fastest & universal for modern apps).
        if selected == sentinel:
            selected = _copy_chord_until_change(sentinel, timeout)
            if selected != sentinel and (selected or "").strip():
                path = "ctrl+c"

        # One retry: chord still polluted or the app was slow to copy.
        if selected == sentinel:
            _wait_modifiers_up(0.08)
            selected = _copy_chord_until_change(sentinel, min(timeout, 0.25))
            if selected != sentinel and (selected or "").strip():
                path = "ctrl+c-retry"

    finally:
        if not _clipboard_write(original_clipboard):
            print("Warning: Failed to restore original clipboard after selection probe", flush=True)

    if not selected or selected == sentinel or not selected.strip():
        print("Context: selection empty (sentinel unchanged)", flush=True)
        return ""
    print(f"Context: selection via {path}", flush=True)
    return selected


def _copy_chord_until_change(sentinel: str, timeout: float) -> str:
    force_release_modifiers()
    try:
        send_copy()
    except Exception as e:
        print(f"Error: Failed to send copy chord for selection: {e}", flush=True)
        return sentinel
    return _poll_clipboard_change(sentinel, timeout=timeout)


def _poll_clipboard_change(sentinel: str, timeout: float) -> str:
    """Poll until clipboard differs from sentinel, or timeout. Returns last read."""
    deadline = time.time() + max(0.04, float(timeout))
    last = sentinel
    while time.time() < deadline:
        time.sleep(0.015)
        cur = _clipboard_read()
        if cur != sentinel:
            time.sleep(0.015)
            cur2 = _clipboard_read()
            return cur2 if cur2 != sentinel else cur
        last = cur
    return last


def _get_selected_text_legacy(original_clipboard: str, timeout: float) -> str:
    """Fallback when sentinel write fails: old change-vs-original logic."""
    selected = original_clipboard
    try:
        force_release_modifiers()
        send_copy()
        deadline = time.time() + max(0.05, float(timeout))
        while time.time() < deadline:
            time.sleep(0.02)
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


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    """Inject text at the cursor via clipboard + paste chord.

    Hold-to-talk restores the user's clipboard after a settle delay. Live
    caret updates pass ``restore_clipboard=False`` and restore once at F7 stop.
    """
    if not text:
        return

    with _CLIPBOARD_LOCK:
        original_clipboard = _clipboard_read() if restore_clipboard else None

        try:
            if not _clipboard_write(text):
                print("Error: Failed to write paste payload to clipboard", flush=True)
                return

            force_release_modifiers()

            time.sleep(0.02 if restore_clipboard else 0.008)
            try:
                send_paste()
            except Exception as e:
                print(f"Error: Failed to perform paste simulation: {e}", flush=True)

            if restore_clipboard:
                delay = max(0.02, float(Config.PASTE_DELAY_SECONDS))
                time.sleep(delay)
            else:
                time.sleep(0.015)
        except Exception as e:
            print(f"Error: Failed to perform paste simulation: {e}", flush=True)
        finally:
            if restore_clipboard:
                if not _clipboard_write(original_clipboard or ""):
                    print(
                        "Warning: Failed to restore original clipboard after paste",
                        flush=True,
                    )


def apply_live_text(current: str, desired: str) -> str:
    """Bring the caret from ``current`` to ``desired`` with minimal edits.

    Shares a common prefix, backspaces the tail, then types or pastes the
    remainder. Short tails prefer ``send_text`` (no clipboard); long tails
    paste without restoring the clipboard (the F7 session restores once).
    """
    current = current or ""
    desired = desired or ""
    if current == desired:
        return desired
    i = 0
    limit = min(len(current), len(desired))
    while i < limit and current[i] == desired[i]:
        i += 1
    back = len(current) - i
    add = desired[i:]
    force_release_modifiers()
    if back:
        send_backspaces(back)
    if add:
        typed = False
        if len(add) <= 32:
            try:
                typed = bool(send_text(add))
            except Exception:
                typed = False
        if not typed:
            paste_text(add, restore_clipboard=False)
    return desired
