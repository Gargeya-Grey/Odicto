"""Full-format clipboard snapshot and restore.

``typer`` replaces the clipboard with its own payload (a transcript to paste,
or a sentinel for the AI selection probe) and then puts the user's clipboard
back. A text-only round trip destroys a copied image, a file list or rich
text, so this module captures every format the platform lets it re-create.

* Windows: an allow-list of user-visible formats (text, DIB, files, HTML,
  RTF, PNG, enhanced metafile) through a private ctypes binding, read on a
  worker thread with a 750 ms budget. Owner-private OLE formats are skipped;
  GDI bitmap/metafile forms are re-synthesized by the system after a restore.
* macOS: every type of every ``NSPasteboard`` item, through AppKit.
* Linux: plain text only (pyperclip); ``xclip``/``wl-paste`` report whether
  the clipboard also holds an image or files, which makes it incomplete.

``typer`` imports this module directly (not through the ``platforms``
facade). Every OS import is lazy, so the module imports cleanly everywhere.
No public function raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

# A snapshot larger than this is not captured in full (complete=False):
# holding hundreds of MB per paste is worse than typing the text instead.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardSnapshot:
    text: str  # plain text ("" if none)
    formats: tuple  # platform-opaque captured payload (may be empty)
    complete: bool  # True when clipboard_restore() recreates everything the user had
    has_non_text: bool
    ok: bool  # False when the clipboard could not be read at all


_UNREADABLE = ClipboardSnapshot(text="", formats=(), complete=False, has_non_text=False, ok=False)


def clipboard_snapshot() -> ClipboardSnapshot:
    """Capture the current clipboard. Never raises; ``ok=False`` on failure."""
    try:
        if sys.platform == "win32":
            return _win_snapshot()
        if sys.platform == "darwin":
            return _mac_snapshot()
        return _linux_snapshot()
    except Exception:
        return _UNREADABLE


RESTORED = "restored"  # the snapshot is back on the clipboard
CHANGED = "changed"  # expect_token mismatch: the clipboard changed, nothing written
FAILED = "failed"  # could not open, or a write failed (possibly after emptying): retry


def clipboard_restore_result(snap: ClipboardSnapshot, expect_token: Optional[int] = None) -> tuple:
    """Re-create ``snap`` on the clipboard. Returns ``(status, token_after)``.

    ``status`` is RESTORED, CHANGED or FAILED. Never raises. An empty snapshot
    clears the clipboard with the platform's own empty operation rather than
    writing an empty string.

    ``expect_token`` (a ``clipboard_change_token()`` value) makes the restore
    conditional: when the clipboard changed since that token, nothing is
    written and CHANGED is returned. Windows checks inside the same
    OpenClipboard session that writes, so no other app can copy in between.
    macOS checks ``changeCount`` immediately before ``clearContents`` (best
    effort: NSPasteboard has no lock). Linux has no token and ignores it.

    FAILED includes a write that failed after the clipboard was already
    emptied: the user's data is not back, so the caller must retry. When that
    failed attempt itself changed the clipboard, ``token_after`` is the change
    token observed inside the same session right after it (Windows sequence
    number, macOS ``clearContents`` count); it is the only value a caller may
    use to replace its guard. Otherwise ``token_after`` is None (nothing was
    written, or the platform cannot tell).
    """
    if snap is None or not snap.ok:
        return FAILED, None
    try:
        if sys.platform == "win32":
            return _win_restore(snap, expect_token)
        if sys.platform == "darwin":
            return _mac_restore(snap, expect_token)
        return (RESTORED if _text_restore(snap.text) else FAILED), None
    except Exception:
        return FAILED, None


def clipboard_restore_status(snap: ClipboardSnapshot, expect_token: Optional[int] = None) -> str:
    """Status-only form of ``clipboard_restore_result``."""
    return clipboard_restore_result(snap, expect_token)[0]


def clipboard_restore(snap: ClipboardSnapshot, expect_token: Optional[int] = None) -> bool:
    """Bool form of ``clipboard_restore_status``: True only when RESTORED."""
    return clipboard_restore_status(snap, expect_token) == RESTORED


def clipboard_change_token() -> Optional[int]:
    """A counter that changes whenever any app writes the clipboard.

    Windows: ``GetClipboardSequenceNumber``. macOS: ``NSPasteboard.changeCount``.
    Linux has no equivalent: None (callers compare text instead).
    """
    try:
        if sys.platform == "win32":
            value = int(_win_api().user32.GetClipboardSequenceNumber())
            return value or None
        if sys.platform == "darwin":
            from AppKit import NSPasteboard

            return int(NSPasteboard.generalPasteboard().changeCount())
    except Exception:
        return None
    return None


WRITTEN = "written"  # clipboard_write_text_token: the text is on the clipboard


def clipboard_write_text_token(text: str, expect_token: Optional[int] = None) -> tuple:
    """Write plain text. Returns ``(status, token)``.

    ``status`` is WRITTEN, CHANGED (``expect_token`` given and the clipboard
    changed since: nothing written) or FAILED. ``token`` is the change token
    of the clipboard as this call left it whenever the call touched the
    clipboard, including a FAILED write that had already emptied it; it is
    None when nothing was written.

    The token identifies Odicto's own write, so a guard built from it can
    never be a user copy made a moment later. Windows reads
    ``GetClipboardSequenceNumber`` inside the same OpenClipboard session that
    wrote. macOS uses the count ``clearContents`` returns: ``changeCount``
    counts ownership changes, so the ``setString:forType:`` that follows does
    not bump it (best effort, NSPasteboard has no lock). Linux has no token
    and ignores ``expect_token``. Never raises.
    """
    try:
        if sys.platform == "win32":
            return _win_write_text_token(text, expect_token)
        if sys.platform == "darwin":
            return _mac_write_text_token(text, expect_token)
        return (WRITTEN if _text_restore(text) else FAILED), None
    except Exception:
        return FAILED, None


def clipboard_read_text_token() -> tuple:
    """Read plain text and the change token of that same clipboard state.

    Windows reads both inside one OpenClipboard session; macOS accepts the
    read only when ``changeCount`` is equal before and after it. Returns
    ``(text, token)``; ``token`` is None when it cannot be tied to the text
    (Linux, busy clipboard). Never raises.
    """
    try:
        if sys.platform == "win32":
            return _win_read_text_token()
        if sys.platform == "darwin":
            from AppKit import NSPasteboard, NSPasteboardTypeString

            pb = NSPasteboard.generalPasteboard()
            before = int(pb.changeCount())
            text = str(pb.stringForType_(NSPasteboardTypeString) or "")
            return text, (before if int(pb.changeCount()) == before else None)
    except Exception:
        pass
    return (_text_read() or ""), None


# --- Text fallback (pyperclip) --------------------------------------------


def _text_read() -> Optional[str]:
    """Clipboard text, or None when the clipboard cannot be read."""
    try:
        import pyperclip

        value = pyperclip.paste()
    except Exception:
        return None
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _text_restore(text: str) -> bool:
    try:
        import pyperclip

        pyperclip.copy(text or "")
        return True
    except Exception:
        return False


# --- Windows ----------------------------------------------------------------

CF_TEXT = 1
CF_OEMTEXT = 7
CF_DIB = 8
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14  # a GDI handle: captured through its bits, not GlobalLock
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17

_WIN_TEXT_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE})
GMEM_MOVEABLE = 0x0002

_WIN_API = None


class _WinApi:
    """Private user32/kernel32/gdi32 bindings with full 64-bit signatures.

    Separate ``WinDLL`` instances, so setting argtypes here never changes the
    shared ``ctypes.windll`` function objects other modules configure.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        HANDLE = wintypes.HANDLE
        HGLOBAL = wintypes.HGLOBAL
        SIZE_T = ctypes.c_size_t
        UINT = wintypes.UINT

        def sig(fn, argtypes, restype):
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        self.OpenClipboard = sig(u.OpenClipboard, [wintypes.HWND], wintypes.BOOL)
        self.CloseClipboard = sig(u.CloseClipboard, [], wintypes.BOOL)
        self.EmptyClipboard = sig(u.EmptyClipboard, [], wintypes.BOOL)
        self.EnumClipboardFormats = sig(u.EnumClipboardFormats, [UINT], UINT)
        self.GetClipboardData = sig(u.GetClipboardData, [UINT], HANDLE)
        self.SetClipboardData = sig(u.SetClipboardData, [UINT, HANDLE], HANDLE)
        self.RegisterClipboardFormatW = sig(
            u.RegisterClipboardFormatW, [wintypes.LPCWSTR], UINT
        )
        self.CreateWindowExW = sig(
            u.CreateWindowExW,
            [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ],
            wintypes.HWND,
        )
        self.DestroyWindow = sig(u.DestroyWindow, [wintypes.HWND], wintypes.BOOL)
        self.user32 = u
        sig(u.GetClipboardSequenceNumber, [], wintypes.DWORD)

        self.GlobalAlloc = sig(k.GlobalAlloc, [UINT, SIZE_T], HGLOBAL)
        self.GlobalFree = sig(k.GlobalFree, [HGLOBAL], HGLOBAL)
        self.GlobalLock = sig(k.GlobalLock, [HGLOBAL], wintypes.LPVOID)
        self.GlobalUnlock = sig(k.GlobalUnlock, [HGLOBAL], wintypes.BOOL)
        self.GlobalSize = sig(k.GlobalSize, [HGLOBAL], SIZE_T)
        self.GetSystemDefaultLCID = sig(k.GetSystemDefaultLCID, [], wintypes.DWORD)

        self.GetEnhMetaFileBits = sig(
            g.GetEnhMetaFileBits, [HANDLE, UINT, wintypes.LPVOID], UINT
        )
        self.SetEnhMetaFileBits = sig(g.SetEnhMetaFileBits, [UINT, wintypes.LPVOID], HANDLE)
        self.DeleteEnhMetaFile = sig(g.DeleteEnhMetaFile, [HANDLE], wintypes.BOOL)


