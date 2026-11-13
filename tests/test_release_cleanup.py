"""Old release folders are retired under manager.lock and deleted after it, with real folders in temporary homes."""
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_managed_distribution as fixtures

dist = fixtures.dist


def make_link(link: Path, target: Path) -> None:
    """A symlink, or on Windows a junction (a symlink needs a privilege there)."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(link: Path) -> None:
    if os.path.lexists(link):
        os.rmdir(link) if os.name == "nt" else os.unlink(link)


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestManagedDistribution(methodName="test_new_install_creates_settings_once_from_the_example")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.releases = self.manager.releases
        self.count = 0

    def activate(self, tag):
        record = self.manager.stage(self.fixture.add_release(tag))
        self.manager.activate(record)
        return record["directory"]

    def folder(self, ready=True, modified=None, files=True) -> str:
        """A release folder that no pointer names, with a nested tree."""
        self.count += 1
        name = f"v1.0.{self.count}-{'a' * 11}{self.count % 10}-{self.count:08x}"
        path = self.releases / name
        (path / "app" / "deep").mkdir(parents=True)
        if files:
            (path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (path / "app" / "deep" / "module.py").write_text("x = 1\n", encoding="utf-8")
        if ready:
            (path / ".odicto-ready.json").write_text("{}", encoding="utf-8")
        if modified is not None:
            os.utime(path, (modified, modified))
        return name

    def trash(self, name):
        return self.releases / (".trash-" + name)

    def names(self):
        return sorted(os.listdir(self.releases))

    def tidy(self):
        with dist.file_lock(self.manager.home / "manager.lock"):
            retired = self.manager.retire_old_releases()
        return retired, self.manager.delete_trash()


class TestWhatIsKept(Cleanup):
    def test_active_previous_pending_and_the_newest_unready_folder_stay(self):
        old = self.fixture.initial()["directory"]
        active = self.activate("v1.3.0")
        pending = self.manager.stage(self.fixture.add_release("v1.4.0"))["directory"]
        orphan = self.folder(ready=True, modified=1000)
        failed_old = self.folder(ready=False, modified=2000)
        failed_new = self.folder(ready=False, modified=3000)
        retired, deleted = self.tidy()
        self.assertEqual(sorted(retired), sorted([orphan, failed_old]))
        self.assertEqual(deleted, 2)
        self.assertEqual(self.names(), sorted([old, active, pending, failed_new]))

    def test_the_newest_unready_folder_goes_by_modification_time_not_by_name(self):
        self.fixture.initial()
        older_name_newer_time = self.folder(ready=False, modified=5000)
        newer_name_older_time = self.folder(ready=False, modified=1000)
        self.assertGreater(newer_name_older_time, older_name_newer_time)
        retired, _ = self.tidy()
        self.assertEqual(retired, [newer_name_older_time])
        self.assertIn(older_name_newer_time, self.names())

    def test_older_ready_and_older_failed_folders_are_retired_and_deleted(self):
        self.fixture.initial()
        names = [self.folder(ready=True, modified=100), self.folder(ready=True, modified=200),
                 self.folder(ready=False, modified=300), self.folder(ready=False, modified=400)]
        retired, deleted = self.tidy()
        self.assertEqual(sorted(retired), sorted(names[:3]))
        self.assertEqual(deleted, 3)
        self.assertEqual(self.names(), sorted([self.manager.active()["directory"], names[3]]))

    def test_each_retired_folder_gets_one_line_in_the_update_log(self):
        self.fixture.initial()
        names = [self.folder(modified=100), self.folder(modified=200)]
        self.tidy()
        lines = [line for line in self.manager.update_log.read_text(encoding="utf-8").splitlines() if "retired" in line]
        self.assertEqual(len(lines), 2)
        for name in names:
            self.assertEqual(sum(name in line for line in lines), 1)

    def test_an_unreadable_pointer_retires_nothing_and_does_not_raise(self):
        self.fixture.initial()
        self.activate("v1.3.0")
        orphan = self.folder()
        before = self.names()
        pending = self.manager.home / "pending.json"
        for path, bad in ((pending, b"not json"), (pending, b"[]"), (pending, b'{"directory": "../x"}'),
                          (pending, b'{"directory": 5}'),
                          (self.manager.pointer, b"not json"),
                          (self.manager.pointer, b'{"directory": "nonsense"}')):
            original = path.read_bytes()
            with self.subTest(path=path.name, content=bad):
                path.write_bytes(bad)
                try:
                    self.assertEqual(self.tidy(), ([], 0))
                finally:
                    path.write_bytes(original)
                self.assertEqual(self.names(), before)
        previous = json.loads(self.manager.pointer.read_text(encoding="utf-8"))
        previous["previous"] = "not a record"
        self.manager.pointer.write_text(json.dumps(previous), encoding="utf-8")
        self.assertEqual(self.tidy(), ([], 0))
        self.assertIn(orphan, self.names())

    def test_no_active_release_retires_nothing(self):
        orphan = self.folder()
        self.assertEqual(self.tidy(), ([], 0))
        self.assertEqual(self.names(), [orphan])

    def test_a_name_that_does_not_match_is_never_touched(self):
        self.fixture.initial()
        valid = self.folder(modified=100)
        stray = ["stray", ".trash-stray", ".trash-v1.0.0-short", "v1.0.0-aaaaaaaaaaaa-bbbbbbbb-extra",
                 ".trash-v1.0.0-aaaaaaaaaaaa-bbbbbbbb-extra", "x.trash-v1.0.0-aaaaaaaaaaaa-bbbbbbbb",
                 ".trash-.trash-v1.0.0-aaaaaaaaaaaa-bbbbbbbb", "V1.0.0-aaaaaaaaaaaa-bbbbbbbb"]
        for name in stray:
            (self.releases / name / "inner").mkdir(parents=True)
            (self.releases / name / "inner" / "keep.txt").write_text("keep", encoding="utf-8")
        # A file with a valid trash name is not a folder: it stays too.
        file_name = ".trash-v1.0.99-aaaaaaaaaaaa-00000099"
        (self.releases / file_name).write_text("not a folder", encoding="utf-8")
        retired, _ = self.tidy()
        self.assertEqual(retired, [valid])
        for name in stray:
            self.assertEqual((self.releases / name / "inner" / "keep.txt").read_text(encoding="utf-8"), "keep", name)
        self.assertEqual((self.releases / file_name).read_text(encoding="utf-8"), "not a folder")


class TestLinksAreRefused(Cleanup):
    def setUp(self):
        super().setUp()
        self.outside = self.fixture.root / "outside"
        self.outside.mkdir()
        (self.outside / "precious.txt").write_text("keep", encoding="utf-8")
        self.fixture.initial()

    def test_a_linked_release_folder_is_not_retired(self):
        link = self.releases / "v1.0.0-aaaaaaaaaaaa-bbbbbbbb"
        make_link(link, self.outside)
        self.addCleanup(remove_link, link)
        retired, deleted = self.tidy()
        self.assertEqual((retired, deleted), ([], 0))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue((self.outside / "precious.txt").is_file())

    def test_a_linked_trash_folder_is_not_deleted_or_followed(self):
        link = self.releases / ".trash-v1.0.0-aaaaaaaaaaaa-bbbbbbbb"
        make_link(link, self.outside)
        self.addCleanup(remove_link, link)
        self.assertEqual(self.manager.delete_trash(), 0)
        self.assertTrue(os.path.lexists(link))
        self.assertTrue((self.outside / "precious.txt").is_file())

    def test_a_link_inside_a_trash_folder_is_removed_without_following_it(self):
        name = self.folder()
        os.rename(self.releases / name, self.trash(name))
        link = self.trash(name) / "app" / "linked"
        make_link(link, self.outside)
        self.addCleanup(remove_link, link)
        self.assertEqual(self.manager.delete_trash(), 1)
        self.assertFalse(os.path.lexists(self.trash(name)))
        self.assertEqual((self.outside / "precious.txt").read_text(encoding="utf-8"), "keep")

    def test_a_reparse_point_is_not_a_real_folder_on_any_platform(self):
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x10)
        reparse = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x410)
        symlink = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        plain = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x20)
        for info, expected in ((folder, True), (reparse, False), (symlink, False), (plain, False)):
            with self.subTest(mode=oct(info.st_mode), attributes=info.st_file_attributes), \
                    patch.object(dist.os, "lstat", return_value=info):
                self.assertIs(dist._real_folder("any"), expected)
        with patch.object(dist.os, "lstat", side_effect=FileNotFoundError):
            self.assertFalse(dist._real_folder("missing"))


class TestRetireFailures(Cleanup):
    def test_a_rename_that_fails_is_skipped_without_an_error(self):
        self.fixture.initial()
        blocked, free = self.folder(modified=100), self.folder(modified=200)
        real = os.rename

        def rename(source, target):
            if Path(source).name == blocked:
                raise PermissionError("a file in it is in use")
            return real(source, target)

        with patch.object(dist.os, "rename", side_effect=rename):
            retired = self.manager.retire_old_releases()
        self.assertEqual(retired, [free])
        self.assertTrue((self.releases / blocked / "VERSION").is_file())
        self.assertFalse(self.trash(blocked).exists())

    def test_a_folder_that_could_not_be_retired_is_retired_by_a_later_run(self):
        self.fixture.initial()
        stuck, newest = self.folder(modified=100), self.folder(ready=False, modified=200)
        with patch.object(dist.os, "rename", side_effect=OSError("busy")):
            self.assertEqual(self.tidy(), ([], 0))
        retired, deleted = self.tidy()
        self.assertEqual((retired, deleted), ([stuck], 1))
        self.assertIn(newest, self.names())

    def test_an_existing_trash_folder_of_the_same_name_is_not_overwritten(self):
        self.fixture.initial()
        name = self.folder(modified=100)
        self.folder(ready=False, modified=200)
        self.trash(name).mkdir()
        (self.trash(name) / "evidence.txt").write_text("keep", encoding="utf-8")
        retired = self.manager.retire_old_releases()
        self.assertEqual(retired, [])
        self.assertTrue((self.releases / name / "VERSION").is_file())
        self.assertTrue((self.trash(name) / "evidence.txt").is_file())


class TestDeleteBudget(Cleanup):
    def make_trash(self, count):
        names = [self.folder() for _ in range(count)]
        for name in names:
            os.rename(self.releases / name, self.trash(name))
        return names

    def test_a_leftover_trash_folder_is_deleted_on_a_later_run(self):
        (name,) = self.make_trash(1)
        read_only = self.trash(name) / "app" / "locked.txt"
        read_only.write_text("x", encoding="utf-8")
        os.chmod(read_only, stat.S_IREAD)
        self.assertEqual(self.manager.delete_trash(), 1)
        self.assertEqual(self.names(), [])

    def test_a_read_only_folder_is_made_writable_and_tried_once_more(self):
        (name,) = self.make_trash(1)
        locked = self.trash(name) / "app" / "locked"
        locked.mkdir()
        (locked / "inside.txt").write_text("x", encoding="utf-8")
        os.chmod(locked / "inside.txt", stat.S_IREAD)
        os.chmod(locked, stat.S_IREAD | stat.S_IEXEC)
        self.assertEqual(self.manager.delete_trash(), 1)
        self.assertEqual(self.names(), [])

    def test_a_budget_of_zero_deletes_nothing(self):
        names = self.make_trash(2)
        self.assertEqual(self.manager.delete_trash(0), 0)
        self.assertEqual(self.names(), sorted(".trash-" + name for name in names))

    def test_the_budget_ends_between_folders_and_a_later_run_finishes(self):
        names = self.make_trash(3)
        now = [0.0]
        removed = []

        def remove(path, parent, deadline, clock):
            now[0] += 10
            removed.append(Path(path).name)
            import shutil
            shutil.rmtree(path)

        with patch.object(dist, "_remove_tree", side_effect=remove):
            self.assertEqual(self.manager.delete_trash(15, clock=lambda: now[0]), 2)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(self.names()), 1)
        self.assertEqual(self.manager.delete_trash(), 1)
        self.assertEqual(self.names(), [])

    def test_the_budget_ends_inside_a_large_folder_and_a_later_run_finishes(self):
        (name,) = self.make_trash(1)
        for index in range(40):
            (self.trash(name) / "app" / f"file{index}.txt").write_text("x", encoding="utf-8")
        ticks = iter(range(10 ** 6))
        self.assertEqual(self.manager.delete_trash(5, clock=lambda: next(ticks)), 0)
        left = sum(len(files) for _, _, files in os.walk(self.trash(name)))
        self.assertTrue(self.trash(name).is_dir())
        self.assertLess(left, 43)
        self.assertGreater(left, 0)
        self.assertEqual(self.manager.delete_trash(), 1)
        self.assertEqual(self.names(), [])

    def test_an_error_in_one_folder_does_not_stop_the_others(self):
        first, second = self.make_trash(2)
        real = dist._remove_tree

        def remove(path, parent, deadline, clock):
            if Path(path).name == ".trash-" + first:
                raise PermissionError("in use")
            return real(path, parent, deadline, clock)

        with patch.object(dist, "_remove_tree", side_effect=remove):
            self.assertEqual(self.manager.delete_trash(), 1)
        self.assertEqual(self.names(), [".trash-" + first])

    def test_a_missing_releases_folder_is_not_an_error(self):
        os.rmdir(self.releases)
        self.assertEqual(self.manager.delete_trash(), 0)
        self.assertEqual(self.manager.retire_old_releases(), [])


class TestWhenItRuns(Cleanup):
    def start(self, command, result=0):
        with patch.object(self.manager, "_launch", return_value=result):
            return self.manager.run(command, automatic_updates=False)

    def test_start_retires_and_deletes_but_restart_and_setup_do_not(self):
        self.fixture.initial()
        orphan = self.folder()
        for command in (["restart"], ["setup"]):
            with self.subTest(command=command):
                self.assertEqual(self.start(command), 0)
                self.assertEqual(self.names(), sorted([self.manager.active()["directory"], orphan]))
        with patch.object(dist.subprocess, "run", return_value=type("Done", (), {"returncode": 0})()):
            for command in (["stop"], ["status"], ["version"]):
                self.assertEqual(self.manager.run(command, automatic_updates=False), 0)
        self.assertEqual(self.names(), sorted([self.manager.active()["directory"], orphan]))
        self.assertEqual(self.start(["start"]), 0)
        self.assertEqual(self.names(), [self.manager.active()["directory"]])

    def test_an_admin_restart_that_becomes_a_start_does_not_prune(self):
        self.fixture.initial()
        orphan = self.folder()
        support = self.manager.support()
        with patch.object(self.manager, "admin_enabled", return_value=True), \
                patch.object(self.manager, "_launch_admin", return_value=0), \
                patch.object(support, "stop", return_value=0), patch.object(support, "elevated", return_value=False):
            self.assertEqual(self.manager.run(["restart"], automatic_updates=False), 0)
        self.assertIn(orphan, self.names())

    def test_a_start_that_failed_or_was_refused_does_not_prune(self):
        self.fixture.initial()
        orphan = self.folder()
        for result in (1, dist.EXIT_ANOTHER_OWNER):
            with self.subTest(result=result), patch.object(dist.sys, "stderr", new_callable=io.StringIO):
                self.assertEqual(self.start(["start"], result), result)
                self.assertIn(orphan, self.names())

    def test_a_busy_manager_lock_skips_the_retire_step_but_still_deletes(self):
        self.fixture.initial()
        orphan = self.folder(modified=100)
        leftover = self.folder(modified=50)
        os.rename(self.releases / leftover, self.trash(leftover))
        with dist.file_lock(self.manager.home / "manager.lock"):
            self.manager._tidy_releases()
        self.assertIn(orphan, self.names())
        self.assertFalse(self.trash(leftover).exists())

    def test_the_two_steps_never_raise_and_never_change_the_start_result(self):
        self.fixture.initial()
        orphan = self.folder()
        for error in (OSError("disk error"), RuntimeError("bug")):
            with self.subTest(error=error), patch.object(dist.os, "listdir", side_effect=error):
                self.assertEqual(self.manager.retire_old_releases(), [])
                self.assertEqual(self.manager.delete_trash(), 0)
                self.assertEqual(self.start(["start"]), 0)
        self.assertIn(orphan, self.names())

    def test_start_retires_under_the_lock_and_deletes_after_it_is_released(self):
        self.fixture.initial()
        orphan = self.folder()
        events = []
        self.watch_the_lock(events)
        self.assertEqual(self.start(["start"]), 0)
        self.assertEqual(events, ["retire while the lock is held", "delete while the lock is free"])
        self.assertEqual(self.names(), [self.manager.active()["directory"]])
        self.assertNotIn(orphan, self.names())

    def watch_the_lock(self, events):
        retire, delete = self.manager.retire_old_releases, self.manager.delete_trash

        def held():
            try:
                with dist.file_lock(self.manager.home / "manager.lock"):
                    return False
            except dist.BusyError:
                return True

        def retiring():
            events.append("retire while the lock is " + ("held" if held() else "free"))
            return retire()

        def deleting(*args, **kwargs):
            events.append("delete while the lock is " + ("held" if held() else "free"))
            return delete(*args, **kwargs)

        for name, wrapper in (("retire_old_releases", retiring), ("delete_trash", deleting)):
            patcher = patch.object(self.manager, name, side_effect=wrapper)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(dist, "Manager", return_value=self.manager), \
                patch.object(dist.shutil, "which", return_value="/synthetic/uv"), \
                patch.object(self.manager, "expose_command", return_value=self.manager.home / "bin" / "odicto"), \
                patch.object(self.manager, "install_shortcuts", return_value=[]), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            code = dist.main(["--home", str(self.manager.home), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_update_retires_under_the_lock_and_deletes_after_it_is_released(self):
        self.fixture.initial()
        orphan = self.folder()
        self.fixture.add_release("v1.3.0")
        events = []
        self.watch_the_lock(events)
        code, out, err = self.command("update")
        self.assertEqual(code, 0, err)
        self.assertIn("Odicto 1.3.0 is ready.", out)
        self.assertNotIn("is active", out)
        self.assertEqual(events, ["retire while the lock is held", "delete while the lock is free"])
        self.assertNotIn(orphan, self.names())
        self.assertEqual(len(self.names()), 2)  # the new release and the previous one

    def test_install_retires_under_the_lock_and_deletes_after_it_is_released(self):
        orphan = self.folder()
        self.fixture.add_release("v1.2.0")
        events = []
        self.watch_the_lock(events)
        code, out, err = self.command("install")
        self.assertEqual(code, 0, err)
        self.assertIn("is active.", out)
        self.assertEqual(events, ["retire while the lock is held", "delete while the lock is free"])
        self.assertNotIn(orphan, self.names())

    def test_an_update_that_changed_nothing_or_failed_removes_nothing(self):
        self.fixture.initial()
        orphan = self.folder()
        events = []
        self.watch_the_lock(events)
        code, out, _ = self.command("update")
        self.assertEqual(code, 0)
        self.assertIn("is up to date", out)
        self.fixture.add_release("v1.3.0")
        self.manager.installer = lambda directory, environment: (_ for _ in ()).throw(dist.DistributionError("fixture"))
        code, _, _ = self.command("update")
        self.assertNotEqual(code, 0)
        self.assertEqual(events, [])
        self.assertIn(orphan, self.names())


class TestRollbackAfterAPrune(Cleanup):
    def test_rollback_still_works_after_a_start_pruned_the_older_releases(self):
        first = self.fixture.initial()["directory"]
        second = self.activate("v1.3.0")
        third = self.activate("v1.4.0")
        with patch.object(self.manager, "_launch", return_value=0):
            self.assertEqual(self.manager.run(["start"], automatic_updates=False), 0)
        self.assertEqual(self.names(), sorted([second, third]))
        self.assertNotIn(first, self.names())
        self.assertEqual(self.manager.rollback()["directory"], second)
        self.assertEqual(self.manager.active()["directory"], second)
        self.assertTrue(self.manager.release_path(self.manager.active()).is_dir())


if __name__ == "__main__":
    unittest.main()
