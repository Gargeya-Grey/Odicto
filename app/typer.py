"""Clipboard helpers: paste injection and selected-text capture for AI context."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from config import Config
from platforms import (
    clipboard_read,
    clipboard_write,
    force_release_modifiers,
    foreground_is_ide_host,
    foreground_is_terminal,
    is_pressed,
    send_copy,
    send_copy_ide,
    send_copy_terminal,
    send_paste,
    send_text_bulk,
    wm_copy_foreground,
)
from platforms.clipboard import (
    ClipboardSnapshot,
    clipboard_change_token,
    clipboard_restore,
    clipboard_snapshot,
)

# Paste and AI selection copy must not interleave. Also guards _PENDING.
_CLIPBOARD_LOCK = threading.RLock()

# Text no longer than this is typed instead of pasted when the clipboard holds
# data that cannot be saved and restored (see paste_text).
_TYPE_INSTEAD_MAX_CHARS = 2000

# The deferred restore runs on a daemon thread. Tests that drive paste_text
# with a fake clipboard set this False to run the same restore inline.
_RESTORE_IN_BACKGROUND = True

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


@dataclass
class _PendingRestore:
    """A paste whose clipboard restore has not run yet."""

    snapshot: ClipboardSnapshot  # the user's clipboard before the paste
    payload: str  # the text Odicto put on the clipboard
    token: Optional[int]  # clipboard_change_token() right after the payload write


_PENDING: Optional[_PendingRestore] = None


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


def _change_token() -> Optional[int]:
    try:
        return clipboard_change_token()
    except Exception:
        return None


def _snapshot(attempts: int = 3) -> ClipboardSnapshot:
    """Snapshot the user's clipboard, retrying briefly while it is busy."""
    snap = None
    for attempt in range(max(1, attempts)):
        try:
            snap = clipboard_snapshot()
        except Exception:
            snap = None
        if snap is not None and snap.ok:
            return snap
        if attempt + 1 < attempts:
            time.sleep(0.03)
    return ClipboardSnapshot(text="", formats=(), complete=False, has_non_text=False, ok=False)


def _restore_snapshot(snap: ClipboardSnapshot, context: str) -> bool:
    """Put the user's clipboard back. Never writes a fake empty clipboard.

    * Unreadable original (``ok=False``): nothing is written; the payload stays.
    * Non-text formats captured in full: full-format restore.
    * Really empty original: the platform's own "empty clipboard".
    * Text: the verified text write.
    """
    try:
        if not snap.ok:
            print(
                f"Warning: original clipboard was unreadable; not restoring {context}",
                flush=True,
            )
            return False
        ok = False
        if snap.has_non_text and snap.complete:
            ok = bool(clipboard_restore(snap))
            if not ok and snap.text:
                ok = _clipboard_write_verified(snap.text, attempts=5)
        elif snap.has_non_text:
            # Incomplete: the non-text part is already gone. Keep the text.
            if snap.text:
                ok = _clipboard_write_verified(snap.text, attempts=5)
            else:
                print(
                    f"Warning: clipboard held only data Odicto cannot save; "
                    f"not restoring {context}",
                    flush=True,
                )
                return False
        elif snap.text == "":
            ok = bool(clipboard_restore(snap)) or _clipboard_write_verified("", attempts=5)
        else:
            ok = _clipboard_write_verified(snap.text, attempts=5)
        if not ok:
            print(f"Warning: Failed to restore original clipboard {context}", flush=True)
        return ok
    except Exception as e:
        print(f"Warning: clipboard restore {context} failed: {e}", flush=True)
        return False


def _clipboard_holds_payload(pending: _PendingRestore) -> bool:
    """True while nobody has changed the clipboard since Odicto wrote its payload."""
    if pending.token is not None:
        current = _change_token()
        if current is not None:
            return current == pending.token
    return _clipboard_read() == pending.payload


def _finish_pending(pending: _PendingRestore, context: str) -> None:
    """Restore for ``pending`` only if the clipboard still holds its payload."""
    if _clipboard_holds_payload(pending):
        _restore_snapshot(pending.snapshot, context)
    else:
        print(
            "Notice: clipboard changed after paste; keeping the new contents",
            flush=True,
        )


def _cancel_pending() -> Optional[_PendingRestore]:
    """Take the pending restore away from its thread. Caller holds the lock."""
    global _PENDING
    pending, _PENDING = _PENDING, None
    return pending