def _win_api() -> _WinApi:
    global _WIN_API
    if _WIN_API is None:
        _WIN_API = _WinApi()
    return _WIN_API


def _win_open(api: _WinApi, hwnd=None, attempts: int = 10) -> bool:
    """OpenClipboard with short retries: another app may hold it open briefly."""
    for attempt in range(attempts):
        if api.OpenClipboard(hwnd):
            return True
        if attempt + 1 < attempts:
            time.sleep(0.01)
    return False


def _win_read_hglobal(api: _WinApi, handle) -> Optional[bytes]:
    size = int(api.GlobalSize(handle) or 0)
    if size <= 0:
        return b""
    ptr = api.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return api.ctypes.string_at(ptr, size)
    finally:
        api.GlobalUnlock(handle)


def _win_read_emf(api: _WinApi, handle) -> Optional[bytes]:
    size = int(api.GetEnhMetaFileBits(handle, 0, None) or 0)
    if size <= 0:
        return None
    buf = api.ctypes.create_string_buffer(size)
    if api.GetEnhMetaFileBits(handle, size, buf) != size:
        return None
    return buf.raw


# Formats captured for a restore. Everything else (OLE / owner-private formats
# such as "Ole Private Data", "DataObject", "Embed Source", "Object Descriptor",
# "Link Source", app-private binaries) is never requested: many are rendered on
# demand (WM_RENDERFORMAT), so reading them makes every paste wait on the owner
# app, and re-publishing them after the owner changed would be dead data.
# Skipping them does not by itself make a snapshot incomplete.
_WIN_ALLOWED_STANDARD = frozenset(
    {CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT, CF_LOCALE, CF_DIB, CF_DIBV5, CF_HDROP, CF_ENHMETAFILE}
)
_WIN_ALLOWED_REGISTERED = (
    "HTML Format",
    "Rich Text Format",
    "PNG",
    "image/png",
    "Preferred DropEffect",
    "FileGroupDescriptorW",
    "FileContents",
    "Shell IDList Array",
    "UniformResourceLocatorW",
)
# A snapshot that has to call the owner app back for data gets this long; a
# slow or hung owner then yields an incomplete snapshot (typer types instead).
_WIN_SNAPSHOT_TIMEOUT_S = 0.75

