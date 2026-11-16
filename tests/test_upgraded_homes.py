"""An install that an older manager made gets what this release added: the loader that tells
`odicto uninstall` which path it was reached by (POSIX), and the Remove Odicto shortcut (Windows).

Review 2, finding 7. Temporary homes and a fake or temporary Start Menu only: no real uv, no real shortcut.
"""
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
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("odicto_upgraded_homes_manager", ROOT / "tools" / "distribution" / "manager.py")
dist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dist
SPEC.loader.exec_module(dist)

# The loader of 1.5.x and of the first 1.6 builds: no ODICTO_LAUNCHER_HOME line.
OLD_LOADER = "".join(line for line in dist.LAUNCHER_PY.splitlines(keepends=True) if "ODICTO_LAUNCHER_HOME" not in line)


class UpgradedPosixHome(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("UV", "ODICTO_BOOTSTRAP_UV"):
            os.environ.pop(name, None)
        posix = patch.object(dist, "_posix_host", return_value=True)  # the POSIX branch, on every host
        posix.start()
        self.addCleanup(posix.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-upgraded-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "bootstrap-uv" / dist.PRIVATE_UV_NAMES[0]
        self.source.parent.mkdir()
        self.source.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.source.chmod(0o755)
        self.manager = dist.Manager(self.root / "home")
        self.manager.initialize()
        with patch.dict(os.environ, {"UV": str(self.source)}):
            self.manager.install_launchers()
        self.loader = self.manager.home / "launcher.py"

    def make_it_old(self, text: str = OLD_LOADER) -> None:
        """The home as an older manager left it: its loader text, and its hash in the marker as accepted."""
        self.loader.write_bytes(text.encode("utf-8"))
        marker = json.loads(self.manager.marker.read_text(encoding="utf-8"))
        marker["launchers"]["launcher.py"] = [hashlib.sha256(text.encode()).hexdigest()]
        dist.atomic_json(self.manager.marker, marker)

    def refresh(self):
        with patch.dict(os.environ, {"UV": str(self.source)}):
            self.manager._refresh_launchers()

    def test_the_old_loader_is_not_the_current_one(self):
        self.assertNotEqual(OLD_LOADER, dist.LAUNCHER_PY)
        self.assertNotIn("ODICTO_LAUNCHER_HOME", OLD_LOADER)

    def test_an_old_loader_is_replaced_at_the_next_start_and_the_command_script_stays(self):
        self.make_it_old()
        script = (self.manager.home / "bin" / "odicto").read_bytes()
        self.refresh()
        self.assertEqual(self.loader.read_bytes(), dist.LAUNCHER_PY.encode())
        self.assertEqual((self.manager.home / "bin" / "odicto").read_bytes(), script)
        recorded = json.loads(self.manager.marker.read_text(encoding="utf-8"))["launchers"]["launcher.py"]
        self.assertEqual(recorded, hashlib.sha256(dist.LAUNCHER_PY.encode()).hexdigest())

    def test_a_missing_loader_is_made_again(self):
        self.loader.unlink()
        self.refresh()
        self.assertEqual(self.loader.read_bytes(), dist.LAUNCHER_PY.encode())

    def test_a_current_home_is_left_alone(self):
        with patch.object(dist.Manager, "install_launchers") as install:
            self.refresh()
        install.assert_not_called()

    def test_a_loader_that_odicto_did_not_write_is_kept_and_the_start_is_not_disturbed(self):
        self.loader.write_text("print('mine')\n", encoding="utf-8")
        script = (self.manager.home / "bin" / "odicto").read_bytes()
        self.refresh()  # must not raise
        self.assertEqual(self.loader.read_text(encoding="utf-8"), "print('mine')\n")
        self.assertEqual((self.manager.home / "bin" / "odicto").read_bytes(), script)
        self.assertIn("unknown ownership", self.manager.update_log.read_text(encoding="utf-8"))

    def test_the_new_loader_passes_the_path_it_was_reached_by(self):
        self.assertIn("ODICTO_LAUNCHER_HOME", dist.LAUNCHER_PY)
        self.assertEqual(self.loader.read_text(encoding="utf-8"), dist.LAUNCHER_PY)


class OldWindowsHome(unittest.TestCase):
    """A Windows home from before the Remove Odicto shortcut: three shortcuts, and no fourth."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-startmenu-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manager = dist.Manager(self.root / "Odicto home")
        self.manager.platform = "win32"
        self.home = self.manager.home
        (self.home / "bin").mkdir(parents=True)
        self.launcher = self.home / "bin" / "odicto.cmd"
        self.launcher.write_text("@exit /b 0\r\n", encoding="ascii")
        dist.atomic_json(self.manager.marker, {"schema": 1, "home": str(self.home)})
        # The default Start Menu of this test's user, so that nothing reads the real one.
        environment = patch.dict(os.environ, {"APPDATA": str(self.root / "AppData")})
        environment.start()
        self.addCleanup(environment.stop)
        self.programs = self.root / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        self.folder = self.programs / "Odicto"

    def three_placeholders(self):
        self.folder.mkdir(parents=True)
        for name in ("Odicto", "Odicto Setup", "Stop Odicto"):
            (self.folder / f"{name}.lnk").write_bytes(b"placeholder")

    def fake_powershell(self, output="Start Menu: Remove Odicto\n", code=0, error=""):
        seen = []

        def run(command, **options):
            seen.append({"command": command, "env": options["env"]})
            return subprocess.CompletedProcess(command, code, output, error)

        return seen, patch.object(dist.subprocess, "run", run)

    def test_the_script_gets_only_the_late_shortcut_to_add(self):
        self.three_placeholders()
        seen, patched = self.fake_powershell()
        with patched:
            lines = self.manager.refresh_menu(self.programs)
        self.assertEqual(lines, ["Start Menu: Remove Odicto"])
        (call,) = seen
        self.assertEqual(call["command"][-1], dist.ADD_SHORTCUT_SCRIPT)
        self.assertEqual(call["env"]["ODICTO_LATE"], "Remove Odicto")
        self.assertEqual(call["env"]["ODICTO_LAUNCHER"], str(self.launcher))
        self.assertEqual(call["env"]["ODICTO_PROGRAMS"], str(self.programs))
        self.assertEqual(json.loads(call["env"]["ODICTO_SHORTCUTS"]), [list(row) for row in dist.SHORTCUTS])

    def test_nothing_runs_when_the_late_shortcut_is_there_or_there_is_no_odicto_folder(self):
        seen, patched = self.fake_powershell()
        with patched:
            self.assertEqual(self.manager.refresh_menu(self.programs), [])  # no Odicto folder in the Start Menu
            self.three_placeholders()
            (self.folder / "Remove Odicto.lnk").write_bytes(b"placeholder")
            self.assertEqual(self.manager.refresh_menu(self.programs), [])  # all four are there
        self.assertEqual(seen, [])

    def test_a_home_without_its_launcher_gets_nothing(self):
        self.three_placeholders()
        self.launcher.unlink()
        seen, patched = self.fake_powershell()
        with patched:
            self.assertEqual(self.manager.refresh_menu(self.programs), [])
        self.assertEqual(seen, [])

    def test_a_failed_script_raises_for_the_update_command_to_print_one_line(self):
        self.three_placeholders()
        _, patched = self.fake_powershell(output="", code=1, error="first\nthe real reason\n")
        with patched, self.assertRaises(dist.DistributionError) as caught:
            self.manager.refresh_menu(self.programs)
        self.assertIn("the real reason", str(caught.exception))

    def test_the_script_adds_nothing_unless_the_other_shortcuts_are_this_homes(self):
        script = dist.ADD_SHORTCUT_SCRIPT
        self.assertEqual(script.count("exit 0"), 4)  # no Programs, no Odicto folder, a foreign shortcut, a missing one
        self.assertIn("$late -contains $name", script)
        self.assertIn("[IO.Path]::GetFullPath($env:ODICTO_LAUNCHER)", script)
        self.assertNotIn("Remove-Item", script)

    def start(self, **patched):
        """A successful `odicto start`, with everything that would launch something replaced."""
        self.manager.active = lambda: {"directory": "fixture", "version": "v1.0.0"}
        self.manager.release_path = lambda record: self.home / "releases" / "fixture"
        output = io.StringIO()
        with patch.object(self.manager, "initialize"), patch.object(self.manager, "_migration_guard"), \
                patch.object(self.manager, "admin_enabled", return_value=False), \
                patch.object(self.manager, "_launch", return_value=0), \
                patch.object(self.manager, "_tidy_releases"), patch.object(self.manager, "_refresh_launchers"), \
                patch.object(self.manager, "support", return_value=type("Support", (), {"elevated": staticmethod(lambda d: False)})), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = self.manager.run(["start"], automatic_updates=False)
        return result, output.getvalue()

    def test_a_successful_start_adds_the_shortcut_and_prints_one_line(self):
        self.three_placeholders()
        seen, patched = self.fake_powershell()
        with patched:
            result, output = self.start()
        self.assertEqual((result, output), (0, "Start Menu: Remove Odicto\n"))
        self.assertEqual(len(seen), 1)

    def test_a_script_that_fails_never_fails_the_start(self):
        self.three_placeholders()
        _, patched = self.fake_powershell(output="", code=1, error="access denied")
        with patched:
            result, output = self.start()
        self.assertEqual((result, output), (0, ""))
        self.assertIn("were not reconciled", self.manager.update_log.read_text(encoding="utf-8"))

    def test_a_script_that_hangs_never_fails_the_start(self):
        self.three_placeholders()

        def hang(command, **options):
            raise subprocess.TimeoutExpired(command, 60)

        with patch.object(dist.subprocess, "run", hang):
            result, _ = self.start()
        self.assertEqual(result, 0)

    def test_the_update_command_prints_the_new_shortcut(self):
        self.three_placeholders()
        seen, patched = self.fake_powershell()
        with patched:
            self.assertEqual(self.manager.refresh_menu(self.programs), ["Start Menu: Remove Odicto"])

    def test_real_powershell_adds_only_the_missing_shortcut_and_only_beside_this_homes_others(self):
        if shutil.which("powershell.exe") is None:
            # No PowerShell: not Windows. The fake-PowerShell tests above hold the contract.
            self.assertNotEqual(os.name, "nt")
            return
        other = self.root / "source checkout" / "start_dictation.bat"
        other.parent.mkdir()
        other.write_text("@exit /b 0\r\n", encoding="ascii")
        self.assertEqual(self.manager.refresh_menu(self.programs), [], "no Start Menu folder: nothing is made")
        self.assertFalse(self.programs.exists())
        self.manager.install_shortcuts(self.programs)
        remove = self.folder / "Remove Odicto.lnk"
        remove.unlink()
        # An older home: the three shortcuts exist and run this launcher. The fourth is added.
        self.assertEqual(self.manager.refresh_menu(self.programs), ["Start Menu: Remove Odicto"])
        self.assertTrue(remove.is_file())
        self.assertEqual(self.manager.refresh_menu(self.programs), [], "the second start does nothing")
        # A shortcut that runs another program: nothing is added beside it.
        remove.unlink()
        setup = self.folder / "Odicto Setup.lnk"
        setup.unlink()
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($env:T_PATH);"
                        "$l.TargetPath = $env:T_TARGET; $l.Save()"],
                       env=dict(os.environ, T_PATH=str(setup), T_TARGET=str(other)), check=True, timeout=60)
        self.assertEqual(self.manager.refresh_menu(self.programs), [])
        self.assertFalse(remove.exists())
        # Another missing shortcut is not "the late one": the user removed it, and it stays removed.
        setup.unlink()
        self.manager.install_shortcuts(self.programs)
        remove.unlink()
        (self.folder / "Stop Odicto.lnk").unlink()
        self.assertEqual(self.manager.refresh_menu(self.programs), [])
        self.assertFalse(remove.exists())
        self.assertFalse((self.folder / "Stop Odicto.lnk").exists())


if __name__ == "__main__":
    unittest.main()
