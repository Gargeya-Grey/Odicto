"""Clipboard helpers: paste injection and selected-text capture for AI context."""

from __future__ import annotations

import sys
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
    clipboard_read_text_token,
    clipboard_restore_result,
    clipboard_write_text_token,
    WRITTEN as WRITE_WRITTEN,
    CHANGED as RESTORE_CHANGED,
    RESTORED as RESTORE_RESTORED,
    clipboard_snapshot,
)

# Paste, its delayed restore and AI selection copy must not interleave.
_CLIPBOARD_LOCK = threading.RLock()

# Text no longer than this is typed instead of pasted when the clipboard holds
# data that cannot be saved and restored (see paste_text).
# Windows (SendInput batches) and macOS type fast enough for long text; the
# Linux keyboard backend types with a per-character delay.
_TYPE_INSTEAD_MAX_CHARS = 2000
_TYPE_INSTEAD_MAX_CHARS_FAST = 20000


def _type_instead_limit() -> int:
    if sys.platform in ("win32", "darwin"):
        return _TYPE_INSTEAD_MAX_CHARS_FAST
    return _TYPE_INSTEAD_MAX_CHARS


# Restore is always synchronous: paste_text sleeps out the paste delay while
# holding _CLIPBOARD_LOCK and restores before it returns. This attribute is
# kept only because frozen tests patch it; nothing branches on it.
_RESTORE_IN_BACKGROUND = False

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

_RESTORE_OK = "restored"
_RESTORE_FAILED = "failed"  # clipboard busy etc.: worth a retry with the same guard
_RESTORE_CHANGED = "changed"  # someone else wrote the clipboard: never retry
_RESTORE_SKIPPED = "skipped"  # nothing that can be put back: done

# A failed restore is retried for about 2 s with the same guard, then dropped:
# no state carries to the next paste (carry-over was removed after three
# review rounds found faults in it).
_RESTORE_RETRIES = 8
_RESTORE_RETRY_BACKOFF_S = 0.25

# True when the most recent paste_text call could not put the user's
# clipboard back (reset at the start of each paste).
_LAST_PASTE_RESTORE_FAILED = False


@dataclass(frozen=True)
class _Guard:
    """Identifies Odicto's own last clipboard write.

    A restore runs only while the clipboard still holds that write. ``token``
    is its change token (Windows, macOS); without one, ``payload`` (its text)
    is compared instead. A guard is never re-armed from the live clipboard:
    only a token the platform observed inside its own failed-restore session
    may replace ``token``.
    """

    token: Optional[int]
    payload: str


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


_WRITE_OK = "written"
_WRITE_FAILED = "failed"
_WRITE_CHANGED = "changed"  # someone wrote between our own attempts: nothing written


def _write_owned(text: str, verify: bool = True, attempts: int = 3) -> tuple:
    """Write Odicto's own clipboard text. Returns ``(status, guard, landed_token)``.

    Where the platform has change tokens, the write and its token come from
    one clipboard session, so the guard can never be a user copy made a
    moment later. ``landed_token`` is the token of every clipboard state this
    call left behind, including a failed write that had already emptied the
    clipboard; it is None only when nothing was touched. A retry after an
    attempt that touched the clipboard writes only while the clipboard still
    holds that attempt's state (otherwise ``changed``). Without tokens
    (Linux) the guard is the text, written through the classic verified (or,
    for a sentinel, plain) write.
    """
    if _change_token() is None:
        ok = _clipboard_write_verified(text) if verify else _clipboard_write(text)
        return (_WRITE_OK if ok else _WRITE_FAILED), _Guard(token=None, payload=text), None
    landed = None
    expect_token = None
    for attempt in range(max(1, attempts)):
        try:
            status, token = clipboard_write_text_token(text, expect_token=expect_token)
        except Exception:
            status, token = _WRITE_FAILED, None
        if status == RESTORE_CHANGED:
            return _WRITE_CHANGED, _Guard(token=landed, payload=text), landed
        if token is not None:
            landed = token
            # Our own write changed the clipboard: later attempts expect it.
            expect_token = token
        if status == WRITE_WRITTEN and token is not None:
            if not verify or _clipboard_read() == text:
                return _WRITE_OK, _Guard(token=token, payload=text), landed
        if attempt + 1 < attempts:
            time.sleep(0.02)
    return _WRITE_FAILED, _Guard(token=landed, payload=text), landed


def _observed_guard(seen: str) -> _Guard:
    """Guard for a clipboard value the probe observed (an app's copy).

    Text and token are read in one clipboard session; when they no longer
    match ``seen`` the guard is text-only and the restore will see "changed"
    rather than overwrite something newer.
    """
    if _change_token() is None:
        return _Guard(token=None, payload=seen)
    try:
        text, token = clipboard_read_text_token()
    except Exception:
        text, token = None, None
    if token is not None and text == seen:
        return _Guard(token=token, payload=seen)
    return _Guard(token=None, payload=seen)


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


