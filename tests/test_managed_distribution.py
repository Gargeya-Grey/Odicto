"""Managed installation and updates with synthetic archives and isolated directories."""
from __future__ import annotations

import hashlib
import builtins
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "distribution" / "manager.py"
SPEC = importlib.util.spec_from_file_location("odicto_distribution_tested", MODULE_PATH)
dist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dist
SPEC.loader.exec_module(dist)


def archive_for(tag="v1.2.0", extras=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Odicto/VERSION", tag[1:] + "\n")
        archive.writestr("Odicto/.env.example", "FAKE_SETTING=default\n")
        archive.writestr("Odicto/tools/distribution/capabilities.json", json.dumps(
            {"schema": 1, "stable_data_root": True, "setup_writer_lock": True}))
        archive.writestr("Odicto/tools/distribution/requirements.lock", "fixture\n")
        archive.writestr("Odicto/odicto.py", "# synthetic release\n")
        for name, contents in extras:
            if isinstance(name, str) and "\\" in name:
                raw_name = zipfile.ZipInfo("unmodified")
                raw_name.filename = name
                name = raw_name
            archive.writestr(name, contents)
    return output.getvalue()


class TestManagedDistribution(unittest.TestCase):
    def setUp(self):
        elevation = patch("tools.distribution.managed_admin.elevated", return_value=False)
        elevation.start()
        self.addCleanup(elevation.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-distribution-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.network = {}
        self.requests = []
        self.installed = []
        self.manager = dist.Manager(self.root / "managed", fetch=self.fetch, installer=self.install)
        self.manager.initialize()
        self.release = self.add_release()

    def add_release(self, tag="v1.2.0", archive=None):
        archive = archive or archive_for(tag)
        name = f"Odicto-{tag}.zip"
        prefix = f"https://github.com/{dist.REPOSITORY}/releases/download/{tag}/"
        release = dist.Release(tag, prefix + name, prefix + name + ".sha256")
        self.network[release.archive_url] = archive
        self.network[release.checksum_url] = f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode()
        self.network[dist.RELEASE_API] = json.dumps({"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": name, "browser_download_url": release.archive_url},
                       {"name": name + ".sha256", "browser_download_url": release.checksum_url}]}).encode()
        return release

    def fetch(self, url, maximum):
        self.requests.append(url)
        value = self.network[url]
        if len(value) > maximum:
            raise dist.DistributionError("fixture exceeds limit")
        return value

    def install(self, directory, environment):
        self.assertEqual(directory.parent, self.manager.releases)
        self.assertTrue(directory.exists())
        self.assertFalse((directory / ".odicto-ready.json").exists())
        self.assertEqual(environment["ODICTO_DATA_ROOT"], str(self.manager.data))
        self.installed.append(directory)
        (directory / ".venv").mkdir()

    def initial(self):
        record = self.manager.stage(self.release)
        self.manager.activate(record)
        return record

    def test_dependency_commands_handoff_from_bootstrap_python_to_active_release(self):
        record = self.initial()
        directory = self.manager.release_path(record)
        for args in (["migrate", "--source", str(self.root / "source"), "--check"], ["admin", "on"]):
            with self.subTest(command=args[0]), patch.object(dist, "Manager", return_value=self.manager), patch.object(
                self.manager, "support", side_effect=AssertionError("bootstrap interpreter touched native dependencies")
            ), patch.object(self.manager, "configure_admin", side_effect=AssertionError("bootstrap administrator change")), patch.object(
                dist.subprocess, "run", return_value=SimpleNamespace(returncode=23)
            ) as run:
                self.assertEqual(dist.main(["--home", str(self.manager.home), *args]), 23)
            self.assertEqual(run.call_args.args[0], [str(self.manager._python(directory)),
                str(directory / "tools/distribution/manager.py"), "--home", str(self.manager.home), *args])
            self.assertEqual(run.call_args.kwargs["env"]["ODICTO_DATA_ROOT"], str(self.manager.data))

    def test_dependency_commands_route_from_python_without_site_packages(self):
        self.initial()
        program = r'''
import importlib.util, json, sys
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('cold_manager',sys.argv[1])
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
calls=[]
module.subprocess.run=lambda command,**kwargs: (calls.append(command) or SimpleNamespace(returncode=0))
for command in (['migrate','--source',sys.argv[3],'--check'],['admin','on']):
    assert module.main(['--home',sys.argv[2],*command])==0
assert len(calls)==2
assert all('/.venv/' in str(command[0]).replace('\\','/') for command in calls)
assert 'psutil' not in sys.modules
print('COLD HANDOFF PASS')
'''
        done = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(MODULE_PATH),
                               str(self.manager.home), str(self.root / "source")],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("COLD HANDOFF PASS", done.stdout)

    def test_accepts_stable_metadata_but_rejects_bad_tags_drafts_and_external_assets(self):
        self.assertEqual(dist.latest_release(self.fetch), self.release)
        metadata = json.loads(self.network[dist.RELEASE_API])
        for invalid in ("1.2.0", "v01.2.0", "v1.2.0-rc1", None, "v1.2.0/../../x"):
            with self.subTest(tag=invalid), self.assertRaises(dist.DistributionError):
                dist.Release.from_metadata({**metadata, "tag_name": invalid})
        for key in ("draft", "prerelease"):
            with self.subTest(key=key), self.assertRaises(dist.DistributionError):
                dist.Release.from_metadata({**metadata, key: True})
        metadata["assets"][0]["browser_download_url"] = "https://evil.invalid/archive.zip"
        with self.assertRaises(dist.DistributionError):
            dist.Release.from_metadata(metadata)

    def test_duplicate_release_assets_are_rejected(self):
        metadata = json.loads(self.network[dist.RELEASE_API])
        metadata["assets"].append(metadata["assets"][0])
        with self.assertRaises(dist.DistributionError):
            dist.Release.from_metadata(metadata)

    def test_direct_release_records_cannot_select_another_trusted_github_repository(self):
        with self.assertRaises(dist.DistributionError):
            dist.Release("v1.2.0", "https://github.com/other/repo/releases/download/v1.2.0/Odicto-v1.2.0.zip",
                         self.release.checksum_url)

    def test_download_redirects_reject_untrusted_hosts_before_requesting_them(self):
        redirect = dist._TrustedRedirect()
        for url in ("http://github.com/a", "https://evil.invalid/a", "https://user@github.com/a",
                    "https://github.com:444/a", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(dist.DistributionError):
                redirect.redirect_request(None, None, 302, "redirect", {}, url)

    def test_bad_checksum_does_not_create_or_install_a_release(self):
        self.network[self.release.checksum_url] = ("0" * 64 + "  Odicto-v1.2.0.zip").encode()
        with self.assertRaises(dist.DistributionError):
            self.manager.stage(self.release)
        self.assertEqual(list(self.manager.releases.iterdir()), [])
        self.assertEqual(self.installed, [])

    def test_checksum_filename_must_match_the_release(self):
        self.network[self.release.checksum_url] = ("0" * 64 + "  other.zip").encode()
        with self.assertRaises(dist.DistributionError):
            self.manager.stage(self.release)

    def test_archive_paths_links_runtime_files_and_case_collisions_are_rejected(self):
        outside = self.root / "escape.txt"
        outside.write_text("keep", encoding="utf-8")
        unsafe = ("Odicto/../escape.txt", "/Odicto/file", "Odicto/C:evil", "Odicto\\file",
                  "Odicto/.env", "Odicto/prompt.txt", "Odicto/.venv/python", "other/file",
                  "Odicto/dictation.lock", "Odicto/.odicto-ready.json", "Odicto/active.json")
        for name in unsafe:
            with self.subTest(name=name), self.assertRaises(dist.DistributionError):
                dist.extract_archive(archive_for(extras=[(name, "bad")]), self.root / "unused")
        link = zipfile.ZipInfo("Odicto/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extras in ([(link, "../escape.txt")], [("Odicto/Foo", "a"), ("Odicto/foo", "b")]):
            with self.assertRaises(dist.DistributionError):
                dist.extract_archive(archive_for(extras=extras), self.root / "unused")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_existing_unmanaged_directories_and_files_are_preserved(self):
        old = self.root / "old"
        old.mkdir()
        private = old / ".env"
        private.write_text("SYNTHETIC=keep", encoding="utf-8")
        with self.assertRaises(dist.DistributionError):
            dist.Manager(old).initialize()
        self.assertEqual(private.read_text(encoding="utf-8"), "SYNTHETIC=keep")
        self.assertEqual(list(old.iterdir()), [private])

    def test_venv_is_built_at_its_final_path_and_pointer_changes_only_after_ready(self):
        record = self.manager.stage(self.release)
        self.assertIsNone(self.manager.active())
        self.assertEqual(self.installed, [self.manager.releases / record["directory"]])
        self.manager.activate(record)
        self.assertEqual(self.manager.active()["directory"], record["directory"])
        self.assertTrue(self.installed[0].is_dir())

    def test_failed_dependency_install_keeps_active_version_and_evidence(self):
        old = self.initial()
        new = self.add_release("v1.3.0")
        self.manager.installer = lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic install failure"))
        with self.assertRaises(RuntimeError):
            self.manager.stage(new)
        self.assertEqual(self.manager.active()["directory"], old["directory"])
        failures = list(self.manager.releases.glob("*/.odicto-stage-failed.json"))
        self.assertEqual(len(failures), 1)
        self.assertFalse((failures[0].parent / ".odicto-ready.json").exists())

    def test_dependency_install_uses_final_venv_and_hash_locked_sync_then_hook_free_checks(self):
        record = self.manager.stage(self.release)
        directory = self.manager.release_path(record)
        with patch.object(dist.shutil, "which", return_value="uv-fixture"), patch.object(
            dist.subprocess, "run", return_value=SimpleNamespace(stdout="Odicto 1.2.0\n")
        ) as run:
            self.manager._install_dependencies(directory, self.manager.environment())
        self.assertEqual(run.call_args_list[0].args[0],
                         ["uv-fixture", "venv", "--python", "3.12", str(directory / ".venv")])
        sync = run.call_args_list[1].args[0]
        self.assertEqual(sync[:3], ["uv-fixture", "pip", "sync"])
        self.assertIn("--require-hashes", sync)
        self.assertEqual(sync[-1], str(directory / "tools" / "distribution" / "requirements.lock"))
        self.assertEqual(run.call_args_list[-1].args[0][-1], "version")
        self.assertEqual(run.call_args_list[2].kwargs["env"]["QT_QPA_PLATFORM"], "offscreen")
        self.assertIn("Config.validate()", run.call_args_list[2].args[0][3])
        self.assertFalse(any("main.py" in argument for call in run.call_args_list for argument in call.args[0]))

    def test_settings_and_unknown_files_survive_update_and_rollback(self):
        old = self.initial()
        settings = self.manager.data / ".env"
        settings.write_text("SYNTHETIC=keep", encoding="utf-8")
        note = self.manager.home / "my-note.txt"
        note.write_text("unknown ownership", encoding="utf-8")
        new = self.manager.stage(self.add_release("v1.3.0"))
        self.manager.activate(new)
        self.manager.initialize_settings(new)
        self.manager.rollback()
        self.assertEqual(self.manager.active()["directory"], old["directory"])
        self.assertEqual(settings.read_text(encoding="utf-8"), "SYNTHETIC=keep")
        self.assertEqual(note.read_text(encoding="utf-8"), "unknown ownership")
        self.assertTrue(self.manager.release_path(new).is_dir())
        self.assertEqual(self.manager.update(force_check=False)["directory"], old["directory"])

    def test_new_install_creates_settings_once_from_the_example(self):
        record = self.initial()
        self.manager.initialize_settings(record)
        path = self.manager.data / ".env"
        self.assertEqual(path.read_text(encoding="utf-8"), "FAKE_SETTING=default\n")
        path.write_text("SYNTHETIC=changed\n", encoding="utf-8")
        self.manager.initialize_settings(record)
        self.assertEqual(path.read_text(encoding="utf-8"), "SYNTHETIC=changed\n")

    def test_owner_setup_and_launch_locks_each_block_activation(self):
        old = self.initial()
        new = self.manager.stage(self.add_release("v1.3.0"))
        for lock in (self.manager.data / "dictation.lock", self.manager.data / "setup.lock", self.manager.home / "launch.lock"):
            with self.subTest(lock=lock.name), dist.file_lock(lock):
                with self.assertRaises(dist.BusyError):
                    self.manager.activate(new)
                self.assertEqual(self.manager.active()["directory"], old["directory"])

    def test_a_staged_update_is_activated_after_owner_exits_without_redownloading(self):
        self.initial()
        self.add_release("v1.3.0")
        with dist.file_lock(self.manager.data / "dictation.lock"):
            with self.assertRaises(dist.BusyError):
                self.manager.update()
        requests = list(self.requests)
        active = self.manager.update(force_check=False)
        self.assertEqual(active["version"], "v1.3.0")
        self.assertEqual(self.requests, requests)

    def test_interruption_before_pointer_replace_leaves_previous_version_active(self):
        old = self.initial()
        new = self.manager.stage(self.add_release("v1.3.0"))
        original = dist.atomic_json
        def interrupted(path, value):
            if path == self.manager.pointer:
                raise OSError("synthetic power failure")
            return original(path, value)
        with patch.object(dist, "atomic_json", side_effect=interrupted), self.assertRaises(OSError):
            self.manager.activate(new)
        self.assertEqual(self.manager.active()["directory"], old["directory"])

    def test_interruption_after_pointer_replace_still_leaves_a_complete_ready_release(self):
        self.initial()
        new = self.manager.stage(self.add_release("v1.3.0"))
        original = dist.atomic_json
        def interrupted(path, value):
            original(path, value)
            if path == self.manager.pointer:
                raise OSError("synthetic power failure after commit")
        with patch.object(dist, "atomic_json", side_effect=interrupted), self.assertRaises(OSError):
            self.manager.activate(new)
        self.assertEqual(self.manager.active()["directory"], new["directory"])
        self.assertEqual(self.manager.rollback()["version"], "v1.2.0")

    def test_invalid_or_not_ready_pointer_never_launches(self):
        old = self.initial()
        (self.manager.release_path(old) / ".odicto-ready.json").unlink()
        with patch.object(dist.subprocess, "run") as command, self.assertRaises(dist.DistributionError):
            self.manager.run(["start"], automatic_updates=False)
        command.assert_not_called()

    def test_admin_mode_install_is_not_silently_repointed(self):
        old = self.initial()
        (self.manager.data / ".odicto-admin-enabled").touch()
        new = self.manager.stage(self.add_release("v1.3.0"))
        with self.assertRaises(dist.DistributionError):
            self.manager.activate(new)
        self.assertEqual(self.manager.active()["directory"], old["directory"])

    def test_network_failure_keeps_existing_version_and_launches_it_with_stable_data_root(self):
        old = self.initial()
        self.manager.fetch = lambda *_: (_ for _ in ()).throw(OSError("offline"))
        with patch.object(dist.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command:
            self.assertEqual(self.manager.run(["start"]), 0)
        self.assertEqual(Path(command.call_args.args[0][1]).parent, self.manager.release_path(old))
        self.assertEqual(command.call_args.kwargs["env"]["ODICTO_DATA_ROOT"], str(self.manager.data))

    def test_stop_command_never_checks_for_updates(self):
        self.initial()
        self.requests.clear()
        with patch.object(dist.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.manager.run(["stop"]), 0)
        self.assertEqual(self.requests, [])

    def test_disabled_automatic_updates_dont_fetch_or_activate_pending_release(self):
        old = self.initial()
        self.manager.stage(self.add_release("v1.3.0"))
        marker = self.manager._read_json(self.manager.marker)
        marker["automatic_updates"] = False
        dist.atomic_json(self.manager.marker, marker)
        self.requests.clear()
        self.assertEqual(self.manager.update(force_check=False)["directory"], old["directory"])
        self.assertEqual(self.requests, [])

    def test_failed_new_start_rolls_back_and_does_not_retry_bad_release_automatically(self):
        old = self.initial()
        self.add_release("v1.3.0")
        with patch.object(dist.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(self.manager.run(["start"]), 1)
        self.assertEqual(self.manager.active()["directory"], old["directory"])
        self.assertEqual(self.manager._read_json(self.manager.marker)["blocked_version"], "v1.3.0")
        self.requests.clear()
        self.assertEqual(self.manager.update(force_check=False)["directory"], old["directory"])
        self.assertEqual(self.requests, [])

    def test_launchers_reject_unknown_overrides_and_generated_loader_compiles(self):
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.manager.install_launchers()
            self.manager.install_launchers()
            compile((self.manager.home / "launcher.py").read_text(encoding="utf-8"), "launcher.py", "exec")
            launcher = self.manager.home / "bin" / "odicto"
            launcher.write_text("owner custom launcher", encoding="utf-8")
            with self.assertRaises(dist.DistributionError):
                self.manager.install_launchers()
            self.assertEqual(launcher.read_text(encoding="utf-8"), "owner custom launcher")

    def test_shell_and_powershell_use_identical_verified_bootstrap_payload(self):
        shell = MODULE_PATH.with_name("bootstrap.sh").read_text(encoding="utf-8")
        powershell = MODULE_PATH.with_name("bootstrap.ps1").read_text(encoding="utf-8")
        shell_python = shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        powershell_python = powershell.split("$bootstrap = @'\n", 1)[1].split("\n'@", 1)[0]
        self.assertEqual(shell_python, powershell_python)
        compile(shell_python, "bootstrap-payload", "exec")

    def test_user_command_exposure_is_idempotent_and_preserves_unrelated_commands(self):
        user_home = self.root / "synthetic-user"
        with patch.object(dist.Path, "home", return_value=user_home), patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.manager.install_launchers()
            command = self.manager.expose_command()
            self.assertEqual(command, self.manager.expose_command())
            command.unlink()
            command.write_text("unrelated command", encoding="utf-8")
            with self.assertRaises(dist.DistributionError):
                self.manager.expose_command()
            self.assertEqual(command.read_text(encoding="utf-8"), "unrelated command")

    def test_verified_bootstrap_registers_module_then_calls_only_install(self):
        shell = MODULE_PATH.with_name("bootstrap.sh").read_text(encoding="utf-8")
        payload = shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        source = ("from __future__ import annotations\nfrom dataclasses import dataclass\n"
                  "import builtins\n@dataclass\nclass Fixture:\n    value: int = 3\n"
                  "def main(args):\n    builtins._odicto_bootstrap_fixture = args\n    return 0\n")
        self.add_release(archive=archive_for(extras=[("Odicto/tools/distribution/manager.py", source)]))
        opener = SimpleNamespace(open=lambda request, timeout: io.BytesIO(self.network[request.full_url]))
        with patch.object(dist.urllib.request, "build_opener", return_value=opener), patch.object(
            sys, "argv", ["bootstrap", "--home", "synthetic-home"]
        ), patch.object(builtins, "_odicto_bootstrap_fixture", None, create=True), patch.dict(sys.modules):
            with self.assertRaises(SystemExit) as stopped:
                exec(compile(payload, "bootstrap-test", "exec"), {})
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(builtins._odicto_bootstrap_fixture, ["--home", "synthetic-home", "install"])

    def test_bootstrap_rejects_hash_mismatch_before_executing_archive_code(self):
        shell = MODULE_PATH.with_name("bootstrap.sh").read_text(encoding="utf-8")
        payload = shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        self.add_release(archive=archive_for(extras=[("Odicto/tools/distribution/manager.py",
                                                    "raise AssertionError('archive executed')")]))
        self.network[self.release.checksum_url] = ("0" * 64 + "  Odicto-v1.2.0.zip").encode()
        opener = SimpleNamespace(open=lambda request, timeout: io.BytesIO(self.network[request.full_url]))
        with patch.object(dist.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(SystemExit) as stopped:
                exec(compile(payload, "bootstrap-test", "exec"), {})
            self.assertEqual(stopped.exception.code, "Release checksum mismatch")

    def test_generated_loader_routes_app_command_to_active_manager_in_a_real_process(self):
        self.release = self.add_release(archive=archive_for(extras=[(
            "Odicto/tools/distribution/manager.py", "import json,sys; print(json.dumps(sys.argv[1:]))\n")]))
        self.initial()
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.manager.install_launchers()
        result = subprocess.run([sys.executable, "-I", str(self.manager.home / "launcher.py"), "autostart"],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), ["--home", str(self.manager.home), "run", "autostart"])

    def test_generated_loader_rejects_pointer_traversal_before_loading_code(self):
        self.initial()
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.manager.install_launchers()
        record = self.manager.active()
        record["directory"] = "../../outside"
        dist.atomic_json(self.manager.pointer, record)
        result = subprocess.run([sys.executable, "-I", str(self.manager.home / "launcher.py"), "version"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active release path is invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
