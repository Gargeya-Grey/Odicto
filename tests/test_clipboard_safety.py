"""Clipboard safety: synchronous guarded restore, full-format snapshots, safe copy chords.

Everything except the Windows round trip runs against a fake clipboard; the real
OS clipboard is never touched by those tests.
"""

from __future__ import annotations

import struct
import sys
import threading
import unittest
from unittest.mock import patch

import typer
from config import Config
from platforms import clipboard as clip_mod
from platforms.clipboard import ClipboardSnapshot


def _snap(text="", formats=(), complete=True, has_non_text=False, ok=True):
    return ClipboardSnapshot(
        text=text, formats=formats, complete=complete, has_non_text=has_non_text, ok=ok
    )


class FakeClipboard:
    """Text clipboard with a change counter and optional non-text payload.

    ``restore_result`` mimics the platform contract: the token check and the
    write happen in one step, and a failed write that emptied the clipboard
    reports the token it produced.
    """

    def __init__(self, text="user clip", non_text=None, complete=True, readable=True):
        self.text = text
        self.non_text = non_text  # stands in for an image / file list
        self.complete = complete
        self.readable = readable
        self.token = 1
        self.writes = []
        self.restores = []
        self.events = []
        self.open_failures = 0  # next N restores cannot open: nothing written
        self.empty_then_fail = 0  # next N restores empty the clipboard, then fail
        self.restore_attempts = 0
        self.guards_seen = []
        self.on_restore_attempt = None  # hook run before each attempt (simulate users)

    def read(self):
        return self.text

    def write(self, text):
        self.writes.append(text)
        self.events.append(("write", text))
        self.text = text
        self.non_text = None
        self.token += 1
        return True

    def snapshot(self):
        if not self.readable:
            return _snap(ok=False, complete=False)
        return _snap(
            text=self.text,
            formats=(("img", self.non_text),) if self.non_text else (),
            complete=self.complete if self.non_text else True,
            has_non_text=bool(self.non_text),
        )

    def restore_result(self, snap, expect_token=None):
        self.restore_attempts += 1
        self.guards_seen.append(expect_token)
        if self.on_restore_attempt is not None:
            self.on_restore_attempt(self.restore_attempts)
        if self.open_failures:
            self.open_failures -= 1
            return "failed", None
        if expect_token is not None and expect_token != self.token:
            self.events.append(("aborted", snap.text))
            return "changed", None
        if self.empty_then_fail:
            self.empty_then_fail -= 1
            self.text = ""
            self.non_text = None
            self.token += 1
            self.events.append(("emptied", ""))
            return "failed", self.token
        self.restores.append(snap)
        self.events.append(("restore", snap.text))
        self.text = snap.text
        self.non_text = dict(snap.formats).get("img")
        self.token += 1
        return "restored", self.token

    def restore(self, snap, expect_token=None):
        return self.restore_result(snap, expect_token)[0] == "restored"

    def user_copies(self, text):
        self.events.append(("user", text))
        self.text = text
        self.non_text = None
        self.token += 1


