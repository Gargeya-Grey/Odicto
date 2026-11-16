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
MANAGER = ROOT / "tools" / "distribution" / "manager.py"
RELEASE = "v1.0.0-aaaaaaaaaaaa-bbbbbbbb"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dist = _load("odicto_distribution_uninstall_tested", MANAGER)


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


def make_link(link: Path, target: Path) -> None:
    """A symbolic link, or a junction where this account may not make one."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def remove_link(link: Path) -> None:
    if os.path.lexists(link) and not dist._real_folder(link):
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)


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
        (home / "releases" / RELEASE / ".venv" / "lib").mkdir(parents=True)
        (home / "releases" / RELEASE / ".venv" / "lib" / "big.bin").write_bytes(b"x" * 64)
        (home / "odicto.png").write_bytes(b"png")
        # The lock files that every real install has, so that a refused uninstall can leave the home as it was.
        for name in ("manager.lock", "launch.lock"):
            (home / name).write_bytes(b"")
        (home / "data" / "sub").mkdir(parents=True)
        for name in ("setup.lock", "dictation.lock"):
            (home / "data" / name).write_bytes(b"")
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

    def activate_release(self, previous=False):
        """An active, ready release, so that the real stop has something to run."""
        record = {"version": "v1.0.0", "directory": RELEASE, "archive_sha256": "a" * 64}
        dist.atomic_json(self.home / "releases" / RELEASE / ".odicto-ready.json", record)
        dist.atomic_json(self.home / "active.json", dict(record, previous=record) if previous else record)
        return record

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

    def test_the_remove_entry_goes_with_the_others(self):
        self.assertTrue((self.menu / "odicto-remove.desktop").is_file())
        _, said = self.uninstall()
        self.assertIn("Removed menu entry: Remove Odicto", said)
        self.assertFalse((self.menu / "odicto-remove.desktop").exists())

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
        make_link(link, target)
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

    def test_the_command_that_runs_it_again_goes_last(self):
        calls = []
        real = dist.Manager._delete_children

        def spy(manager, held, relative, **options):
            calls.append((relative, options.get("only") or frozenset()))
            return real(manager, held, relative, **options)

        with patch.object(dist.Manager, "_delete_children", spy):
            self.uninstall()
        retry = next(index for index, call in enumerate(calls) if "bin" in call[1])
        releases = next(index for index, call in enumerate(calls) if call[0] == "releases")
        self.assertLess(0, releases)
        self.assertLess(releases, retry)
        self.assertLess(retry, len(calls) - 1, "the lock files and the notice go after the command")


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
        make_link(link, self.home)
        self.addCleanup(remove_link, link)
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


class TestAHomeThatChangesUnderneath(Installed):
    """The delete goes through the folder that was checked, never through the path again."""

    def test_a_home_swapped_for_a_link_after_the_check_is_never_followed(self):
        moved = self.root / "moved-home"
        real_scandir = os.scandir
        swapped = []

        def scandir(target):
            if not swapped:  # the first listing is the home's own, right after it was checked
                try:
                    os.rename(self.home, moved)
                    make_link(self.home, self.outside)
                    swapped.append(True)
                except OSError:
                    swapped.append(False)
            return real_scandir(target)

        self.addCleanup(remove_link, self.home)
        with patch.object(dist.os, "scandir", side_effect=scandir):
            done, _ = self.uninstall()
        self.assertEqual((self.outside / "keep.txt").read_text(encoding="ascii"), "not Odicto's")
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["keep.txt"])
        # Windows: the folder is held open, so the swap is refused and the uninstall finishes normally.
        # Elsewhere the swap happens, and the delete goes on in the folder that was checked.
        self.assertEqual(swapped, [os.name != "nt"])
        self.assertTrue(done)
        if swapped == [True]:
            self.assertEqual(sorted(path.name for path in moved.iterdir()), [".odicto-managed.json", "data"])

    def test_a_folder_above_the_home_swapped_for_a_link_is_never_followed(self):
        parent = self.root / "parent"
        parent.mkdir()
        shutil.move(str(self.home), str(parent / self.home.name))
        self.manager = dist.Manager(parent / self.home.name)
        self.manager.platform = "linux"
        self.home = self.manager.home
        # The marker names the new place.
        marker = json.loads((self.home / ".odicto-managed.json").read_text(encoding="utf-8"))
        marker["home"] = str(self.home)
        dist.atomic_json(self.home / ".odicto-managed.json", marker)
        decoy = self.outside / self.home.name
        decoy.mkdir()
        (decoy / "precious.txt").write_text("precious", encoding="ascii")
        moved = self.root / "parent-moved"
        real_scandir = os.scandir
        swapped = []

        def scandir(target):
            if not swapped:
                try:
                    os.rename(parent, moved)
                    make_link(parent, self.outside)
                    swapped.append(True)
                except OSError:
                    swapped.append(False)
            return real_scandir(target)

        self.addCleanup(remove_link, parent)
        with patch.object(dist.os, "scandir", side_effect=scandir):
            self.uninstall()
        self.assertEqual((decoy / "precious.txt").read_text(encoding="ascii"), "precious")
        self.assertEqual((self.outside / "keep.txt").read_text(encoding="ascii"), "not Odicto's")
        self.assertEqual(swapped, [os.name != "nt"])


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

    def test_the_default_stop_runs_the_release_stop_while_the_locks_are_held(self):
        self.activate_release()
        calls = []

        def run(command, **options):
            calls.append((command, options))
            # The uninstall holds both lifecycle locks: the managed `run` would be busy here.
            for name in ("manager.lock", "launch.lock"):
                with self.assertRaises(dist.BusyError):
                    with dist.file_lock(self.home / name):
                        pass
            return subprocess.CompletedProcess(command, 0)

        with patch.object(dist.subprocess, "run", run), patch.object(self.manager, "run") as managed:
            done, said = self.run_default_stop()
        managed.assert_not_called()
        self.assertTrue(done)
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(command[1:], [str(self.home / "releases" / RELEASE / "odicto.py"), "stop"])
        self.assertEqual(options["cwd"], self.home / "releases" / RELEASE)
        self.assertIn("Odicto is stopped.", said)

    def run_default_stop(self):
        said = []
        done = self.manager.uninstall(say=said.append, programs=self.menu, command_directory=self.commands)
        return done, said

    def test_a_nonzero_stop_ends_the_uninstall_with_nothing_deleted(self):
        self.activate_release()
        before = snapshot(self.root)
        with patch.object(dist.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)):
            with self.assertRaises(dist.DistributionError) as caught:
                self.run_default_stop()
        self.assertIn("did not stop", str(caught.exception))
        self.assertIn("Nothing was removed", str(caught.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_with_no_active_release_nothing_runs(self):
        with patch.object(dist.subprocess, "run") as ran:
            done, _ = self.run_default_stop()
        ran.assert_not_called()
        self.assertTrue(done)

    def test_an_unreadable_release_record_keeps_everything_and_says_why(self):
        (self.home / "active.json").write_text("not json at all", encoding="ascii")
        before = snapshot(self.root)
        with patch.object(dist.subprocess, "run") as ran:
            with self.assertRaises(dist.DistributionError) as caught:
                self.run_default_stop()
        ran.assert_not_called()
        message = str(caught.exception)
        self.assertIn("cannot tell whether it is running", message)
        self.assertIn("Nothing was removed", message)
        self.assertEqual(snapshot(self.root), before, "an unreadable record must not count as stopped")

    def test_an_unreadable_release_record_is_exit_1_from_the_command(self):
        (self.home / "active.json").write_text("not json at all", encoding="ascii")
        before = snapshot(self.root)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch.object(dist.Manager, "admin_enabled", lambda self: False):
            code = dist.main(["--home", str(self.home), "uninstall"])
        self.assertEqual(code, 1)
        self.assertTrue(errors.getvalue().startswith("Odicto: "))
        self.assertIn("Nothing was removed", errors.getvalue())
        self.assertEqual(snapshot(self.root), before)


class TestTheLifecycleLocks(Installed):
    """manager.lock and launch.lock are held from the stop to the last delete; Setup and the app must be free."""

    def interleave(self):
        """What a start, an update or a rollback finds while the uninstall deletes."""
        found = {}
        for name in ("manager.lock", "launch.lock"):
            try:
                with dist.file_lock(self.home / name):
                    found[name] = "free"
            except dist.BusyError:
                found[name] = "busy"
        other = dist.Manager(self.home)
        other.platform = "linux"
        record = {"version": "v1.0.0", "directory": RELEASE, "archive_sha256": "a" * 64}
        for label, action in (("start", lambda: other.run(["start"], automatic_updates=False)),
                              ("rollback", lambda: other.rollback()),
                              ("activate", lambda: other.activate(record))):
            try:
                action()
                found[label] = "ran"
            except dist.DistributionError as error:
                found[label] = str(error)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            found["update code"] = dist.main(["--home", str(self.home), "update"])
        found["update says"] = errors.getvalue()
        return found

    def test_a_start_an_update_and_a_rollback_cannot_run_during_the_delete(self):
        self.activate_release(previous=True)
        results = []
        real = dist.Manager._delete_children

        def spy(manager, held, relative, **options):
            if not results:
                results.append(self.interleave())
            return real(manager, held, relative, **options)

        with patch.object(dist.Manager, "_delete_children", spy):
            done, _ = self.uninstall()
        self.assertTrue(done)
        (found,) = results
        for label, text in found.items():
            if label in ("start", "rollback", "activate"):
                self.assertTrue("being removed" in text or "owns" in text or "busy" in text.lower(), (label, text))
        self.assertEqual(found["manager.lock"], "busy")
        self.assertEqual(found["launch.lock"], "busy")
        for label in ("start", "rollback", "activate"):
            self.assertNotEqual(found[label], "ran", label)
        self.assertEqual(found["update code"], 1)
        self.assertNotEqual(found["update says"], "")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".odicto-managed.json", "data"])

    def test_the_locks_are_free_again_when_it_ends(self):
        self.uninstall()
        for name in ("manager.lock", "launch.lock"):
            with dist.file_lock(self.home / name):
                pass

    def test_an_open_setup_page_refuses_the_uninstall_and_deletes_nothing(self):
        before = snapshot(self.root)
        with dist.file_lock(self.home / "data" / "setup.lock"):
            with self.assertRaises(dist.BusyError) as caught:
                self.uninstall()
        self.assertIn("Setup is open", str(caught.exception))
        self.assertIn("Nothing was removed", str(caught.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_a_running_app_that_stays_refuses_the_uninstall_and_deletes_nothing(self):
        before = snapshot(self.root)
        with dist.file_lock(self.home / "data" / "dictation.lock"):
            with self.assertRaises(dist.BusyError) as caught:
                self.uninstall()
        self.assertIn("still running", str(caught.exception))
        self.assertEqual(snapshot(self.root), before)

    def test_a_start_that_began_before_refuses_the_uninstall(self):
        before = snapshot(self.root)
        with dist.file_lock(self.home / "launch.lock"):
            with self.assertRaises(dist.BusyError) as caught:
                self.uninstall()
        self.assertIn("busy", str(caught.exception))
        self.assertEqual(snapshot(self.root), before)


class TestTheRemovalNotice(Installed):
    """A start that waits on the lock must refuse once the uninstall has begun."""

    def notice(self):
        dist.atomic_json(self.home / dist.REMOVAL_NOTICE, {"schema": 1, "home": str(self.home), "pid": 1})

    def test_start_update_and_rollback_refuse_while_it_stands(self):
        record = self.activate_release(previous=True)
        self.notice()
        other = dist.Manager(self.home)
        other.platform = "linux"
        for action in (lambda: other.run(["start"], automatic_updates=False), lambda: other.initialize(),
                       lambda: other.activate(record), lambda: other.rollback()):
            with self.assertRaises(dist.DistributionError) as caught:
                action()
            self.assertIn("being removed", str(caught.exception))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(dist.main(["--home", str(self.home), "update"]), 1)
        self.assertIn("being removed", errors.getvalue())

    def test_install_may_clear_it(self):
        self.notice()
        other = dist.Manager(self.home)
        other.initialize(reinstall=True)  # what `odicto install` does first
        with dist.file_lock(self.home / "manager.lock"):
            other.clear_removal_notice()
        other.initialize()
        self.assertFalse((self.home / dist.REMOVAL_NOTICE).exists())

    def test_the_notice_is_there_while_it_runs_and_gone_when_it_ends(self):
        seen = []
        real = dist.Manager._delete_children

        def spy(manager, held, relative, **options):
            seen.append((self.home / dist.REMOVAL_NOTICE).is_file())
            return real(manager, held, relative, **options)

        with patch.object(dist.Manager, "_delete_children", spy):
            self.uninstall()
        self.assertTrue(seen and all(seen))
        self.assertFalse((self.home / dist.REMOVAL_NOTICE).exists())

    def test_a_failed_uninstall_leaves_it_so_that_nothing_starts_in_between(self):
        with patch.object(dist.Manager, "_delete_children",
                          return_value=[("releases", OSError(13, "Permission denied"))]):
            done, _ = self.uninstall()
        self.assertFalse(done)
        self.assertTrue((self.home / dist.REMOVAL_NOTICE).is_file())
        with self.assertRaises(dist.DistributionError):
            dist.Manager(self.home).run(["start"], automatic_updates=False)


class TestAFailureThatCanBeRunAgain(Installed):
    def denied(self, target, allowed):
        real = Path.unlink

        def unlink(path, *args, **options):
            if path == target and not allowed():
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **options)

        return patch.object(Path, "unlink", unlink)

    def test_a_startup_entry_that_cannot_be_deleted_fails_the_uninstall_and_a_retry_finishes_it(self):
        open_door = []
        with self.denied(self.startup, lambda: bool(open_door)):
            done, said = self.uninstall()
            self.assertFalse(done)
            self.assertTrue(self.startup.is_file(), "the entry that could not go is still there")
            self.assertTrue((self.home / "autostart.json").is_file(), "its receipt must stay for the retry")
            self.assertTrue((self.home / "bin" / "odicto").is_file())
            self.assertTrue((self.home / "releases").is_dir())
            self.assertTrue(any("Could not remove the start-at-sign-in entry" in line for line in said))
            self.assertIn("run odicto uninstall again.", said[-1])
            self.assertNotIn("Removed Odicto.", said)
            open_door.append(True)
            done, said = self.uninstall()
        self.assertTrue(done)
        self.assertFalse(self.startup.exists())
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".odicto-managed.json", "data"])
        self.assertIn("Removed Odicto.", said)

    def test_a_menu_entry_that_cannot_be_deleted_fails_the_uninstall_and_a_retry_finishes_it(self):
        target = self.menu / "odicto-setup.desktop"
        real = dist.os.unlink
        open_door = []

        def unlink(path, *args, **options):
            if Path(path) == target and not open_door:
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **options)

        with patch.object(dist.os, "unlink", unlink):
            done, said = self.uninstall()
            self.assertFalse(done)
            self.assertTrue(target.is_file())
            self.assertFalse((self.menu / "odicto.desktop").exists(), "the other entries still go")
            self.assertIn("Removed menu entry: Odicto", said)
            self.assertTrue(any("Could not remove the menu entry Odicto Setup" in line for line in said))
            self.assertIn("menu_entries", json.loads((self.home / ".odicto-managed.json").read_text(encoding="utf-8")))
            self.assertTrue((self.home / "bin" / "odicto").is_file())
            open_door.append(True)
            done, said = self.uninstall()
        self.assertTrue(done)
        self.assertFalse(target.exists())
        self.assertIn("Removed menu entry: Odicto Setup", said)

    def test_a_command_that_cannot_be_deleted_fails_the_uninstall(self):
        open_door = []
        with self.denied(self.command, lambda: bool(open_door)):
            done, said = self.uninstall()
            self.assertFalse(done)
            self.assertTrue(os.path.lexists(self.command))
            self.assertTrue(any("Could not remove the odicto command" in line for line in said))
            open_door.append(True)
            done, _ = self.uninstall()
        self.assertTrue(done)
        self.assertFalse(os.path.lexists(self.command))

    def test_a_foreign_item_is_kept_and_is_not_a_failure(self):
        self.startup.write_bytes(b"[Desktop Entry]\nName=Somebody else\n")
        done, said = self.uninstall()
        self.assertTrue(done)
        self.assertTrue(any(line.startswith("Kept the start-at-sign-in entry:") for line in said))


class TestWhatTheFailureMessageSays(Installed):
    FAILING = [("releases", OSError(13, "Permission denied"))]

    def failed(self, **options):
        with patch.object(dist.Manager, "_delete_children", return_value=list(self.FAILING)):
            done, said = self.uninstall(**options)
        self.assertFalse(done)
        return said

    def test_keeping_the_settings_it_says_to_keep_data_and_to_run_it_again(self):
        said = self.failed()
        text = "\n".join(said)
        self.assertIn("Could not remove releases: Permission denied", said)
        self.assertIn("run odicto uninstall again.", text)
        self.assertIn(f"Keep the folder {self.home / 'data'}: it holds your settings.", text)
        self.assertNotIn("delete the folder", text.lower().replace("do not delete the folder", ""))
        self.assertNotIn("--all", text)

    def test_with_all_it_says_to_run_it_again_with_all(self):
        said = self.failed(everything=True)
        text = "\n".join(said)
        self.assertIn("run odicto uninstall --all again.", text)
        self.assertNotIn("yourself", text)

    def test_a_failure_while_the_settings_go_leaves_the_command_that_runs_it_again(self):
        real = dist.Manager._delete_children

        def spy(manager, held, relative, **options):
            if options.get("only") == frozenset({"data"}):
                return list(self.FAILING)
            return real(manager, held, relative, **options)

        with patch.object(dist.Manager, "_delete_children", spy):
            done, said = self.uninstall(everything=True)
        self.assertFalse(done)
        self.assertTrue((self.home / "bin" / "odicto").is_file())
        self.assertTrue((self.home / "launcher.py").is_file())
        self.assertTrue((self.home / "data" / ".env").is_file())
        done, _ = self.uninstall(everything=True)
        self.assertTrue(done)
        self.assertFalse(self.home.exists())


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


class TestMacLaunchAgentUnload(unittest.TestCase):
    """A loaded LaunchAgent is booted out before its file goes, and a foreign job of the same label never."""

    LABEL = "com.odicto.managed"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / "odicto").write_text("launcher", encoding="utf-8")
        self.target = self.root / "LaunchAgents" / "com.odicto.managed.plist"
        self.calls = []
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(managed_autostart, "_location", return_value=self.target))
        self.context.enter_context(patch.object(managed_autostart, "sys", SimpleNamespace(platform="darwin")))
        self.context.enter_context(patch.object(
            managed_autostart, "os", SimpleNamespace(name="posix", replace=os.replace, getuid=lambda: 501)))
        managed_autostart.configure(self.home, True)
        self.assertTrue(self.target.is_file())

    def fake_launchctl(self, loaded_from, *, error=None):
        def run(command, **options):
            self.calls.append((list(command), self.target.exists()))
            if error:
                raise error
            if command[1] == "print":
                if loaded_from is None:
                    return subprocess.CompletedProcess(command, 113, "", "Could not find service")
                return subprocess.CompletedProcess(
                    command, 0, f"gui/501/{self.LABEL} = {{\n\tstate = running\n\tpath = {loaded_from}\n}}\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        return patch.object(managed_autostart.subprocess, "run", run)

    def test_its_own_loaded_job_is_booted_out_before_the_file_is_deleted(self):
        with self.fake_launchctl(self.target):
            self.assertTrue(managed_autostart.remove(self.home))
        self.assertEqual([call[0] for call in self.calls],
                         [["launchctl", "print", f"gui/501/{self.LABEL}"], ["launchctl", "bootout", f"gui/501/{self.LABEL}"]])
        self.assertTrue(self.calls[-1][1], "the file was still there when launchd was told")
        self.assertFalse(self.target.exists())

    def test_a_foreign_job_with_the_same_label_is_not_touched(self):
        with self.fake_launchctl(self.root / "somebody-else" / "com.odicto.managed.plist"):
            self.assertTrue(managed_autostart.remove(self.home))
        self.assertEqual([call[0][1] for call in self.calls], ["print"])
        self.assertFalse(self.target.exists())

    def test_a_job_that_is_not_loaded_is_left_alone(self):
        with self.fake_launchctl(None):
            self.assertTrue(managed_autostart.remove(self.home))
        self.assertEqual([call[0][1] for call in self.calls], ["print"])

    def test_a_launchctl_that_fails_does_not_stop_the_removal(self):
        for error in (OSError("no launchctl"), subprocess.TimeoutExpired("launchctl", 10)):
            with self.subTest(error=type(error).__name__):
                managed_autostart.configure(self.home, True)
                with self.fake_launchctl(self.target, error=error):
                    self.assertTrue(managed_autostart.remove(self.home))
                self.assertFalse(self.target.exists())

    def test_a_foreign_file_is_never_unloaded(self):
        self.target.write_bytes(b"theirs")
        with self.fake_launchctl(self.target):
            with self.assertRaises(RuntimeError):
                managed_autostart.remove(self.home)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.target.read_bytes(), b"theirs")

    def test_remove_autostart_unloads_too(self):
        with self.fake_launchctl(self.target):
            managed_autostart.configure(self.home, False)
        self.assertEqual([call[0][1] for call in self.calls], ["print", "bootout"])
        self.assertFalse(self.target.exists())

    def test_other_systems_never_call_launchctl(self):
        with patch.object(managed_autostart, "sys", SimpleNamespace(platform="linux")):
            with self.fake_launchctl(self.target):
                managed_autostart.remove(self.home)
        self.assertEqual(self.calls, [])


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

    def test_a_failed_script_fails_the_uninstall_and_keeps_the_home(self):
        home = self.manager.home
        (home / "data").mkdir()
        dist.atomic_json(home / ".odicto-managed.json", {"schema": 1, "home": str(home)})
        failed = subprocess.CompletedProcess([], 1, "", "access denied")
        said = []
        with patch.object(dist.subprocess, "run", return_value=failed), \
                patch.object(dist.Manager, "_autostart_module", staticmethod(
                    lambda: SimpleNamespace(remove=lambda path: False))), \
                patch.object(Path, "home", return_value=self.root / "user"), \
                patch.object(dist.Manager, "admin_enabled", lambda self: False):
            done = self.manager.uninstall(stop=lambda: None, say=said.append, programs=self.programs,
                                          command_directory=self.root / "bin")
        self.assertFalse(done)
        self.assertTrue(self.launcher.is_file())
        self.assertTrue(any("Could not remove the menu entries" in line for line in said))

    def test_the_script_deletes_only_a_link_to_this_launcher(self):
        script = dist.REMOVE_SHORTCUT_SCRIPT
        self.assertIn("Remove-Item -LiteralPath $path -Force", script)
        self.assertEqual(script.count("Remove-Item"), 2)
        self.assertIn("[IO.Path]::GetFullPath($env:ODICTO_LAUNCHER)", script)
        self.assertIn("Get-ChildItem -LiteralPath $folder -Force | Select-Object -First 1", script)

    def test_the_remove_shortcut_runs_the_uninstall_with_its_question_in_a_visible_window(self):
        self.assertEqual(dist.SHORTCUTS[-1], ("Remove Odicto", "uninstall --ask", "Remove Odicto from this computer"))
        self.assertIn("$command -like 'uninstall*'", dist.SHORTCUT_SCRIPT)
        self.assertIn("WindowStyle = 1", dist.SHORTCUT_SCRIPT)
        self.assertIn("WindowStyle = 7", dist.SHORTCUT_SCRIPT)

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
                                 "Removed Start Menu item: Stop Odicto",
                                 "Removed Start Menu item: Remove Odicto"])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["Odicto Setup.lnk"])
        links.unlink()
        self.manager.install_shortcuts(self.programs)
        self.manager.remove_shortcuts(self.programs)
        self.assertFalse(self.folder.exists(), "an empty Odicto folder is removed")
        self.assertTrue(self.programs.is_dir())


class TestTheQuestion(Installed):
    """`odicto uninstall --ask`: the Remove Odicto shortcut. One question; Enter keeps the settings."""

    def ask(self, *answers):
        replies = iter(answers)
        shown = []
        result = dist._ask_about_settings(read=lambda prompt: next(replies), show=shown.append)
        return result, shown

    def test_enter_keeps_the_settings(self):
        self.assertEqual(self.ask("")[0], "keep")

    def test_delete_removes_them_too(self):
        self.assertEqual(self.ask("DELETE")[0], "all")
        self.assertEqual(self.ask(" delete ")[0], "all")

    def test_n_cancels(self):
        self.assertEqual(self.ask("n")[0], None)

    def test_a_closed_input_cancels(self):
        def read(prompt):
            raise EOFError
        self.assertIsNone(dist._ask_about_settings(read=read, show=lambda text: None))

    def test_a_word_it_does_not_know_asks_again_and_then_cancels(self):
        result, shown = self.ask("maybe", "huh", "what")
        self.assertIsNone(result)
        self.assertEqual(sum("Please press Enter" in line for line in shown), 3)
        self.assertEqual(self.ask("maybe", "")[0], "keep")

    def test_the_question_uses_plain_words(self):
        prompts = []
        dist._ask_about_settings(read=lambda prompt: prompts.append(prompt) or "", show=lambda text: None)
        self.assertEqual(len(prompts), 1)
        self.assertIn("keep your settings", prompts[0])
        self.assertIn("DELETE", prompts[0])

    def test_keep_removes_odicto_and_keeps_the_settings(self):
        done, said = self.uninstall(ask=lambda: "keep")
        self.assertTrue(done)
        self.assertTrue((self.home / "data" / ".env").is_file())

    def test_all_removes_the_settings_too(self):
        done, said = self.uninstall(ask=lambda: "all")
        self.assertTrue(done)
        self.assertFalse(self.home.exists())

    def test_cancel_removes_nothing_and_stops_nothing(self):
        stopped = []
        before = snapshot(self.root)
        done, said = self.uninstall(ask=lambda: None, stop=lambda: stopped.append(1))
        self.assertTrue(done)
        self.assertEqual(said, ["Nothing was removed."])
        self.assertEqual(stopped, [])
        self.assertEqual(snapshot(self.root), before)

    def test_the_command_asks_waits_and_exits_0(self):
        order = []
        output, errors = io.StringIO(), io.StringIO()
        fake_stdin = SimpleNamespace(isatty=lambda: True)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch.object(sys, "stdin", fake_stdin), \
                patch.object(dist, "_ask_about_settings", lambda: order.append("asked") or "keep"), \
                patch.object(dist, "_pause", lambda: order.append("paused")), \
                patch.object(dist.Manager, "_stop_for_uninstall", lambda self: None), \
                patch.object(dist.Manager, "admin_enabled", lambda self: False):
            code = dist.main(["--home", str(self.home), "uninstall", "--ask"])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(order, ["asked", "paused"])
        self.assertTrue((self.home / "data" / ".env").is_file())

    def test_the_window_waits_after_a_refusal_too(self):
        order = []
        errors = io.StringIO()
        (self.home / ".odicto-managed.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors), \
                patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: True)), \
                patch.object(dist, "_pause", lambda: order.append("paused")):
            code = dist.main(["--home", str(self.home), "uninstall", "--ask"])
        self.assertEqual((code, order), (1, ["paused"]))
        self.assertIn("Odicto: ", errors.getvalue())

    def test_without_a_terminal_it_refuses_instead_of_guessing(self):
        errors = io.StringIO()
        before = snapshot(self.root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors), \
                patch.object(sys, "stdin", io.StringIO("")), patch.object(dist, "_pause", lambda: None):
            code = dist.main(["--home", str(self.home), "uninstall", "--ask"])
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", errors.getvalue())
        self.assertEqual(snapshot(self.root), before)

    def test_ask_and_all_exclude_each_other(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dist.main(["--home", str(self.home), "uninstall", "--ask", "--all"])


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
        self.assertIn("--ask", helper.getvalue())
        self.assertIn("Windows admin mode must be off first", " ".join(helper.getvalue().split()))
        self.assertTrue((self.home / "bin").exists())

    def test_a_home_the_launcher_reached_through_a_link_is_refused(self):
        link = self.root / "link-to-home"
        make_link(link, self.home)
        self.addCleanup(remove_link, link)
        with patch.dict(os.environ, {"ODICTO_LAUNCHER_HOME": str(link)}):
            code, output, errors = self.run_main("uninstall", "--all")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("it is a link or not a folder", errors)
        self.assertTrue((self.home / "data" / ".env").is_file())
        self.assertNotIn("ODICTO_LAUNCHER_HOME", os.environ, "the variable must not reach the app")


class TestTheStableLauncherPassesTheHomeAsReached(unittest.TestCase):
    """The real launcher text, run: `link-to-home/bin/odicto uninstall --all` must not delete the home."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.user = self.root / "user"
        self.user.mkdir()
        self.manager = dist.Manager(self.root / "Odicto home")
        self.home = self.manager.home
        self.home.mkdir()
        dist.atomic_json(self.home / ".odicto-managed.json", {"schema": 1, "home": str(self.home)})
        with patch.object(dist.shutil, "which", return_value="/usr/bin/uv"):
            self.manager.install_launchers()
        (self.home / "data").mkdir()
        (self.home / "data" / ".env").write_text("settings", encoding="ascii")
        record = {"version": "v1.0.0", "directory": RELEASE, "archive_sha256": "a" * 64}
        release = self.home / "releases" / RELEASE
        (release / "tools" / "distribution").mkdir(parents=True)
        dist.atomic_json(release / ".odicto-ready.json", record)
        dist.atomic_json(self.home / "active.json", record)
        self.probe = self.root / "probe.json"
        self.link = self.root / "link-to-home"
        make_link(self.link, self.home)
        self.addCleanup(remove_link, self.link)

    def stub(self, text):
        (self.home / "releases" / RELEASE / "tools" / "distribution" / "manager.py").write_text(text, encoding="utf-8")

    def run_launcher(self, folder, *arguments):
        environment = dict(os.environ, PROBE=str(self.probe), HOME=str(self.user), USERPROFILE=str(self.user),
                           XDG_DATA_HOME=str(self.user / "share"))
        environment.pop("ODICTO_LAUNCHER_HOME", None)
        return subprocess.run([sys.executable, str(folder / "launcher.py"), *arguments], env=environment,
                              capture_output=True, text=True, timeout=60)

    def test_the_manager_is_told_the_home_as_it_was_reached(self):
        self.stub("import json, os, sys\n"
                  "open(os.environ['PROBE'], 'w').write(json.dumps({'argv': sys.argv[1:], "
                  "'reached': os.environ.get('ODICTO_LAUNCHER_HOME')}))\n")
        done = self.run_launcher(self.link, "uninstall", "--all")
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(self.probe.read_text(encoding="utf-8"))
        self.assertEqual(seen["argv"], ["--home", str(self.home), "uninstall", "--all"])
        self.assertEqual(Path(seen["reached"]), self.link)
        done = self.run_launcher(self.home, "uninstall")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(json.loads(self.probe.read_text(encoding="utf-8"))["reached"]), self.home)

    def test_the_real_manager_behind_the_launcher_refuses_a_link_and_deletes_nothing(self):
        self.stub("import importlib.util, sys\n"
                  f"spec = importlib.util.spec_from_file_location('odicto_real_manager', {str(MANAGER)!r})\n"
                  "module = importlib.util.module_from_spec(spec)\n"
                  "sys.modules[spec.name] = module\n"
                  "spec.loader.exec_module(module)\n"
                  "raise SystemExit(module.main(sys.argv[1:]))\n")
        before = snapshot(self.home)
        done = self.run_launcher(self.link, "uninstall", "--all")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("it is a link or not a folder", done.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual((self.home / "data" / ".env").read_text(encoding="ascii"), "settings")


if __name__ == "__main__":
    unittest.main()
