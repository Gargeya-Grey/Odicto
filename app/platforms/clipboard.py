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


def clipboard_restore(snap: ClipboardSnapshot) -> bool:
    """Re-create ``snap`` on the clipboard. Never raises; False on failure.

    An empty snapshot clears the clipboard with the platform's own empty
    operation rather than writing an empty string.
    """
    if snap is None or not snap.ok:
        return False
    try:
        if sys.platform == "win32":
            return _win_restore(snap)
        if sys.platform == "darwin":
            return _mac_restore(snap)
        return _text_restore(snap.text)
    except Exception:
        return False


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


def _win_build_snapshot(seen, allowed, read) -> ClipboardSnapshot:
    """Build a snapshot from enumerated formats. ``read(fmt)`` -> bytes or None.

    Only allow-listed formats are read. Pure apart from ``read``, so tests
    drive it with fake formats.
    """
    captured = []
    total = 0
    complete = True
    for fmt in seen:
        if fmt not in allowed:
            continue
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

        return _win_build_snapshot(seen, allowed, read)
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


def _win_restore(snap: ClipboardSnapshot) -> bool:
    api = _win_api()
    formats = tuple(snap.formats or ())
    if not formats and snap.text:
        formats = ((CF_UNICODETEXT, (snap.text + "\x00").encode("utf-16-le")),)
    # SetClipboardData needs an owner window after EmptyClipboard; a message-only
    # window (HWND_MESSAGE) created and destroyed on this thread serves.
    HWND_MESSAGE = -3
    hwnd = api.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        if not _win_open(api, hwnd):
            return False
        try:
            if not api.EmptyClipboard():
                return False
            ok = True
            for fmt, data in formats:
                if not _win_set_bytes(api, int(fmt), bytes(data)):
                    ok = False
            return ok
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


def _mac_restore(snap: ClipboardSnapshot) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardItem
        from Foundation import NSData
    except Exception:
        return _text_restore(snap.text)
    if not snap.formats and snap.text:
        return _text_restore(snap.text)
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    objects = []
    for entries in snap.formats:
        item = NSPasteboardItem.alloc().init()
        for type_name, raw in entries:
            item.setData_forType_(NSData.dataWithBytes_length_(raw, len(raw)), type_name)
        objects.append(item)
    if not objects:
        return True  # clearContents already emptied it
    return bool(pb.writeObjects_(objects))


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