def _take_original() -> tuple:
    """(snapshot of the user's clipboard, came_from_pending). Caller holds the lock.

    A pending restore means the clipboard still holds Odicto's previous payload,
    not the user's data. Its original snapshot is reused, so a back-to-back paste
    or probe never saves Odicto's own text as "the user's clipboard". When the
    clipboard changed since that paste, the new contents are the user's.
    """
    pending = _cancel_pending()
    if pending is not None and _clipboard_holds_payload(pending):
        return pending.snapshot, True
    return _snapshot(), False


def _deferred_restore(pending: _PendingRestore, delay: float) -> None:
    global _PENDING
    time.sleep(delay)
    with _CLIPBOARD_LOCK:
        if _PENDING is not pending:
            return  # a later paste/probe/flush took it over
        _PENDING = None
        try:
            _finish_pending(pending, "after paste")
        except Exception as e:
            print(f"Warning: deferred clipboard restore failed: {e}", flush=True)


def _schedule_restore(pending: _PendingRestore) -> None:
    """Restore after the target app has had time to read the payload.

    SendInput only queues the paste chord; Electron apps, busy browsers and RDP
    sessions read the clipboard much later. The restore waits on a background
    thread so paste_text returns at once, and is skipped if the clipboard
    changed meanwhile (the user copied something, or the app wrote to it).
    """
    global _PENDING
    _PENDING = pending
    delay = max(0.15, float(Config.PASTE_DELAY_SECONDS))
    if not _RESTORE_IN_BACKGROUND:
        _deferred_restore(pending, delay)
        return
    try:
        threading.Thread(
            target=_deferred_restore,
            args=(pending, delay),
            name="odicto-clipboard-restore",
            daemon=True,
        ).start()
    except Exception as e:
        print(f"Warning: could not defer clipboard restore ({e}); restoring now", flush=True)
        if _PENDING is pending:
            _PENDING = None
            _finish_pending(pending, "after paste")


def flush_pending_restore() -> None:
    """Run any pending deferred clipboard restore now (call at shutdown)."""
    try:
        with _CLIPBOARD_LOCK:
            pending = _cancel_pending()
            if pending is not None:
                _finish_pending(pending, "at flush")
    except Exception as e:
        print(f"Warning: clipboard restore flush failed: {e}", flush=True)


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


def _ide_target() -> bool:
    """True when the focused window is an IDE that hosts a terminal (VS Code, JetBrains).

    The selection probe sends Ctrl+Insert there: plain Ctrl+C in the integrated
    terminal is SIGINT. Unresolved windows report False.
    """
    try:
        return bool(foreground_is_ide_host())
    except Exception:
        return False


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

    1. Snapshot the user's clipboard in every format the platform can restore.
    2. Write a unique **sentinel** to the clipboard so we detect a copy even when
       the selection already equals the previous clipboard contents.
    3. Release held modifiers so a synthetic copy chord is not polluted by the
       AI chord.
    4. Check native foreground copy (WM_COPY) or trigger platform copy chord —
       Ctrl+Shift+C in a terminal and Ctrl+Insert in an IDE host (VS Code,
       JetBrains), where plain Ctrl+C is SIGINT.
    5. Poll clipboard with low latency for the captured selection.
    6. Always restore the user's original clipboard.

    The probe is skipped (returns ``""``) when the clipboard cannot be read, or
    holds data the platform cannot save: the sentinel would destroy it.

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
    ide = False if terminal else _ide_target()
    original, from_pending = _take_original()
    if not original.ok:
        print("Warning: clipboard unreadable; selection probe skipped", flush=True)
        return ""
    if original.has_non_text and not original.complete:
        if from_pending:
            _restore_snapshot(original, "after skipped selection probe")
        print(
            "Notice: clipboard holds data Odicto cannot save; selection probe skipped",
            flush=True,
        )
        return ""

    sentinel = f"﻿odicto-sel-{uuid.uuid4().hex}﻿"
    if not _clipboard_write(sentinel):
        print("Warning: could not write clipboard sentinel; selection probe degraded", flush=True)
        return _get_selected_text_legacy(original, timeout, terminal, ide)

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
        # In a terminal this is Ctrl+Shift+C and in an IDE host Ctrl+Insert —
        # plain Ctrl+C is SIGINT and would interrupt the running command.
        copy_label = "ctrl+shift+c" if terminal else ("ctrl+insert" if ide else "ctrl+c")
        if selected == sentinel:
            selected = _copy_chord_until_change(sentinel, timeout, terminal, ide)
            if selected != sentinel and (selected or "").strip():
                path = copy_label

        # One retry: chord still polluted or the app was slow to copy.
        if selected == sentinel:
            _wait_modifiers_up(0.08)
            selected = _copy_chord_until_change(
                sentinel, min(timeout, 0.25), terminal, ide
            )
            if selected != sentinel and (selected or "").strip():
                path = f"{copy_label}-retry"

    finally:
        _restore_snapshot(original, "after selection probe")

    if not selected or selected == sentinel or not selected.strip():
        print("Context: selection empty (sentinel unchanged)", flush=True)
        return ""
    print(f"Context: selection via {path}", flush=True)
    return selected


