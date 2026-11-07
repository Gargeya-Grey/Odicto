"""The no-terminal Windows path: Start Menu shortcuts and the double-click installer."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = ROOT / "tools" / "distribution"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("odicto_easy_installer_tested", DISTRIBUTION / "build_easy_installer.py")
dist = _load("odicto_distribution_shortcuts_tested", DISTRIBUTION / "manager.py")
WINDOWS = sys.platform == "win32"


class TestEasyInstallerBuild(unittest.TestCase):
    def setUp(self):
        self.bootstrap = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        self.built = builder.build(self.bootstrap).decode("ascii")

    def test_embeds_the_bootstrap_unchanged_except_its_requires_line(self):
        body = "\r\n".join(line for line in self.bootstrap.splitlines() if not line.startswith("#Requires"))
        self.assertEqual(self.built.count(body), 1)
        self.assertNotIn("#Requires", self.built)

    def test_crlf_lines_and_one_marker_line_after_the_header(self):
        self.assertNotIn("\n", self.built.replace("\r\n", ""))
        lines = self.built.split("\r\n")
        self.assertEqual(lines[0], "@echo off")
        self.assertEqual(lines.count(builder.MARKER), 1)
        self.assertEqual(self.built.count(builder.MARKER), 1)
        self.assertLess(lines.index("exit /b %ODICTO_STATUS%"), lines.index(builder.MARKER))

    def test_steps_after_install_are_autostart_start_setup_in_order(self):
        tail = self.built.split("$odicto = ", 1)[1]
        positions = [tail.index("& $odicto " + step) for step in ("autostart", "start", "setup")]
        self.assertEqual(positions, sorted(positions))

    def test_cli_writes_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "Install-Odicto.cmd"
            self.assertEqual(builder.main([str(target)]), 0)
            self.assertEqual(target.read_bytes(), self.built.encode("ascii"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main([]), 2)


@unittest.skipUnless(WINDOWS, "cmd.exe and Windows PowerShell")
class TestWindowsEasyInstallerRuns(unittest.TestCase):
    """Runs the built file for real, with a fake bootstrap and a fake odicto command."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.folder = root / "O'Brien 100% (Downloads)"
        self.folder.mkdir()
        self.local = root / "local app data"
        self.bin = self.local / "Odicto" / "bin"
        self.calls = root / "calls.txt"

    def fake_odicto(self, failing=""):
        self.bin.mkdir(parents=True)
        lines = ["@echo off", f'>>"{self.calls}" echo %*']
        if failing:
            lines.append(f'if "%1"=="{failing}" exit /b 3')
        lines.append("exit /b 0")
        (self.bin / "odicto.cmd").write_text("\r\n".join(lines) + "\r\n", encoding="ascii")

    def run_installer(self, bootstrap):
        installer = self.folder / "Install-Odicto.cmd"
        installer.write_bytes(builder.build(bootstrap))
        environment = dict(os.environ, LOCALAPPDATA=str(self.local))
        done = subprocess.run(["cmd.exe", "/d", "/c", str(installer)], env=environment, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        calls = self.calls.read_text(encoding="ascii").split() if self.calls.exists() else []
        return done, calls

    def test_success_turns_on_autostart_then_starts_then_opens_setup(self):
        self.fake_odicto()
        done, calls = self.run_installer("#Requires -Version 5.1\nparam([string]$HomePath)\nWrite-Host 'FAKE BOOTSTRAP'\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("FAKE BOOTSTRAP", done.stdout)
        self.assertEqual(calls, ["autostart", "start", "setup"])
        self.assertIn("Odicto is running", done.stdout)

    def test_failed_bootstrap_runs_no_odicto_command(self):
        self.fake_odicto()
        done, calls = self.run_installer("param([string]$HomePath)\nthrow 'fixture failure'\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Odicto was not installed: fixture failure", done.stdout)
        self.assertEqual(calls, [])

    def test_failed_start_exits_non_zero_and_skips_setup(self):
        self.fake_odicto(failing="start")
        done, calls = self.run_installer("param([string]$HomePath)\n")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(calls, ["autostart", "start"])
        self.assertIn("Odicto did not start", done.stdout)

    def test_failed_autostart_still_starts(self):
        self.fake_odicto(failing="autostart")
        done, calls = self.run_installer("param([string]$HomePath)\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(calls, ["autostart", "start", "setup"])

    def test_real_bootstrap_payload_parses(self):
        installer = self.folder / "Install-Odicto.cmd"
        installer.write_bytes(builder.build((DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")))
        script = ("$t = [IO.File]::ReadAllText($env:T_FILE); $m = '#' + 'ODICTO-POWERSHELL'; $e = $null;"
                  "[void][Management.Automation.Language.Parser]::ParseInput($t.Substring($t.IndexOf($m)), [ref]$null, [ref]$e);"
                  "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              env=dict(os.environ, T_FILE=str(installer)), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


@unittest.skipUnless(WINDOWS, "Windows Start Menu shortcuts")
class TestWindowsStartMenuShortcuts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.manager = dist.Manager(root / "Odicto home 100%")
        (self.manager.home / "bin").mkdir(parents=True)
        self.launcher = self.manager.home / "bin" / "odicto.cmd"
        self.launcher.write_text("@exit /b 0\r\n", encoding="ascii")
        self.programs = root / "Programs"
        self.folder = self.programs / "Odicto"

    def read(self, name):
        script = ("$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:T_LINK);"
                  "@{target=$l.TargetPath; arguments=$l.Arguments; directory=$l.WorkingDirectory;"
                  "style=$l.WindowStyle} | ConvertTo-Json -Compress")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              env=dict(os.environ, T_LINK=str(self.folder / (name + ".lnk"))),
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_creates_three_shortcuts_that_run_the_stable_command(self):
        lines = self.manager.install_shortcuts(self.programs)
        self.assertEqual(lines, ["Start Menu: Odicto", "Start Menu: Odicto Setup", "Start Menu: Stop Odicto"])
        for name, command, _ in dist.SHORTCUTS:
            with self.subTest(name=name):
                self.assertEqual(self.read(name), {"target": str(self.launcher), "arguments": command,
                                                   "directory": str(self.manager.home), "style": 7})

    def test_second_run_refreshes_the_same_shortcuts(self):
        self.manager.install_shortcuts(self.programs)
        lines = self.manager.install_shortcuts(self.programs)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("Start Menu: ") for line in lines))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         ["Odicto Setup.lnk", "Odicto.lnk", "Stop Odicto.lnk"])

    def test_a_shortcut_that_runs_another_program_is_kept(self):
        other = self.programs.parent / "source checkout" / "start_dictation.bat"
        other.parent.mkdir()
        other.write_text("@exit /b 0\r\n", encoding="ascii")
        self.folder.mkdir(parents=True)
        script = ("$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:T_LINK);"
                  "$l.TargetPath = $env:T_TARGET; $l.Save()")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=dict(os.environ, T_LINK=str(self.folder / "Odicto.lnk"), T_TARGET=str(other)),
                       capture_output=True, timeout=60, check=True)
        lines = self.manager.install_shortcuts(self.programs)
        self.assertEqual(lines[0], "Kept Odicto (it runs another program)")
        self.assertEqual(self.read("Odicto")["target"], str(other))
        self.assertEqual(self.read("Stop Odicto")["target"], str(self.launcher))

    def test_missing_launcher_creates_nothing(self):
        self.launcher.unlink()
        with self.assertRaises(dist.DistributionError):
            self.manager.install_shortcuts(self.programs)
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