def _restore_once(snap: ClipboardSnapshot, guard: _Guard) -> tuple:
    """One guarded restore attempt. Returns ``(status, guard)``.

    * Unreadable original (``ok=False``): nothing is written (skipped).
    * Non-text formats captured in full: full-format restore.
    * Incomplete non-text: only its text comes back (the rest is already gone).
    * Really empty original: the platform's own "empty clipboard".
    * Text: the platform restore, or the verified text write without a token.

    With a token the platform checks it inside its own clipboard session; a
    mismatch writes nothing ("changed"). The returned guard differs from the
    input only when a failed attempt changed the clipboard itself and the
    platform reported the token it saw inside that same session.
    """
    if not snap.ok:
        print("Warning: original clipboard was unreadable; not restoring it", flush=True)
        return _RESTORE_SKIPPED, guard
    target = snap
    if snap.has_non_text and not snap.complete:
        if not snap.text:
            print(
                "Warning: clipboard held only data Odicto cannot save; not restoring it",
                flush=True,
            )
            return _RESTORE_SKIPPED, guard
        target = ClipboardSnapshot(
            text=snap.text, formats=(), complete=True, has_non_text=False, ok=True
        )

    if guard.token is not None:
        status, token_after = clipboard_restore_result(target, expect_token=guard.token)
        if status == RESTORE_RESTORED:
            return _RESTORE_OK, guard
        if status == RESTORE_CHANGED:
            return _RESTORE_CHANGED, guard
        if token_after is not None:
            guard = _Guard(token=token_after, payload=guard.payload)
        return _RESTORE_FAILED, guard

    # No change token (Linux, or the platform could not report one): the
    # payload text is the guard.
    if _clipboard_read() != guard.payload:
        return _RESTORE_CHANGED, guard
    if target.has_non_text:
        ok = bool(clipboard_restore(target))
        if not ok and target.text:
            ok = _clipboard_write_verified(target.text, attempts=5)
    elif target.text == "":
        ok = bool(clipboard_restore(target)) or _clipboard_write_verified("", attempts=5)
    else:
        ok = _clipboard_write_verified(target.text, attempts=5)
    return (_RESTORE_OK if ok else _RESTORE_FAILED), guard


def _guarded_restore(snap: ClipboardSnapshot, guard: _Guard, context: str) -> str:
    """Restore ``snap`` while ``guard`` still holds; retry a failure with the same guard.

    Caller holds _CLIPBOARD_LOCK. A "changed" outcome stops at once. When every
    retry fails the restore is dropped with a warning; nothing is kept.
    """
    status = _RESTORE_FAILED
    for attempt in range(_RESTORE_RETRIES):
        try:
            status, guard = _restore_once(snap, guard)
        except Exception as e:
            print(f"Warning: clipboard restore {context} failed: {e}", flush=True)
            status = _RESTORE_FAILED
        if status != _RESTORE_FAILED:
            if status == _RESTORE_CHANGED:
                print(
                    f"Notice: clipboard changed {context}; keeping the new contents",
                    flush=True,
                )
            return status
        if attempt + 1 < _RESTORE_RETRIES:
            time.sleep(_RESTORE_RETRY_BACKOFF_S)
    print(
        f"Warning: Failed to restore original clipboard {context} after "
        f"{_RESTORE_RETRIES} attempts; giving up",
        flush=True,
    )
    return status


def last_paste_restore_failed() -> bool:
    """True when the most recent paste_text call left the user's clipboard unrestored."""
    return _LAST_PASTE_RESTORE_FAILED


# Set once by request_restore_now() when Odicto is quitting; never cleared
# (only the test helper below resets it). Every later paste skips its wait.
_RESTORE_NOW = threading.Event()


def request_restore_now() -> None:
    """Odicto is quitting: end paste waits so their guarded restores run at once.

    main.py calls this at shutdown before waiting for the lifecycle lock. The
    request is sticky: an insertion that starts later also skips its wait
    (after the 0.15 s floor).
    """
    _RESTORE_NOW.set()


def _reset_restore_now_for_tests() -> None:
    """Test-only: forget a request_restore_now() call."""
    _RESTORE_NOW.clear()


def _wait_restore_window(timeout: float) -> None:
    if timeout > 0:
        _RESTORE_NOW.wait(timeout)