_WIN_ALLOWED = None
_WIN_SNAPSHOT_THREAD = None
_WIN_TIMEOUT_LOGGED = False


def _win_allowed_formats(api: _WinApi) -> frozenset:
    """Standard allow-list plus the ids of the registered allow-listed names."""
    global _WIN_ALLOWED
    if _WIN_ALLOWED is None:
        ids = set(_WIN_ALLOWED_STANDARD)
        for name in _WIN_ALLOWED_REGISTERED:
            fmt = int(api.RegisterClipboardFormatW(name) or 0)
            if fmt:
                ids.add(fmt)
        _WIN_ALLOWED = frozenset(ids)
    return _WIN_ALLOWED


def _win_build_snapshot(seen, allowed, read, deadline=None, clock=time.monotonic) -> ClipboardSnapshot:
    """Build a snapshot from enumerated formats. ``read(fmt)`` -> bytes or None.

    Only allow-listed formats are read. Once ``clock()`` passes ``deadline`` no
    further format is read and the snapshot is incomplete, so the caller can
    close the clipboard at once. Pure apart from ``read``/``clock``, so tests
    drive it with fake formats.
    """
    captured = []
    total = 0
    complete = True
    for fmt in seen:
        if fmt not in allowed:
            continue
        if deadline is not None and clock() >= deadline:
            complete = False  # budget spent: stop asking the owner to render
            break
        data = read(fmt)
        if data is None:
            complete = False  # an allow-listed format we could not save
            continue
        if total + len(data) > MAX_SNAPSHOT_BYTES:
            complete = False
            continue
        total += len(data)
        captured.append((fmt, data))
    if seen and not captured:
        # Only owner-private formats: nothing user-visible could be saved.
        complete = False

    text = ""
    for fmt, data in captured:
        if fmt == CF_UNICODETEXT:
            text = data.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            break
    has_non_text = any(f not in _WIN_TEXT_FORMATS for f in seen)
    return ClipboardSnapshot(
        text=text,
        formats=tuple(captured),
        complete=complete,
        has_non_text=has_non_text,
        ok=True,
    )


