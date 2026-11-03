"""Managed installation and updates with synthetic archives and isolated directories."""
from __future__ import annotations

import hashlib
import builtins
import importlib.util
import io
import json
from pathlib import Path
import stat
import shutil
import sys
import re
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

    def test_dependency_binding_rejects_an_incompatible_interpreter_before_dispatch(self):
        record = self.initial()
        directory = self.manager.release_path(record)
        (directory / ".venv/pyvenv.cfg").write_text(f"version_info = {sys.version_info.major + 1}.0\n")
        before = list(sys.path)
        with patch.object(dist, "Manager", return_value=self.manager), patch.object(self.manager, "run") as run:
            self.assertEqual(dist.main(["--home", str(self.manager.home), "run", "stop"]), 1)
        run.assert_not_called()
        self.assertEqual(sys.path, before)

    def test_cold_bootstrap_repairs_admin_enabled_installation_from_verified_release(self):
        import os
        self.initial()
        helper = MODULE_PATH.with_name("managed_admin.py").read_text(encoding="utf-8")
        newer = self.manager.stage(self.add_release("v1.3.0", archive=archive_for("v1.3.0", extras=[
            ("Odicto/tools/distribution/managed_admin.py", helper)])))
        directory = self.manager.release_path(newer)
        capabilities = directory / "tools/distribution/capabilities.json"
        value = json.loads(capabilities.read_text())
        value["managed_admin_mode"] = True
        capabilities.write_text(json.dumps(value))
        identity = hashlib.sha256(os.path.normcase(str(self.manager.home)).encode()).hexdigest()[:12]
        dist.atomic_json(self.manager.data / ".odicto-admin-enabled", {"schema": 1,
            "home": str(self.manager.home), "task": "Odicto-managed-" + identity})
        program = r'''
import json, sys, types
from pathlib import Path
from types import SimpleNamespace
module = types.ModuleType('odicto_verified_bootstrap')
module.__file__ = '<verified-release-manager>'
sys.modules[module.__name__] = module
exec(compile(Path(sys.argv[1]).read_bytes(), module.__file__, 'exec'), module.__dict__)
module.sys = SimpleNamespace(platform='win32', stderr=sys.stderr, path=sys.path)
def unexpected(*args, **kwargs):
    raise AssertionError('Repair must use the staged release without network, native dependencies or lifecycle actions')
module.latest_release = unexpected
module.Manager.bind_dependencies = unexpected
module.subprocess.run = unexpected
module.subprocess.Popen = unexpected
module.shutil.which = lambda name: 'fixture-uv'
module.Manager.expose_command = lambda self: self.home / 'bin/odicto.cmd'
assert module.main(['--home', sys.argv[2], 'install']) == 0
home = Path(sys.argv[2])
active = json.loads((home / 'active.json').read_text())
assert active['version'] == 'v1.3.0'
assert (home / 'bin/odicto.cmd').is_file()
assert Path(sys.modules['tools.distribution.managed_admin'].__file__).resolve() == (
    home / 'releases' / active['directory'] / 'tools/distribution/managed_admin.py').resolve()
print('COLD ADMIN REPAIR PASS')
'''
        done = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(MODULE_PATH),
                               str(self.manager.home)], cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("COLD ADMIN REPAIR PASS", done.stdout)

    def test_missing_support_import_returns_a_clean_cli_error(self):
        with patch.object(dist, "Manager", return_value=self.manager), patch.object(
            self.manager, "update", side_effect=ImportError("fixture missing support")
        ), patch.object(dist.sys, "stderr", new_callable=io.StringIO) as error:
            self.assertEqual(dist.main(["--home", str(self.manager.home), "install"]), 1)
        self.assertIn("Odicto: fixture missing support", error.getvalue())

    def test_updater_can_rollback_without_active_application_dependencies(self):
        original = self.initial()
        newer = self.manager.stage(self.add_release("v1.3.0"))
        self.manager.activate(newer)
        with patch.object(dist, "Manager", return_value=self.manager), patch.object(
            self.manager, "bind_dependencies", side_effect=AssertionError("Recovery must not require the broken app dependencies")
        ):
            self.assertEqual(dist.main(["--home", str(self.manager.home), "rollback"]), 0)
        self.assertEqual(self.manager.active()["directory"], original["directory"])

    def test_dependency_commands_route_from_python_without_site_packages(self):
        record = self.initial()
        directory = self.manager.release_path(record)
        environment = directory / ".venv"
        (environment / "pyvenv.cfg").write_text(f"version_info = {sys.version_info.major}.{sys.version_info.minor}\n")
        packages = environment / ("Lib/site-packages" if sys.platform == "win32" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        packages.mkdir(parents=True)
        (packages / "psutil.py").write_text("MARKER = 'verified-active-dependency'\n")
        program = r'''
import importlib.util, json, os, sys
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('cold_manager',sys.argv[1])
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
sys.path.insert(0,str(Path(sys.argv[1]).parents[2]))
from tools.distribution import migration
assert 'psutil' not in sys.modules
calls=[]; supervisor=os.getpid()
def verified():
    import psutil
    assert psutil.MARKER=='verified-active-dependency'
    assert os.getpid()==supervisor
    calls.append(True)
    return 0
def runtime(self, arguments, automatic_updates):
    assert arguments in (['stop'],['restart']) and not automatic_updates
    return verified()
def unexpected(*args,**kwargs):
    raise AssertionError('An extra manager process would lose supervisor identity')
module.subprocess.run=unexpected
module.Manager.run=runtime
module.Manager.configure_admin=lambda self,enabled: verified()
module.Manager.admin_task=lambda self: verified()
module.Manager.support=lambda self: SimpleNamespace(MigrationAdapter=lambda: None)
migration.migrate=lambda *args,**kwargs: {'state':'synthetic'} if verified()==0 else None
for command in (['migrate','--source',sys.argv[3],'--check'],['admin','on'],['admin-task'],['run','--no-auto-updates','stop'],['run','--no-auto-updates','restart']):
    assert module.main(['--home',sys.argv[2],*command])==0
assert len(calls)==5
print('COLD DEPENDENCY PASS')
'''
        done = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(MODULE_PATH),
                               str(self.manager.home), str(self.root / "source")],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("COLD DEPENDENCY PASS", done.stdout)

    def test_bootstrap_imports_real_native_dependencies_in_the_same_process(self):
        import dotenv
        import psutil
        record = self.initial()
        environment = self.manager.release_path(record) / ".venv"
        (environment / "pyvenv.cfg").write_text(f"version_info = {sys.version_info.major}.{sys.version_info.minor}\n")
        packages = environment / ("Lib/site-packages" if sys.platform == "win32" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        packages.mkdir(parents=True)
        for package in (dotenv, psutil):
            shutil.copytree(Path(package.__file__).parent, packages / package.__name__)
        program = r'''
import importlib.util, io, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('native_bound_manager',sys.argv[1])
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
manager=module.Manager(Path(sys.argv[2]))
before=os.getpid()
manager.bind_dependencies()
import psutil
from dotenv.parser import parse_stream
assert psutil.Process(before).pid==before==os.getpid()
assert next(parse_stream(io.StringIO("'SYSTEM_PROMPT_FILE'=custom.txt\n"))).value=='custom.txt'
assert str(Path(psutil.__file__).resolve()).startswith(sys.argv[3]+os.sep)
print('NATIVE DEPENDENCY PASS')
'''
        done = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(MODULE_PATH),
                               str(self.manager.home), str(packages.resolve())],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("NATIVE DEPENDENCY PASS", done.stdout)

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