class ClipboardSafetyBase(unittest.TestCase):
    use_token = True

    def setUp(self):
        self.clip = FakeClipboard()
        self.copy_calls = []
        typer._UNRESTORED = None
        patches = [
            patch("typer.clipboard_read", side_effect=lambda: self.clip.read()),
            patch("typer.clipboard_write", side_effect=lambda t: self.clip.write(t)),
            patch("typer.clipboard_snapshot", side_effect=lambda: self.clip.snapshot()),
            patch(
                "typer.clipboard_restore",
                side_effect=lambda s, expect_token=None: self.clip.restore(s, expect_token),
            ),
            patch(
                "typer.clipboard_restore_result",
                side_effect=lambda s, expect_token=None: self.clip.restore_result(
                    s, expect_token
                ),
            ),
            patch(
                "typer.clipboard_change_token",
                side_effect=lambda: self.clip.token if self.use_token else None,
            ),
            patch("typer.foreground_is_terminal", return_value=False),
            patch("typer.foreground_is_ide_host", return_value=False),
            patch("typer._wait_modifiers_up"),
            patch("typer.force_release_modifiers"),
            patch("typer.wm_copy_foreground", return_value=False),
            patch("typer.send_paste"),
            patch("typer.send_text_bulk", return_value=True),
            patch("typer.send_copy", side_effect=lambda: self.copy_calls.append("ctrl+c")),
            patch(
                "typer.send_copy_terminal",
                side_effect=lambda: self.copy_calls.append("ctrl+shift+c"),
            ),
            patch(
                "typer.send_copy_ide", side_effect=lambda: self.copy_calls.append("ctrl+insert")
            ),
            patch.object(Config, "PASTE_DELAY_SECONDS", 1.0),
        ]
        self.mocks = {}
        for p in patches:
            self.mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, typer, "_UNRESTORED", None)

    def _paste(self, text="transcript", **kwargs):
        """paste_text with time.sleep recorded instead of slept."""
        sleeps = []
        with patch("typer.time.sleep", side_effect=sleeps.append):
            typer.paste_text(text, **kwargs)
        return sleeps

    def _probe(self, selection="picked text"):
        def on_copy(label):
            self.copy_calls.append(label)
            self.clip.user_copies(selection)

        for name, label in (
            ("send_copy", "ctrl+c"),
            ("send_copy_terminal", "ctrl+shift+c"),
            ("send_copy_ide", "ctrl+insert"),
        ):
            self.mocks[name].side_effect = lambda label=label: on_copy(label)
        with patch("typer.time.sleep"):
            return typer.get_selected_text(timeout=0.2)