def _win_snapshot_blocking() -> ClipboardSnapshot:
    # The budget is checked before each format read, and the clipboard is closed
    # as soon as it is spent. One GetClipboardData call that blocks on a hung
    # owner's WM_RENDERFORMAT cannot be interrupted (Windows has no API for
    # it); while it blocks, OpenClipboard stays held. Any clipboard reader,
    # including the old text-only read, had the same exposure.
    deadline = time.monotonic() + _WIN_SNAPSHOT_TIMEOUT_S
    api = _win_api()
    allowed = _win_allowed_formats(api)
    if not _win_open(api):
        return _UNREADABLE
    try:
        seen = []
        fmt = 0
        while True:
            fmt = int(api.EnumClipboardFormats(fmt))
            if not fmt:
                break
            seen.append(fmt)

        def read(fmt: int) -> Optional[bytes]:
            handle = api.GetClipboardData(fmt)
            if not handle:
                return None
            if fmt == CF_ENHMETAFILE:
                return _win_read_emf(api, handle)
            return _win_read_hglobal(api, handle)

        return _win_build_snapshot(seen, allowed, read, deadline)
    finally:
        api.CloseClipboard()


_WIN_TIMED_OUT = ClipboardSnapshot(text="", formats=(), complete=False, has_non_text=True, ok=True)


