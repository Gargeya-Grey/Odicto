"""Opaque settings migration against isolated files, fake startup and real file leases."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.distribution import manager as dist
from tools.distribution import migration as migration


class FakeStartup:
    def __init__(self):
        self.preference = migration.StartupPreference(True, True, {"task": "synthetic-owned-task"})
        self.inactive = True
        self.inspections = 0
        self.applications = 0
        self.verifications = 0
        self.fail_once = False
        self.applied = False

    def inspect(self, source, manager):
        self.inspections += 1
        return self.preference

    def assert_inactive(self, source, manager):
        if not self.inactive:
            raise migration.MigrationError("A source or destination process is live or uncertain")

    def apply(self, source, manager, preference, transaction_dir):
        self.applications += 1
        self.applied = True
        self.last_preference = preference
        self.transaction_dir = transaction_dir
        if self.fail_once:
            self.fail_once = False
            raise OSError("synthetic interruption")

    def verify(self, source, manager, preference, transaction_dir):
        self.verifications += 1
        if not self.applied:
            raise migration.MigrationError("Startup transition was not applied")


class TestManagedMigration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-migration-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        (self.source / "app").mkdir(parents=True)
        for relative in ("main.py", "odicto.py", "app/paths.py"):
            (self.source / relative).write_text("# synthetic source\n", encoding="utf-8")
        (self.source / "tools/distribution").mkdir(parents=True)
        (self.source / "tools/distribution/capabilities.json").write_text(
            json.dumps({"schema": 1, "source_migration_guard": True}), encoding="utf-8")
        (self.source / ".env.example").write_bytes(b"SYNTHETIC=default\n")
        self.settings = b"SYNTHETIC=opaque\xff\x00\r\n"
        self.prompt = b"synthetic private prompt\xfe\r\n"
        (self.source / ".env").write_bytes(self.settings)
        (self.source / "prompt.txt").write_bytes(self.prompt)
        self.manager = dist.Manager(self.root / "managed", installer=lambda *_: None)
        self.manager.initialize()
        self.release = self.manager.releases / "v1.3.0-aaaaaaaaaaaa-bbbbbbbb"
        self.release.mkdir()
        (self.release / ".env.example").write_bytes(b"SYNTHETIC=default\n")
        (self.release / "prompt.txt.example").write_bytes(b"synthetic default prompt\n")
        self.record = {"version": "v1.3.0", "directory": self.release.name, "archive_sha256": "a" * 64}
        dist.atomic_json(self.release / ".odicto-ready.json", self.record)
        dist.atomic_json(self.manager.pointer, self.record)
        self.launcher = self.manager.home / "bin" / ("odicto.ps1" if os.name == "nt" else "odicto")
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b"synthetic stable launcher")
        marker = self.manager._read_json(self.manager.marker)
        marker["launchers"] = {self.launcher.relative_to(self.manager.home).as_posix():
                               hashlib.sha256(self.launcher.read_bytes()).hexdigest()}
        dist.atomic_json(self.manager.marker, marker)
        self.adapter = FakeStartup()

    def migrate(self, **kwargs):
        return migration.migrate(self.manager, self.source, adapter=self.adapter,
                                 lock_factory=dist.file_lock, **kwargs)

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_check_is_read_only_and_marks_lease_guarantee_as_advisory(self):
        before = self.inventory()
        plan = self.migrate(check=True)
        self.assertEqual(plan["state"], "ready")
        self.assertTrue(plan["advisory"])
        self.assertFalse(plan["guaranteed_no_live_owner"])
        self.assertEqual({file["name"] for file in plan["files"]}, {".env", "prompt.txt"})
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.adapter.applications, 0)

    def test_copies_only_known_files_opaquely_and_preserves_source_and_unknown_files(self):
        unknown_source = self.source / "do-not-import.txt"
        unknown_source.write_bytes(b"source unknown")
        unknown_target = self.manager.data / "keep.txt"
        unknown_target.write_bytes(b"target unknown")
        original_read_text = Path.read_text

        def public_text(path, *args, **kwargs):
            if path.name in migration.KNOWN_FILES:
                raise AssertionError("Private settings must never be decoded")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", public_text):
            result = self.migrate()
        self.assertEqual(result["state"], "complete")
        self.assertEqual((self.manager.data / ".env").read_bytes(), self.settings)
        self.assertEqual((self.manager.data / "prompt.txt").read_bytes(), self.prompt)
        self.assertEqual((self.source / ".env").read_bytes(), self.settings)
        self.assertEqual((self.source / "prompt.txt").read_bytes(), self.prompt)
        self.assertEqual(unknown_source.read_bytes(), b"source unknown")
        self.assertEqual(unknown_target.read_bytes(), b"target unknown")
        self.assertFalse((self.manager.data / unknown_source.name).exists())
        recovery = Path(result["recovery"])
        self.assertEqual((recovery / "source/.env").read_bytes(), self.settings)
        self.assertTrue(self.adapter.last_preference.admin)
        self.assertTrue(self.adapter.last_preference.autostart)
        self.assertEqual(migration.validated_forward(self.source), self.launcher)
        receipts = (self.manager.home / "migration.json").read_bytes() + (self.source / migration.SOURCE_RECEIPT).read_bytes()
        self.assertNotIn(self.settings, receipts)
        self.assertNotIn(self.prompt, receipts)

    def test_default_destination_is_recoverably_replaced(self):
        destination = self.manager.data / ".env"
        original = (self.release / ".env.example").read_bytes()
        destination.write_bytes(original)
        result = self.migrate()
        self.assertEqual(destination.read_bytes(), self.settings)
        self.assertEqual((Path(result["recovery"]) / "destination/.env").read_bytes(), original)

    def test_configured_destination_conflict_does_not_write_settings_or_receipts(self):
        (self.manager.data / ".env").write_bytes(b"SYNTHETIC=owner configured\n")
        before = self.inventory()
        with self.assertRaisesRegex(migration.MigrationError, "already configured"):
            self.migrate(check=True)
        self.assertEqual(self.inventory(), before)
        with self.assertRaisesRegex(migration.MigrationError, "already configured"):
            self.migrate()
        self.assertFalse((self.manager.home / "migration.json").exists())
        self.assertFalse((self.source / migration.SOURCE_RECEIPT).exists())
        self.assertEqual((self.manager.data / ".env").read_bytes(), b"SYNTHETIC=owner configured\n")

    def test_missing_source_prompt_does_not_accept_custom_destination_prompt(self):
        (self.source / "prompt.txt").unlink()
        target = self.manager.data / "prompt.txt"
        target.write_bytes(b"destination custom prompt")
        before = self.inventory()
        with self.assertRaisesRegex(migration.MigrationError, "already configured"):
            self.migrate(check=True)
        self.assertEqual(self.inventory(), before)

    def test_missing_source_prompt_cannot_be_shadowed_by_destination_default_file(self):
        (self.source / "prompt.txt").unlink()
        (self.source / ".env").write_bytes(b"SYSTEM_PROMPT=synthetic inline prompt\n")
        target = self.manager.data / "prompt.txt"
        target.write_bytes((self.release / "prompt.txt.example").read_bytes())
        with self.assertRaisesRegex(migration.MigrationError, "already configured"):
            self.migrate(check=True)

    def test_retry_rechecks_originally_absent_files_before_any_apply(self):
        (self.source / "prompt.txt").unlink()
        self.adapter.fail_once = True
        with self.assertRaises(OSError):
            self.migrate()
        self.adapter.applications = 0
        target = self.manager.data / "prompt.txt"
        target.write_bytes(b"new destination prompt")
        before = target.read_bytes()
        with self.assertRaisesRegex(migration.MigrationError, "changed|configured"):
            self.migrate()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.adapter.applications, 0)

    def test_actual_dotenv_key_grammar_cannot_bypass_prompt_guard(self):
        (self.source / "prompt.txt").unlink()
        for assignment in ("'SYSTEM_PROMPT_FILE'=custom.txt", "export 'SYSTEM_PROMPT_FILE'='custom.txt'",
                           "\u00a0SYSTEM_PROMPT_FILE=custom.txt", "SYSTEM_PROMPT_FILE=\"custom\\nfile.txt\""):
            with self.subTest(assignment=assignment):
                (self.source / ".env").write_bytes((assignment + "\n").encode("utf-8"))
                with self.assertRaisesRegex(migration.MigrationError, "Save.*source Setup"):
                    self.migrate(check=True)

    def test_prompt_validation_is_bound_to_the_exact_recorded_settings_bytes(self):
        settings = self.source / ".env"
        (self.source / "prompt.txt").unlink()
        original_digest = migration._digest
        for check in (True, False):
            with self.subTest(check=check):
                settings.write_bytes(b"SYSTEM_PROMPT_FILE=prompt.txt\n")
                changed = [False]
                def concurrent_edit(path):
                    if path == settings and not changed[0]:
                        changed[0] = True
                        settings.write_bytes(b"SYSTEM_PROMPT_FILE=custom-legacy.txt\n")
                    return original_digest(path)
                with patch.object(migration, "_digest", side_effect=concurrent_edit):
                    with self.assertRaisesRegex(migration.MigrationError, "changed|source Setup"):
                        self.migrate(check=check)
                self.assertFalse((self.manager.home / "migration.json").exists())
                self.assertFalse((self.source / migration.SOURCE_RECEIPT).exists())
                self.assertFalse((self.manager.data / ".env").exists())

    def test_legacy_prompt_reference_blocks_before_any_receipt_or_settings_change(self):
        (self.source / "prompt.txt").unlink()
        for value in (b"custom.txt", b"'prompts/private.txt'", b'"C:/other/private.txt"'):
            with self.subTest(value=value):
                (self.source / ".env").write_bytes(b"SYSTEM_PROMPT_FILE=" + value + b"\n")
                before = self.inventory()
                with self.assertRaisesRegex(migration.MigrationError, "Save.*source Setup"):
                    self.migrate(check=True)
                self.assertEqual(self.inventory(), before)

    def test_canonical_prompt_reference_stays_opaque_and_migrates(self):
        (self.source / ".env").write_bytes(b'SYSTEM_PROMPT_FILE="prompt.txt"\nSYNTHETIC=opaque\xff\n')
        result = self.migrate()
        self.assertEqual(result["state"], "complete")
        self.assertEqual((self.manager.data / ".env").read_bytes(), (self.source / ".env").read_bytes())

    def test_configured_prompt_conflict_is_detected_before_any_setting_is_copied(self):
        (self.manager.data / "prompt.txt").write_bytes(b"owner's synthetic prompt")
        with self.assertRaisesRegex(migration.MigrationError, "already configured"):
            self.migrate()
        self.assertFalse((self.manager.data / ".env").exists())
        self.assertEqual(self.adapter.applications, 0)

    def test_completed_migration_is_idempotent_without_reapplying_startup(self):
        self.migrate()
        first = (self.manager.home / "migration.json").read_bytes()
        result = self.migrate()
        self.assertTrue(result["already_migrated"])
        self.assertEqual((self.manager.home / "migration.json").read_bytes(), first)
        self.assertEqual(self.adapter.applications, 1)
        self.assertEqual(self.adapter.inspections, 1)

    def test_failed_startup_transaction_blocks_source_and_recovers_on_same_token(self):
        (self.manager.data / ".env").write_bytes((self.release / ".env.example").read_bytes())
        self.adapter.fail_once = True
        with self.assertRaisesRegex(OSError, "synthetic interruption"):
            self.migrate()
        failed = self.manager._read_json(self.manager.home / "migration.json")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["failure"], "OSError")
        with self.assertRaisesRegex(migration.MigrationError, "incomplete"):
            migration.validated_forward(self.source)
        result = self.migrate()
        complete = self.manager._read_json(self.manager.home / "migration.json")
        self.assertEqual(complete["token"], failed["token"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.adapter.applications, 2)
        self.assertEqual(self.adapter.verifications, 1)
        self.assertEqual((Path(result["recovery"]) / "destination/.env").read_bytes(), b"SYNTHETIC=default\n")

    def test_resume_preserves_destination_edit_after_failure(self):
        self.adapter.fail_once = True
        with self.assertRaises(OSError):
            self.migrate()
        changed = b"synthetic edit made after failure"
        (self.manager.data / ".env").write_bytes(changed)
        with self.assertRaisesRegex(migration.MigrationError, "changed during migration"):
            self.migrate()
        self.assertEqual((self.manager.data / ".env").read_bytes(), changed)
        self.assertEqual(self.adapter.applications, 1)

    def test_resume_preserves_source_edit_after_failure(self):
        self.adapter.fail_once = True
        with self.assertRaises(OSError):
            self.migrate()
        changed = b"synthetic source edit made after failure"
        (self.source / ".env").write_bytes(changed)
        with self.assertRaisesRegex(migration.MigrationError, "changed after migration began"):
            self.migrate()
        self.assertEqual((self.source / ".env").read_bytes(), changed)

    def test_live_or_uncertain_process_blocks_both_plan_and_execution(self):
        self.adapter.inactive = False
        for check in (True, False):
            with self.subTest(check=check), self.assertRaisesRegex(migration.MigrationError, "live or uncertain"):
                self.migrate(check=check)
        self.assertFalse((self.manager.data / ".env").exists())
        self.assertEqual(self.adapter.applications, 0)

    def test_source_and_target_runtime_and_setup_lease_races_block_without_copy(self):
        for root in (self.source, self.manager.data):
            for name in ("dictation.lock", "setup.lock"):
                with self.subTest(root=root.name, name=name), dist.file_lock(root / name):
                    with self.assertRaises(dist.BusyError):
                        self.migrate()
                self.assertFalse((self.manager.data / ".env").exists())
        self.assertEqual(self.adapter.applications, 0)

    def test_managed_pending_startup_reservation_is_checked_under_launch_lease(self):
        with patch.object(self.manager, "_recover_launch_intent", side_effect=dist.BusyError("pending startup")):
            with self.assertRaisesRegex(dist.BusyError, "pending startup"):
                self.migrate()
        self.assertFalse((self.manager.data / ".env").exists())
        self.assertEqual(self.adapter.applications, 0)

    def test_pending_source_guard_exists_before_settings_or_startup_changes(self):
        observed = []
        original = migration._copy_verified

        def copy_with_guard(source, target, digest):
            marker = json.loads((self.source / migration.SOURCE_RECEIPT).read_text(encoding="utf-8"))
            observed.append(marker["state"])
            with self.assertRaises(migration.MigrationError):
                migration.validated_forward(self.source)
            return original(source, target, digest)

        with patch.object(migration, "_copy_verified", side_effect=copy_with_guard):
            self.migrate()
        self.assertTrue(observed)
        self.assertEqual(set(observed), {"pending"})

    def test_unknown_receipts_do_not_authorize_forwarding_or_overwrite(self):
        unknown = {"schema": 1, "source": str(self.source), "home": str(self.manager.home),
                   "token": "c" * 32, "state": "complete"}
        (self.source / migration.SOURCE_RECEIPT).write_text(json.dumps(unknown), encoding="utf-8")
        before = (self.source / migration.SOURCE_RECEIPT).read_bytes()
        for operation in (lambda: self.migrate(), lambda: migration.validated_forward(self.source)):
            with self.assertRaisesRegex(migration.MigrationError, "no destination transaction"):
                operation()
        self.assertEqual((self.source / migration.SOURCE_RECEIPT).read_bytes(), before)

    def test_forwarding_rejects_modified_launcher_and_mismatched_reciprocal_receipt(self):
        self.migrate()
        original = self.launcher.read_bytes()
        self.launcher.write_bytes(b"unknown modified launcher")
        with self.assertRaisesRegex(migration.MigrationError, "unknown ownership"):
            migration.validated_forward(self.source)
        self.launcher.write_bytes(original)
        receipt = self.manager._read_json(self.manager.home / "migration.json")
        receipt["token"] = "f" * 32
        dist.atomic_json(self.manager.home / "migration.json", receipt)
        with self.assertRaisesRegex(migration.MigrationError, "disagree"):
            migration.validated_forward(self.source)

    def test_missing_receipt_returns_no_forward_target(self):
        self.assertIsNone(migration.validated_forward(self.source))

    def test_settings_links_and_unknown_recovery_file_are_preserved(self):
        linked = self.root / "synthetic-settings"
        linked.write_bytes(self.settings)
        (self.source / ".env").unlink()
        os.link(linked, self.source / ".env")
        with self.assertRaisesRegex(migration.MigrationError, "multiply linked"):
            self.migrate()
        self.assertEqual(linked.read_bytes(), self.settings)

    def test_complete_journal_recovers_crash_before_source_completion(self):
        self.migrate()
        marker = self.manager._read_json(self.source / migration.SOURCE_RECEIPT)
        marker["state"] = "pending"
        dist.atomic_json(self.source / migration.SOURCE_RECEIPT, marker)
        with self.assertRaisesRegex(migration.MigrationError, "incomplete"):
            migration.validated_forward(self.source)
        self.assertTrue(self.migrate()["already_migrated"])
        self.assertEqual(migration.validated_forward(self.source), self.launcher)
        self.assertEqual(self.adapter.applications, 1)

    def test_copy_interruption_recovers_imported_first_file_and_preserves_original_defaults(self):
        (self.manager.data / ".env").write_bytes(b"SYNTHETIC=default\n")
        original = migration._copy_verified

        def interrupted(source, target, digest):
            if target.name == "prompt.txt":
                raise OSError("synthetic copy interruption")
            return original(source, target, digest)

        with patch.object(migration, "_copy_verified", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "copy interruption"):
                self.migrate()
        self.assertEqual((self.manager.data / ".env").read_bytes(), self.settings)
        self.assertFalse((self.manager.data / "prompt.txt").exists())
        self.assertEqual(self.adapter.applications, 0)
        result = self.migrate()
        self.assertEqual((self.manager.data / "prompt.txt").read_bytes(), self.prompt)
        self.assertEqual((Path(result["recovery"]) / "destination/.env").read_bytes(), b"SYNTHETIC=default\n")

    def test_startup_transition_holds_both_setup_and_runtime_leases_and_live_supervisor(self):
        original = self.adapter.apply

        def require_leases(source, manager, preference, transaction_dir):
            journal = manager._read_json(manager.home / "migration.json")
            self.assertEqual(journal["supervisor_pid"], os.getpid())
            self.assertEqual(journal["state"], "pending")
            for directory in (source, manager.data):
                for name in ("dictation.lock", "setup.lock"):
                    with self.assertRaises(dist.BusyError), dist.file_lock(directory / name):
                        self.fail("Migration released an owner lease before retiring source startup")
            return original(source, manager, preference, transaction_dir)

        with patch.object(self.adapter, "apply", side_effect=require_leases):
            self.migrate()
        self.assertEqual(self.adapter.verifications, 1)

    def test_failed_startup_verification_does_not_authorize_source_forwarding(self):
        with patch.object(self.adapter, "verify", side_effect=migration.MigrationError("synthetic startup mismatch")):
            with self.assertRaisesRegex(migration.MigrationError, "startup mismatch"):
                self.migrate()
        journal = self.manager._read_json(self.manager.home / "migration.json")
        self.assertEqual(journal["state"], "failed")
        with self.assertRaisesRegex(migration.MigrationError, "incomplete"):
            migration.validated_forward(self.source)
        self.assertEqual(self.migrate()["state"], "complete")

    def test_invalid_file_inventory_on_resume_cannot_escape_known_files(self):
        self.adapter.fail_once = True
        with self.assertRaises(OSError):
            self.migrate()
        journal = self.manager._read_json(self.manager.home / "migration.json")
        journal["files"][0]["name"] = "../unknown.txt"
        dist.atomic_json(self.manager.home / "migration.json", journal)
        with self.assertRaisesRegex(migration.MigrationError, "file inventory"):
            self.migrate()
        self.assertFalse((self.manager.home / "unknown.txt").exists())

    def test_overlap_and_managed_source_are_rejected_without_writes(self):
        with self.assertRaisesRegex(migration.MigrationError, "separate directories"):
            migration.plan_migration(self.manager, self.manager.home, adapter=self.adapter)
        (self.source / ".odicto-managed.json").write_text("{}", encoding="utf-8")
        before = self.inventory()
        with self.assertRaisesRegex(migration.MigrationError, "not another managed"):
            self.migrate(check=True)
        self.assertEqual(self.inventory(), before)

    def test_source_without_legacy_start_guard_requires_checkout_update(self):
        capability = self.source / "tools/distribution/capabilities.json"
        for contents in (None, {"schema": 1}, {"schema": 1, "source_migration_guard": False}):
            with self.subTest(contents=contents):
                if contents is None:
                    capability.unlink()
                else:
                    capability.write_text(json.dumps(contents), encoding="utf-8")
                with self.assertRaisesRegex(migration.MigrationError, "Update the source checkout first"):
                    self.migrate()
        self.assertFalse((self.manager.data / ".env").exists())


if __name__ == "__main__":
    unittest.main()
