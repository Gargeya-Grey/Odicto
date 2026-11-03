"""Managed privilege routing with fake task, process and UAC boundaries only."""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import migration_guard
import setup_web
import windows_admin
from tools.distribution import managed_admin as admin
from tests import test_managed_distribution as fixtures
from tests.test_managed_launch import Child

dist = fixtures.dist
ROOT = Path(__file__).resolve().parents[1]


class TestManagedAdmin(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TestManagedDistribution(methodName="test_new_install_creates_settings_once_from_the_example")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.manager = fixture.manager
        self.old = fixture.initial()
        self.directory = self.manager.release_path(self.old)
        self.support = SimpleNamespace(enabled=MagicMock(return_value=True),
            elevated=MagicMock(return_value=False), stop=MagicMock(return_value=0),
            start_task=MagicMock(), assert_inactive=MagicMock(), helper=MagicMock())
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(self.manager, "support", return_value=self.support))
        self.context.enter_context(patch.object(self.manager, "admin_enabled", return_value=True))
        self.context.enter_context(patch.object(dist.subprocess, "Popen", side_effect=AssertionError("unmocked process launch")))
        self.context.enter_context(patch.object(dist.subprocess, "run", side_effect=AssertionError("unmocked command launch")))

    def task(self):
        with patch.object(self.manager, "_ready", return_value=True):
            self.assertEqual(self.manager.admin_task(), 0)

    def reservation(self, state="pending"):
        token = "a" * 32
        dist.atomic_json(self.manager.home / "launch-intent.json", {"token": token,
            "directory": self.directory.name, "supervisor_pid": os.getpid(), "child_pid": None,
            "receipt": "launch-receipt-" + token + ".json"})
        dist.atomic_json(self.manager.home / "admin-request.json", {"schema": 1,
            "home": str(self.manager.home), "token": token, "directory": self.directory.name,
            "state": state, "launcher_pid": os.getpid()})
        return token

    def test_medium_start_reserves_before_task_and_receives_authoritative_result(self):
        seen = []
        def task(home, directory):
            with self.assertRaises(dist.BusyError):
                with dist.file_lock(home / "launch.lock"):
                    pass
            intent = self.manager._read_json(home / "launch-intent.json")
            request = self.manager._read_json(home / "admin-request.json")
            self.assertEqual(request["token"], intent["token"])
            self.assertEqual(request["state"], "pending")
            seen.append(directory)
            dist.atomic_json(home / ("admin-result-" + intent["token"] + ".json"),
                             {"token": intent["token"], "code": 0})
        self.support.start_task.side_effect = task
        with patch.object(self.manager, "_ready", return_value=False):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
        self.assertEqual(seen, [self.directory])

    def test_ready_elevated_owner_is_left_alone(self):
        with patch.object(self.manager, "_ready", return_value=True):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
        self.support.start_task.assert_not_called()

    def test_elevated_task_recognizes_owner_hidden_from_medium_readiness_probe(self):
        import odicto
        import psutil
        import time
        directory = self.directory
        (self.manager.data / "dictation.pid").write_text("12345")
        dist.atomic_json(self.manager.data / "dictation-health.json", {"pid": 12345,
            "updated_at": time.time(), "ready": True, "code_root": str(directory),
            "launch_token": "b" * 32,
            "microphone": {"closed": False, "callback_age_s": 0, "callback_count": 1}})
        owner = MagicMock()
        owner.create_time.return_value = time.time() - 20
        owner.cmdline.side_effect = [psutil.AccessDenied(12345), ["python", str(directory / "main.py")]]
        def readiness(*args):
            return odicto.cmd_wait_ready(SimpleNamespace(timeout=0, expected_root=str(directory))) == 0
        def task(home, directory):
            with patch.object(self.support, "elevated", return_value=True):
                self.assertEqual(self.manager.admin_task(), 0)
        self.support.start_task.side_effect = task
        with patch.object(self.manager, "_ready", side_effect=readiness), patch.object(
            odicto, "_data_root", return_value=str(self.manager.data)
        ), patch.object(psutil, "Process", return_value=owner), patch("platforms.base.is_odicto_command", return_value=True):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
        self.assertFalse((self.manager.home / "launch-intent.json").exists())
        self.assertEqual(self.manager._read_json(self.manager.home / "admin-request.json")["state"], "released")

    def test_stop_uses_stable_quit_identity_without_child_or_task(self):
        self.assertEqual(self.manager.run(["stop"], automatic_updates=False), 0)
        self.support.stop.assert_called_once_with(self.manager, self.directory)
        self.support.start_task.assert_not_called()

    def test_failed_stop_never_starts_replacement(self):
        self.support.stop.return_value = 1
        self.assertEqual(self.manager.run(["restart"], automatic_updates=False), 1)
        self.support.start_task.assert_not_called()

    def test_restart_stops_before_task_reservation(self):
        calls = []
        self.support.stop.side_effect = lambda *args: calls.append("stop") or 0
        def task(home, directory):
            calls.append("task")
            token = self.manager._read_json(home / "launch-intent.json")["token"]
            dist.atomic_json(home / ("admin-result-" + token + ".json"), {"token": token, "code": 0})
        self.support.start_task.side_effect = task
        with patch.object(self.manager, "_ready", return_value=False):
            self.assertEqual(self.manager.run(["restart"], automatic_updates=False), 0)
        self.assertEqual(calls, ["stop", "task"])

    def test_task_failure_cancels_unclaimed_reservation_before_late_task_can_start(self):
        self.support.start_task.side_effect = RuntimeError("synthetic task failure")
        with patch.object(self.manager, "_ready", return_value=False), self.assertRaises(RuntimeError):
            self.manager.run(["start"], automatic_updates=False)
        self.assertFalse((self.manager.home / "launch-intent.json").exists())
        self.assertEqual(self.manager._read_json(self.manager.home / "admin-request.json")["state"], "cancelled")
        self.support.elevated.return_value = True
        with self.assertRaises(dist.DistributionError):
            self.manager.admin_task()

    def test_timeout_after_task_claim_keeps_launch_intent_and_cancels_task(self):
        def task(home, directory):
            path = home / "admin-request.json"
            value = self.manager._read_json(path)
            value.update(state="claimed", supervisor_pid=os.getpid())
            dist.atomic_json(path, value)
        self.support.start_task.side_effect = task
        with patch.object(self.manager, "_ready", return_value=False), patch.object(dist, "STARTUP_TIMEOUT", 0), patch.object(dist, "CANCEL_TIMEOUT", 0):
            with self.assertRaises(dist.BusyError):
                self.manager.run(["start"], automatic_updates=False)
        self.assertTrue((self.manager.home / "launch-intent.json").exists())
        self.assertEqual(self.manager._read_json(self.manager.home / "admin-request.json")["state"], "cancelled")

    def test_unknown_request_is_preserved(self):
        request = self.manager.home / "admin-request.json"
        request.write_bytes(b'{"owner":"someone else"}')
        with patch.object(self.manager, "_ready", return_value=False), self.assertRaises(dist.DistributionError):
            self.manager.run(["start"], automatic_updates=False)
        self.assertEqual(request.read_bytes(), b'{"owner":"someone else"}')
        self.support.start_task.assert_not_called()

    def test_elevated_task_retains_existing_s_then_r_handshake(self):
        token = self.reservation()
        self.support.elevated.return_value = True
        child = Child()
        child.stdin = MagicMock(spec=io.BytesIO)
        child.stdin.closed = False
        with patch.object(dist.subprocess, "Popen", return_value=child), patch.object(self.manager, "_ready", side_effect=[False, True]):
            self.assertEqual(self.manager.admin_task(), 0)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S", b"R"])
        result = self.manager._read_json(self.manager.home / ("admin-result-" + token + ".json"))
        self.assertEqual(result, {"token": token, "code": 0})
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_task_refuses_dead_medium_supervisor(self):
        self.reservation()
        self.support.elevated.return_value = True
        with patch.object(self.manager, "_pid_alive", return_value=False), self.assertRaises(dist.DistributionError):
            self.manager.admin_task()

    def test_task_refuses_mismatched_release_and_cancelled_request(self):
        self.support.elevated.return_value = True
        for state in ("cancelled", "claimed"):
            self.reservation(state)
            with self.assertRaises(dist.DistributionError):
                self.manager.admin_task()
        self.reservation()
        value = self.manager._read_json(self.manager.home / "admin-request.json")
        value["directory"] = "unowned-release"
        dist.atomic_json(self.manager.home / "admin-request.json", value)
        with self.assertRaises(dist.DistributionError):
            self.manager.admin_task()

    def test_parent_death_during_elevated_start_never_releases_child(self):
        self.reservation()
        self.support.elevated.return_value = True
        child = Child()
        child.stdin = MagicMock(spec=io.BytesIO)
        child.stdin.closed = False
        with patch.object(dist.subprocess, "Popen", return_value=child), patch.object(self.manager, "_ready", return_value=False), patch.object(self.manager, "_pid_alive", side_effect=[True, False]):
            self.assertEqual(self.manager.admin_task(), 1)
        self.assertEqual([call.args[0] for call in child.stdin.write.call_args_list], [b"S"])
        child.stdin.close.assert_called_once()
        self.assertEqual(child.wait_calls, 1)

    def test_task_failure_retains_uncertain_child_and_blocks_activation(self):
        self.reservation()
        self.support.elevated.return_value = True
        child = Child(wait_error=True)
        with patch.object(dist.subprocess, "Popen", return_value=child), patch.object(self.manager, "_ready", return_value=False), patch.object(dist, "STARTUP_TIMEOUT", 0):
            self.assertEqual(self.manager.admin_task(), 1)
        self.assertTrue((self.manager.home / "launch-intent.json").exists())
        with self.assertRaises(dist.BusyError):
            self.manager.activate(self.old)

    def released_reservation(self):
        token = self.reservation()
        request = self.manager._read_json(self.manager.home / "admin-request.json")
        request.update(state="released", supervisor_pid=os.getpid())
        dist.atomic_json(self.manager.home / "admin-request.json", request)
        intent = self.manager._read_json(self.manager.home / "launch-intent.json")
        intent["child_pid"] = 12344
        dist.atomic_json(self.manager.home / "launch-intent.json", intent)
        dist.atomic_json(self.manager.home / intent["receipt"], {"token": token, "pid": 12345, "state": "owner-starting"})
        return token

    def test_released_elevated_reservation_recovers_without_medium_readiness(self):
        self.released_reservation()
        with patch.object(self.manager, "_pid_alive", side_effect=lambda pid: pid != os.getpid()), patch.object(self.manager, "_ready", return_value=False):
            self.manager._recover_launch_intent()
        self.assertFalse((self.manager.home / "launch-intent.json").exists())
        with dist.file_lock(self.manager.data / "dictation.lock"):
            with self.assertRaises(dist.BusyError):
                self.manager.activate(self.old)

    def test_live_privileged_cleanup_writer_keeps_released_reservation(self):
        self.released_reservation()
        with patch.object(self.manager, "_pid_alive", return_value=True), self.assertRaises(dist.BusyError):
            self.manager._recover_launch_intent()
        self.assertTrue((self.manager.home / "launch-intent.json").exists())

    def test_stop_can_quit_owner_before_recovering_uncertain_elevated_reservation(self):
        self.released_reservation()
        request = self.manager._read_json(self.manager.home / "admin-request.json")
        request["state"] = "claimed"
        dist.atomic_json(self.manager.home / "admin-request.json", request)
        alive = [True]
        self.support.stop.side_effect = lambda *_: alive.__setitem__(0, False) or 0
        with patch.object(self.manager, "_pid_alive", side_effect=lambda *_: alive[0]), patch.object(self.manager, "_ready", return_value=False):
            self.assertEqual(self.manager.run(["stop"], automatic_updates=False), 0)
        self.support.stop.assert_called_once()
        self.assertFalse((self.manager.home / "launch-intent.json").exists())

    def test_successful_stop_preserves_live_startup_writer_reservation(self):
        self.released_reservation()
        with patch.object(self.manager, "_pid_alive", return_value=True), patch.object(self.manager, "_ready", return_value=False):
            self.assertEqual(self.manager.run(["stop"], automatic_updates=False), 0)
        self.support.stop.assert_called_once()
        self.assertTrue((self.manager.home / "launch-intent.json").exists())

    def test_admin_enabled_version_switch_requires_capability_and_stable_marker(self):
        marker = self.manager.data / ".odicto-admin-enabled"
        marker.write_text("synthetic-enabled")
        newer = self.manager.stage(self.fixture.add_release("v1.3.0"))
        with self.assertRaisesRegex(dist.DistributionError, "cannot preserve"):
            self.manager.activate(newer)
        capabilities = self.manager.release_path(newer) / "tools/distribution/capabilities.json"
        value = json.loads(capabilities.read_text())
        value["managed_admin_mode"] = True
        capabilities.write_text(json.dumps(value))
        self.manager.activate(newer)
        self.assertEqual(self.manager.active()["version"], "v1.3.0")
        with self.assertRaisesRegex(dist.DistributionError, "cannot preserve"):
            self.manager.rollback()
        self.assertEqual(self.manager.active()["version"], "v1.3.0")

    def test_partial_migration_blocks_launch_and_activation(self):
        dist.atomic_json(self.manager.home / "migration.json", {"state": "failed"})
        with self.assertRaisesRegex(dist.DistributionError, "unfinished"):
            self.manager.run(["start"], automatic_updates=False)
        with self.assertRaisesRegex(dist.DistributionError, "unfinished"):
            self.manager.activate(self.old)
        self.support.start_task.assert_not_called()

    def test_admin_switch_holds_owner_and_activation_leases_and_leaves_stopped(self):
        self.support.enabled.side_effect = [False, True]
        actions = []
        def configure(manager, action):
            for path in (manager.home / "manager.lock", manager.home / "launch.lock", manager.data / "dictation.lock"):
                with self.assertRaises(dist.BusyError):
                    with dist.file_lock(path):
                        pass
            actions.append(action)
        self.support.helper.side_effect = configure
        with patch.object(dist, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr)):
            self.manager.configure_admin(True)
        self.assertEqual(actions, ["Enable"])
        self.support.stop.assert_called_once()
        self.support.start_task.assert_not_called()

    def test_admin_switch_failed_stop_preserves_preference(self):
        self.support.enabled.return_value = False
        self.support.stop.return_value = 1
        with patch.object(dist, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr)), self.assertRaises(dist.DistributionError):
            self.manager.configure_admin(True)
        self.support.helper.assert_not_called()

    def test_admin_switch_serializes_against_pending_start(self):
        with dist.file_lock(self.manager.home / "launch.lock"), patch.object(dist, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr)):
            with self.assertRaises(dist.BusyError):
                self.manager.configure_admin(False)
        self.support.stop.assert_not_called()

    def test_disabled_admin_cannot_inherit_elevated_setup_token(self):
        self.support.elevated.return_value = True
        with patch.object(self.manager, "admin_enabled", return_value=False):
            with patch.object(dist, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr)):
                with self.assertRaisesRegex(dist.DistributionError, "normal terminal"):
                    self.manager.run(["start"], automatic_updates=False)
        self.support.start_task.assert_not_called()