def _win_snapshot() -> ClipboardSnapshot:
    """Run the blocking snapshot on a worker thread with a time budget.

    GetClipboardData on a delay-rendered format waits for the owner app. On
    timeout the snapshot reports incomplete non-text data (so typer types the
    text) and the stuck worker is left alone; while it is still stuck, later
    snapshots return the same result at once instead of stacking threads.
    """
    global _WIN_SNAPSHOT_THREAD, _WIN_TIMEOUT_LOGGED
    previous = _WIN_SNAPSHOT_THREAD
    if previous is not None and previous.is_alive():
        return _WIN_TIMED_OUT
    result = []

    def run() -> None:
        try:
            result.append(_win_snapshot_blocking())
        except Exception:
            result.append(_UNREADABLE)

    worker = threading.Thread(target=run, name="odicto-clipboard-snapshot", daemon=True)
    _WIN_SNAPSHOT_THREAD = worker
    worker.start()
    worker.join(_WIN_SNAPSHOT_TIMEOUT_S)
    if result:
        return result[0]
    if not _WIN_TIMEOUT_LOGGED:
        _WIN_TIMEOUT_LOGGED = True
        print(
            "Warning: clipboard owner is slow to render its data; treating the "
            "clipboard as unsaveable for this paste",
            flush=True,
        )
    return _WIN_TIMED_OUT


def _win_set_bytes(api: _WinApi, fmt: int, data: bytes) -> bool:
    if fmt == CF_ENHMETAFILE:
        buf = api.ctypes.create_string_buffer(data, len(data))
        hemf = api.SetEnhMetaFileBits(len(data), buf)
        if not hemf:
            return False
        if not api.SetClipboardData(fmt, hemf):
            api.DeleteEnhMetaFile(hemf)
            return False
        return True
    handle = api.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        return False
    ptr = api.GlobalLock(handle)
    if not ptr:
        api.GlobalFree(handle)
        return False
    try:
        if data:
            api.ctypes.memmove(ptr, data, len(data))
    finally:
        api.GlobalUnlock(handle)
    if not api.SetClipboardData(fmt, handle):
        # The system owns the handle only after SetClipboardData succeeds.
        api.GlobalFree(handle)
        return False
    return True


def _encode_or_ascii(text: str, codec: str) -> bytes:
    """Encode with a Windows code-page codec; ASCII where it does not exist."""
    try:
        return text.encode(codec, "replace")
    except LookupError:
        return text.encode("ascii", "replace")


def _win_text_family(api: _WinApi, text: str) -> list:
    """CF_UNICODETEXT plus the CF_TEXT / CF_OEMTEXT / CF_LOCALE forms of ``text``.

    Windows synthesizes the missing text formats at CloseClipboard, and each
    synthesized format bumps the sequence number after the session ends, so a
    token read inside the session would never match again. Writing the whole
    family leaves nothing to synthesize: the in-session token is final.

    The system locale defines the ANSI ("mbcs") and OEM ("oem") code pages, so
    CF_LOCALE is the system default LCID, not the user locale.
    """
    import struct

    nul = chr(0)
    return [
        (CF_UNICODETEXT, (text + nul).encode("utf-16-le")),
        (CF_TEXT, _encode_or_ascii(text, "mbcs") + b"\x00"),
        (CF_OEMTEXT, _encode_or_ascii(text, "oem") + b"\x00"),
        (CF_LOCALE, struct.pack("<I", int(api.GetSystemDefaultLCID()))),
    ]


def _win_complete_text_family(api: _WinApi, formats: tuple, text: str) -> tuple:
    """Add the text formats Windows would otherwise synthesize at close."""
    have = {int(fmt) for fmt, _ in formats}
    if CF_UNICODETEXT not in have:
        return formats
    extra = [(f, d) for f, d in _win_text_family(api, text) if f not in have]
    return tuple(formats) + tuple(extra)


