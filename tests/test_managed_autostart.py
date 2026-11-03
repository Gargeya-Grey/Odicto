"""Managed startup entries use the stable launcher and preserve unknown files."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import managed_autostart


class TestManagedAutostart(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "Odicto managed 100%"
        (self.home / "bin").mkdir(parents=True)
        self.target = self.root / "startup" / "odicto-entry"
        self.receipt = self.home / "autostart.json"
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(managed_autostart, "_location", return_value=self.target))

    def platform(self, name, windows=False):
        self.context.enter_context(patch.object(managed_autostart, "sys", SimpleNamespace(platform=name)))
        self.context.enter_context(patch.object(managed_autostart, "os", SimpleNamespace(
            name="nt" if windows else "posix", replace=os.replace)))
        launcher = self.home / "bin" / ("odicto.cmd" if windows else "odicto")
        launcher.write_text("synthetic launcher", encoding="utf-8")
        return launcher.resolve()

    def test_mac_plist_launches_stable_command(self):
        launcher = self.platform("darwin")
        managed_autostart.configure(self.home, True)
        self.assertEqual(plistlib.loads(self.target.read_bytes()), {
            "Label": "com.odicto.managed", "ProgramArguments": [str(launcher), "start"],
            "RunAtLoad": True, "KeepAlive": False})
        self.assertNotIn("releases", self.target.read_text())

    def test_linux_desktop_launches_quoted_stable_command(self):
        launcher = self.platform("linux")
        managed_autostart.configure(self.home, True)
        command = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        self.assertEqual(self.target.read_text(),
            "[Desktop Entry]\nType=Application\nName=Odicto\n"
            f'Exec="{command}" start\nTerminal=false\nX-GNOME-Autostart-enabled=true\n')

    def test_windows_batch_launches_stable_command(self):
        launcher = self.platform("win32", windows=True)
        managed_autostart.configure(self.home, True)
        expected = ("@echo off\r\nsetlocal DisableDelayedExpansion\r\n"
                    f'call "{str(launcher).replace("%", "%%")}" start\r\n')
        self.assertEqual(self.target.read_bytes(), expected.encode("utf-8"))

    def test_repeat_enable_and_disable_preserve_unrelated_files(self):
        self.platform("linux")
        unknown = self.home / "unknown-owner.txt"
        unknown.write_bytes(b"keep me")
        managed_autostart.configure(self.home, True)
        original = self.target.read_bytes()
        managed_autostart.configure(self.home, True)
        self.assertEqual(self.target.read_bytes(), original)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt, {"path": str(self.target),
                                  "sha256": hashlib.sha256(original).hexdigest()})
        managed_autostart.configure(self.home, False)
        managed_autostart.configure(self.home, False)
        self.assertFalse(self.target.exists())
        self.assertEqual(unknown.read_bytes(), b"keep me")

    def test_disable_without_existing_entry_is_a_no_op(self):
        managed_autostart.configure(self.home, False)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.receipt.exists())

    def test_unknown_existing_entry_is_preserved_when_enabling_or_disabling(self):
        self.platform("linux")
        self.target.parent.mkdir()
        self.target.write_bytes(b"belongs to someone else")
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaisesRegex(RuntimeError, "unknown ownership"):
                managed_autostart.configure(self.home, enabled)
            self.assertEqual(self.target.read_bytes(), b"belongs to someone else")
        self.assertFalse(self.receipt.exists())

    def test_modified_owned_entry_is_preserved_when_enabling_or_disabling(self):
        self.platform("linux")
        managed_autostart.configure(self.home, True)
        original_receipt = self.receipt.read_bytes()
        self.target.write_bytes(b"external modification")
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaisesRegex(RuntimeError, "unknown ownership"):
                managed_autostart.configure(self.home, enabled)
            self.assertEqual(self.target.read_bytes(), b"external modification")
            self.assertEqual(self.receipt.read_bytes(), original_receipt)

    def test_receipt_for_different_path_does_not_authorize_existing_entry(self):
        self.platform("linux")
        self.target.parent.mkdir()
        contents = b"unknown entry"
        self.target.write_bytes(contents)
        self.receipt.write_text(json.dumps({"path": str(self.root / "other-entry"),
            "sha256": hashlib.sha256(contents).hexdigest()}))
        with self.assertRaisesRegex(RuntimeError, "unknown ownership"):
            managed_autostart.configure(self.home, False)
        self.assertEqual(self.target.read_bytes(), contents)

    def test_symlink_entry_guard_preserves_destination(self):
        self.platform("linux")
        self.target.parent.mkdir()
        self.target.write_bytes(b"symlink destination fixture")
        real_is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
            side_effect=lambda path: path == self.target or real_is_symlink(path)):
            with self.assertRaisesRegex(RuntimeError, "unknown ownership"):
                managed_autostart.configure(self.home, False)
        self.assertEqual(self.target.read_bytes(), b"symlink destination fixture")

    def test_symlink_receipt_guard_preserves_entry_and_metadata(self):
        self.platform("linux")
        self.receipt.write_bytes(b"not owned metadata")
        real_is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
            side_effect=lambda path: path == self.receipt or real_is_symlink(path)):
            with self.assertRaisesRegex(RuntimeError, "metadata is a symlink"):
                managed_autostart.configure(self.home, True)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.receipt.read_bytes(), b"not owned metadata")

    def test_missing_launcher_creates_no_startup_entry(self):
        self.context.enter_context(patch.object(managed_autostart, "sys", SimpleNamespace(platform="linux")))
        self.context.enter_context(patch.object(managed_autostart, "os", SimpleNamespace(name="posix", replace=os.replace)))
        with self.assertRaisesRegex(RuntimeError, "stable managed launcher is missing"):
            managed_autostart.configure(self.home, True)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.receipt.exists())
