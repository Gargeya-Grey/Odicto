"""odicto uninstall: what it removes, what it keeps, and when it refuses. Temporary homes only.

Nothing here stops a real Odicto, touches the real Start Menu, sign-in entries, menu or
`~/.local/bin`: every outside location is a folder under the test's own root.
"""
from contextlib import ExitStack
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import managed_autostart

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dist = _load("odicto_distribution_uninstall_tested", ROOT / "tools" / "distribution" / "manager.py")


def snapshot(root: Path) -> dict:
    """Every file and folder under root with its bytes; a link is listed, never followed."""
    found = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = "link"
        elif path.is_dir():
            found[relative] = "dir"
        else:
            found[relative] = path.read_bytes()
    return found


class Installed(unittest.TestCase):
    """An installed home plus every place outside it that an install writes to, all temporary."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.user = self.root / "user"
        self.user.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("not Odicto's", encoding="ascii")
        self.menu = self.user / "share" / "applications"
        self.commands = self.user / ".local" / "bin"
        self.startup = self.user / "startup" / "odicto-managed"
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(Path, "home", return_value=self.user))
        self.context.enter_context(patch.object(managed_autostart, "_location", return_value=self.startup))
        self.context.enter_context(patch.object(dist.Manager, "_autostart_module",
                                                staticmethod(lambda: managed_autostart)))
        self.manager = dist.Manager(self.root / "Odicto home 100%")
        self.manager.platform = "linux"
        self.home = self.manager.home
        self.install()

    def install(self):
        home = self.home
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "odicto").write_text("#!/bin/sh\n", encoding="ascii")
        (home / "bin" / "odicto.cmd").write_text("@exit /b 0\r\n", encoding="ascii")
        (home / "launcher.py").write_text("print('launcher')\n", encoding="ascii")
        (home / "releases" / "v1.0.0-aaaaaaaaaaaa-bbbbbbbb" / ".venv" / "lib").mkdir(parents=True)
        (home / "releases" / "v1.0.0-aaaaaaaaaaaa-bbbbbbbb" / ".venv" / "lib" / "big.bin").write_bytes(b"x" * 64)
        (home / "odicto.png").write_bytes(b"png")
        (home / "manager.lock").write_bytes(b"")
        (home / "data" / "sub").mkdir(parents=True)
        (home / "data" / ".env").write_text("GEMINI_API_KEY=secret-key\n", encoding="ascii")
        (home / "data" / "prompt.txt").write_text("my prompt\n", encoding="utf-8")
        (home / "data" / "sub" / "history.json").write_text("{}", encoding="ascii")
        dist.atomic_json(home / ".odicto-managed.json",
                         {"schema": 1, "home": str(home), "automatic_updates": False,
                          "launchers": {"launcher.py": "abc"}, "shortcut_icon": "def"})
        self.manager.install_menu(self.menu)
        self.command = self.manager.expose_command()
        self.assertEqual(self.command.parent, self.commands)
        self.enable_startup()

    def enable_startup(self):
        with ExitStack() as patched:
            patched.enter_context(patch.object(managed_autostart, "sys", SimpleNamespace(platform="linux")))
            patched.enter_context(patch.object(managed_autostart, "os", SimpleNamespace(name="posix", replace=os.replace)))
            managed_autostart.configure(self.home, True)
        self.assertTrue(self.startup.is_file())

    def uninstall(self, **options):
        said = []
        stop = options.pop("stop", lambda: None)
        done = self.manager.uninstall(stop=stop, say=said.append, programs=self.menu,
                                      command_directory=self.commands, **options)
        return done, said

    def outside_snapshot(self):
        return {"menu": snapshot(self.menu), "commands": snapshot(self.commands), "startup": self.startup.read_bytes(),
                "outside": snapshot(self.outside)}


class TestWhatItRemoves(Installed):
    def test_removes_each_item_and_keeps_the_settings(self):
        done, said = self.uninstall()
        self.assertTrue(done)
        self.assertFalse(self.startup.exists())
        self.assertEqual(snapshot(self.menu), {})
        self.assertFalse(self.command.exists() or self.command.is_symlink())
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".odicto-managed.json", "data"])
        self.assertEqual((self.home / "data" / ".env").read_text(encoding="ascii"), "GEMINI_API_KEY=secret-key\n")
        self.assertEqual((self.home / "data" / "prompt.txt").read_text(encoding="utf-8"), "my prompt\n")
        self.assertEqual((self.home / "data" / "sub" / "history.json").read_text(encoding="ascii"), "{}")
        self.assertEqual((self.outside / "keep.txt").read_text(encoding="ascii"), "not Odicto's")
        self.assertIn("Removed Odicto.", said)
        self.assertIn(f"Your settings are kept in {self.home / 'data'}. They hold your keys and your prompt.", said)

    def test_the_marker_that_stays_names_the_home_and_nothing_else(self):
        self.uninstall()
        self.assertEqual(json.loads((self.home / ".odicto-managed.json").read_text(encoding="utf-8")),
                         {"schema": 1, "home": str(self.home), "automatic_updates": False})

    def test_after_keeping_the_settings_a_second_uninstall_and_a_fresh_install_work(self):
        self.uninstall()
        done, said = self.uninstall()
        self.assertTrue(done)
        self.assertEqual((self.home / "data" / ".env").read_text(encoding="ascii"), "GEMINI_API_KEY=secret-key\n")
        self.manager.initialize()  # what a new install does first; the kept settings are accepted
        self.assertTrue((self.home / "data" / ".env").is_file())

    def test_all_removes_the_settings_and_the_home(self):
        done, said = self.uninstall(everything=True)
        self.assertTrue(done)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.startup.exists())
        self.assertEqual(snapshot(self.menu), {})
        self.assertIn("Removed Odicto and its settings.", said)
        self.assertEqual((self.outside / "keep.txt").read_text(encoding="ascii"), "not Odicto's")
        self.assertTrue(self.user.is_dir())

    def test_the_lines_follow_the_order_stop_sign_in_menu_command_home(self):
        _, said = self.uninstall()
        order = ["Odicto is stopped.", "Removed start at sign-in.", "Removed menu entry: Odicto",
                 f"Removed the command {self.command}", "Removed Odicto."]
        positions = [said.index(line) for line in order]
        self.assertEqual(positions, sorted(positions))

    def test_a_link_inside_the_home_is_removed_and_its_target_is_not_followed(self):
        target = self.outside / "target-folder"
        target.mkdir()
        (target / "precious.txt").write_text("precious", encoding="ascii")
        link = self.home / "link-to-outside"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        done, _ = self.uninstall(everything=True)
        self.assertTrue(done)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual((target / "precious.txt").read_text(encoding="ascii"), "precious")

    def test_a_missing_home_says_so_and_is_not_an_error(self):
        shutil.rmtree(self.home)
        done, said = self.uninstall()
        self.assertTrue(done)
        self.assertEqual(said, [f"Odicto is not installed in {self.home}. Nothing was removed."])
        self.assertFalse(self.home.exists())


class TestWhatItKeeps(Installed):
    def test_a_foreign_menu_entry_command_and_startup_entry_stay(self):
        before = {}
        (self.menu / "odicto-setup.desktop").write_bytes(b"[Desktop Entry]\nName=Mine\n")
        before["menu"] = (self.menu / "odicto-setup.desktop").read_bytes()
        self.command.unlink()
        self.command.write_bytes(b"#!/bin/sh\necho the user's own command\n")
        before["command"] = self.command.read_bytes()
        self.startup.write_bytes(b"[Desktop Entry]\nName=Somebody else\n")
        before["startup"] = self.startup.read_bytes()
        done, said = self.uninstall()
        self.assertTrue(done)
        self.assertEqual((self.menu / "odicto-setup.desktop").read_bytes(), before["menu"])
        self.assertEqual(self.command.read_bytes(), before["command"])
        self.assertEqual(self.startup.read_bytes(), before["startup"])
        self.assertIn("Kept Odicto Setup (it is not an Odicto file)", said)
        self.assertIn(f"Kept {self.command} (it is not Odicto's command)", said)
        self.assertTrue(any(line.startswith("Kept the start-at-sign-in entry:") for line in said))
        self.assertFalse((self.menu / "odicto.desktop").exists())
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".odicto-managed.json", "data"])

    def test_a_command_that_leads_to_another_program_stays(self):
        other = self.outside / "other-odicto"
        other.write_text("#!/bin/sh\n", encoding="ascii")
        self.command.unlink()
        if WINDOWS:
            self.command.write_bytes(b'@"C:\\elsewhere\\odicto.cmd" %*\r\n')
        else:
            self.command.symlink_to(other)
        before = self.command.read_bytes() if WINDOWS else os.readlink(self.command)
        self.uninstall()
        self.assertTrue(self.command.exists() or self.command.is_symlink())
        self.assertEqual(self.command.read_bytes() if WINDOWS else os.readlink(self.command), before)
        self.assertEqual(other.read_text(encoding="ascii"), "#!/bin/sh\n")


class TestWhenItRefuses(Installed):
    def refused(self, **options):
        before = snapshot(self.root)
        with self.assertRaises(dist.DistributionError) as caught:
            self.uninstall(**options)
        self.assertEqual(snapshot(self.root), before, "a refused uninstall changed something")
        return str(caught.exception)

    def test_admin_mode_on_stops_with_the_message_and_deletes_nothing(self):
        stopped = []
        with patch.object(self.manager, "admin_enabled", return_value=True):
            message = self.refused(stop=lambda: stopped.append("stop"))
        self.assertIn("turn off admin mode", message)
        self.assertIn("Odicto Setup", message)
        self.assertIn("Nothing was removed", message)
        self.assertEqual(stopped, [])
        self.assertTrue(self.startup.exists())

    def test_admin_mode_that_cannot_be_read_stops_too(self):
        with patch.object(self.manager, "admin_enabled", side_effect=RuntimeError("unknown ownership")):
            before = snapshot(self.root)
            with self.assertRaises(RuntimeError):
                self.uninstall()
            self.assertEqual(snapshot(self.root), before)

    def test_a_folder_that_is_not_a_managed_home_is_refused(self):
        marker = self.home / ".odicto-managed.json"
        marker.unlink()
        self.assertIn("marker is missing or unreadable", self.refused())

    def test_a_marker_that_names_another_folder_is_refused(self):
        dist.atomic_json(self.home / ".odicto-managed.json", {"schema": 1, "home": str(self.root / "elsewhere")})
        self.assertIn("names another folder", self.refused())

    def test_a_marker_with_another_schema_is_refused(self):
        dist.atomic_json(self.home / ".odicto-managed.json", {"schema": 2, "home": str(self.home)})
        self.assertIn("names another folder", self.refused())

    def test_a_marker_that_is_not_json_is_refused(self):
        (self.home / ".odicto-managed.json").write_text("not json", encoding="ascii")
        self.assertIn("marker is missing or unreadable", self.refused())

    def test_a_folder_of_the_users_own_files_is_refused(self):
        folder = self.root / "Documents"
        folder.mkdir()
        (folder / "thesis.txt").write_text("years of work", encoding="ascii")
        self.manager = dist.Manager(folder)
        self.manager.platform = "linux"
        self.assertIn("marker is missing or unreadable", self.refused())
        self.assertEqual((folder / "thesis.txt").read_text(encoding="ascii"), "years of work")

    def test_a_home_that_is_a_link_is_refused_and_its_target_is_untouched(self):
        link = self.root / "link-home"
        try:
            os.symlink(self.home, link, target_is_directory=True)
        except OSError:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.home)], check=True, capture_output=True)
        self.assertTrue(os.path.lexists(link) and not dist._real_folder(link))
        self.manager = dist.Manager(link)
        self.manager.platform = "linux"
        self.assertEqual(self.manager.home, self.home)  # Manager resolves the link: only `requested` still shows it
        self.assertIn("it is a link or not a folder", self.refused(requested=link))

    def test_a_marker_that_is_a_link_is_refused(self):
        marker = self.home / ".odicto-managed.json"
        real = self.outside / "marker.json"
        shutil.move(marker, real)
        try:
            os.symlink(real, marker)
        except OSError:
            # No link right on this account: a folder in the marker's place is the same refusal.
            marker.mkdir()
        self.assertIn("marker is missing or unreadable", self.refused())

    def test_a_folder_that_holds_the_users_own_home_is_refused(self):
        with patch.object(Path, "home", return_value=self.home / "data" / "sub"):
            self.assertIn("holds your own home folder", self.refused())
        with patch.object(Path, "home", return_value=self.home):
            self.assertIn("holds your own home folder", self.refused())

    def test_an_unfinished_migration_is_refused(self):
        (self.home / "migration.json").write_text(json.dumps({"state": "applying"}), encoding="ascii")
        self.assertIn("Migration is unfinished", self.refused())

    def test_a_finished_migration_does_not_block(self):
        (self.home / "migration.json").write_text(json.dumps({"state": "complete"}), encoding="ascii")
        done, _ = self.uninstall()
        self.assertTrue(done)


class TestStoppingFirst(Installed):
    def test_a_running_app_is_stopped_before_anything_is_deleted(self):
        seen = []

        def stop():
            seen.append({"marker": (self.home / ".odicto-managed.json").is_file(),
                         "release": (self.home / "releases").is_dir(), "startup": self.startup.is_file(),
                         "menu": (self.menu / "odicto.desktop").is_file(), "command": os.path.lexists(self.command),
                         "settings": (self.home / "data" / ".env").is_file()})
        self.uninstall(stop=stop)
        self.assertEqual(seen, [dict.fromkeys(("marker", "release", "startup", "menu", "command", "settings"), True)])
        self.assertFalse(self.startup.exists())

    def test_an_app_that_does_not_stop_ends_the_uninstall_before_it_deletes(self):
        def stop():
            raise dist.DistributionError("Odicto did not stop (exit 1). Nothing was removed.")
        before = snapshot(self.root)
        with self.assertRaises(dist.DistributionError):
            self.uninstall(stop=stop)
        self.assertEqual(snapshot(self.root), before)

    def test_the_default_stop_runs_the_managed_stop_without_an_update_check(self):
        with patch.object(self.manager, "active", return_value={"version": "v1.0.0"}), \
                patch.object(self.manager, "run", return_value=0) as run:
            self.manager._stop_for_uninstall()
        run.assert_called_once_with(["stop"], automatic_updates=False)

    def test_a_nonzero_stop_raises(self):
        with patch.object(self.manager, "active", return_value={"version": "v1.0.0"}), \
                patch.object(self.manager, "run", return_value=3):
            with self.assertRaises(dist.DistributionError) as caught:
                self.manager._stop_for_uninstall()
        self.assertIn("did not stop", str(caught.exception))

    def test_with_no_active_release_nothing_runs(self):
        with patch.object(self.manager, "active", return_value=None), \
                patch.object(self.manager, "run") as run:
            self.manager._stop_for_uninstall()
        run.assert_not_called()

    def test_an_unreadable_release_record_warns_and_goes_on(self):
        error = io.StringIO()
        with patch.object(self.manager, "active", side_effect=dist.DistributionError("bad")), \
                patch.object(self.manager, "run") as run, contextlib.redirect_stderr(error):
            self.manager._stop_for_uninstall()
        run.assert_not_called()
        self.assertIn("Make sure it is stopped", error.getvalue())


class TestAutostartRemoval(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / "odicto").write_text("launcher", encoding="utf-8")
        self.target = self.root / "startup" / "entry"
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(managed_autostart, "_location", return_value=self.target))
        self.context.enter_context(patch.object(managed_autostart, "sys", SimpleNamespace(platform="linux")))
        self.context.enter_context(patch.object(managed_autostart, "os", SimpleNamespace(name="posix", replace=os.replace)))

    def test_removes_its_own_entry_once(self):
        managed_autostart.configure(self.home, True)
        self.assertTrue(managed_autostart.remove(self.home))
        self.assertFalse(self.target.exists())
        self.assertFalse(managed_autostart.remove(self.home))

    def test_keeps_an_entry_it_did_not_write(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"theirs")
        with self.assertRaises(RuntimeError):
            managed_autostart.remove(self.home)
        self.assertEqual(self.target.read_bytes(), b"theirs")

    def test_keeps_an_entry_that_changed_after_odicto_wrote_it(self):
        managed_autostart.configure(self.home, True)
        self.target.write_bytes(self.target.read_bytes() + b"X-Edited=1\n")
        with self.assertRaises(RuntimeError):
            managed_autostart.remove(self.home)
        self.assertTrue(self.target.exists())

    def test_no_entry_is_not_an_error(self):
        self.assertFalse(managed_autostart.remove(self.home))


class TestStartMenuRemoval(unittest.TestCase):
    """Windows shortcuts. The contract runs everywhere with a fake PowerShell; the real script runs where
    PowerShell exists, against a temporary Programs folder."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manager = dist.Manager(self.root / "Odicto home 100%")
        self.manager.platform = "win32"
        (self.manager.home / "bin").mkdir(parents=True)
        self.launcher = self.manager.home / "bin" / "odicto.cmd"
        self.launcher.write_text("@exit /b 0\r\n", encoding="ascii")
        self.programs = self.root / "Programs"
        self.folder = self.programs / "Odicto"

    def test_the_script_gets_the_launcher_and_the_folder_through_the_environment(self):
        seen = {}

        def fake(command, **options):
            seen.update(command=command, env=options["env"])
            return subprocess.CompletedProcess(command, 0, "Removed Start Menu item: Odicto\n\n", "")
        with patch.object(dist.subprocess, "run", fake):
            lines = self.manager.remove_shortcuts(self.programs)
        self.assertEqual(lines, ["Removed Start Menu item: Odicto"])
        self.assertEqual(seen["command"][-1], dist.REMOVE_SHORTCUT_SCRIPT)
        self.assertEqual(seen["env"]["ODICTO_LAUNCHER"], str(self.launcher))
        self.assertEqual(seen["env"]["ODICTO_PROGRAMS"], str(self.programs))
        self.assertEqual(json.loads(seen["env"]["ODICTO_SHORTCUTS"]), [list(row) for row in dist.SHORTCUTS])

    def test_a_failed_script_raises_with_its_last_line(self):
        failed = subprocess.CompletedProcess([], 1, "", "first\nthe real reason\n")
        with patch.object(dist.subprocess, "run", return_value=failed):
            with self.assertRaises(dist.DistributionError) as caught:
                self.manager.remove_shortcuts(self.programs)
        self.assertIn("the real reason", str(caught.exception))

    def test_the_script_deletes_only_a_link_to_this_launcher(self):
        script = dist.REMOVE_SHORTCUT_SCRIPT
        self.assertIn("Remove-Item -LiteralPath $path -Force", script)
        self.assertEqual(script.count("Remove-Item"), 2)
        self.assertIn("[IO.Path]::GetFullPath($env:ODICTO_LAUNCHER)", script)
        self.assertIn("Get-ChildItem -LiteralPath $folder -Force | Select-Object -First 1", script)

    def test_real_powershell_removes_only_what_odicto_wrote(self):
        if shutil.which("powershell.exe") is None:
            # No PowerShell: not Windows. The fake-PowerShell tests above hold the contract.
            self.assertFalse(WINDOWS)
            return
        other = self.root / "source checkout" / "start_dictation.bat"
        other.parent.mkdir()
        other.write_text("@exit /b 0\r\n", encoding="ascii")
        self.manager.install_shortcuts(self.programs)
        links = self.folder / "Odicto Setup.lnk"
        links.unlink()
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($env:T_PATH);"
                        "$l.TargetPath = $env:T_TARGET; $l.Save()"],
                       env=dict(os.environ, T_PATH=str(links), T_TARGET=str(other)), check=True, timeout=60)
        lines = self.manager.remove_shortcuts(self.programs)
        self.assertEqual(lines, ["Removed Start Menu item: Odicto",
                                 "Kept Odicto Setup (it runs another program)",
                                 "Removed Start Menu item: Stop Odicto"])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["Odicto Setup.lnk"])
        links.unlink()
        self.manager.install_shortcuts(self.programs)
        self.manager.remove_shortcuts(self.programs)
        self.assertFalse(self.folder.exists(), "an empty Odicto folder is removed")
        self.assertTrue(self.programs.is_dir())


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "Odicto"
        (self.home / "data").mkdir(parents=True)
        (self.home / "data" / ".env").write_text("settings", encoding="ascii")
        (self.home / "bin").mkdir()
        (self.home / "bin" / "odicto").write_text("x", encoding="ascii")
        dist.atomic_json(self.home / ".odicto-managed.json", {"schema": 1, "home": str(self.home)})
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        for name, value in (("_stop_for_uninstall", lambda self: None),
                            ("remove_shortcuts", lambda self, folder=None: [])):
            self.context.enter_context(patch.object(dist.Manager, name, value))
        self.context.enter_context(patch.object(dist.Manager, "_autostart_module", staticmethod(
            lambda: SimpleNamespace(remove=lambda home: False))))
        self.context.enter_context(patch.object(Path, "home", return_value=self.root / "user"))
        self.context.enter_context(patch.object(dist.Manager, "admin_enabled", lambda self: False))

    def run_main(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = dist.main(["--home", str(self.home), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_uninstall_keeps_the_settings_and_prints_their_folder(self):
        code, output, errors = self.run_main("uninstall")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(f"Your settings are kept in {self.home / 'data'}.", output)
        self.assertEqual((self.home / "data" / ".env").read_text(encoding="ascii"), "settings")
        self.assertFalse((self.home / "bin").exists())

    def test_uninstall_all_deletes_the_settings(self):
        code, output, _ = self.run_main("uninstall", "--all")
        self.assertEqual(code, 0)
        self.assertFalse(self.home.exists())
        self.assertIn("Removed Odicto and its settings.", output)

    def test_a_refusal_is_one_line_and_exit_1(self):
        (self.home / ".odicto-managed.json").unlink()
        code, output, errors = self.run_main("uninstall")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("Odicto: "), errors)
        self.assertEqual(errors.count("\n"), 1)
        self.assertTrue((self.home / "data" / ".env").is_file())

    def test_an_unknown_argument_is_refused_before_anything_runs(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dist.main(["--home", str(self.home), "uninstall", "--everything"])
        self.assertTrue((self.home / "bin").exists())

    def test_the_guide_and_the_command_help_name_the_command(self):
        code, output, _ = self.run_main("help")
        self.assertEqual(code, 0)
        self.assertIn("odicto uninstall ", output)
        self.assertIn("odicto uninstall --all", output)
        helper = io.StringIO()
        with contextlib.redirect_stdout(helper), self.assertRaises(SystemExit) as stopped:
            dist.main(["--home", str(self.home), "uninstall", "--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--all", helper.getvalue())
        self.assertIn("Windows admin mode must be off first", " ".join(helper.getvalue().split()))
        self.assertTrue((self.home / "bin").exists())


if __name__ == "__main__":
    unittest.main()