class TestManagedWindowsIdentity(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "managed"
        (self.home / "data").mkdir(parents=True)
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.dict(os.environ, {"ODICTO_MANAGED_HOME": str(self.home), "ODICTO_DATA_ROOT": str(self.home / "data")}))
        self.context.enter_context(patch.object(windows_admin, "_is_windows", return_value=True))
        self.context.enter_context(patch.object(windows_admin.subprocess, "run", side_effect=AssertionError("unmocked command launch")))

    def marker(self):
        (self.home / "data/.odicto-admin-enabled").write_text(json.dumps({"schema": 1,
            "home": str(self.home), "task": admin.task_name(self.home)}))

    def test_marker_and_quit_identity_are_stable_across_release_versions(self):
        self.marker()
        for version in ("v1.3.0", "v1.4.0"):
            code = self.home / "releases" / version
            self.assertTrue(windows_admin.admin_mode_enabled(code))
            self.assertEqual(windows_admin.marker_path(code), str(self.home / "data/.odicto-admin-enabled"))
        self.assertEqual(windows_admin.quit_event_name(self.home / "data"), windows_admin.quit_event_name(self.home / "data"))
        self.assertNotEqual(admin.task_name(self.home), admin.task_name(self.home / "other"))

    def test_managed_medium_main_cannot_bypass_stable_launcher(self):
        self.marker()
        with patch.object(windows_admin, "caller_is_elevated", return_value=False), self.assertRaisesRegex(RuntimeError, "stable odicto"):
            windows_admin.run_if_needed(self.home / "releases/v1.3.0", "Ensure")
        with patch.object(windows_admin, "caller_is_elevated", return_value=True):
            self.assertIsNone(windows_admin.run_if_needed(self.home / "releases/v1.3.0", "Ensure"))

    def test_bad_marker_or_data_identity_fails_closed(self):
        self.marker()
        path = self.home / "data/.odicto-admin-enabled"
        path.write_text('{"schema":1,"task":"Odicto"}')
        with self.assertRaises(RuntimeError):
            windows_admin.admin_mode_enabled(self.home)
        with patch.dict(os.environ, {"ODICTO_DATA_ROOT": str(self.home / "other")}):
            with self.assertRaises(RuntimeError):
                windows_admin.marker_path(self.home)

    def test_setup_switch_uses_stable_command_and_never_source_helper(self):
        (self.home / "launcher.py").write_text("# fixture")
        with patch.object(windows_admin.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            ok, text = windows_admin.set_admin_mode(self.home / "releases/v1.3.0", True)
        self.assertTrue(ok)
        self.assertIn("odicto start", text)
        self.assertEqual(run.call_args.args[0], [sys.executable, str(self.home / "launcher.py"), "admin", "on"])

    def test_managed_setup_start_routes_to_manager_without_source_admin_dispatch(self):
        with patch.object(windows_admin, "run_if_needed", side_effect=AssertionError("source admin route")), patch.object(setup_web, "restart_odicto", return_value=setup_web.RestartMessage("ready", started=True)):
            result = setup_web.start_odicto()
        self.assertTrue(result["ok"])


class TestManagedNativeBoundary(unittest.TestCase):
    def test_process_identity_resolves_script_path_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            alias = root / "alias"
            physical = root / "physical"
            physical.mkdir()
            process = SimpleNamespace(pid=12345, info={"name": "python.exe"},
                cmdline=lambda: ["python", str(alias / "main.py"), "setup"])
            original = os.path.realpath
            def resolve_alias(value, *args, **kwargs):
                path = Path(value)
                if path.is_relative_to(alias):
                    return str(physical / path.relative_to(alias))
                return original(value, *args, **kwargs)
            with patch("os.path.realpath", side_effect=resolve_alias), patch("psutil.process_iter", return_value=[process]):
                with self.assertRaisesRegex(RuntimeError, "lifecycle process"):
                    admin.assert_inactive(alias, alias, allow_setup=True)

    def test_relative_process_scripts_resolve_against_process_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = Path(temporary).resolve()
            process = SimpleNamespace(pid=12345, info={"name": "python.exe"}, cwd=lambda: str(code),
                cmdline=lambda: ["python", "main.py", "setup"])
            with patch("psutil.process_iter", return_value=[process]):
                with self.assertRaisesRegex(RuntimeError, "lifecycle process"):
                    admin.assert_inactive(code, code, allow_setup=True)
                process.cmdline = lambda: ["python", "odicto.py", "_managed-setup"]
                admin.assert_inactive(code, code, allow_setup=True)

    def test_recycled_non_python_health_pid_does_not_block_inactive_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "dictation-health.json").write_text(json.dumps({"pid": 12345, "updated_at": 1}))
            with patch("psutil.pid_exists", return_value=True), patch("psutil.Process", return_value=SimpleNamespace(name=lambda: "browser.exe")), patch("psutil.process_iter", return_value=[]):
                admin.assert_inactive(data, data)

    def test_inaccessible_health_pid_keeps_migration_blocked(self):
        import psutil
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "dictation-health.json").write_text(json.dumps({"pid": 12345, "updated_at": 1}))
            with patch("psutil.pid_exists", return_value=True), patch("psutil.Process", side_effect=psutil.AccessDenied(12345)):
                with self.assertRaisesRegex(RuntimeError, "unreadable"):
                    admin.assert_inactive(data, data)

    def test_posix_migration_preserves_already_owned_managed_autostart(self):
        from tests.test_managed_migration import TestManagedMigration
        fixture = TestManagedMigration("test_check_is_read_only_and_marks_lease_guarantee_as_advisory")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.adapter = admin.MigrationAdapter()
        startup = SimpleNamespace(inspect=MagicMock(return_value=(Path("synthetic"), Path("receipt"), True)), configure=MagicMock())
        with patch.object(admin, "sys", SimpleNamespace(platform="linux")), patch.object(admin, "_autostart", return_value=startup), patch.object(admin, "assert_inactive"), patch.object(Path, "home", return_value=fixture.root):
            plan = fixture.migrate(check=True)
            self.assertTrue(plan["startup"]["autostart"])
            self.assertEqual(fixture.migrate()["state"], "complete")
        startup.configure.assert_called_once_with(fixture.manager.home, True)

    def test_live_or_uncertain_pid_blocks_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "dictation.pid").write_text("12345")
            with patch("psutil.pid_exists", return_value=True), patch("psutil.Process", return_value=SimpleNamespace(name=lambda: "python.exe")), self.assertRaisesRegex(RuntimeError, "live or uncertain"):
                admin.assert_inactive(data, data)
            (data / "dictation.pid").write_text("invalid")
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                admin.assert_inactive(data, data)

    def test_unreadable_python_owner_blocks_migration(self):
        import psutil
        process = SimpleNamespace(pid=12345, info={"name": "pythonw.exe"}, cmdline=MagicMock(side_effect=psutil.AccessDenied(12345)))
        with tempfile.TemporaryDirectory() as tmp, patch("psutil.process_iter", return_value=[process]), self.assertRaisesRegex(RuntimeError, "unreadable"):
            admin.assert_inactive(Path(tmp), Path(tmp))

    def test_only_real_setup_can_share_admin_switch_not_main_with_setup_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp)
            process = SimpleNamespace(pid=12345, info={"name": "python.exe"}, cmdline=lambda: ["python", str(code / "odicto.py"), "_managed-setup"])
            with patch("psutil.process_iter", return_value=[process]):
                admin.assert_inactive(code, code, allow_setup=True)
            process.cmdline = lambda: ["python", str(code / "main.py"), "setup"]
            with patch("psutil.process_iter", return_value=[process]), self.assertRaisesRegex(RuntimeError, "lifecycle process"):
                admin.assert_inactive(code, code, allow_setup=True)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell parser and pure policy functions")
    def test_powershell_task_ownership_with_fake_objects(self):
        program = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:ODICTO_MANAGED_HELPER,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw ($errors | Out-String) }