def flush_pending_restore(max_wait: float = 1.5) -> None:
    """No-op kept for main.py's shutdown call.

    Restores are synchronous and nothing is kept after a failed one, so there
    is never anything left to flush. ``max_wait`` is accepted and unused.
    """
    del max_wait


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
    original = _snapshot()
    if not original.ok:
        print("Warning: clipboard unreadable; selection probe skipped", flush=True)
        return ""
    if original.has_non_text and not original.complete:
        print(
            "Notice: clipboard holds data Odicto cannot save; selection probe skipped",
            flush=True,
        )
        return ""

    sentinel = f"\ufeffodicto-sel-{uuid.uuid4().hex}\ufeff"
    # The guard starts at the sentinel's own write and moves only to the copy
    # the probe itself observed, so a later user copy is never overwritten.
    status, guard, landed = _write_owned(sentinel, verify=False, attempts=1)
    if status != _WRITE_OK:
        if landed is not None:
            # The failed write already touched the clipboard: skip the probe
            # and put the original back.
            _guarded_restore(original, guard, "after failed selection probe")
            return ""
        # Nothing was written: the old change-vs-original probe is safe.
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
                guard = _observed_guard(cur)
                selected = cur
                path = "WM_COPY"

        # Path B: synthetic copy chord (fastest & universal for modern apps).
        # In a terminal this is Ctrl+Shift+C and in an IDE host Ctrl+Insert —
        # plain Ctrl+C is SIGINT and would interrupt the running command.
        copy_label = "ctrl+shift+c" if terminal else ("ctrl+insert" if ide else "ctrl+c")
        if selected == sentinel:
            selected = _copy_chord_until_change(sentinel, timeout, terminal, ide)
            if selected != sentinel:
                guard = _observed_guard(selected)
                if (selected or "").strip():
                    path = copy_label

        # One retry: chord still polluted or the app was slow to copy.
        if selected == sentinel:
            _wait_modifiers_up(0.08)
            selected = _copy_chord_until_change(
                sentinel, min(timeout, 0.25), terminal, ide
            )
            if selected != sentinel:
                guard = _observed_guard(selected)
                if (selected or "").strip():
                    path = f"{copy_label}-retry"

    finally:
        _guarded_restore(original, guard, "after selection probe")

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
    guard = _observed_guard(original_text)
    try:
        force_release_modifiers()
        _send_copy_chord(terminal, ide)
        deadline = time.time() + max(0.05, float(timeout))
        while time.time() < deadline:
            time.sleep(0.02)
            cur = _clipboard_read()
            if cur != original_text:
                guard = _observed_guard(cur)
                selected = cur
                break
    except Exception as e:
        print(f"Error: Failed to copy selection: {e}", flush=True)
        selected = original_text
    finally:
        _guarded_restore(original, guard, "after selection probe")

    if selected == original_text or not (selected or "").strip():
        return ""
    return selected


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    """Inject text at the cursor, typing into terminals and pasting elsewhere.

    Terminals reject or reassign the paste chord, so there the text is typed
    directly and the user's clipboard is never touched. Everywhere else the
    text goes through the clipboard + paste chord. Hold-to-talk then waits
    ``PASTE_DELAY_SECONDS`` (the target app reads the clipboard late) while
    holding the clipboard lock, and restores the user's clipboard (every format
    the platform can save) before returning, only if the clipboard still holds
    the payload. When the clipboard holds data that cannot be saved (an image
    or file list on Linux), short text is typed instead so that data survives.

    F7 passes ``restore_clipboard=False`` and leaves its final payload
    available for asynchronous paste consumers.
    """
    global _LAST_PASTE_RESTORE_FAILED
    _LAST_PASTE_RESTORE_FAILED = False
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
            # F7 leaves its payload on the clipboard: nothing to restore.
            if _write_owned(text)[0] != _WRITE_OK:
                raise RuntimeError("Could not write the paste payload to clipboard")
            _wait_modifiers_up(0.08)
            time.sleep(0.008)
            send_paste()
            time.sleep(0.015)
            return

        original = _snapshot()

        if original.ok and original.has_non_text and not original.complete:
            if len(text) <= _type_instead_limit():
                _wait_modifiers_up(0.08)
                # A RuntimeError here means a partial injection: do not paste a
                # second copy on top of it.
                if send_text_bulk(text):
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

        status, guard, landed = _write_owned(text)
        if status != _WRITE_OK:
            # Restore whenever the failed write touched the clipboard (it may
            # have emptied it); otherwise the clipboard was not ours to touch.
            if landed is not None or (guard.token is None and _clipboard_read() == text):
                _note_restore(_guarded_restore(original, guard, "after failed paste"))
            raise RuntimeError("Could not write the paste payload to clipboard")

        try:
            _wait_modifiers_up(0.08)
            time.sleep(0.02)
            send_paste()
        except BaseException:
            _note_restore(_guarded_restore(original, guard, "after failed paste"))
            raise

        # SendInput only queues the paste chord; Electron apps, busy browsers
        # and RDP sessions read the clipboard much later. The lock stays held,
        # so no probe or paste can replace the payload before they read it.
        # request_restore_now() (quitting) skips the rest of the wait; the
        # 0.15 s floor always runs.
        delay = max(0.15, float(Config.PASTE_DELAY_SECONDS))
        time.sleep(0.15)
        _wait_restore_window(delay - 0.15)
        _note_restore(_guarded_restore(original, guard, "after paste"))


def _note_restore(status: str) -> None:
    """Record a paste restore that gave up, for last_paste_restore_failed()."""
    global _LAST_PASTE_RESTORE_FAILED
    if status == _RESTORE_FAILED:
        _LAST_PASTE_RESTORE_FAILED = True


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
