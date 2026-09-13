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
    foreground_is_terminal,
    is_pressed,
    send_backspaces,
    send_copy,
    send_copy_terminal,
    send_paste,
    send_text,
    send_text_bulk,
    wm_copy_foreground,
)

# Live paste, F7 clipboard restore, and AI selection copy must not interleave.
_CLIPBOARD_LOCK = threading.RLock()

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


def _clipboard_write_verified(text: str, attempts: int = 3) -> bool:
    """Write the clipboard and read it back, retrying while it is busy.

    On Windows ``OpenClipboard`` fails transiently when another app (clipboard
    manager, remote desktop, browser) holds the clipboard open. A blind write
    then pastes stale content, so every write is verified with a read-back.
    """
    for _ in range(max(1, int(attempts))):
        if _clipboard_write(text) and _clipboard_read() == text:
            return True
        time.sleep(0.02)
    return _clipboard_read() == text


def clipboard_snapshot() -> str:
    """Read the clipboard under the process-wide clipboard lock."""
    with _CLIPBOARD_LOCK:
        return _clipboard_read()


def _terminal_target() -> bool:
    """True when the focused window is a terminal.

    Terminals share no paste chord (Ctrl+V / Ctrl+Shift+V / Shift+Insert all
    differ, and a TUI can claim the chord outright) and treat Ctrl+C as SIGINT,
    so Odicto types into them instead. Detection is best-effort: an unresolved
    window reports False and the caller keeps the normal chord behavior.
    """
    if not Config.TYPE_IN_TERMINAL:
        return False
    try:
        return bool(foreground_is_terminal(Config.EXTRA_TERMINAL_APPS))
    except Exception:
        return False


def clipboard_restore(text: str) -> None:
    """Write the clipboard under the process-wide clipboard lock."""
    with _CLIPBOARD_LOCK:
        if not _clipboard_write_verified(text or ""):
            print("Warning: Failed to restore clipboard", flush=True)


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
    3. Check native foreground copy (WM_COPY) or trigger platform copy chord —
       Ctrl+Shift+C instead of Ctrl+C when the target is a terminal, where
       plain Ctrl+C is SIGINT.
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
    terminal = _terminal_target()
    original_clipboard = _clipboard_read()
    if original_clipboard is None:
        original_clipboard = ""

    sentinel = f"\ufeffodicto-sel-{uuid.uuid4().hex}\ufeff"
    if not _clipboard_write(sentinel):
        print("Warning: could not write clipboard sentinel; selection probe degraded", flush=True)
        return _get_selected_text_legacy(original_clipboard, timeout, terminal)

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
        # In a terminal this is Ctrl+Shift+C — plain Ctrl+C is SIGINT and would
        # interrupt the running command instead of copying the selection.
        copy_label = "ctrl+shift+c" if terminal else "ctrl+c"
        if selected == sentinel:
            selected = _copy_chord_until_change(sentinel, timeout, terminal)
            if selected != sentinel and (selected or "").strip():
                path = copy_label

        # One retry: chord still polluted or the app was slow to copy.
        if selected == sentinel:
            _wait_modifiers_up(0.08)
            selected = _copy_chord_until_change(
                sentinel, min(timeout, 0.25), terminal
            )
            if selected != sentinel and (selected or "").strip():
                path = f"{copy_label}-retry"

    finally:
        if not _clipboard_write_verified(original_clipboard, attempts=5):
            print("Warning: Failed to restore original clipboard after selection probe", flush=True)

    if not selected or selected == sentinel or not selected.strip():
        print("Context: selection empty (sentinel unchanged)", flush=True)
        return ""
    print(f"Context: selection via {path}", flush=True)
    return selected


def _copy_chord_until_change(
    sentinel: str, timeout: float, terminal: bool = False
) -> str:
    force_release_modifiers()
    try:
        if terminal:
            send_copy_terminal()
        else:
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