$wanted=@('Assert-ManagedTask','ConvertTo-UserSid','Test-EnumIs','Test-ZeroTime','Get-TaskMismatch','Inspect-Source')
$defs=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $n.Name},$true))
if($defs.Count -ne $wanted.Count) { throw 'Missing pure ownership policy' }
foreach($definition in $defs) { Invoke-Expression $definition.Extent.Text }
$ManagedHome='C:\Synthetic managed'
$PowerShellExe='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$ManagedArguments='-NoProfile -ExecutionPolicy Bypass -File "C:\Synthetic managed\bin\odicto.ps1" admin-task'
$sid='S-1-5-21-111-222-333-1001'
function New-FakeTask {
    return [pscustomobject]@{
        Actions=@([pscustomobject]@{Execute=$PowerShellExe;Arguments=$ManagedArguments;WorkingDirectory=$ManagedHome})
        Principal=[pscustomobject]@{UserId=$sid;RunLevel='Highest';LogonType='Interactive'}
        Triggers=@()
        Settings=[pscustomobject]@{MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT0S';Enabled=$true;DisallowStartIfOnBatteries=$false;StopIfGoingOnBatteries=$false;StartWhenAvailable=$true}
        State='Ready'
    }
}
foreach($release in @('v1.2.1','v1.3.0')) { Assert-ManagedTask (New-FakeTask) }
$mutations=@(
    {param($t) $t.Actions[0].Execute='unrelated.exe'},
    {param($t) $t.Actions[0].Arguments='-File "C:\Synthetic managed\releases\v1.2.1\start.ps1"'},
    {param($t) $t.Actions[0].WorkingDirectory='C:\Other'},
    {param($t) $t.Principal.UserId='S-1-5-21-999-999-999-1001'},
    {param($t) $t.Principal.RunLevel='Limited'},
    {param($t) $t.Triggers=@([pscustomobject]@{Enabled=$true})},
    {param($t) $t.Settings.MultipleInstances='Parallel'},
    {param($t) $t.Settings.ExecutionTimeLimit='PT72H'},
    {param($t) $t.Settings.DisallowStartIfOnBatteries=$true}
)
foreach($mutate in $mutations) {
    $task=New-FakeTask
    & $mutate $task
    $refused=$false
    try { Assert-ManagedTask $task } catch { $refused=$true }
    if(-not $refused) { throw 'Unknown task ownership accepted' }
}
$source=New-FakeTask
$source.Actions[0].Execute='C:\Source\.venv\Scripts\pythonw.exe'
$source.Actions[0].Arguments='"C:\Source\main.py"'
$source.Actions[0].WorkingDirectory='C:\Source'
$source.Triggers=@([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'};Enabled=$true;UserId=$sid;Delay='';StartBoundary='';EndBoundary=''})
if(@(Get-TaskMismatch $source $sid 'C:\Source\.venv\Scripts\pythonw.exe' 'C:\Source\main.py' 'C:\Source').Count) { throw 'Owned source task rejected' }
$source.Actions[0].WorkingDirectory='C:\Other'
if(-not @(Get-TaskMismatch $source $sid 'C:\Source\.venv\Scripts\pythonw.exe' 'C:\Source\main.py' 'C:\Source').Count) { throw 'Unrelated source task accepted' }
$source.Actions[0].WorkingDirectory='C:\Source'
$source.Principal.RunLevel='Limited'
$source.Triggers[0].Delay='PT15S'
$script:SourceRoot='C:\Source'
$script:FakeSourceTask=$source
function Read-Task { return $script:FakeSourceTask }
function Test-Path {
    param($LiteralPath,$PathType)
    if($LiteralPath -like '*.lnk') { return $script:FakeShortcutPresent }
    return $false
}
function Test-OwnShortcut { return $true }
foreach($present in @($false,$true)) {
    $script:FakeShortcutPresent=$present
    $refused=$false
    try { $null=Inspect-Source } catch {
        if($_.Exception.Message -notmatch 'Source.*task.*preserved') { throw }
        $refused=$true
    }
    if(-not $refused) { throw 'Unsupported limited source login task was ignored' }
}
'FAKE OWNERSHIP PASS'
'''
        environment = dict(os.environ, ODICTO_MANAGED_HELPER=str(ROOT / "scripts/windows/managed_admin.ps1"))
        result = subprocess.run(["powershell", "-NoProfile", "-Command", program], env=environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE OWNERSHIP PASS", result.stdout)

    def test_task_run_has_stable_per_home_identity_and_no_uac(self):
        home = Path(tempfile.gettempdir()) / "synthetic managed home"
        fake = SimpleNamespace(_system32=lambda name: "system/" + name)
        with patch.object(admin, "_windows_admin", return_value=fake), patch.object(admin.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            admin.start_task(home, Path("synthetic release"))
        self.assertEqual(run.call_args.args[0], ["system/schtasks.exe", "/Run", "/TN", admin.task_name(home)])
        self.assertNotIn("RunAs", str(run.call_args))

    def test_stop_signals_stable_data_root(self):
        fake = SimpleNamespace(_owner_pid=MagicMock(return_value=123), request_quit=MagicMock(return_value=True))
        manager = SimpleNamespace(data=Path("synthetic data"))
        with patch.object(admin, "_windows_admin", return_value=fake):
            self.assertEqual(admin.stop(manager, Path("release")), 0)
        fake.request_quit.assert_called_once_with(manager.data, 15)

    def test_readonly_inspect_uses_stdout_and_no_receipt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = dist.Manager(Path(tmp) / "managed")
            manager.initialize()
            with patch.object(manager, "active", return_value={}), patch.object(manager, "release_path", return_value=Path("release")), patch.object(admin, "_windows_admin", return_value=SimpleNamespace(_powershell_exe=lambda: "fake-powershell")), patch.object(admin.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True,"home": str(manager.home),"result": {"admin": True}}).encode())) as run:
                self.assertEqual(admin.helper(manager, "Inspect", source=Path("source")), {"admin": True})
            self.assertNotIn("-ReceiptPath", run.call_args.args[0])
            self.assertFalse(list(manager.home.glob("admin-receipt-*")))

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell parser")
    def test_powershell_helpers_parse_without_execution(self):
        helper = ROOT / "scripts/windows/managed_admin.ps1"
        wrapper = ROOT / "install-managed.ps1"
        command = "$ErrorActionPreference='Stop'; foreach($p in $args) { $t=$null; $e=$null; $null=[System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e); if($e.Count) { throw ($e | Out-String) } }; 'AST PASS'"
        # The command only parses files. It never invokes any helper statement.
        program = "& { " + command + " } " + " ".join("'" + str(p).replace("'", "''") + "'" for p in (helper, wrapper))
        result = subprocess.run(["powershell", "-NoProfile", "-Command", program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AST PASS", result.stdout)