class TestSynchronousRestore(ClipboardSafetyBase):
    def test_restore_happens_before_paste_returns_after_the_delay(self):
        order = []
        self.mocks["send_paste"].side_effect = lambda: order.append("paste")
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            order.append(("sleep", s))

        with patch("typer.time.sleep", side_effect=sleep):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user clip")
        self.assertIn(("sleep", 1.0), order)
        self.assertLess(order.index("paste"), order.index(("sleep", 1.0)))
        self.assertEqual(self.clip.events, [("write", "transcript"), ("restore", "user clip")])
        self.assertIsNone(typer._UNRESTORED)

    def test_user_copy_during_delay_is_never_overwritten(self):
        def sleep(s):
            if s >= 0.15:
                self.clip.user_copies("user copied during delay")

        with patch("typer.time.sleep", side_effect=sleep):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user copied during delay")
        self.assertEqual(self.clip.restores, [])
        self.assertEqual(self.clip.restore_attempts, 1)  # "changed": no retry
        self.assertIsNone(typer._UNRESTORED)

    def test_user_copy_detected_by_text_when_no_token(self):
        self.use_token = False

        def sleep(s):
            if s >= 0.15:
                self.clip.user_copies("app wrote this")

        with patch("typer.time.sleep", side_effect=sleep):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "app wrote this")

    def test_failed_restore_retries_with_same_guard_and_never_adopts_a_user_copy(self):
        # Codex scenario: attempt 1 fails (clipboard busy, nothing written); the
        # user copies before attempt 2. The retry must keep the payload guard
        # and report "changed", not adopt the user's copy and overwrite it.
        self.clip.open_failures = 1

        def on_attempt(n):
            if n == 2:
                self.clip.user_copies("user copy between retries")

        self.clip.on_restore_attempt = on_attempt
        sleeps = self._paste()
        payload_guard = self.clip.guards_seen[0]
        self.assertEqual(self.clip.guards_seen, [payload_guard, payload_guard])
        self.assertIn(typer._RESTORE_RETRY_BACKOFF_S, sleeps)
        self.assertEqual(self.clip.text, "user copy between retries")
        self.assertIsNone(typer._UNRESTORED)

    def test_open_failure_keeps_the_old_guard(self):
        self.clip.open_failures = 3
        sleeps = self._paste()
        guard = self.clip.guards_seen[0]
        self.assertEqual(self.clip.guards_seen, [guard, guard, guard])
        self.assertEqual(sleeps.count(typer._RESTORE_RETRY_BACKOFF_S), 2)
        self.assertIsNotNone(typer._UNRESTORED)
        self.assertEqual(typer._UNRESTORED.guard.token, guard)

    def test_empty_then_failed_write_uses_the_in_session_token(self):
        self.clip.empty_then_fail = 1
        self._paste()
        first, second = self.clip.guards_seen[:2]
        self.assertNotEqual(first, second)
        self.assertEqual(second, first + 1)  # the token the failed attempt produced
        self.assertEqual(self.clip.text, "user clip")
        self.assertIsNone(typer._UNRESTORED)

    def test_unrestored_record_is_retried_by_the_next_paste(self):
        self.clip.open_failures = 3
        self._paste("first")
        self.assertEqual(self.clip.text, "first")
        self.assertIsNotNone(typer._UNRESTORED)
        self._paste("second")
        # The record was retried first; the second paste snapshotted fresh.
        kinds = [e for e in self.clip.events if e[0] != "aborted"]
        self.assertEqual(
            kinds,
            [
                ("write", "first"),
                ("restore", "user clip"),
                ("write", "second"),
                ("restore", "user clip"),
            ],
        )
        self.assertIsNone(typer._UNRESTORED)

    def test_unrestored_record_never_overwrites_a_newer_user_copy(self):
        self.clip.open_failures = 3
        self._paste("first")
        self.clip.user_copies("newer user copy")
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "newer user copy")
        self.assertIsNone(typer._UNRESTORED)
        self._paste("second")
        self.assertEqual(self.clip.text, "newer user copy")

    def test_flush_retries_the_unrestored_record_once(self):
        self.clip.open_failures = 4
        self._paste()
        attempts = self.clip.restore_attempts
        typer.flush_pending_restore()  # still failing: kept
        self.assertEqual(self.clip.restore_attempts, attempts + 1)
        self.assertIsNotNone(typer._UNRESTORED)
        typer.flush_pending_restore(max_wait=0)
        self.assertEqual(self.clip.text, "user clip")
        self.assertIsNone(typer._UNRESTORED)
        typer.flush_pending_restore()  # nothing left: a no-op

    def test_restore_in_background_flag_is_off(self):
        self.assertFalse(typer._RESTORE_IN_BACKGROUND)

    def test_full_format_original_is_restored(self):
        self.clip.non_text = b"PNGDATA"
        self._paste()
        self.assertEqual(self.clip.text, "user clip")
        self.assertEqual(self.clip.non_text, b"PNGDATA")

    def test_read_failure_never_writes_empty_restore(self):
        self.clip.readable = False
        self._paste()
        self.assertEqual(self.clip.writes, ["transcript"])
        self.assertEqual(self.clip.restores, [])
        self.assertEqual(self.clip.text, "transcript")

    def test_really_empty_original_is_cleared_not_written(self):
        self.clip.text = ""
        self._paste()
        self.assertEqual(self.clip.writes, ["transcript"])
        self.assertEqual(len(self.clip.restores), 1)
        self.assertEqual(self.clip.text, "")

    def test_incomplete_snapshot_types_instead(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        self._paste("short transcript")
        self.mocks["send_text_bulk"].assert_called_once_with("short transcript")
        self.mocks["send_paste"].assert_not_called()
        self.assertEqual(self.clip.writes, [])
        self.assertEqual(self.clip.non_text, b"files")

    def test_incomplete_snapshot_long_text_typed_on_windows(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        with patch.object(typer.sys, "platform", "win32"):
            self._paste("x" * 20000)
        self.mocks["send_text_bulk"].assert_called_once()
        self.mocks["send_paste"].assert_not_called()
        self.assertEqual(self.clip.non_text, b"files")

    def test_incomplete_snapshot_over_limit_still_pastes(self):
        for platform, limit in (("win32", 20000), ("darwin", 20000), ("linux", 2000)):
            with self.subTest(platform=platform):
                self.clip = FakeClipboard(non_text=b"files", complete=False)
                self.mocks["send_text_bulk"].reset_mock()
                self.mocks["send_paste"].reset_mock()
                with patch.object(typer.sys, "platform", platform):
                    self._paste("x" * (limit + 1))
                self.mocks["send_text_bulk"].assert_not_called()
                self.mocks["send_paste"].assert_called_once()
                self.assertEqual(self.clip.text, "user clip")

    def test_linux_types_only_up_to_2000_chars(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        with patch.object(typer.sys, "platform", "linux"):
            self._paste("x" * 2000)
        self.mocks["send_text_bulk"].assert_called_once()
        self.mocks["send_paste"].assert_not_called()

    def test_incomplete_snapshot_typing_failure_falls_back_to_paste(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        self.mocks["send_text_bulk"].return_value = False
        self._paste("short")
        self.mocks["send_paste"].assert_called_once()

    def test_f7_leaves_payload_and_retries_unrestored_first(self):
        self.clip.open_failures = 3
        self._paste("hold-to-talk text")
        self._paste("live final", restore_clipboard=False)
        self.assertEqual(
            self.clip.events[-2:], [("restore", "user clip"), ("write", "live final")]
        )
        self.assertEqual(self.clip.text, "live final")
        self.assertIsNone(typer._UNRESTORED)


class TestSelectionProbe(ClipboardSafetyBase):
    def test_plain_window_uses_ctrl_c(self):
        self.assertEqual(self._probe(), "picked text")
        self.assertEqual(self.copy_calls, ["ctrl+c"])
        self.assertEqual(self.clip.text, "user clip")

    def test_terminal_uses_ctrl_shift_c(self):
        self.mocks["foreground_is_terminal"].return_value = True
        self.mocks["foreground_is_ide_host"].return_value = True
        self.assertEqual(self._probe(), "picked text")
        self.assertEqual(self.copy_calls, ["ctrl+shift+c"])

    def test_ide_host_uses_ctrl_insert(self):
        self.mocks["foreground_is_ide_host"].return_value = True
        self.assertEqual(self._probe(), "picked text")
        self.assertEqual(self.copy_calls, ["ctrl+insert"])
        self.assertNotIn("ctrl+c", self.copy_calls)

    def test_probe_restores_full_format_original(self):
        self.clip.non_text = b"PNGDATA"
        self.assertEqual(self._probe(), "picked text")
        self.assertEqual(self.clip.non_text, b"PNGDATA")
        self.assertEqual(self.clip.text, "user clip")

    def test_probe_guard_is_the_copy_it_observed(self):
        self.assertEqual(self._probe(), "picked text")
        # The guard moved from the sentinel token to the observed copy's token.
        self.assertEqual(len(self.clip.guards_seen), 1)
        self.assertEqual(self.clip.events[-1], ("restore", "user clip"))

    def test_probe_retries_unrestored_record_first(self):
        self.clip.open_failures = 3
        self._paste("transcript")
        self.assertEqual(self._probe(), "picked text")
        self.assertIsNone(typer._UNRESTORED)
        self.assertEqual(self.clip.text, "user clip")

    def test_unreadable_clipboard_skips_probe(self):
        self.clip.readable = False
        self.assertEqual(self._probe(), "")
        self.assertEqual(self.clip.writes, [])
        self.assertEqual(self.copy_calls, [])

    def test_incomplete_clipboard_skips_probe(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        self.assertEqual(self._probe(), "")
        self.assertEqual(self.clip.writes, [])
        self.assertEqual(self.clip.non_text, b"files")


class TestPasteProbeSerialization(ClipboardSafetyBase):
    def test_probe_waits_for_paste_restore(self):
        in_delay = threading.Event()
        release = threading.Event()
        lock = threading.Lock()  # FakeClipboard is not thread-safe on its own

        def sleep(s):
            if s >= 0.15 and threading.current_thread().name == "paste":
                in_delay.set()
                release.wait(5)

        def on_copy():
            with lock:
                self.clip.user_copies("picked text")

        self.mocks["send_copy"].side_effect = on_copy
        results = {}
        with patch("typer.time.sleep", side_effect=sleep):
            paste = threading.Thread(
                target=lambda: typer.paste_text("transcript"), name="paste"
            )
            probe = threading.Thread(
                target=lambda: results.setdefault("sel", typer.get_selected_text(0.2)),
                name="probe",
            )
            paste.start()
            self.assertTrue(in_delay.wait(5))
            probe.start()
            probe.join(0.3)
            # The paste holds the lock through its delay: the probe has not
            # written its sentinel and the payload is still on the clipboard.
            self.assertTrue(probe.is_alive())
            self.assertEqual(self.clip.text, "transcript")
            release.set()
            paste.join(5)
            probe.join(5)
        self.assertEqual(results.get("sel"), "picked text")
        kinds = [e[0] for e in self.clip.events]
        # payload, paste's restore, then the probe's sentinel, copy, restore.
        self.assertEqual(kinds[:3], ["write", "restore", "write"])
        self.assertEqual(self.clip.events[1], ("restore", "user clip"))
        self.assertEqual(self.clip.text, "user clip")


class TestIdeHostTable(unittest.TestCase):
    def test_ide_names_match_and_terminals_do_not(self):
        from platforms.base import is_ide_host_identifier

        for name in ("Code.exe", "idea64.exe", "Code - Insiders.exe", "code", "pycharm"):
            self.assertTrue(is_ide_host_identifier(("", name)), name)
        for name in ("WindowsTerminal.exe", "chrome.exe", "", "notepad.exe"):
            self.assertFalse(is_ide_host_identifier(("", name)), name)

    def test_facade_exports_ide_helpers(self):
        import platforms

        self.assertTrue(callable(platforms.foreground_is_ide_host))
        self.assertTrue(callable(platforms.send_copy_ide))
        self.assertIsInstance(platforms.foreground_is_ide_host(), bool)


def _small_dib() -> bytes:
    """A 2x2 24-bit bottom-up DIB (BITMAPINFOHEADER + pixels)."""
    width, height = 2, 2
    row = bytes([0, 0, 255, 0, 255, 0]) + b"\x00\x00"  # 2 BGR pixels, padded to 4
    pixels = row * height
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0
    )
    return header + pixels


class _FakeWinApi:
    """Stands in for the private ctypes binding inside _win_restore."""

    def __init__(self, set_failures, can_open=True):
        self.set_failures = set_failures
        self.can_open = can_open
        self.seq = 7
        self.calls = []
        self.user32 = self

    def CreateWindowExW(self, *args):
        return 1

    def DestroyWindow(self, hwnd):
        return True

    def OpenClipboard(self, hwnd):
        return self.can_open

    def CloseClipboard(self):
        self.calls.append("close")
        return True

    def GetClipboardSequenceNumber(self):
        return self.seq

    def EmptyClipboard(self):
        self.calls.append("empty")
        self.seq += 1
        return True


class TestWindowsRestoreOutcome(unittest.TestCase):
    def _run(self, set_failures, expect_token=None, can_open=True):
        api = _FakeWinApi(set_failures, can_open)

        def fake_set(_api, fmt, data):
            api.calls.append(("set", fmt))
            if api.set_failures:
                api.set_failures -= 1
                return False
            return True

        snap = _snap(text="t", formats=((clip_mod.CF_UNICODETEXT, b"t\x00\x00"),))
        with patch.object(clip_mod, "_win_api", return_value=api), patch.object(
            clip_mod, "_win_set_bytes", side_effect=fake_set
        ), patch.object(clip_mod.time, "sleep"):
            status, token_after = clip_mod._win_restore(snap, expect_token)
        return status, token_after, api.calls

    def test_write_failure_after_empty_retries_once_in_session(self):
        status, _token, calls = self._run(set_failures=1)
        self.assertEqual(status, clip_mod.RESTORED)
        self.assertEqual(calls.count("empty"), 2)
        self.assertEqual(calls[-1], "close")

    def test_write_failing_twice_after_empty_reports_in_session_token(self):
        status, token_after, calls = self._run(set_failures=2, expect_token=7)
        self.assertEqual(status, clip_mod.FAILED)
        self.assertEqual(calls.count("empty"), 2)
        self.assertEqual(token_after, 9)  # read inside the session, after our empties

    def test_token_mismatch_is_changed_and_empties_nothing(self):
        status, token_after, calls = self._run(set_failures=0, expect_token=6)
        self.assertEqual(status, clip_mod.CHANGED)
        self.assertIsNone(token_after)
        self.assertNotIn("empty", calls)

    def test_open_failure_writes_nothing_and_reports_no_token(self):
        status, token_after, calls = self._run(set_failures=0, can_open=False)
        self.assertEqual(status, clip_mod.FAILED)
        self.assertIsNone(token_after)
        self.assertNotIn("empty", calls)


@unittest.skipUnless(sys.platform == "win32", "real clipboard round trip is Windows-only")
class TestWindowsRoundTrip(unittest.TestCase):
    def setUp(self):
        saved = clip_mod.clipboard_snapshot()
        if not saved.ok:
            self.skipTest("clipboard is busy")
        if saved.has_non_text and not saved.complete:
            self.skipTest("developer clipboard holds data that cannot be saved")
        self.addCleanup(clip_mod.clipboard_restore, saved)

    def test_text_and_dib_survive_snapshot_and_restore(self):
        dib = _small_dib()
        text = "odicto round trip ✓"
        seeded = _snap(
            text=text,
            formats=(
                (clip_mod.CF_UNICODETEXT, (text + "\x00").encode("utf-16-le")),
                (clip_mod.CF_DIB, dib),
            ),
            has_non_text=True,
        )
        self.assertTrue(clip_mod.clipboard_restore(seeded))

        snap = clip_mod.clipboard_snapshot()
        self.assertTrue(snap.ok)
        self.assertTrue(snap.complete)
        self.assertTrue(snap.has_non_text)
        self.assertEqual(snap.text, text)
        token_before = clip_mod.clipboard_change_token()

        self.assertTrue(clip_mod.clipboard_restore(_snap(text="overwritten")))
        self.assertNotEqual(clip_mod.clipboard_change_token(), token_before)
        self.assertEqual(clip_mod.clipboard_snapshot().text, "overwritten")

        self.assertTrue(clip_mod.clipboard_restore(snap))
        back = clip_mod.clipboard_snapshot()
        self.assertEqual(back.text, text)
        formats = dict(back.formats)
        self.assertIn(clip_mod.CF_DIB, formats)
        self.assertEqual(formats[clip_mod.CF_DIB][: len(dib)], dib)

    def test_token_mismatch_aborts_restore_without_writing(self):
        self.assertTrue(clip_mod.clipboard_restore(_snap(text="kept")))
        stale = clip_mod.clipboard_change_token() - 1
        self.assertFalse(clip_mod.clipboard_restore(_snap(text="must not land"), expect_token=stale))
        self.assertEqual(clip_mod.clipboard_snapshot().text, "kept")
        current = clip_mod.clipboard_change_token()
        self.assertTrue(clip_mod.clipboard_restore(_snap(text="lands"), expect_token=current))
        self.assertEqual(clip_mod.clipboard_snapshot().text, "lands")

    def test_empty_snapshot_clears_clipboard(self):
        self.assertTrue(clip_mod.clipboard_restore(_snap(text="")))
        snap = clip_mod.clipboard_snapshot()
        self.assertTrue(snap.ok)
        self.assertEqual(snap.text, "")
        self.assertEqual(snap.formats, ())



class TestWindowsFormatAllowList(unittest.TestCase):
    """Pure allow-list logic: fake formats and a recording reader, any OS."""

    HTML = 0xC100
    OLE_PRIVATE = 0xC200
    EMBED_SOURCE = 0xC201
    ALLOWED = frozenset(clip_mod._WIN_ALLOWED_STANDARD | {HTML})

    def _build(self, seen, data=None):
        reads = []
        data = data or {}

        def read(fmt):
            reads.append(fmt)
            return data.get(fmt, b"x")

        return clip_mod._win_build_snapshot(seen, self.ALLOWED, read), reads

    def test_owner_private_formats_are_never_read(self):
        text = ("hi" + chr(0)).encode("utf-16-le")
        snap, reads = self._build(
            [clip_mod.CF_UNICODETEXT, self.HTML, self.OLE_PRIVATE, self.EMBED_SOURCE],
            {clip_mod.CF_UNICODETEXT: text},
        )
        self.assertEqual(reads, [clip_mod.CF_UNICODETEXT, self.HTML])
        self.assertTrue(snap.complete)
        self.assertTrue(snap.has_non_text)
        self.assertEqual(snap.text, "hi")
        self.assertNotIn(self.OLE_PRIVATE, dict(snap.formats))

    def test_only_private_formats_is_incomplete(self):
        snap, reads = self._build([self.OLE_PRIVATE, self.EMBED_SOURCE])
        self.assertEqual(reads, [])
        self.assertFalse(snap.complete)
        self.assertTrue(snap.has_non_text)

    def test_unreadable_allowed_format_is_incomplete(self):
        snap, _ = self._build([clip_mod.CF_DIB], {clip_mod.CF_DIB: None})
        self.assertFalse(snap.complete)

    def test_budget_stops_further_reads(self):
        now = [0.0]

        def read(fmt):
            reads.append(fmt)
            now[0] += 1.0  # each render takes a "second"
            return b"x"

        reads = []
        snap = clip_mod._win_build_snapshot(
            [clip_mod.CF_UNICODETEXT, clip_mod.CF_DIB, self.HTML],
            self.ALLOWED,
            read,
            deadline=1.5,
            clock=lambda: now[0],
        )
        self.assertEqual(reads, [clip_mod.CF_UNICODETEXT, clip_mod.CF_DIB])
        self.assertFalse(snap.complete)

    def test_empty_clipboard_is_complete(self):
        snap, _ = self._build([])
        self.assertTrue(snap.complete)
        self.assertFalse(snap.has_non_text)


class TestWindowsSnapshotBudget(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, clip_mod, "_WIN_SNAPSHOT_THREAD", None)

    def test_slow_owner_times_out_as_incomplete_non_text(self):
        release = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            release.wait(5)
            return _snap(text="late")

        self.addCleanup(release.set)
        with patch.object(clip_mod, "_win_snapshot_blocking", side_effect=slow), patch.object(
            clip_mod, "_WIN_SNAPSHOT_TIMEOUT_S", 0.05
        ):
            snap = clip_mod._win_snapshot()
            self.assertTrue(snap.ok)
            self.assertFalse(snap.complete)
            self.assertTrue(snap.has_non_text)
            # The stuck worker is not stacked: the next call returns at once.
            again = clip_mod._win_snapshot()
            self.assertFalse(again.complete)
            self.assertEqual(len(calls), 1)
            release.set()
            clip_mod._WIN_SNAPSHOT_THREAD.join(5)

    def test_fast_owner_returns_the_snapshot(self):
        with patch.object(
            clip_mod, "_win_snapshot_blocking", return_value=_snap(text="quick")
        ):
            self.assertEqual(clip_mod._win_snapshot().text, "quick")


if __name__ == "__main__":
    unittest.main()