def _get_selected_text_legacy(
    original_clipboard: str, timeout: float, terminal: bool = False
) -> str:
    """Fallback when sentinel write fails: old change-vs-original logic."""
    selected = original_clipboard
    try:
        force_release_modifiers()
        if terminal:
            send_copy_terminal()
        else:
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
        _clipboard_write_verified(original_clipboard, attempts=5)

    if selected == original_clipboard or not (selected or "").strip():
        return ""
    return selected


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    """Inject text at the cursor, typing into terminals and pasting elsewhere.

    Terminals reject or reassign the paste chord, so there the text is typed
    directly and the user's clipboard is never touched. Everywhere else the
    text goes through the clipboard + paste chord; hold-to-talk restores the
    clipboard after a settle delay, while live caret updates pass
    ``restore_clipboard=False`` and restore once at F7 stop.
    """
    if not text:
        return

    with _CLIPBOARD_LOCK:
        if _terminal_target():
            _wait_modifiers_up(0.08)
            if send_text_bulk(text):
                print(">>> Terminal target: typed text (clipboard untouched)", flush=True)
                return
            print(
                "Warning: typing into terminal failed; falling back to paste chord",
                flush=True,
            )

        original_clipboard = _clipboard_read() if restore_clipboard else None

        try:
            if not _clipboard_write_verified(text):
                print("Error: Failed to write paste payload to clipboard", flush=True)
                return

            _wait_modifiers_up(0.08)

            time.sleep(0.02 if restore_clipboard else 0.008)
            try:
                send_paste()
            except Exception as e:
                print(f"Error: Failed to perform paste simulation: {e}", flush=True)

            if restore_clipboard:
                # Floor 0.15s: SendInput only queues the paste chord — a
                # browser contenteditable (X, etc.) drains it async, so a
                # 0.05s settle can restore the clipboard before the app has
                # read the payload (truncation) or leave the payload behind
                # for the next compose to resurrect.
                delay = max(0.15, float(Config.PASTE_DELAY_SECONDS))
                time.sleep(delay)
            else:
                time.sleep(0.015)
        except Exception as e:
            print(f"Error: Failed to perform paste simulation: {e}", flush=True)
        finally:
            if restore_clipboard:
                if not _clipboard_write_verified(original_clipboard or "", attempts=5):
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


def get_clipboard_image(max_dim: int = 1600) -> Optional[bytes]:
    """Retrieve image bytes (PNG format) from the system clipboard under lock."""
    with _CLIPBOARD_LOCK:
        return _get_clipboard_image_locked(max_dim=max_dim)


def _get_clipboard_image_locked(max_dim: int = 1600) -> Optional[bytes]:
    """Inspects clipboard for image data and returns PNG bytes."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            cb = app.clipboard()
            if cb is not None:
                img = cb.image()
                if not img.isNull() and img.width() > 0 and img.height() > 0:
                    if max_dim and (img.width() > max_dim or img.height() > max_dim):
                        img = img.scaled(
                            max_dim,
                            max_dim,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QIODevice.OpenModeFlag.WriteOnly)
                    if img.save(buf, "PNG"):
                        buf.close()
                        data = bytes(ba.data())
                        if data and len(data) > 0:
                            return data
                    buf.close()
    except Exception as e:
        print(f"Notice: Qt clipboard image probe failed: {e}", flush=True)
    return None


def capture_ai_context(timeout: float = 0.35) -> tuple[str, Optional[bytes]]:
    """Capture selected text and/or clipboard image without destroying clipboard bitmap.

    1. Snapshot pre-existing clipboard image (e.g. screenshot via Win+Shift+S / Cmd+Shift+4).
    2. Probe for selected text in the active application.
    3. Return (selected_text, image_bytes).
    """
    with _CLIPBOARD_LOCK:
        image_bytes = _get_clipboard_image_locked(max_dim=1600)
        selected_text = get_selected_text(timeout=timeout)
        return selected_text, image_bytes