def _windows_dacls(*paths):
    """The token user, then each path's DACL in SDDL, read by .NET (Access section only)."""
    script = ("$ErrorActionPreference='Stop'; [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
              "foreach($p in $args) { if(Test-Path -LiteralPath $p -PathType Container) "
              "{ [IO.Directory]::GetAccessControl($p,'Access').GetSecurityDescriptorSddlForm('Access') } "
              "else { [IO.File]::GetAccessControl($p,'Access').GetSecurityDescriptorSddlForm('Access') } }")
    program = "& { " + script + " } " + " ".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    # Popen, not run: a caller may replace subprocess.run with a fake while this reads the real ACL.
    process = subprocess.Popen(["powershell", "-NoProfile", "-Command", program],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output, error = process.communicate(timeout=30)
    if process.returncode:
        raise AssertionError(error)
    lines = output.split()
    if len(lines) != len(paths) + 1:
        raise AssertionError(output + error)
    user = lines[0]
    return [user] + [_user_as_full_sid(user, dacl) for dacl in lines[1:]]


def _user_as_full_sid(user, dacl):
    """SDDL may name the token user by an alias: the built-in Administrator (RID 500, as on GitHub's
    Windows runners) is "LA". The product canonicalizes aliases; write the user's as its full SID."""
    return re.sub(r";([A-Z]{2})\)", lambda match: ";%s)" % user
                  if _full_sid(match.group(1)) == user else match.group(0), dacl)


def _full_sid(alias):
    """The S-1-... form of an SDDL SID alias such as LA or BA, or the alias when Windows has none."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    binary, text = ctypes.c_void_p(), ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(alias, ctypes.byref(binary)):
        return alias
    try:
        if not advapi.ConvertSidToStringSidW(binary, ctypes.byref(text)):
            return alias
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(binary)


class TestPrivateDirectoryPolicy(unittest.TestCase):
    """Pure DACL policy: add one explicit inheritable grant for the token user, relax nothing."""

    USER = "S-1-5-21-111-222-333-1001"

    def test_python_private_dacl_gains_only_the_token_user_grant(self):
        python_mode_700 = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
        self.assertEqual(dist._with_token_user(python_mode_700, self.USER),
                         "D:P(A;OICI;FA;;;S-1-5-21-111-222-333-1001)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)")

    def test_existing_effective_grant_is_left_unchanged(self):
        for dacl in ("D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-111-222-333-1001)",
                     "D:AI(A;OICIID;FA;;;S-1-5-21-111-222-333-1001)(A;OICIID;FA;;;SY)",
                     "D:P(A;OICI;0x1f01ff;;;S-1-5-21-111-222-333-1001)",
                     "D:NO_ACCESS_CONTROL"):
            with self.subTest(dacl=dacl):
                self.assertIsNone(dist._with_token_user(dacl, self.USER))
        self.assertIsNone(dist._with_token_user("D:P(A;OICI;FA;;;SY)", "S-1-5-18",
                                                canonical=lambda sid: {"SY": "S-1-5-18"}.get(sid, sid)))

    def test_partial_and_inherit_only_grants_do_not_count_and_every_entry_is_kept(self):
        user = self.USER
        dacl = "D:P(A;OICIIO;FA;;;%s)(A;OICI;FR;;;%s)(A;OICI;0x1200a9;;;S-1-5-21-9-9-9-500)" % (user, user)
        self.assertEqual(dist._with_token_user(dacl, user),
                         "D:P(A;OICI;FA;;;%s)(A;OICIIO;FA;;;%s)(A;OICI;FR;;;%s)"
                         "(A;OICI;0x1200a9;;;S-1-5-21-9-9-9-500)" % (user, user, user))

    def test_grant_goes_after_explicit_denies_and_before_inherited_entries(self):
        user = self.USER
        self.assertEqual(dist._with_token_user("D:P(D;OICI;FR;;;AN)(A;OICI;FA;;;SY)", user),
                         "D:P(D;OICI;FR;;;AN)(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)" % user)
        # An inherited deny first: the explicit grant must precede it, or .NET rejects the list.
        self.assertEqual(dist._with_token_user("D:AI(D;OICIID;FR;;;AN)(A;OICIID;FA;;;SY)", user),
                         "D:AI(A;OICI;FA;;;%s)(D;OICIID;FR;;;AN)(A;OICIID;FA;;;SY)" % user)
        self.assertEqual(dist._with_token_user("D:P(OD;CI;0x4;00000000-0000-0000-0000-000000000001;;AN)", user),
                         "D:P(OD;CI;0x4;00000000-0000-0000-0000-000000000001;;AN)(A;OICI;FA;;;%s)" % user)

    def test_existing_grant_is_not_claimed_as_effective_access_past_denies(self):
        # Deny entries are not modelled (CREATOR OWNER, logon and deny-only groups). The policy only
        # adds the explicit grant; a reader that is still denied reports access denied.
        dacl = "D:P(D;OICI;FR;;;%s)(A;OICI;FA;;;%s)" % (self.USER, self.USER)
        self.assertIsNone(dist._with_token_user(dacl, self.USER))

    def test_unreadable_metadata_names_access_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admin-receipt-synthetic.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=PermissionError(13, "Access is denied")):
                with self.assertRaises(dist.DistributionError) as raised:
                    dist.Manager._read_json(path)
        self.assertIn("access denied", str(raised.exception))
        self.assertIn("admin-receipt-synthetic.json", str(raised.exception))

    def test_unrecognized_dacl_text_fails_closed(self):
        for dacl in ("O:BAD:P(A;OICI;FA;;;SY)", "D:P(A;OICI;FA;;;SY", "D:P(A;OICI;FA;;;SY)junk",
                     "S:(AU;SA;FA;;;WD)"):
            with self.subTest(dacl=dacl), self.assertRaises(dist.DistributionError):
                dist._with_token_user(dacl, self.USER)

    def test_posix_private_directory_keeps_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "private"
            with patch.object(dist, "os", SimpleNamespace(name="posix")), patch.object(
                dist, "_grant_token_user", side_effect=AssertionError("POSIX has no Windows access list")
            ):
                path = dist.private_directory(target)
            self.assertEqual(path, target)
            self.assertTrue(path.is_dir())
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)


@unittest.skipUnless(sys.platform == "win32", "Windows DACLs and the elevated helper's PowerShell")
class TestWindowsPrivateDirectoryAcl(unittest.TestCase):
    """A file an elevated process writes is owned by Administrators. The same account must still
    read it through an explicit inherited grant, never through ownership or a wider principal."""

    @staticmethod
    def grant(user):
        return "(A;OICI;FA;;;%s)" % user

    def assert_private(self, user, dacl):
        import re
        self.assertIn(self.grant(user), dacl)
        if dacl.startswith("D:P"):
            sids = set(re.findall(r";([^;()]+)\)", dacl))
            self.assertLessEqual(sids, {"SY", "BA", "OW", user}, dacl)

    def test_managed_and_release_directories_keep_elevated_files_readable(self):
        fixture = TestManagedDistribution("test_missing_support_import_returns_a_clean_cli_error")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.initial()
        manager = fixture.manager
        written = manager.data / "elevated-owner-fixture.json"
        dist.atomic_json(written, {"schema": 1})
        user, *dacls = _windows_dacls(manager.home, manager.data, manager.releases,
                                      manager.release_path(record), written)
        for dacl in dacls[:4]:
            self.assert_private(user, dacl)
        self.assertIn("(A;ID;FA;;;%s)" % user, dacls[4])

    def assert_private_with_foreign_deny(self, user, dacl):
        self.assertIn(self.grant(user), dacl)
        self.assertIn(";AN)", dacl)

    def test_existing_directory_with_an_inherited_deny_stays_canonical(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "parent"
            parent.mkdir()
            subprocess.run(["icacls", str(parent), "/deny", "*S-1-5-7:(OI)(CI)(AD)"], check=True, capture_output=True)
            child = parent / "child"
            os.mkdir(child)  # inherits the deny first, then the allows
            dist.private_directory(child, exist_ok=True)
            user, dacl = _windows_dacls(child)
            canonical = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[IO.Directory]::GetAccessControl($env:P,'Access').AreAccessRulesCanonical"],
                env=dict(os.environ, P=str(child)), capture_output=True, text=True, timeout=30)
        self.assertIn(self.grant(user), dacl)
        self.assertLess(dacl.index(self.grant(user)), dacl.index("(D;OICIID;"), dacl)
        self.assertEqual(canonical.stdout.strip(), "True", canonical.stderr + dacl)

    def test_new_private_directory_is_protected_atomically_under_a_permissive_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "shared"
            parent.mkdir()
            subprocess.run(["icacls", str(parent), "/grant", "*S-1-5-32-545:(OI)(CI)(M)"],
                           check=True, capture_output=True)
            user, dacl = _windows_dacls(dist.private_directory(parent / "private"))
        self.assertTrue(dacl.startswith("D:P"), dacl)
        self.assert_private(user, dacl)
        self.assertNotIn(";BU)", dacl)

    def test_existing_private_directory_gains_the_grant_once_and_keeps_entries(self):
        import os
        import re
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "existing"
            os.mkdir(path, 0o700)
            user, before = _windows_dacls(path)
            dist.private_directory(path, exist_ok=True)
            _, after = _windows_dacls(path)
            dist.private_directory(path, exist_ok=True)
            _, again = _windows_dacls(path)
        self.assert_private(user, after)
        self.assertEqual(after, again)
        for ace in re.findall(r"\([^()]*\)", before):
            self.assertIn(ace, after)

    def test_migration_transaction_directories_grant_the_token_user(self):
        from tests.test_managed_migration import TestManagedMigration
        fixture = TestManagedMigration("test_check_is_read_only_and_marks_lease_guarantee_as_advisory")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        recovery = Path(fixture.migrate()["recovery"])
        user, *dacls = _windows_dacls(recovery.parent, recovery, recovery / "source", recovery / "destination")
        for dacl in dacls:
            self.assert_private(user, dacl)

    def test_source_admin_receipt_directory_lets_this_account_read_an_elevated_receipt(self):
        import os
        import windows_admin
        seen = []

        def fake_run(command, **kwargs):
            receipt = Path(command[command.index("-ReceiptPath") + 1])
            seen.extend(_windows_dacls(receipt.parent))
            receipt.write_text(json.dumps({"ok": False, "steps": ["elevated"], "error": "synthetic refusal"}))
            return SimpleNamespace(returncode=1)

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ), patch.object(
            windows_admin, "_is_windows", return_value=True
        ), patch.object(windows_admin.subprocess, "run", side_effect=fake_run):
            os.environ.pop("ODICTO_MANAGED_HOME", None)
            root = Path(temporary)
            helper = root.joinpath(*windows_admin.HELPER_PARTS)
            helper.parent.mkdir(parents=True)
            helper.write_text("# never executed")
            ok, message = windows_admin.set_admin_mode(root, False)
        self.assertFalse(ok)
        self.assertIn("synthetic refusal", message)
        user, dacl = seen
        self.assertIsNone(dist._with_token_user(dacl, user), dacl)

    def test_source_admin_receipt_directory_never_removes_a_folder_it_did_not_make(self):
        import os
        import windows_admin
        with tempfile.TemporaryDirectory() as temporary:
            taken = Path(temporary) / ("odicto-admin-" + "ab" * 16)
            taken.mkdir()
            (taken / "other-process.txt").write_text("kept", encoding="utf-8")
            with patch.object(tempfile, "tempdir", temporary), patch.object(os, "urandom", return_value=b"\xab" * 16):
                with self.assertRaises(FileExistsError):
                    windows_admin._receipt_directory(str(MODULE_PATH.parents[2]))
            self.assertEqual((taken / "other-process.txt").read_text(encoding="utf-8"), "kept")

    def test_source_admin_receipt_directory_inherits_nothing_from_a_shared_temp_folder(self):
        import os
        import windows_admin
        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / "shared-temp"
            shared.mkdir()
            # Another account holds an inheritable Modify grant on TEMP (seen on real machines).
            subprocess.run(["icacls", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)(M)"],
                           check=True, capture_output=True)
            with patch.object(tempfile, "tempdir", str(shared)):
                scratch = windows_admin._receipt_directory(str(MODULE_PATH.parents[2]))
            try:
                user, dacl = _windows_dacls(Path(scratch))
            finally:
                os.rmdir(scratch)
        self.assertTrue(dacl.startswith("D:P"), dacl)
        self.assert_private(user, dacl)
        self.assertNotIn("S-1-5-32-545", dacl)
        self.assertNotIn(";BU)", dacl)

    def test_elevated_helper_grants_only_the_same_account_and_preserves_entries(self):
        import os
        import re
        import _winapi
        program = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:ODICTO_MANAGED_HELPER,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw ($errors | Out-String) }
$wanted=@('Assert-SameAccount','Grant-SameAccount')
$defs=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $n.Name},$true))
if($defs.Count -ne $wanted.Count) { throw 'Missing same-account policy' }
foreach($definition in $defs) { Invoke-Expression $definition.Extent.Text }
$user=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$refused=$false
try { Assert-SameAccount 'S-1-5-21-999-999-999-1001' $user } catch { $refused=$true }
if(-not $refused) { throw 'A different approving account was accepted' }
Assert-SameAccount '' $user
Assert-SameAccount $user $user
$linked=$false
try { Grant-SameAccount $env:ODICTO_ACL_LINK $user } catch { $linked=$true }
if(-not $linked) { throw 'A linked directory was granted' }
Grant-SameAccount $env:ODICTO_ACL_FOREIGN_DENIED $user
"FOREIGN=" + [IO.Directory]::GetAccessControl($env:ODICTO_ACL_FOREIGN_DENIED,'Access').GetSecurityDescriptorSddlForm('Access')
$target=$env:ODICTO_ACL_FIXTURE
function Read-Dacl { [IO.Directory]::GetAccessControl($target,'Access').GetSecurityDescriptorSddlForm('Access') }
$before=Read-Dacl
Grant-SameAccount $target $user
$after=Read-Dacl
Grant-SameAccount $target $user
$again=Read-Dacl
"USER=$user"; "BEFORE=$before"; "AFTER=$after"; "AGAIN=$again"
'''
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "owned"
            os.mkdir(target, 0o700)
            link = Path(temporary) / "linked"
            _winapi.CreateJunction(str(target), str(link))
            foreign_denied = Path(temporary) / "foreign"
            os.mkdir(foreign_denied, 0o700)
            subprocess.run(["icacls", str(foreign_denied), "/deny", "*S-1-5-7:(OI)(CI)(AD)"],
                           check=True, capture_output=True)
            environment = dict(os.environ, ODICTO_ACL_FIXTURE=str(target), ODICTO_ACL_LINK=str(link),
                               ODICTO_ACL_FOREIGN_DENIED=str(foreign_denied),
                               ODICTO_MANAGED_HELPER=str(MODULE_PATH.parents[2] / "scripts/windows/managed_admin.ps1"))
            try:
                result = subprocess.run(["powershell", "-NoProfile", "-Command", program], env=environment,
                                        capture_output=True, text=True, timeout=30)
            finally:
                os.rmdir(link)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.split() if "=" in line)
        values = {key: _user_as_full_sid(values["USER"], value) for key, value in values.items()}
        self.assert_private(values["USER"], values["AFTER"])
        self.assert_private_with_foreign_deny(values["USER"], values["FOREIGN"])
        self.assertEqual(values["AFTER"], values["AGAIN"])
        for ace in re.findall(r"\([^()]*\)", values["BEFORE"]):
            self.assertIn(ace, values["AFTER"])


if __name__ == "__main__":
    unittest.main()
