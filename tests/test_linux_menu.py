"""Linux application menu entries: written with the ownership rule of the Windows shortcuts.

Everything runs in a temporary home and a temporary menu folder, on every platform.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dist = _load("odicto_distribution_menu_tested", ROOT / "tools" / "distribution" / "manager.py")


def read_entry(path: Path) -> dict:
    """A .desktop file read the way a desktop reads it: string escapes first, then the Exec rule."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("["):
            key, value = line.split("=", 1)
            values[key] = value
    for key, value in values.items():
        out, i = [], 0
        while i < len(value):
            if value[i] == "\\" and i + 1 < len(value):
                out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r"}.get(value[i + 1], value[i + 1]))
                i += 2
            else:
                out.append(value[i])
                i += 1
        values[key] = "".join(out)
    return values


def split_exec(text: str) -> list[str]:
    """The Exec rule: words split at spaces, double quotes group, a backslash escapes in quotes,
    and %% is one percent sign."""
    words, current, quoted, started, i = [], [], False, False, 0
    while i < len(text):
        c = text[i]
        if quoted and c == "\\" and i + 1 < len(text):
            current.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            quoted, started = not quoted, True
        elif c == " " and not quoted:
            if started or current:
                words.append("".join(current))
            current, started = [], False
        elif c == "%" and text[i + 1:i + 2] == "%":
            current.append("%")
            i += 1
        else:
            current.append(c)
        i += 1
    if started or current:
        words.append("".join(current))
    return words


class MenuFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manager = dist.Manager(self.root / "Odicto home 100%")
        self.manager.platform = "linux"
        self.home = self.manager.home
        (self.home / "bin").mkdir(parents=True)
        self.launcher = self.home / "bin" / "odicto"
        self.launcher.write_text("#!/bin/sh\n", encoding="ascii")
        dist.atomic_json(self.home / ".odicto-managed.json",
                         {"schema": 1, "home": str(self.home), "automatic_updates": True})
        self.folder = self.root / "share" / "applications"

    def marker(self) -> dict:
        return json.loads((self.home / ".odicto-managed.json").read_text(encoding="utf-8"))

    def with_release_icon(self, contents=b"release icon"):
        release = self.home / "releases" / "fixture"
        (release / "assets").mkdir(parents=True, exist_ok=True)
        (release / "assets" / "odicto-icon-256.png").write_bytes(contents)
        for name, value in (("active", {"directory": "fixture"}), ("release_path", release)):
            patcher = patch.object(self.manager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def names(self):
        return sorted(path.name for path in self.folder.iterdir()) if self.folder.exists() else []


class TestExecQuoting(unittest.TestCase):
    def test_a_hard_path_survives_the_desktop_reading_rules(self):
        paths = ["/home/a/.local/share/odicto/bin/odicto", "/home/a b/odicto", "/home/100%/odicto",
                 "/home/$HOME/odicto", '/home/"quoted"/odicto', "/home/back`tick/odicto",
                 "C:\\back\\slash\\odicto", "/home/mix \"$`\\%/odicto"]
        for path in paths:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as folder:
                entry = Path(folder) / "x.desktop"
                entry.write_text(f"[Desktop Entry]\nExec={dist._desktop_command(path)} start\n", encoding="utf-8")
                self.assertEqual(split_exec(read_entry(entry)["Exec"]), [path, "start"])

    def test_a_path_with_a_line_break_is_refused(self):
        with self.assertRaises(dist.DistributionError):
            dist._desktop_command("/home/a\nb/odicto")


class TestInstallMenu(MenuFixture):
    def test_writes_the_three_entries_that_run_the_stable_command(self):
        self.with_release_icon()
        lines = self.manager.install_menu(self.folder)
        self.assertEqual(lines, ["Menu: Odicto", "Menu: Odicto Setup", "Menu: Stop Odicto"])
        self.assertEqual(self.names(), ["odicto-setup.desktop", "odicto-stop.desktop", "odicto.desktop"])
        for name, command, text in dist.SHORTCUTS:
            with self.subTest(name=name):
                entry = read_entry(self.folder / dist.MENU_FILES[name])
                self.assertEqual(entry["Type"], "Application")
                self.assertEqual(entry["Name"], name)
                self.assertEqual(entry["Comment"], text)
                self.assertEqual(entry["Terminal"], "false")
                self.assertEqual(split_exec(entry["Exec"]), [str(self.launcher), command])
                self.assertEqual(entry["Icon"], str(self.home / "odicto.png"))
        self.assertEqual((self.home / "odicto.png").read_bytes(), b"release icon")

    def test_the_marker_records_what_was_written(self):
        self.with_release_icon()
        self.manager.install_menu(self.folder)
        recorded = self.marker()
        for name, _, _ in dist.SHORTCUTS:
            filename = dist.MENU_FILES[name]
            digest = hashlib.sha256((self.folder / filename).read_bytes()).hexdigest()
            self.assertEqual(recorded["menu_entries"][filename], [digest])
        self.assertEqual(recorded["menu_icon"], hashlib.sha256(b"release icon").hexdigest())

    def test_a_second_run_writes_the_same_entries(self):
        self.with_release_icon()
        self.manager.install_menu(self.folder)
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        lines = self.manager.install_menu(self.folder)
        self.assertTrue(all(line.startswith("Menu: ") for line in lines) and len(lines) == 3)
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir()}, before)
        self.assertEqual(len(self.marker()["menu_entries"]["odicto.desktop"]), 1)

    def test_an_entry_that_an_older_odicto_wrote_is_rewritten(self):
        self.with_release_icon()
        self.folder.mkdir(parents=True)
        old = b"[Desktop Entry]\nType=Application\nName=Odicto\nExec=/old/place start\n"
        (self.folder / "odicto.desktop").write_bytes(old)
        marker = self.marker()
        marker["menu_entries"] = {"odicto.desktop": [hashlib.sha256(old).hexdigest()]}
        dist.atomic_json(self.home / ".odicto-managed.json", marker)
        lines = self.manager.install_menu(self.folder)
        self.assertIn("Menu: Odicto", lines)
        self.assertEqual(split_exec(read_entry(self.folder / "odicto.desktop")["Exec"]),
                         [str(self.launcher), "start"])
        recorded = self.marker()["menu_entries"]["odicto.desktop"]
        self.assertEqual(len(recorded), 2)
        self.assertIn(hashlib.sha256(old).hexdigest(), recorded)

    def test_an_entry_that_odicto_did_not_write_is_kept(self):
        self.with_release_icon()
        self.folder.mkdir(parents=True)
        foreign = b"[Desktop Entry]\nType=Application\nName=My own Odicto\nExec=/usr/bin/true\n"
        (self.folder / "odicto.desktop").write_bytes(foreign)
        lines = self.manager.install_menu(self.folder)
        self.assertEqual(lines[0], "Kept Odicto (it is not an Odicto file)")
        self.assertEqual((self.folder / "odicto.desktop").read_bytes(), foreign)
        self.assertEqual(self.names(), ["odicto-setup.desktop", "odicto-stop.desktop", "odicto.desktop"])
        self.assertNotIn("odicto.desktop", self.marker()["menu_entries"])

    def test_a_link_or_a_folder_with_an_entry_name_is_kept(self):
        self.with_release_icon()
        self.folder.mkdir(parents=True)
        target = self.root / "somebody-elses.desktop"
        target.write_bytes(b"theirs")
        try:
            os.symlink(target, self.folder / "odicto-setup.desktop")
        except OSError:  # a Windows account without the link right: a folder is the same kind of obstacle
            (self.folder / "odicto-setup.desktop").mkdir()
        lines = self.manager.install_menu(self.folder)
        self.assertIn("Kept Odicto Setup (it is not an Odicto file)", lines)
        self.assertEqual(target.read_bytes(), b"theirs")
        self.assertTrue((self.folder / "odicto-setup.desktop").is_symlink()
                        or (self.folder / "odicto-setup.desktop").is_dir())

    def test_an_icon_file_odicto_did_not_write_is_kept_and_not_used(self):
        self.with_release_icon()
        (self.home / "odicto.png").write_bytes(b"the user's own picture")
        self.manager.install_menu(self.folder)
        self.assertEqual((self.home / "odicto.png").read_bytes(), b"the user's own picture")
        self.assertNotIn("Icon", read_entry(self.folder / "odicto.desktop"))

    def test_a_new_release_icon_replaces_the_icon_odicto_wrote(self):
        self.with_release_icon(b"first")
        self.manager.install_menu(self.folder)
        self.with_release_icon(b"second")
        self.manager.install_menu(self.folder)
        self.assertEqual((self.home / "odicto.png").read_bytes(), b"second")
        self.assertEqual(read_entry(self.folder / "odicto.desktop")["Icon"], str(self.home / "odicto.png"))

    def test_without_a_release_icon_the_entry_has_no_icon_line(self):
        self.manager.install_menu(self.folder)
        self.assertNotIn("Icon", read_entry(self.folder / "odicto.desktop"))

    def test_a_missing_launcher_creates_nothing(self):
        self.launcher.unlink()
        with self.assertRaises(dist.DistributionError):
            self.manager.install_menu(self.folder)
        self.assertFalse(self.folder.exists())

    def test_a_folder_that_does_not_exist_is_made(self):
        self.manager.install_menu(self.folder)
        self.assertTrue(self.folder.is_dir())


