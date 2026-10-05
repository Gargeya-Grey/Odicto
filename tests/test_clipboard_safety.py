"""Clipboard safety: synchronous guarded restore, full-format snapshots, safe copy chords.

Everything except the Windows round trip runs against a fake clipboard; the real
OS clipboard is never touched by those tests.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

import typer
from config import Config
from platforms import clipboard as clip_mod
from platforms.clipboard import ClipboardSnapshot

_REAL_WAIT_RESTORE_WINDOW = typer._wait_restore_window


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
        self.write_empty_then_fail = 0  # next N owned writes empty it, then fail
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

    def write_token(self, text, expect_token=None):
        """clipboard_write_text_token: check, write and token in one step."""
        if expect_token is not None and expect_token != self.token:
            self.events.append(("write-aborted", text))
            return "changed", None
        if self.write_empty_then_fail:
            self.write_empty_then_fail -= 1
            self.text = ""
            self.non_text = None
            self.token += 1
            self.events.append(("emptied", ""))
            return "failed", self.token
        self.write(text)
        return "written", self.token

    def read_token(self):
        return self.text, self.token

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
        typer._reset_restore_now_for_tests()
        self.addCleanup(typer._reset_restore_now_for_tests)
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
            patch(
                "typer.clipboard_write_text_token",
                side_effect=lambda t, expect_token=None: self.clip.write_token(t, expect_token),
            ),
            patch("typer.clipboard_read_text_token", side_effect=lambda: self.clip.read_token()),
            patch("typer._wait_restore_window", side_effect=self._window),
            patch.object(Config, "PASTE_DELAY_SECONDS", 1.0),
            # Safety net: nothing in these tests may reach the real OS clipboard.
            patch.object(clip_mod, "_win_api", side_effect=AssertionError("real clipboard")),
            patch.object(clip_mod, "_text_restore", side_effect=AssertionError("real clipboard")),
            patch.object(clip_mod, "_text_read", side_effect=AssertionError("real clipboard")),
        ]
        self.mocks = {}
        for p in patches:
            self.mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)

    def _window(self, timeout):
        """Stands in for the post-chord wait (an Event wait in typer)."""
        self.windows = getattr(self, "windows", [])
        self.windows.append(timeout)
        hook = getattr(self, "on_window", None)
        if hook is not None:
            hook(timeout)

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
        self.on_window = lambda t: order.append(("window", t))

        with patch("typer.time.sleep", side_effect=lambda s: order.append(("sleep", s))):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user clip")
        # 0.15 s floor sleep, then the rest of the 1.0 s delay as an Event wait.
        i_paste = order.index("paste")
        i_floor = order.index(("sleep", 0.15), i_paste)
        window = [e for e in order if e[0] == "window"]
        self.assertEqual(len(window), 1)
        self.assertAlmostEqual(window[0][1], 0.85)
        self.assertLess(i_floor, order.index(window[0]))
        self.assertEqual(self.clip.events, [("write", "transcript"), ("restore", "user clip")])
        self.assertFalse(typer.last_paste_restore_failed())

    def test_user_copy_during_delay_is_never_overwritten(self):
        def sleep(s):
            if s >= 0.15:
                self.clip.user_copies("user copied during delay")

        with patch("typer.time.sleep", side_effect=sleep):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user copied during delay")
        self.assertEqual(self.clip.restores, [])
        self.assertEqual(self.clip.restore_attempts, 1)  # "changed": no retry
        self.assertFalse(typer.last_paste_restore_failed())

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
        self.assertFalse(typer.last_paste_restore_failed())

    def test_empty_then_failed_write_uses_the_in_session_token(self):
        self.clip.empty_then_fail = 1
        self._paste()
        first, second = self.clip.guards_seen[:2]
        self.assertNotEqual(first, second)
        self.assertEqual(second, first + 1)  # the token the failed attempt produced
        self.assertEqual(self.clip.text, "user clip")
        self.assertFalse(typer.last_paste_restore_failed())

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

class TestRoundThreeFixes(ClipboardSafetyBase):
    def test_guard_is_the_payload_write_not_a_user_copy_right_after_it(self):
        # A user copy lands right after the payload write (here: during its
        # verification read). The guard must still be the payload's own token,
        # so the restore reports "changed" and keeps the user's copy.
        state = {"armed": True}
        real_read = self.clip.read

        def read():
            value = real_read()
            if state["armed"] and value == "transcript":
                state["armed"] = False
                self.clip.user_copies("user copy right after the write")
            return value

        self.mocks["clipboard_read"].side_effect = read
        self._paste()
        self.assertEqual(self.clip.text, "user copy right after the write")
        self.assertEqual(self.clip.restores, [])

    def test_probe_sentinel_guard_comes_from_its_own_write(self):
        self.assertEqual(self._probe(), "picked text")
        write_tokens = [e for e in self.clip.events if e[0] == "write"]
        self.assertTrue(write_tokens[0][1].startswith("\ufeffodicto-sel-"))

    def test_request_restore_now_ends_the_wait(self):
        done = threading.Event()
        self.mocks["_wait_restore_window"].side_effect = _REAL_WAIT_RESTORE_WINDOW

        def run():
            with patch.object(Config, "PASTE_DELAY_SECONDS", 10.0):
                typer.paste_text("transcript")
            done.set()

        with patch("typer.time.sleep"):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertFalse(done.wait(0.3))  # waiting out the 10 s delay
            self.assertEqual(self.clip.text, "transcript")
            typer.request_restore_now()
            self.assertTrue(done.wait(2))
            worker.join(2)
        self.assertEqual(self.clip.text, "user clip")

    def test_restore_now_is_sticky_for_later_pastes(self):
        # Quit's request must survive an insertion that starts afterwards.
        typer.request_restore_now()
        self.mocks["_wait_restore_window"].side_effect = _REAL_WAIT_RESTORE_WINDOW
        sleeps = []
        with patch.object(Config, "PASTE_DELAY_SECONDS", 10.0):
            started = time.monotonic()
            for text in ("one", "two"):
                with patch("typer.time.sleep", side_effect=sleeps.append):
                    typer.paste_text(text)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(sleeps.count(0.15), 2)  # the floor still runs
        self.assertTrue(typer._RESTORE_NOW.is_set())
        self.assertEqual(self.clip.text, "user clip")


class TestRoundFourFixes(ClipboardSafetyBase):
    def test_write_that_emptied_then_failed_restores_the_original(self):
        self.clip.write_empty_then_fail = 3
        with self.assertRaises(RuntimeError), patch("typer.time.sleep"):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user clip")
        self.assertFalse(typer.last_paste_restore_failed())
        self.mocks["send_paste"].assert_not_called()


class TestNoCarryOver(ClipboardSafetyBase):
    """A failed restore retries ~2 s, then is dropped; nothing carries over."""

    def test_open_failure_retries_with_the_same_guard_then_gives_up(self):
        self.clip.open_failures = 100
        sleeps = self._paste()
        guard = self.clip.guards_seen[0]
        self.assertEqual(self.clip.guards_seen, [guard] * typer._RESTORE_RETRIES)
        backoff = sleeps.count(typer._RESTORE_RETRY_BACKOFF_S)
        self.assertEqual(backoff, typer._RESTORE_RETRIES - 1)
        self.assertGreaterEqual(backoff * typer._RESTORE_RETRY_BACKOFF_S, 1.75)  # ~2 s
        self.assertTrue(typer.last_paste_restore_failed())
        self.assertEqual(self.clip.text, "transcript")  # dropped: payload stays

    def test_flag_is_reset_by_the_next_paste_and_nothing_is_retried(self):
        self.clip.open_failures = typer._RESTORE_RETRIES
        self._paste("first")
        self.assertTrue(typer.last_paste_restore_failed())
        attempts = self.clip.restore_attempts
        self._paste("second")
        self.assertFalse(typer.last_paste_restore_failed())
        # Exactly one restore for the second paste: no stale record retried.
        self.assertEqual(self.clip.restore_attempts, attempts + 1)
        self.assertEqual(self.clip.text, "first")  # its fresh original

    def test_codex_a_then_b_scenario_ends_with_b(self):
        self.clip.text = "A"
        self.clip.open_failures = typer._RESTORE_RETRIES
        self._paste("payload1")
        self.clip.user_copies("B")
        self._paste("payload2")
        self.assertEqual(self.clip.text, "B")
        self.assertNotIn(("restore", "A"), self.clip.events)

    def test_changed_stops_retrying_at_once(self):
        self.clip.open_failures = 1

        def on_attempt(n):
            if n == 2:
                self.clip.user_copies("user copy")

        self.clip.on_restore_attempt = on_attempt
        self._paste()
        self.assertEqual(self.clip.restore_attempts, 2)
        self.assertEqual(self.clip.text, "user copy")
        self.assertFalse(typer.last_paste_restore_failed())

    def test_incomplete_snapshot_types_after_a_failed_earlier_restore(self):
        # Codex image scenario: an earlier restore failed; the user then copies
        # an image the platform cannot save. The next paste must type.
        self.clip.open_failures = typer._RESTORE_RETRIES
        self._paste("first")
        self.clip.user_copies("")
        self.clip.non_text = b"image"
        self.clip.complete = False
        self._paste("second")
        self.mocks["send_text_bulk"].assert_called_once_with("second")
        self.assertEqual(self.clip.non_text, b"image")

    def test_flush_is_a_no_op(self):
        self.clip.open_failures = typer._RESTORE_RETRIES
        self._paste()
        attempts = self.clip.restore_attempts
        typer.flush_pending_restore()
        typer.flush_pending_restore(max_wait=0)
        self.assertEqual(self.clip.restore_attempts, attempts)

    def test_failed_payload_write_that_emptied_and_cannot_restore_sets_flag(self):
        self.clip.write_empty_then_fail = 3
        self.clip.open_failures = 100
        with self.assertRaises(RuntimeError), patch("typer.time.sleep"):
            typer.paste_text("transcript")
        self.assertTrue(typer.last_paste_restore_failed())

    def test_probe_sentinel_write_that_touched_clipboard_skips_and_restores(self):
        self.clip.write_empty_then_fail = 1
        self.assertEqual(self._probe(), "")
        self.assertEqual(self.copy_calls, [])
        self.assertEqual(self.clip.text, "user clip")
        self.assertEqual(self.clip.guards_seen[0], self.clip.token - 1)

    def test_f7_leaves_payload(self):
        self._paste("live final", restore_clipboard=False)
        self.assertEqual(self.clip.text, "live final")
        self.assertEqual(self.clip.restore_attempts, 0)

class TestWindowsTextFamily(unittest.TestCase):
    class _Api:
        def GetSystemDefaultLCID(self):
            return 0x0809

        def GetUserDefaultLCID(self):
            raise AssertionError("CF_LOCALE must come from the system locale")

    def test_locale_is_system_lcid_and_code_pages_match(self):
        text = "caf\u00e9"
        family = dict(clip_mod._win_text_family(self._Api(), text))
        self.assertEqual(family[clip_mod.CF_LOCALE], struct.pack("<I", 0x0809))
        self.assertEqual(family[clip_mod.CF_UNICODETEXT], (text + chr(0)).encode("utf-16-le"))
        self.assertEqual(
            family[clip_mod.CF_TEXT], clip_mod._encode_or_ascii(text, "mbcs") + bytes(1)
        )
        self.assertEqual(
            family[clip_mod.CF_OEMTEXT], clip_mod._encode_or_ascii(text, "oem") + bytes(1)
        )
        if sys.platform == "win32":
            self.assertEqual(family[clip_mod.CF_TEXT], text.encode("mbcs") + bytes(1))
            self.assertEqual(family[clip_mod.CF_OEMTEXT], text.encode("oem", "replace") + bytes(1))


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

    def GetSystemDefaultLCID(self):
        return 1033

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

    def test_write_text_token_is_the_token_of_that_write(self):
        status, token = clip_mod.clipboard_write_text_token("owned write")
        self.assertEqual(status, clip_mod.WRITTEN)
        self.assertEqual(token, clip_mod.clipboard_change_token())
        self.assertEqual(clip_mod.clipboard_read_text_token(), ("owned write", token))
        # Conditional write: a stale token writes nothing.
        status, none = clip_mod.clipboard_write_text_token("must not land", expect_token=token - 1)
        self.assertEqual((status, none), (clip_mod.CHANGED, None))
        self.assertEqual(clip_mod.clipboard_read_text_token()[0], "owned write")

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
