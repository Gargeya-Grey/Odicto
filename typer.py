"""Clipboard helpers: paste injection and selected-text capture for AI context."""

from __future__ import annotations

import time
import uuid

from config import Config
from platforms import (
    clipboard_read,
    clipboard_write,
    force_release_modifiers,
    send_backspaces,
    send_copy,
    send_paste,
    wm_copy_foreground,
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
    original_clipboard = _clipboard_read()
    if original_clipboard is None:
        original_clipboard = ""

    sentinel = f"\ufeffodicto-sel-{uuid.uuid4().hex}\ufeff"
    if not _clipboard_write(sentinel):
        print("Warning: could not write clipboard sentinel; selection probe degraded", flush=True)
        return _get_selected_text_legacy(original_clipboard, timeout)

    selected = sentinel
    try:
        force_release_modifiers()

        # Path A: fast native foreground-copy if supported and immediate.
        # SendMessageW WM_COPY is synchronous: if the control handles it, the
        # clipboard updates immediately before the call returns.
        if wm_copy_foreground():
            cur = _clipboard_read()
            if cur != sentinel and cur.strip():
                selected = cur

        # Path B: synthetic copy chord (fastest & universal for modern apps).
        if selected == sentinel:
            force_release_modifiers()
            try:
                send_copy()
            except Exception as e:
                print(f"Error: Failed to send copy chord for selection: {e}", flush=True)
            selected = _poll_clipboard_change(sentinel, timeout=timeout)

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


def paste_text(text: str) -> None:
    """Inject text at the cursor via clipboard + paste chord, then restore clipboard."""
    if not text:
        return

    original_clipboard = _clipboard_read()

    try:
        if not _clipboard_write(text):
            print("Error: Failed to write paste payload to clipboard", flush=True)
            return

        force_release_modifiers()

        time.sleep(0.02)
        try:
            send_paste()
        except Exception as e:
            print(f"Error: Failed to perform paste simulation: {e}", flush=True)

        delay = max(0.02, float(Config.PASTE_DELAY_SECONDS))
        time.sleep(delay)
    except Exception as e:
        print(f"Error: Failed to perform paste simulation: {e}", flush=True)
    finally:
        if not _clipboard_write(original_clipboard):
            print("Warning: Failed to restore original clipboard after paste", flush=True)


def apply_live_text(current: str, desired: str) -> str:
    """Bring the caret from ``current`` to ``desired`` with minimal edits.

    Shares a common prefix, backspaces the tail, then pastes the remainder.
    Used by F7 live dictation so text appears in the focused field as you speak.
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
        paste_text(add)
    return desired