class TestMenuFolderAndDispatch(MenuFixture):
    def test_the_folder_follows_xdg_data_home_when_it_is_absolute(self):
        shared = self.root / "xdg"
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(shared)}):
            self.assertEqual(dist.Manager._menu_folder(), shared / "applications")
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative/path"}), \
                patch.object(Path, "home", return_value=self.root / "user"):
            self.assertEqual(dist.Manager._menu_folder(), self.root / "user" / ".local" / "share" / "applications")

    def test_the_default_folder_is_under_the_users_home(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": ""}), patch.object(Path, "home", return_value=self.root / "user"):
            self.manager.install_menu()
        self.assertTrue((self.root / "user" / ".local" / "share" / "applications" / "odicto.desktop").is_file())

    def test_install_shortcuts_writes_the_menu_on_linux_and_nothing_on_macos(self):
        self.assertEqual(self.manager.install_shortcuts(self.folder),
                         ["Menu: Odicto", "Menu: Odicto Setup", "Menu: Stop Odicto"])
        self.manager.platform = "darwin"
        shutil_before = self.names()
        self.assertEqual(self.manager.install_shortcuts(self.folder), [])
        self.assertEqual(self.names(), shutil_before)

    def test_install_shortcuts_keeps_the_start_menu_code_for_windows(self):
        self.manager.platform = "win32"
        with patch.object(self.manager, "_install_start_menu", return_value=["Start Menu: Odicto"]) as windows:
            self.assertEqual(self.manager.install_shortcuts(self.folder), ["Start Menu: Odicto"])
        windows.assert_called_once_with(self.folder)
        self.assertFalse(self.folder.exists())


class TestRefreshAfterAnUpdate(MenuFixture):
    def test_entries_odicto_wrote_follow_the_current_text(self):
        self.with_release_icon(b"first")
        self.manager.install_menu(self.folder)
        self.with_release_icon(b"second")
        with patch.object(dist.Manager, "_menu_folder", return_value=self.folder):
            lines = self.manager.refresh_menu()
        self.assertEqual(len(lines), 3)
        self.assertEqual((self.home / "odicto.png").read_bytes(), b"second")

    def test_a_home_that_never_had_entries_gets_none(self):
        self.with_release_icon()
        with patch.object(dist.Manager, "_menu_folder", return_value=self.folder):
            self.assertEqual(self.manager.refresh_menu(), [])
        self.assertFalse(self.folder.exists())

    def test_other_systems_get_none(self):
        self.manager.install_menu(self.folder)
        self.manager.platform = "darwin"
        self.assertEqual(self.manager.refresh_menu(self.folder), [])


class TestRemoveMenu(MenuFixture):
    def test_removes_the_entries_odicto_wrote_and_keeps_the_rest(self):
        self.with_release_icon()
        self.folder.mkdir(parents=True)
        foreign = b"[Desktop Entry]\nName=Mine\n"
        (self.folder / "odicto-stop.desktop").write_bytes(foreign)
        unrelated = self.folder / "other.desktop"
        unrelated.write_bytes(b"other")
        self.manager.install_menu(self.folder)
        lines = self.manager.remove_menu(self.folder)
        self.assertEqual(lines, ["Removed menu entry: Odicto", "Removed menu entry: Odicto Setup",
                                 "Kept Stop Odicto (it is not an Odicto file)"])
        self.assertEqual(self.names(), ["odicto-stop.desktop", "other.desktop"])
        self.assertEqual((self.folder / "odicto-stop.desktop").read_bytes(), foreign)

    def test_an_entry_the_user_edited_is_kept(self):
        self.manager.install_menu(self.folder)
        edited = self.folder / "odicto.desktop"
        edited.write_bytes(edited.read_bytes() + b"X-Mine=1\n")
        self.assertIn("Kept Odicto (it is not an Odicto file)", self.manager.remove_menu(self.folder))
        self.assertTrue(edited.is_file())

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(self.manager.remove_menu(self.folder), [])


class TestInstallCommandPrintsTheMenu(unittest.TestCase):
    """main() on `install` calls install_shortcuts on Linux; a failure is one line and never fails the install."""

    def setUp(self):
        from tests import test_managed_distribution as fixtures
        self.dist = fixtures.dist
        self.fixture = fixtures.TestManagedDistribution(
            methodName="test_new_install_creates_settings_once_from_the_example")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.manager.speech_preparer = lambda directory, environment: None
        self.manager.platform = "linux"

    def run_main(self, shortcuts):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(self.dist, "Manager", return_value=self.manager), \
                patch.object(self.manager, "install_launchers"), \
                patch.object(self.manager, "expose_command", return_value=Path("odicto")), \
                patch.object(self.manager, "install_shortcuts", **shortcuts) as menu, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = self.dist.main(["--home", str(self.manager.home), "install"])
        return code, output.getvalue(), errors.getvalue(), menu

    def test_the_lines_are_printed_before_the_setup_hint(self):
        code, output, errors, menu = self.run_main({"return_value": ["Menu: Odicto"]})
        self.assertEqual(code, 0, errors)
        menu.assert_called_once_with()
        self.assertLess(output.index("Menu: Odicto"), output.index("Configure with:"))

    def test_a_failure_is_one_line_and_the_install_goes_on(self):
        code, output, errors, _ = self.run_main({"side_effect": self.dist.DistributionError("no launcher")})
        self.assertEqual(code, 0)
        self.assertIn("Menu entries unchanged: no launcher", errors)
        self.assertIn("is active.", output)


if __name__ == "__main__":
    unittest.main()
