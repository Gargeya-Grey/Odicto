"""Legacy entrypoints refuse incomplete migrations and forward owned complete receipts."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
import migration_guard
import setup_web
from tests import test_managed_migration as fixtures
from tools.distribution import manager as dist
from tools.distribution import migration


class TestMigrationGuard(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TestManagedMigration(methodName="test_copies_only_known_files_opaquely_and_preserves_source_and_unknown_files")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.source
        self.context = patch.dict(os.environ, {})
        self.context.start()
        self.addCleanup(self.context.stop)
        os.environ.pop("ODICTO_MANAGED_HOME", None)

    def complete(self):
        migration.migrate(self.fixture.manager, self.root, adapter=self.fixture.adapter, lock_factory=dist.file_lock)

    def test_unmigrated_source_does_not_forward_or_launch(self):
        with patch.object(migration_guard.subprocess, "run", side_effect=AssertionError("launch")):
            self.assertIsNone(migration_guard.forward(self.root, ["start"]))
            self.assertIsNone(migration_guard.check_source(self.root))

    def test_complete_receipt_forwards_lifecycle_to_owned_stable_command(self):
        self.complete()
        for command in ("start", "stop", "restart", "setup"):
            with patch.object(migration_guard.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run:
                self.assertEqual(migration_guard.forward(self.root, [command]), 7)
            launched = run.call_args.args[0]
            self.assertEqual(launched[-1], command)
            self.assertTrue(any(str(self.fixture.manager.home / "bin") in part for part in launched))
            self.assertFalse(any(str(self.root) in part for part in launched))

    def test_unknown_or_pending_receipt_never_launches(self):
        marker = self.root / migration.SOURCE_RECEIPT
        for contents in (b'{}', b'{"schema":1,"state":"pending"}', b'not-json'):
            marker.write_bytes(contents)
            with patch.object(migration_guard.subprocess, "run", side_effect=AssertionError("launch")):
                with self.assertRaises(RuntimeError):
                    migration_guard.forward(self.root, ["start"])

    def test_windows_forwarding_rejects_missing_or_modified_powershell_entry(self):
        self.complete()
        command = self.fixture.manager.home / "bin/odicto.cmd"
        command.write_bytes(b"synthetic stable command")
        marker = self.fixture.manager._read_json(self.fixture.manager.marker)
        import hashlib
        marker["launchers"]["bin/odicto.cmd"] = hashlib.sha256(command.read_bytes()).hexdigest()
        dist.atomic_json(self.fixture.manager.marker, marker)
        powershell = command.with_suffix(".ps1")
        for contents in (None, b"unknown owner"):
            with self.subTest(contents=contents):
                if contents is not None:
                    powershell.write_bytes(contents)
                elif powershell.exists():
                    powershell.unlink()
                with patch.object(migration, "sys", SimpleNamespace(platform="win32"), create=True), patch.object(
                    migration_guard, "sys", SimpleNamespace(platform="win32")
                ), patch.object(migration_guard.subprocess, "run", side_effect=AssertionError("unowned launcher executed")):
                    with self.assertRaisesRegex(RuntimeError, "ownership"):
                        migration_guard.forward(self.root, ["start"])

    def test_managed_environment_never_redirects_to_source_receipt(self):
        (self.root / migration.SOURCE_RECEIPT).write_bytes(b'unknown')
        with patch.dict(os.environ, {"ODICTO_MANAGED_HOME": "synthetic-home"}):
            self.assertIsNone(migration_guard.forward(self.root, ["start"]))
            self.assertIsNone(migration_guard.check_source(self.root))

    def test_source_lock_claim_rechecks_receipt_before_orphan_sweep(self):
        self.complete()
        with patch.object(main, "ROOT", self.root), patch.object(main, "acquire_single_instance_lock", return_value=True), patch.object(main, "release_single_instance_lock") as release, patch.object(main.platforms, "kill_other_odicto_processes", side_effect=AssertionError("orphan sweep")):
            self.assertFalse(main.claim_install(str(self.root / "dictation.pid")))
        release.assert_called_once()

    def test_source_setup_rechecks_receipt_under_lease_and_never_opens_page(self):
        self.complete()
        with patch.object(setup_web, "ROOT", self.root), patch.object(setup_web, "DATA_ROOT", self.root), patch.object(setup_web, "_run_server", side_effect=AssertionError("setup page")):
            with self.assertRaises(RuntimeError):
                setup_web.run_server(open_browser=False)
        with dist.file_lock(self.root / "setup.lock"):
            pass

    def test_source_wrapper_guards_run_before_legacy_startup_writes(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "scripts/windows/start_dictation.bat").read_text()
        self.assertLess(text.index(".odicto-migration.json"), text.index(".env"))
        self.assertIn("goto :managed_forward", text)
        self.assertIn("odicto.py\" %ODICTO_FORWARD%", text)


if __name__ == "__main__":
    unittest.main()