def _send_copy_chord(terminal: bool = False, ide: bool = False) -> None:
    if terminal:
        send_copy_terminal()
    elif ide:
        send_copy_ide()
    else:
        send_copy()


def _copy_chord_until_change(
    sentinel: str, timeout: float, terminal: bool = False, ide: bool = False
) -> str:
    force_release_modifiers()
    try:
        _send_copy_chord(terminal, ide)
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
    original: ClipboardSnapshot, timeout: float, terminal: bool = False, ide: bool = False
) -> str:
    """Fallback when sentinel write fails: old change-vs-original logic."""
    original_text = original.text
    selected = original_text
    try:
        force_release_modifiers()
        _send_copy_chord(terminal, ide)
        deadline = time.time() + max(0.05, float(timeout))
        while time.time() < deadline:
            time.sleep(0.02)
            cur = _clipboard_read()
            if cur != original_text:
                selected = cur
                break
        else:
            selected = _clipboard_read()
    except Exception as e:
        print(f"Error: Failed to copy selection: {e}", flush=True)
        selected = original_text
    finally:
        _restore_snapshot(original, "after selection probe")

    if selected == original_text or not (selected or "").strip():
        return ""
    return selected


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    """Inject text at the cursor, typing into terminals and pasting elsewhere.

    Terminals reject or reassign the paste chord, so there the text is typed
    directly and the user's clipboard is never touched. Everywhere else the
    text goes through the clipboard + paste chord, and hold-to-talk restores
    the user's clipboard (every format the platform can save) on a background
    thread after ``PASTE_DELAY_SECONDS``, only if nobody changed it meanwhile.
    When the clipboard holds data that cannot be saved (an image or file list
    on Linux), short text is typed instead so that data survives.

    F7 passes ``restore_clipboard=False`` and leaves its final payload
    available for asynchronous paste consumers.
    """
    if not text:
        return

    with _CLIPBOARD_LOCK:
        if _terminal_target():
            _wait_modifiers_up(0.08)
            if send_text_bulk(text):
                print(">>> Terminal target: typed text (clipboard untouched)", flush=True)
                return
            raise RuntimeError("Could not type into terminal; no paste chord was sent")

        if not restore_clipboard:
            _cancel_pending()
            if not _clipboard_write_verified(text):
                raise RuntimeError("Could not write the paste payload to clipboard")
            _wait_modifiers_up(0.08)
            time.sleep(0.008)
            send_paste()
            time.sleep(0.015)
            return

        original, from_pending = _take_original()

        if original.ok and original.has_non_text and not original.complete:
            if len(text) <= _TYPE_INSTEAD_MAX_CHARS:
                _wait_modifiers_up(0.08)
                # A RuntimeError here means a partial injection: do not paste a
                # second copy on top of it.
                if send_text_bulk(text):
                    if from_pending:
                        _restore_snapshot(original, "after typing")
                    print(
                        ">>> Clipboard holds data Odicto cannot save: typed text instead",
                        flush=True,
                    )
                    return
            print(
                "Warning: clipboard holds data Odicto cannot save; pasting anyway "
                "(only its text will come back)",
                flush=True,
            )

        if not _clipboard_write_verified(text):
            _restore_snapshot(original, "after failed paste")
            raise RuntimeError("Could not write the paste payload to clipboard")
        token = _change_token()

        try:
            _wait_modifiers_up(0.08)
            time.sleep(0.02)
            send_paste()
        except BaseException:
            _restore_snapshot(original, "after failed paste")
            raise

        _schedule_restore(_PendingRestore(snapshot=original, payload=text, token=token))


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
