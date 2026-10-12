"""Launcher regressions: three root entry points per platform, admin-mode start/stop wiring.

Static checks read the batch files, shell wrappers and installer scripts. The Windows runs use
scratch install folders without a .venv, so they stop at the venv check: they never start, stop
or signal an Odicto, never reach UAC, never create a Start Menu entry and never read .env.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ROOT / "scripts" / "windows"
# Built from parts so this file itself never contains the retired name.
RETIRED = "start_dictation" + "_admin"


def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class TestRootEntryPoints(unittest.TestCase):
    def test_windows_root_has_exactly_the_three_launchers(self):
        self.assertEqual({"setup.bat", "start_dictation.bat", "stop_dictation.bat"},
                         {p.name for p in ROOT.glob("*.bat")})

    def test_posix_root_has_the_three_launchers_and_the_installer(self):
        self.assertEqual({"install.sh", "setup.sh", "start_dictation.sh", "stop_dictation.sh"},
                         {p.name for p in ROOT.glob("*.sh")})

    def test_each_windows_wrapper_calls_its_implementation(self):
        for name in ("start_dictation.bat", "setup.bat", "stop_dictation.bat"):
            with self.subTest(wrapper=name):
                text = read(name)
                self.assertIn('call "%~dp0scripts\\windows\\' + name + '"', text)
                self.assertTrue((WINDOWS / name).is_file())
                self.assertLess(len(text.splitlines()), 30, "root launchers stay thin")

    def test_each_posix_wrapper_runs_its_implementation(self):
        for name in ("start_dictation.sh", "setup.sh", "stop_dictation.sh"):
            with self.subTest(wrapper=name):
                text = read(name)
                self.assertTrue(text.startswith("#!/usr/bin/env bash"))
                self.assertIn('exec bash "$REPO_DIR/scripts/posix/%s" "$@"' % name, text)
                self.assertTrue((ROOT / "scripts" / "posix" / name).is_file())

    def test_new_posix_wrappers_are_executable_in_git(self):
        try:
            listing = subprocess.run(["git", "ls-files", "-s", "--", "setup.sh", "stop_dictation.sh"],
                                     cwd=ROOT, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        for line in listing.splitlines():
            with self.subTest(entry=line):
                self.assertTrue(line.startswith("100755"), line)

    def test_the_retired_admin_launcher_is_gone_everywhere(self):
        self.assertFalse((ROOT / (RETIRED + ".bat")).exists())
        self.assertFalse((WINDOWS / (RETIRED + ".ps1")).exists())
        self.assertTrue((WINDOWS / "admin_mode.ps1").is_file())
        skipped = {".git", ".venv", "__pycache__", ".claude", "node_modules"}
        hits = []
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in skipped and not d.startswith(".venv")]
            for name in files:
                path = Path(folder) / name
                if path.suffix in {".png", ".ico", ".pyc", ".lnk", ".wav", ".log"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if RETIRED in text:
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual([], hits)


class TestRootStartBat(unittest.TestCase):
    text = read("start_dictation.bat")

    def test_no_argument_closes_after_three_seconds_on_success_and_pauses_on_failure(self):
        self.assertIn("ping -n 4 127.0.0.1", self.text)
        self.assertIn("3 seconds", self.text)
        started, passthrough = "\n:started\n", "\n:passthrough\n"
        ok = self.text[self.text.index(started):self.text.index(passthrough)]
        self.assertIn("ping -n 4", ok)
        self.assertNotIn("pause", ok)
        failed = self.text[:self.text.index(started)]
        self.assertIn("\npause\n", failed)
        self.assertIn('if "%ODICTO_EXIT%"=="0" goto :started', failed)

    def test_arguments_pass_straight_through_without_waiting(self):
        self.assertIn('if not "%~1"=="" goto :passthrough', self.text)
        tail = self.text[self.text.index("\n:passthrough\n"):]
        self.assertIn('call "%~dp0scripts\\windows\\start_dictation.bat" %*', tail)
        self.assertIn("exit /b %ERRORLEVEL%", tail)
        self.assertNotIn("pause", tail)

    def test_it_no_longer_restarts_on_a_double_click(self):
        commands = [line for line in self.text.splitlines() if not line.upper().startswith("REM")]
        self.assertNotIn("/restart", "\n".join(commands))

    def test_the_inner_script_does_not_pause_a_second_time(self):
        inner = read("scripts", "windows", "start_dictation.bat")
        self.assertIn('set "ODICTO_NO_PAUSE=1"', self.text)
        self.assertIn("if defined ODICTO_NO_PAUSE exit /b 0", inner)
        self.assertEqual(1, len(re.findall(r"^pause\b", inner, re.M)))


class TestStopAndSetupBat(unittest.TestCase):
    def test_root_stop_closes_on_success_and_pauses_on_failure(self):
        text = read("stop_dictation.bat")
        stopped, passthrough = "\n:stopped\n", "\n:passthrough\n"
        ok = text[text.index(stopped):text.index(passthrough)]
        self.assertIn("ping -n 4 127.0.0.1", ok)
        self.assertNotIn("pause", ok)
        self.assertIn("\npause\n", text[:text.index(stopped)])

    def test_setup_starts_pythonw_through_start_and_exits(self):
        text = read("scripts", "windows", "setup.bat")
        self.assertIn('start "" "%ODICTO_ROOT%\\.venv\\Scripts\\pythonw.exe" "%ODICTO_ROOT%\\odicto.py" setup', text)
        self.assertIn("exit /b 0", text)
        self.assertNotIn("python.exe", text)

    def test_setup_reports_a_missing_venv_and_waits_before_the_launch(self):
        text = read("scripts", "windows", "setup.bat")
        self.assertLess(text.index("not found"), text.index("pause"))
        self.assertLess(text.index("pause"), text.index('start ""'))
        self.assertIn("exit /b 1", text)


class TestStartBatAdminWiring(unittest.TestCase):
    text = read("scripts", "windows", "start_dictation.bat")

    def test_admin_branch_is_driven_by_the_marker_and_comes_after_the_venv_check(self):
        self.assertLess(self.text.index("if defined ODICTO_VENV_PROBLEM goto :venv_problem"),
                        self.text.index(".odicto-admin-enabled"))

    def test_start_min_and_nostartup_leave_a_running_owner_alone(self):
        for argument in ("", "/min", "/nostartup"):
            with self.subTest(argument=argument):
                pattern = ('if "%~1"=="" goto :admin_start' if not argument
                           else 'if /I "%%~1"=="%s" goto :admin_start' % argument)
                self.assertIn(pattern, self.text)
        start = self.text[self.text.index("\n:admin_start\n"):self.text.index("\n:admin_restart\n")]
        self.assertIn('odicto.py" start', start)
        self.assertNotIn("restart", start)

    def test_restart_goes_through_odicto_restart(self):
        self.assertIn('if /I "%~1"=="/restart" goto :admin_restart', self.text)
        section = self.text[self.text.index("\n:admin_restart\n"):self.text.index("\n:venv_problem\n")]
        self.assertIn('odicto.py" restart', section)

    def test_no_launcher_starts_the_admin_helper_or_uac_directly(self):
        for path in [ROOT / "start_dictation.bat", ROOT / "setup.bat", ROOT / "stop_dictation.bat",
                     WINDOWS / "start_dictation.bat", WINDOWS / "stop_dictation.bat", WINDOWS / "setup.bat",
                     WINDOWS / "run_debug.bat"]:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8").lower()
                self.assertNotIn("admin_mode.ps1", text)
                self.assertNotIn("runas", text)
                self.assertNotIn("-verb", text)
                self.assertNotIn("powershell.exe", text)

    def test_stop_goes_through_odicto_stop_which_quits_by_event(self):
        text = read("scripts", "windows", "stop_dictation.bat")
        self.assertIn('odicto.py" stop', text)


@unittest.skipUnless(sys.platform == "win32", "runs the Windows batch files")
class TestBatFilesRun(unittest.TestCase):
    """The batch files run in a scratch install without a .venv: they stop at the venv check."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="odicto launch test ")
        self.addCleanup(tmp.cleanup)
        self.install = Path(tmp.name) / "install"
        (self.install / "scripts" / "windows").mkdir(parents=True)
        for name in ("start_dictation.bat", "setup.bat", "stop_dictation.bat"):
            shutil.copy(ROOT / name, self.install / name)
            shutil.copy(WINDOWS / name, self.install / "scripts" / "windows" / name)

    def run_bat(self, *command):
        done = subprocess.run([str(self.install / command[0]), *command[1:]], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, cwd=self.install.parent)
        return done.returncode, done.stdout + done.stderr

    def test_setup_without_a_venv_shows_the_error_and_fails(self):
        code, text = self.run_bat("setup.bat")
        self.assertEqual(1, code, text)
        self.assertIn("pythonw.exe not found", text)
        self.assertIn("install.ps1", text)

    def test_root_start_without_a_venv_fails_visibly_and_does_not_close_itself(self):
        code, text = self.run_bat("start_dictation.bat")
        self.assertEqual(1, code, text)
        self.assertIn("pythonw.exe not found", text)
        self.assertIn("did not start", text)
        self.assertNotIn("closes in 3 seconds", text)

    def test_root_start_with_an_argument_passes_it_on(self):
        code, text = self.run_bat("start_dictation.bat", "/nostartup")
        self.assertEqual(1, code, text)
        self.assertIn("pythonw.exe not found", text)
        self.assertNotIn("did not start", text)

    def test_admin_marker_without_a_venv_still_stops_at_the_venv_check(self):
        (self.install / ".odicto-admin-enabled").write_text("enabled\n")
        code, text = self.run_bat("start_dictation.bat", "/min")
        self.assertEqual(1, code, text)
        self.assertIn("pythonw.exe not found", text)


