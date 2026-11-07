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
from unittest.mock import patch

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


def _powershell(script, **values):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          env=dict(os.environ, **values), capture_output=True, text=True, timeout=60, check=True)


def _make_links(*pairs):
    """Make every (link, target) shortcut with one PowerShell process: each start costs seconds."""
    for path, _ in pairs:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    _powershell("$s = New-Object -ComObject WScript.Shell;"
                "foreach ($row in $env:T_LINKS.Split([char]10)) { $p = $row.Split([char]9);"
                "$l = $s.CreateShortcut($p[0]); $l.TargetPath = $p[1]; $l.Save() }",
                T_LINKS="\n".join(f"{path}\t{target}" for path, target in pairs))


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

    # The fake bootstrap puts the fake command where the real install puts odicto.cmd.
    INSTALL = ("param([string]$HomePath)\nNew-Item -ItemType Directory -Force -Path $env:T_BIN | Out-Null\n"
               "Copy-Item -LiteralPath $env:T_FAKE -Destination (Join-Path $env:T_BIN 'odicto.cmd')\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "O'Brien 100% (Downloads)"
        self.folder.mkdir()
        self.local = self.root / "local app data"
        self.roaming = self.root / "roaming app data"
        self.menu = self.roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        self.bin = self.local / "Odicto" / "bin"
        self.fake = self.root / "fake-odicto.cmd"
        self.calls = self.root / "calls.txt"
        self.fake_odicto()

    def fake_odicto(self, failing=""):
        lines = ["@echo off", '>>"%T_CALLS%" echo %*']
        if failing:
            lines.append(f'if "%1"=="{failing}" exit /b 3')
        lines.append("exit /b 0")
        self.fake.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")

    def managed_links(self, *extra):
        _make_links(*[(self.menu / "Odicto" / (name + ".lnk"), self.bin / "odicto.cmd")
                      for name in ("Odicto", "Odicto Setup", "Stop Odicto")], *extra)

    def run_installer(self, bootstrap=None):
        self.calls.unlink(missing_ok=True)
        installer = self.folder / "Install-Odicto.cmd"
        installer.write_bytes(builder.build(self.INSTALL if bootstrap is None else bootstrap))
        environment = dict(os.environ, LOCALAPPDATA=str(self.local), APPDATA=str(self.roaming),
                           T_BIN=str(self.bin), T_FAKE=str(self.fake), T_CALLS=str(self.calls))
        done = subprocess.run(["cmd.exe", "/d", "/c", str(installer)], env=environment, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        calls = self.calls.read_text(encoding="ascii").split() if self.calls.exists() else []
        return done, calls

    def test_new_install_turns_on_autostart_then_starts_then_opens_setup(self):
        self.managed_links()
        done, calls = self.run_installer("#Requires -Version 5.1\n" + self.INSTALL + "Write-Host 'FAKE BOOTSTRAP'\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("FAKE BOOTSTRAP", done.stdout)
        self.assertEqual(calls, ["autostart", "start", "setup"])
        self.assertIn("It starts by itself when you sign in.", done.stdout)
        self.assertIn("Your browser shows Odicto Setup.", done.stdout)
        self.assertIn("Step 3 of 3", done.stdout)
        self.assertIn("Odicto is ready.", done.stdout)
        self.assertIn("Try it now:", done.stdout)
        self.assertIn("are in the Start Menu", done.stdout)

    def test_repair_keeps_the_sign_in_choice(self):
        self.managed_links()
        self.bin.mkdir(parents=True)
        (self.bin / "odicto.cmd").write_bytes(self.fake.read_bytes())
        (self.local / "Odicto" / "autostart.json").write_text("{}", encoding="ascii")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(calls, ["start", "setup"])
        self.assertNotIn("sign in", done.stdout)

    def test_a_repair_without_a_receipt_keeps_sign_in_off(self):
        self.managed_links()
        self.bin.mkdir(parents=True)
        (self.bin / "odicto.cmd").write_bytes(self.fake.read_bytes())
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(calls, ["start", "setup"])

    def test_source_shortcuts_after_a_finished_migration_do_not_block(self):
        other = self.root / "checkout" / "start_dictation.bat"
        other.parent.mkdir()
        other.write_text("@exit /b 0\r\n", encoding="ascii")
        self.managed_links((self.menu / "Startup" / "Odicto.lnk", other))
        (self.local / "Odicto").mkdir(parents=True)
        receipt = self.local / "Odicto" / "migration.json"
        receipt.write_text('{"state": "complete"}', encoding="ascii")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(calls, ["start", "setup"])
        receipt.write_text('{"state": "copying"}', encoding="ascii")
        done, calls = self.run_installer()
        self.assertEqual(calls, [])

    def test_a_source_checkout_shortcut_stops_before_anything_starts(self):
        other = self.root / "checkout" / "start_dictation.bat"
        other.parent.mkdir()
        other.write_text("@exit /b 0\r\n", encoding="ascii")
        for link in (self.menu / "Odicto" / "Odicto.lnk", self.menu / "Startup" / "Odicto.lnk"):
            with self.subTest(link=link.parent.name):
                self.managed_links((link, other))
                done, calls = self.run_installer()
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(calls, [])
                self.assertIn("another copy of Odicto is on this computer", done.stdout)
                link.unlink()

    def test_migrated_source_items_are_not_reported_missing(self):
        (self.local / "Odicto").mkdir(parents=True)
        (self.local / "Odicto" / "migration.json").write_text('{"state": "complete"}', encoding="ascii")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("hands over to this copy", done.stdout)

    def test_a_pending_sign_in_receipt_is_tried_again(self):
        self.managed_links()
        self.bin.mkdir(parents=True)
        (self.bin / "odicto.cmd").write_bytes(self.fake.read_bytes())
        (self.local / "Odicto" / "autostart.json").write_text('{"pending": true}', encoding="ascii")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(calls, ["autostart", "start", "setup"])

    def test_a_failed_repair_says_to_stop_odicto_first(self):
        self.bin.mkdir(parents=True)
        (self.bin / "odicto.cmd").write_bytes(self.fake.read_bytes())
        done, calls = self.run_installer("param([string]$HomePath)\nthrow 'fixture busy'\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("If Odicto is open, stop it first", done.stdout)
        self.assertEqual(calls, [])

    def test_missing_start_menu_items_are_reported(self):
        _make_links(*[(self.menu / "Odicto" / (name + ".lnk"), self.bin / "odicto.cmd")
                      for name in ("Odicto Setup", "Stop Odicto")])
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(calls, ["autostart", "start", "setup"])
        self.assertIn("The Start Menu items were not made", done.stdout)
        self.assertNotIn("are in the Start Menu", done.stdout)

    def test_failed_bootstrap_runs_no_odicto_command(self):
        done, calls = self.run_installer("param([string]$HomePath)\nthrow 'fixture failure'\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Odicto was not installed: fixture failure", done.stdout)
        self.assertEqual(calls, [])

    def test_failed_start_exits_non_zero_and_skips_setup(self):
        self.managed_links()
        self.fake_odicto(failing="start")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(calls, ["autostart", "start"])
        self.assertIn("Odicto did not start", done.stdout)

    def test_failed_autostart_still_starts_and_says_so(self):
        self.managed_links()
        self.fake_odicto(failing="autostart")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(calls, ["autostart", "start", "setup"])
        self.assertIn("It does not start by itself when you sign in.", done.stdout)
        self.assertNotIn("It starts by itself", done.stdout)

    def test_failed_setup_says_so(self):
        self.managed_links()
        self.fake_odicto(failing="setup")
        done, calls = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertIn("Odicto Setup did not open.", done.stdout)
        self.assertIn("Some steps did not finish.", done.stdout)
        self.assertNotIn("Odicto is ready.", done.stdout)
        self.assertNotIn("Your browser shows", done.stdout)

    def test_real_bootstrap_payload_parses(self):
        installer = self.folder / "Install-Odicto.cmd"
        installer.write_bytes(builder.build((DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")))
        _powershell("$t = [IO.File]::ReadAllText($env:T_FILE); $m = '#' + 'ODICTO-POWERSHELL'; $e = $null;"
                    "[void][Management.Automation.Language.Parser]::ParseInput($t.Substring($t.IndexOf($m)), [ref]$null, [ref]$e);"
                    "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }", T_FILE=str(installer))


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

    def read(self):
        """All three shortcuts, read with one PowerShell process."""
        done = _powershell("$s = New-Object -ComObject WScript.Shell; $o = @{};"
                           "foreach ($n in $env:T_NAMES.Split([char]10)) {"
                           "$l = $s.CreateShortcut((Join-Path $env:T_DIR ($n + '.lnk')));"
                           "$o[$n] = @{target=$l.TargetPath; arguments=$l.Arguments; directory=$l.WorkingDirectory;"
                           "style=$l.WindowStyle; icon=$l.IconLocation} }; $o | ConvertTo-Json -Compress",
                           T_DIR=str(self.folder), T_NAMES="\n".join(name for name, _, _ in dist.SHORTCUTS))
        return json.loads(done.stdout)

    def with_release_icon(self):
        release = self.manager.home / "releases" / "fixture"
        (release / "assets").mkdir(parents=True)
        (release / "assets" / "odicto.ico").write_bytes(b"release icon")
        for name, value in (("active", {"directory": "fixture"}), ("release_path", release)):
            patcher = patch.object(self.manager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_creates_three_shortcuts_that_run_the_stable_command(self):
        lines = self.manager.install_shortcuts(self.programs)
        self.assertEqual(lines, ["Start Menu: Odicto", "Start Menu: Odicto Setup", "Start Menu: Stop Odicto"])
        links = self.read()
        for name, command, _ in dist.SHORTCUTS:
            with self.subTest(name=name):
                link = links[name]
                del link["icon"]
                self.assertEqual(link, {"target": str(self.launcher), "arguments": command,
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
        _make_links((self.folder / "Odicto.lnk", other))
        lines = self.manager.install_shortcuts(self.programs)
        self.assertEqual(lines[0], "Kept Odicto (it runs another program)")
        links = self.read()
        self.assertEqual(links["Odicto"]["target"], str(other))
        self.assertEqual(links["Stop Odicto"]["target"], str(self.launcher))

    def test_icon_is_copied_into_the_home_and_used(self):
        self.with_release_icon()
        self.manager.install_shortcuts(self.programs)
        icon = self.manager.home / "odicto.ico"
        self.assertEqual(icon.read_bytes(), b"release icon")
        self.assertEqual(self.read()["Odicto"]["icon"], str(icon) + ",0")

    def test_an_icon_file_odicto_did_not_write_is_kept_and_not_used(self):
        self.with_release_icon()
        icon = self.manager.home / "odicto.ico"
        icon.write_bytes(b"the user's own file")
        self.manager.install_shortcuts(self.programs)
        self.assertEqual(icon.read_bytes(), b"the user's own file")
        self.assertNotIn("odicto.ico", self.read()["Odicto"]["icon"])

    def test_missing_launcher_creates_nothing(self):
        self.launcher.unlink()
        with self.assertRaises(dist.DistributionError):
            self.manager.install_shortcuts(self.programs)
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
