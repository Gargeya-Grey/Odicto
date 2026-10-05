"""Clipboard safety: deferred guarded restore, full-format snapshots, safe copy chords.

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
    """Text clipboard with a change counter and optional non-text payload."""

    def __init__(self, text="user clip", non_text=None, complete=True, readable=True):
        self.text = text
        self.non_text = non_text  # stands in for an image / file list
        self.complete = complete
        self.readable = readable
        self.token = 1
        self.writes = []
        self.restores = []

    def read(self):
        return self.text

    def write(self, text):
        self.writes.append(text)
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

    def restore(self, snap):
        self.restores.append(snap)
        self.text = snap.text
        self.non_text = dict(snap.formats).get("img")
        self.token += 1
        return True

    def user_copies(self, text):
        self.text = text
        self.non_text = None
        self.token += 1


class ClipboardSafetyBase(unittest.TestCase):
    use_token = True

    def setUp(self):
        self.clip = FakeClipboard()
        self.sleeps = []
        self.copy_calls = []
        typer._PENDING = None
        patches = [
            patch("typer.clipboard_read", side_effect=lambda: self.clip.read()),
            patch("typer.clipboard_write", side_effect=lambda t: self.clip.write(t)),
            patch("typer.clipboard_snapshot", side_effect=lambda: self.clip.snapshot()),
            patch("typer.clipboard_restore", side_effect=lambda s: self.clip.restore(s)),
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
            # Deferred restores run inline (after the recorded "sleep") unless a
            # test explicitly wants the background thread.
            patch.object(typer, "_RESTORE_IN_BACKGROUND", False),
            patch.object(Config, "PASTE_DELAY_SECONDS", 1.0),
        ]
        self.mocks = {}
        for p in patches:
            self.mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, typer, "_PENDING", None)

    def _hold_restore(self):
        """Make the next deferred restore stay pending until flushed."""
        return patch("typer._deferred_restore")


class TestDeferredRestore(ClipboardSafetyBase):
    def test_restore_happens_after_the_delay(self):
        with patch("typer.time.sleep", side_effect=self.sleeps.append):
            typer.paste_text("transcript")
        self.assertIn("transcript", self.clip.writes)
        self.assertIn(1.0, self.sleeps)
        self.assertEqual(self.clip.text, "user clip")
        self.assertIsNone(typer._PENDING)

    def test_paste_returns_before_background_restore(self):
        release = threading.Event()
        done = threading.Event()
        real_deferred = typer._deferred_restore

        def gated(pending, delay):
            release.wait(5)
            real_deferred(pending, 0)
            done.set()

        with patch.object(typer, "_RESTORE_IN_BACKGROUND", True), patch(
            "typer._deferred_restore", side_effect=gated
        ):
            typer.paste_text("transcript")
            # paste_text has returned; the payload is still on the clipboard.
            self.assertEqual(self.clip.text, "transcript")
            release.set()
            self.assertTrue(done.wait(5))
        self.assertEqual(self.clip.text, "user clip")

    def test_user_change_during_delay_prevents_restore(self):
        with self._hold_restore():
            typer.paste_text("transcript")
        self.clip.user_copies("something new")
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "something new")

    def test_app_change_detected_by_text_when_no_token(self):
        self.use_token = False
        with self._hold_restore():
            typer.paste_text("transcript")
        self.clip.user_copies("app wrote this")
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "app wrote this")

    def test_flush_runs_the_pending_restore_now(self):
        with self._hold_restore():
            typer.paste_text("transcript")
        self.assertIsNotNone(typer._PENDING)
        self.assertEqual(self.clip.text, "transcript")
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "user clip")
        self.assertIsNone(typer._PENDING)
        typer.flush_pending_restore()  # nothing pending: a no-op

    def test_back_to_back_pastes_keep_the_first_original(self):
        with self._hold_restore():
            typer.paste_text("first")
            typer.paste_text("second")
        self.assertEqual(typer._PENDING.snapshot.text, "user clip")
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "user clip")
        self.assertNotIn("first", [s.text for s in self.clip.restores])

    def test_full_format_original_is_restored(self):
        self.clip.non_text = b"PNGDATA"
        typer.paste_text("transcript")
        self.assertEqual(self.clip.text, "user clip")
        self.assertEqual(self.clip.non_text, b"PNGDATA")

    def test_read_failure_never_writes_empty_restore(self):
        self.clip.readable = False
        with patch("typer.time.sleep"):
            typer.paste_text("transcript")
        self.assertEqual(self.clip.writes, ["transcript"])
        self.assertEqual(self.clip.restores, [])
        self.assertEqual(self.clip.text, "transcript")

    def test_really_empty_original_is_cleared_not_written(self):
        self.clip.text = ""
        typer.paste_text("transcript")
        self.assertEqual(self.clip.writes, ["transcript"])
        self.assertEqual(len(self.clip.restores), 1)
        self.assertEqual(self.clip.text, "")

    def test_incomplete_snapshot_types_instead(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        typer.paste_text("short transcript")
        self.mocks["send_text_bulk"].assert_called_once_with("short transcript")
        self.mocks["send_paste"].assert_not_called()
        self.assertEqual(self.clip.writes, [])
        self.assertEqual(self.clip.non_text, b"files")

    def test_incomplete_snapshot_long_text_still_pastes(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        typer.paste_text("x" * 2001)
        self.mocks["send_text_bulk"].assert_not_called()
        self.mocks["send_paste"].assert_called_once()
        self.assertEqual(self.clip.text, "user clip")

    def test_incomplete_snapshot_typing_failure_falls_back_to_paste(self):
        self.clip.non_text = b"files"
        self.clip.complete = False
        self.mocks["send_text_bulk"].return_value = False
        typer.paste_text("short")
        self.mocks["send_paste"].assert_called_once()

    def test_f7_cancels_pending_restore(self):
        with self._hold_restore():
            typer.paste_text("hold-to-talk text")
        with patch("typer.time.sleep"):
            typer.paste_text("live final", restore_clipboard=False)
        self.assertIsNone(typer._PENDING)
        typer.flush_pending_restore()
        self.assertEqual(self.clip.text, "live final")


class TestSelectionProbe(ClipboardSafetyBase):
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
            result = typer.get_selected_text(timeout=0.2)
        return result

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

    def test_probe_after_paste_reuses_pending_original(self):
        with self._hold_restore():
            typer.paste_text("transcript")
        self.assertEqual(self._probe(), "picked text")
        self.assertIsNone(typer._PENDING)
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

    def test_empty_snapshot_clears_clipboard(self):
        self.assertTrue(clip_mod.clipboard_restore(_snap(text="")))
        snap = clip_mod.clipboard_snapshot()
        self.assertTrue(snap.ok)
        self.assertEqual(snap.text, "")
        self.assertEqual(snap.formats, ())


if __name__ == "__main__":
    unittest.main()