class TestInstallerShortcuts(unittest.TestCase):
    def test_no_shortcuts_switch_exists_and_the_function_runs_after_the_venv_only_exit(self):
        text = read("install.ps1")
        self.assertIn("[switch]$NoShortcuts", text)
        self.assertIn("if ($NoShortcuts)", text)
        self.assertIn("New-OdictoStartMenuShortcuts -Root $PSScriptRoot", text)
        self.assertLess(text.index("if ($VenvOnly)"), text.index("New-OdictoStartMenuShortcuts -Root"))

    def test_the_three_shortcuts_target_the_root_launchers_with_the_logo(self):
        helpers = read("scripts", "windows", "install_helpers.ps1")
        body = helpers[helpers.index("function New-OdictoStartMenuShortcuts"):helpers.index("function Get-OdictoProcessSnapshot")]
        for name, target in (("Odicto", "start_dictation.bat"), ("Odicto Setup", "setup.bat"),
                             ("Stop Odicto", "stop_dictation.bat")):
            self.assertRegex(body, r"Name = '%s';\s+Target = '%s'" % (re.escape(name), re.escape(target)))
        self.assertIn("assets\\odicto.ico", body)
        self.assertIn("IconLocation", body)
        self.assertIn("WorkingDirectory = $rootFull", body)
        self.assertIn("[Environment]::GetFolderPath('Programs')", body)

    def test_the_logo_ships(self):
        self.assertTrue((ROOT / "assets" / "odicto.ico").is_file())


if __name__ == "__main__":
    unittest.main()