def _win_write_text_token(text: str, expect_token: Optional[int] = None) -> tuple:
    """Write the text family; compare and read the sequence number in one session."""
    api = _win_api()
    family = _win_text_family(api, text)
    HWND_MESSAGE = -3
    hwnd = api.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        if not _win_open(api, hwnd):
            return FAILED, None
        try:
            if (
                expect_token is not None
                and int(api.user32.GetClipboardSequenceNumber()) != int(expect_token)
            ):
                return CHANGED, None
            if not api.EmptyClipboard():
                return FAILED, None
            if not all(_win_set_bytes(api, fmt, data) for fmt, data in family):
                # Emptied but not written: report the token of that state.
                return FAILED, int(api.user32.GetClipboardSequenceNumber())
            return WRITTEN, int(api.user32.GetClipboardSequenceNumber())
        finally:
            api.CloseClipboard()
    finally:
        if hwnd:
            api.DestroyWindow(hwnd)


def _win_read_text_token() -> tuple:
    """CF_UNICODETEXT and the sequence number of the same clipboard state."""
    api = _win_api()
    if not _win_open(api):
        return (_text_read() or ""), None
    try:
        token = int(api.user32.GetClipboardSequenceNumber())
        handle = api.GetClipboardData(CF_UNICODETEXT)
        raw = _win_read_hglobal(api, handle) if handle else b""
        text = (raw or b"").decode("utf-16-le", errors="replace").split(chr(0), 1)[0]
        return text, token
    finally:
        api.CloseClipboard()


def _win_restore(snap: ClipboardSnapshot, expect_token: Optional[int] = None) -> tuple:
    """(status, token_after). See ``clipboard_restore_result``."""
    api = _win_api()
    formats = tuple(snap.formats or ())
    if not formats and snap.text:
        formats = ((CF_UNICODETEXT, (snap.text + chr(0)).encode("utf-16-le")),)
    formats = _win_complete_text_family(api, formats, snap.text)
    # SetClipboardData needs an owner window after EmptyClipboard; a message-only
    # window (HWND_MESSAGE) created and destroyed on this thread serves.
    HWND_MESSAGE = -3
    hwnd = api.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        if not _win_open(api, hwnd):
            return FAILED, None  # nothing written
        try:
            if (
                expect_token is not None
                and int(api.user32.GetClipboardSequenceNumber()) != int(expect_token)
            ):
                return CHANGED, None  # changed since the paste: write nothing
            # A write that fails after EmptyClipboard would leave the user with
            # an empty clipboard: empty and write everything once more in the
            # same session before giving up.
            touched = False
            for _attempt in range(2):
                if not api.EmptyClipboard():
                    continue
                touched = True
                if all(_win_set_bytes(api, int(fmt), bytes(data)) for fmt, data in formats):
                    return RESTORED, int(api.user32.GetClipboardSequenceNumber())
            # Our own failed write changed the clipboard: report the token seen
            # inside this session, before any other app can open it.
            if touched:
                return FAILED, int(api.user32.GetClipboardSequenceNumber())
            return FAILED, None
        finally:
            api.CloseClipboard()
    finally:
        if hwnd:
            api.DestroyWindow(hwnd)


# --- macOS ------------------------------------------------------------------

_MAC_TEXT_TYPES = frozenset(
    {
        "public.utf8-plain-text",
        "public.utf16-plain-text",
        "public.utf16-external-plain-text",
        "public.plain-text",
        "NSStringPboardType",
    }
)


def _mac_snapshot() -> ClipboardSnapshot:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except Exception:
        # No PyObjC: text only. Nothing else can be seen, so nothing else is lost
        # by this module; report complete text.
        text = _text_read()
        if text is None:
            return _UNREADABLE
        return ClipboardSnapshot(text=text, formats=(), complete=True, has_non_text=False, ok=True)

    pb = NSPasteboard.generalPasteboard()
    items = pb.pasteboardItems() or []
    captured = []
    total = 0
    complete = True
    has_non_text = False
    for item in items:
        entries = []
        for t in item.types() or []:
            type_name = str(t)
            if type_name not in _MAC_TEXT_TYPES:
                has_non_text = True
            data = item.dataForType_(t)
            if data is None:
                complete = False
                continue
            raw = bytes(data)
            if total + len(raw) > MAX_SNAPSHOT_BYTES:
                complete = False
                continue
            total += len(raw)
            entries.append((type_name, raw))
        captured.append(tuple(entries))
    text = pb.stringForType_(NSPasteboardTypeString) or ""
    return ClipboardSnapshot(
        text=str(text),
        formats=tuple(captured),
        complete=complete,
        has_non_text=has_non_text,
        ok=True,
    )


def _mac_write_text_token(text: str, expect_token: Optional[int] = None) -> tuple:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except Exception:
        return (WRITTEN if _text_restore(text) else FAILED), None
    pb = NSPasteboard.generalPasteboard()
    # Best effort: compared immediately before clearContents (no lock).
    if expect_token is not None and int(pb.changeCount()) != int(expect_token):
        return CHANGED, None
    # changeCount counts ownership changes: clearContents bumps it and returns
    # the new value; writing data to the pasteboard we now own does not.
    cleared = int(pb.clearContents())
    if not pb.setString_forType_(text, NSPasteboardTypeString):
        return FAILED, cleared
    return WRITTEN, cleared


def _mac_restore(snap: ClipboardSnapshot, expect_token: Optional[int] = None) -> tuple:
    """(status, token_after). See ``clipboard_restore_result``."""
    try:
        from AppKit import NSPasteboard, NSPasteboardItem
        from Foundation import NSData
    except Exception:
        return (RESTORED if _text_restore(snap.text) else FAILED), None
    pb = NSPasteboard.generalPasteboard()
    # Best effort: compared immediately before clearContents; NSPasteboard has
    # no lock, so a copy in the instant between the two is not excluded.
    if expect_token is not None and int(pb.changeCount()) != int(expect_token):
        return CHANGED, None
    if not snap.formats and snap.text:
        return (RESTORED if _text_restore(snap.text) else FAILED), None
    # clearContents returns the change count it produced: our own change.
    cleared = int(pb.clearContents())
    objects = []
    for entries in snap.formats:
        item = NSPasteboardItem.alloc().init()
        for type_name, raw in entries:
            item.setData_forType_(NSData.dataWithBytes_length_(raw, len(raw)), type_name)
        objects.append(item)
    if not objects:
        return RESTORED, cleared  # clearContents already emptied it
    if pb.writeObjects_(objects):
        return RESTORED, None
    return FAILED, cleared


# --- Linux ------------------------------------------------------------------

# Targets that mean the clipboard holds something pyperclip cannot carry back.
# text/html and text/rtf next to text/plain are accepted on purpose: the plain
# text is kept and only its formatting is lost, which is better than typing
# every paste. Images and copied files are not acceptable losses.
_LINUX_NON_TEXT_EXACT = frozenset(
    {"text/uri-list", "x-special/gnome-copied-files", "application/x-qt-image"}
)


def _linux_targets() -> tuple:
    """Clipboard MIME targets, or () when no tool can list them (0.5 s timeout)."""
    if os.environ.get("WAYLAND_DISPLAY"):
        cmd = ["wl-paste", "--list-types"]
    elif os.environ.get("DISPLAY"):
        cmd = ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]
    else:
        return ()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=0.5)
    except Exception:
        return ()
    if result.returncode != 0:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _linux_snapshot() -> ClipboardSnapshot:
    text = _text_read()
    if text is None:
        return _UNREADABLE
    targets = _linux_targets()
    has_non_text = any(
        t.lower().startswith("image/") or t.lower() in _LINUX_NON_TEXT_EXACT for t in targets
    )
    return ClipboardSnapshot(
        text=text,
        formats=(),
        complete=not has_non_text,
        has_non_text=has_non_text,
        ok=True,
    )
