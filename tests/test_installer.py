"""Installer regressions: Python selection, broken .venv repair and the Windows launcher gate.

These tests run the real installer pieces (install.ps1 / install.sh, their helper files and
the Windows launcher) against throwaway install folders. Interpreters are probed by running
them, as the installers do. Windows tests exist on Windows; the shell tests exist on macOS
and Linux, so no test skips on any platform. They never read .env or prompt.txt of the
real install, never start Odicto and never use the network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPERS_PS1 = ROOT / "scripts" / "windows" / "install_helpers.ps1"
HELPERS_SH = ROOT / "scripts" / "posix" / "install_python.sh"
SECRET_ENV = b"GEMINI_API_KEY=fixture-not-a-real-key\r\n"
PROMPT = b"fixture prompt\n"

# The process scan (Get-OdictoProcessSnapshot) is the one boundary these tests control. The real
# one lists every process on the machine, so a running Odicto (an elevated pythonw.exe whose path
# a normal process cannot read) would make the venv move-aside check refuse for a reason that has
# nothing to do with the fixture. The helper logic above the boundary stays real.
#   * no processes: the machine is quiet.
#   * only the PIDs a test started: real CIM data for those processes and nothing else.
SCAN_NO_PROCESSES = "function Get-OdictoProcessSnapshot { @() }\n"
SCAN_ONLY_TEST_PIDS = r"""
function Get-OdictoProcessSnapshot {
    foreach ($id in @($env:ODICTO_TEST_PIDS -split ',' | Where-Object { $_ })) {
        Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$id" -ErrorAction Stop
    }
}
"""


def _supported(version=None):
    major, minor = (version or sys.version_info)[:2]
    return major == 3 and 10 <= minor <= 12


def _python_version_text():
    return "%d.%d.%d" % sys.version_info[:3]


def _vanish_home(venv: Path):
    """Point pyvenv.cfg at a base Python that no longer exists (the deleted-Miniconda case)."""
    cfg = venv / "pyvenv.cfg"
    lines = []
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.split("=")[0].strip() == "home":
            line = "home = " + str(venv.parent / "removed-base-python")
        lines.append(line)
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_venv(path: Path):
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, capture_output=True, timeout=120)


class _InstallFolderCase(unittest.TestCase):
    """A scratch install folder with private-file sentinels that must survive any repair."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="odicto-inst-test ")
        self.addCleanup(self._tmp.cleanup)
        # The installers use the canonical spelling of the install folder (Windows PowerShell's
        # GetFullPath expands 8.3 names; install.sh uses cd -P). CI temp folders have a second
        # spelling (C:\Users\RUNNER~1, /var -> /private/var), so start from the canonical one.
        self.work = Path(os.path.realpath(self._tmp.name))
        self.install = self.work / "install"
        self.install.mkdir()
        (self.install / ".env").write_bytes(SECRET_ENV)
        (self.install / "prompt.txt").write_bytes(PROMPT)

    def assert_private_files_untouched(self):
        self.assertEqual(SECRET_ENV, (self.install / ".env").read_bytes())
        self.assertEqual(PROMPT, (self.install / "prompt.txt").read_bytes())

    def backups(self):
        return sorted(p for p in self.install.iterdir() if p.name.startswith(".venv-backup-"))


if sys.platform == "win32":

    def _ps(script, **env):
        """Run PowerShell with the helpers dot-sourced; returns (exit code, output text)."""
        full = ("$ErrorActionPreference='Stop'; . $env:ODICTO_HELPERS; "
                "try { " + script + " } catch { Write-Output ('ERR: ' + $_.Exception.Message); exit 1 }")
        environment = dict(os.environ, ODICTO_HELPERS=str(HELPERS_PS1), **env)
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", full],
            capture_output=True, text=True, env=environment, timeout=180, stdin=subprocess.DEVNULL)
        return done.returncode, (done.stdout + done.stderr).strip()

    def _ps_json(script, **env):
        code, text = _ps(script, **env)
        if code != 0:
            raise AssertionError("PowerShell failed: " + text)
        return json.loads(text.splitlines()[-1])

    class TestWindowsInstaller(_InstallFolderCase):
        @classmethod
        def setUpClass(cls):
            cls._shared = tempfile.TemporaryDirectory(prefix="odicto-venv-template ")
            shared = Path(cls._shared.name)
            cls.healthy_template = shared / "healthy"
            cls.broken_template = shared / "broken"
            _make_venv(cls.healthy_template)
            shutil.copytree(cls.healthy_template, cls.broken_template)
            _vanish_home(cls.broken_template)
            # The premise: the file exists, yet it cannot run.
            cls.broken_runs = subprocess.run(
                [str(cls.broken_template / "Scripts" / "python.exe"), "-c", "pass"],
                capture_output=True, timeout=60).returncode == 0
            cls.healthy_runs = subprocess.run(
                [str(cls.healthy_template / "Scripts" / "python.exe"), "-c", "pass"],
                capture_output=True, timeout=60).returncode == 0

        @classmethod
        def tearDownClass(cls):
            cls._shared.cleanup()

        def setUp(self):
            super().setUp()
            self.assertFalse(self.broken_runs, "fixture premise: a venv without its base Python must not run")
            self.assertTrue(self.healthy_runs, "fixture premise: the healthy venv must run")
            (self.work / "fakes").mkdir()

        def fake_python(self, name, version):
            path = self.work / "fakes" / (name + ".cmd")
            path.write_text("@echo off\r\necho %s\r\n" % version, encoding="ascii")
            return str(path)

        def install_venv(self, kind):
            target = self.install / ".venv"
            shutil.copytree(kind, target)
            return target

        # ---- probing and selection -------------------------------------------------------
        def test_probe_runs_the_interpreter_instead_of_checking_the_file(self):
            broken = self.work / "broken"
            shutil.copytree(self.broken_template, broken)
            probe = _ps_json("Invoke-OdictoPythonProbe -Exe $env:T_EXE | ConvertTo-Json -Compress",
                             T_EXE=str(broken / "Scripts" / "python.exe"))
            self.assertTrue((broken / "Scripts" / "python.exe").exists())
            self.assertFalse(probe["Ok"], probe)
            real = _ps_json("Invoke-OdictoPythonProbe -Exe $env:T_EXE | ConvertTo-Json -Compress",
                            T_EXE=sys.executable)
            self.assertTrue(real["Ok"], real)
            self.assertEqual(_python_version_text(), real["Version"])

        def test_selection_skips_a_newer_default_and_prefers_the_highest_supported(self):
            candidates = "|".join([
                self.fake_python("py313", "3 13 2"),       # the machine default, unsupported
                self.fake_python("py310", "3 10 4"),
                self.fake_python("py311", "3 11 9"),
                str(self.work / "fakes" / "never-installed.exe"),
            ])
            found = _ps_json("$r = Find-OdictoPython -Candidates @($env:T_C -split '\\|'); $r | ConvertTo-Json -Compress",
                             T_C=candidates)
            self.assertEqual("3.11.9", found["Version"])
            self.assertTrue(found["Path"].endswith("py311.cmd"))

        def test_selection_finds_nothing_when_only_unsupported_pythons_exist(self):
            candidates = "|".join([self.fake_python("py313", "3 13 2"), self.fake_python("py39", "3 9 18")])
            code, text = _ps("$r = Find-OdictoPython -Candidates @($env:T_C -split '\\|'); if ($r) { 'FOUND' } else { 'NONE' }",
                             T_C=candidates)
            self.assertEqual((0, "NONE"), (code, text))

        def test_explicit_unsupported_python_is_rejected_without_switching(self):
            installed = self.fake_python("py311", "3 11 9")
            fake313 = self.fake_python("py313", "3 13 2")
            code, text = _ps("Resolve-OdictoPythonChoice -Spec $env:T_SPEC -Candidates @($env:T_C)",
                             T_SPEC=fake313, T_C=installed)
            self.assertEqual(1, code, text)
            self.assertIn("3.13.2", text)
            self.assertIn("3.10, 3.11 or 3.12", text)
            self.assertNotIn("py311", text)
            # A version that is not installed is an error too, not a different version.
            code, text = _ps("Resolve-OdictoPythonChoice -Spec '3.12' -Candidates @($env:T_C)", T_C=installed)
            self.assertEqual(1, code, text)
            self.assertIn("no installed Python 3.12", text)
            code, text = _ps("Resolve-OdictoPythonChoice -Spec '3.13' -Candidates @($env:T_C)", T_C=installed)
            self.assertEqual(1, code, text)
            self.assertIn("not supported", text)

        def test_explicit_version_and_path_choose_exactly_what_was_asked(self):
            fakes = "|".join([self.fake_python("py312", "3 12 1"), self.fake_python("py311", "3 11 9")])
            picked = _ps_json("Resolve-OdictoPythonChoice -Spec '3.11' -Candidates @($env:T_C -split '\\|') | ConvertTo-Json -Compress",
                              T_C=fakes)
            self.assertEqual("3.11.9", picked["Version"])
            by_path = _ps_json("Resolve-OdictoPythonChoice -Spec $env:T_SPEC | ConvertTo-Json -Compress",
                               T_SPEC=self.fake_python("py310", "3 10 4"))
            self.assertEqual("3.10.4", by_path["Version"])
            garbage = self.work / "fakes" / "garbage.exe"
            garbage.write_bytes(b"this is not an executable")
            code, text = _ps("Resolve-OdictoPythonChoice -Spec $env:T_SPEC", T_SPEC=str(garbage))
            self.assertEqual(1, code, text)
            self.assertIn("cannot run", text)

        def test_real_interpreter_is_accepted_only_inside_the_supported_range(self):
            code, text = _ps("(Resolve-OdictoPythonChoice -Spec $env:T_SPEC).Version", T_SPEC=sys.executable)
            if _supported():
                self.assertEqual((0, _python_version_text()), (code, text))
            else:
                self.assertEqual(1, code, text)
                self.assertIn(_python_version_text(), text)

        # ---- venv state -------------------------------------------------------------------
        def test_venv_state_is_decided_by_running_it(self):
            def state(venv):
                return _ps_json("Get-OdictoVenvState -VenvDir $env:T_VENV | ConvertTo-Json -Compress",
                                T_VENV=str(venv))
            self.assertEqual("Missing", state(self.install / ".venv")["State"])
            broken = state(self.install_venv(self.broken_template))
            self.assertEqual("Broken", broken["State"], broken)
            self.assertIn("no longer exists", broken["Reason"])
            shutil.rmtree(self.install / ".venv")
            healthy = state(self.install_venv(self.healthy_template))
            self.assertEqual("Healthy" if _supported() else "Unsupported", healthy["State"], healthy)
            self.assertEqual(_python_version_text(), healthy["Version"])
            shutil.rmtree(self.install / ".venv")
            (self.install / ".venv").write_text("a file, not a venv")
            self.assertEqual("Broken", state(self.install / ".venv")["State"])

        # ---- moving a venv aside -----------------------------------------------------------
        def move(self, root=None, venv=None, pids=()):
            return _ps(SCAN_ONLY_TEST_PIDS + "Move-OdictoVenvAside -Root $env:T_ROOT -VenvDir $env:T_VENV",
                       ODICTO_TEST_PIDS=",".join(str(pid) for pid in pids),
                       T_ROOT=str(root or self.install), T_VENV=str(venv or self.install / ".venv"))

        def test_move_aside_keeps_everything_and_never_reuses_a_name(self):
            moved = []
            for _ in range(2):
                venv = self.install_venv(self.broken_template)
                (venv / "keep.txt").write_text("installed packages stand in here")
                code, text = self.move()
                self.assertEqual(0, code, text)
                moved.append(Path(text.splitlines()[-1]))
                self.assertFalse(venv.exists())
            self.assertNotEqual(moved[0], moved[1])
            for path in moved:
                self.assertTrue((path / "keep.txt").is_file())
                self.assertTrue(path.name.startswith(".venv-backup-"))
                self.assertEqual(self.install, path.parent)
            self.assertEqual(2, len(self.backups()))
            self.assert_private_files_untouched()

        def test_move_aside_refuses_unsafe_targets(self):
            other = self.install / "venv"
            shutil.copytree(self.broken_template, other)
            code, text = self.move(venv=other)
            self.assertEqual(1, code, text)
            self.assertIn("not the .venv", text)
            self.assertTrue((other / "pyvenv.cfg").is_file())
            elsewhere = self.work / "elsewhere" / ".venv"
            shutil.copytree(self.broken_template, elsewhere)
            code, text = self.move(venv=elsewhere)
            self.assertEqual(1, code, text)
            self.assertTrue((elsewhere / "pyvenv.cfg").is_file())
            target = self.work / "link-target"
            target.mkdir()
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.install / ".venv"), str(target)],
                                  capture_output=True, text=True)
            self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            self.addCleanup(lambda: os.path.isdir(self.install / ".venv") and os.rmdir(self.install / ".venv"))
            code, text = self.move()
            self.assertEqual(1, code, text)
            self.assertIn("is a link", text)
            self.assertTrue(target.is_dir())
            self.assertEqual([], self.backups())

        def test_move_aside_refuses_while_a_process_runs_from_the_venv(self):
            venv = self.install_venv(self.broken_template)
            runner = venv / "Scripts" / "odicto-fake-app.exe"
            shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe", runner)
            proc = subprocess.Popen([str(runner), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=0x08000000)
            self.addCleanup(proc.kill)
            code, text = self.move(pids=[proc.pid])
            self.assertEqual(1, code, text)
            self.assertIn("PID %d" % proc.pid, text)
            self.assertIn("Quit Odicto", text)
            self.assertTrue(venv.is_dir())
            proc.kill()
            proc.wait(timeout=30)
            code, text = self.move(pids=[proc.pid])
            self.assertEqual(0, code, text)
            self.assertFalse(venv.exists())

        # ---- a process started through another spelling of the install folder -----------------
        def short_path(self, path):
            """The 8.3 spelling of an existing path. Skips when the volume has no 8.3 names (a Dev
            Drive, or 8.3 creation turned off); tools/run_tests.py allows that skip. CI's temp
            folder (C:\\Users\\RUNNER~1) always has them, so there the test fails instead."""
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            kernel32.GetShortPathNameW.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            if not kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
                raise ctypes.WinError(ctypes.get_last_error())
            if buffer.value.lower() == str(path).lower():
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    self.fail("CI's temp folder must have 8.3 names; %s has none" % path)
                self.skipTest("the volume of %s has no 8.3 names" % path)
            return buffer.value

        def start_hidden(self, args):
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=0x08000000)
            self.addCleanup(proc.kill)
            return proc

        def assert_move_refused_for(self, proc, root):
            code, text = self.move(root=root, venv=os.path.join(str(root), ".venv"), pids=[proc.pid])
            self.assertEqual(1, code, text)
            self.assertIn("PID %d" % proc.pid, text)
            self.assertIn("Quit Odicto", text)

        def test_move_aside_refuses_a_venv_program_started_through_a_short_path(self):
            # WMI reports a process in the spelling that started it. C:\Users\RUNNER~1\... (CI's
            # temp folder) names the same .venv as the long path, so the installer must refuse.
            venv = self.install_venv(self.broken_template)
            runner = venv / "Scripts" / "odicto-fake-app.exe"
            shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe", runner)
            cases = [  # (how the process is started, the install folder the installer is given)
                ("8.3 path", self.short_path(runner), self.install),
                ("one 8.3 folder", self.short_path(self.work) + str(runner)[len(str(self.work)):], self.install),
                ("long path, 8.3 install folder", str(runner), self.short_path(self.install)),
            ]
            for label, exe, root in cases:
                with self.subTest(label):
                    proc = self.start_hidden([exe, "-n", "60", "127.0.0.1"])
                    try:
                        self.assert_move_refused_for(proc, root)
                    finally:
                        proc.kill()
                        proc.wait(timeout=30)
                    self.assertTrue(venv.is_dir())
            code, text = self.move()
            self.assertEqual(0, code, text)
            self.assertFalse(venv.exists())

        def test_move_aside_refuses_python_running_main_py_through_a_short_path(self):
            # A Python outside the .venv that runs this install's main.py: only its command line
            # names the install folder, in the spelling that started it.
            root = self.work / "Odicto Home"         # a long name, so its 8.3 name differs
            shutil.copytree(self.broken_template, root / ".venv")
            (root / "main.py").write_text("import time\ntime.sleep(60)\n", encoding="ascii")
            python = getattr(sys, "_base_executable", sys.executable)   # not a venv launcher
            cases = [
                ("8.3 path", self.short_path(root / "main.py")),
                ("one 8.3 folder", self.short_path(self.work) + r"\Odicto Home\main.py"),
            ]
            for label, main_py in cases:
                with self.subTest(label):
                    proc = self.start_hidden([python, main_py])
                    try:
                        self.assert_move_refused_for(proc, root)
                    finally:
                        proc.kill()
                        proc.wait(timeout=30)
            code, text = self.move(root=root, venv=root / ".venv")
            self.assertEqual(0, code, text)
            self.assertFalse((root / ".venv").exists())

        def test_other_spellings_in_the_process_list_are_recognised(self):
            # python3.11.exe started through an 8.3 path has a WMI Name such as PYTHON~1.EXE, or a
            # hashed one such as PY4F2A~1.EXE; its expanded path still says python3.11.exe. A
            # \\?\-prefixed path is the same folder. Notepad with main.py open is not a user.
            exe = self.work / "fakes" / "python3.11.exe"
            exe.write_bytes(b"")
            fakes = r"""
            function Get-OdictoProcessSnapshot {
                $main = Join-Path $env:T_ROOT 'main.py'
                $app = '\\?\' + (Join-Path $env:T_VENV 'Scripts\odicto-fake-app.exe')
                @([pscustomobject]@{ProcessId=900011; Name='PY4F2A~1.EXE'; ExecutablePath=$env:T_EXE; CommandLine="`"$env:T_EXE`" `"$main`""},
                  [pscustomobject]@{ProcessId=900012; Name='PYTHON~1.EXE'; ExecutablePath=$null; CommandLine=$null},
                  [pscustomobject]@{ProcessId=900013; Name='odicto-fake-app.exe'; ExecutablePath=$app; CommandLine='odicto-fake-app.exe -n 60'},
                  [pscustomobject]@{ProcessId=900014; Name='notepad.exe'; ExecutablePath='C:\Windows\notepad.exe'; CommandLine="notepad `"$main`""})
            }
            """
            found = _ps_json(fakes + "ConvertTo-Json -Compress -InputObject @(Get-OdictoVenvUsers -Root $env:T_ROOT "
                             "-VenvDir $env:T_VENV -Retries 0 | ForEach-Object { [int]$_.ProcessId })",
                             T_EXE=self.short_path(exe), T_ROOT=str(self.install), T_VENV=str(self.install / ".venv"))
            self.assertEqual([900011, 900012, 900013], sorted(found))

        # A fake process list: PID 900001 is the Python process whose metadata changes by mode.
        FAKE_PROCESSES = r"""
        $script:calls = 0
        function Get-OdictoProcessSnapshot {
            $script:calls++
            $first = $script:calls -eq 1
            $t = [datetime]'2026-01-01'
            $venvPy = Join-Path $env:T_VENV 'Scripts\python.exe'
            $unreadable = [pscustomobject]@{ProcessId=900001; Name='python.exe'; ExecutablePath=$null; CommandLine=$null; CreationDate=$t}
            $other = [pscustomobject]@{ProcessId=900002; Name='python.exe'; ExecutablePath=$null; CommandLine=$null; CreationDate=$t}
            $reused = [pscustomobject]@{ProcessId=900001; Name='python.exe'; ExecutablePath=$null; CommandLine=$null; CreationDate=$t.AddMinutes(5)}
            $outside = [pscustomobject]@{ProcessId=900001; Name='python.exe'; ExecutablePath='C:\Elsewhere\python.exe'; CommandLine='python other.py'; CreationDate=$t}
            $inside = [pscustomobject]@{ProcessId=900001; Name='python.exe'; ExecutablePath=$venvPy; CommandLine="`"$venvPy`" app.py"; CreationDate=$t}
            $list = @([pscustomobject]@{ProcessId=4; Name='System'; ExecutablePath=$null; CommandLine=$null; CreationDate=$t},
                      [pscustomobject]@{ProcessId=5; Name='notepad.exe'; ExecutablePath='C:\Windows\notepad.exe'; CommandLine='notepad'; CreationDate=$t})
            switch ($env:T_MODE) {
                'gone'             { if ($first) { $list += $unreadable } }
                'readable-outside' { $list += $(if ($first) { $unreadable } else { $outside }) }
                'readable-inside'  { $list += $(if ($first) { $unreadable } else { $inside }) }
                'persistent'       { $list += $unreadable }
                'swapped'          { $list += $(if ($first) { $unreadable } else { $other }) }
                'pid-reused'       { $list += $(if ($first) { $unreadable } else { $reused }) }
                'confirmed'        { $list += $inside }
            }
            $list
        }
        """

        def fake_users(self, mode):
            return _ps_json(
                self.FAKE_PROCESSES +
                "$u = @(Get-OdictoVenvUsers -Root $env:T_ROOT -VenvDir $env:T_VENV -Retries 3 -DelayMs 10); "
                "[pscustomobject]@{Ids=@($u | ForEach-Object { [int]$_.ProcessId }); Calls=$script:calls} | ConvertTo-Json -Compress",
                T_MODE=mode, T_ROOT=str(self.install), T_VENV=str(self.install / ".venv"))

        def test_unreadable_python_is_looked_at_again_before_it_blocks(self):
            # A process that was starting or exiting is gone, or readable and elsewhere, a moment later.
            self.assertEqual({"Ids": [], "Calls": 2}, self.fake_users("gone"))
            self.assertEqual({"Ids": [], "Calls": 2}, self.fake_users("readable-outside"))
            # Still unreadable on every look: nobody can tell, so it stays a user (3 retries).
            self.assertEqual({"Ids": [900001], "Calls": 4}, self.fake_users("persistent"))
            # Readable and inside the .venv: a user, whether seen at once or on the second look.
            self.assertEqual({"Ids": [900001], "Calls": 2}, self.fake_users("readable-inside"))
            self.assertEqual({"Ids": [900001], "Calls": 1}, self.fake_users("confirmed"))

        def test_a_new_unreadable_python_during_the_recheck_still_blocks(self):
            # The first unknown is gone, but a different one arrives and stays: current doubt blocks.
            self.assertEqual({"Ids": [900002], "Calls": 4}, self.fake_users("swapped"))
            # The same PID with a new start time is a different process, and it stays unreadable.
            self.assertEqual({"Ids": [900001], "Calls": 4}, self.fake_users("pid-reused"))

        def test_move_aside_ignores_a_vanished_process_but_not_a_persistent_unknown(self):
            venv = self.install_venv(self.broken_template)
            move = ("Move-OdictoVenvAside -Root $env:T_ROOT -VenvDir $env:T_VENV")
            env = dict(T_ROOT=str(self.install), T_VENV=str(venv))
            code, text = _ps(self.FAKE_PROCESSES + move, T_MODE="persistent", **env)
            self.assertEqual(1, code, text)
            self.assertIn("PID 900001", text)
            self.assertTrue(venv.is_dir())
            self.assertEqual([], self.backups())
            code, text = _ps(self.FAKE_PROCESSES + move, T_MODE="gone", **env)
            self.assertEqual(0, code, text)
            self.assertFalse(venv.exists())
            self.assertEqual(1, len(self.backups()))

        def test_move_aside_refuses_while_the_app_holds_dictation_lock(self):
            import msvcrt
            venv = self.install_venv(self.broken_template)
            lock_path = self.install / "dictation.lock"
            held = open(lock_path, "a+b")     # the same call the app makes
            self.addCleanup(held.close)
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            code, text = self.move()
            self.assertEqual(1, code, text)
            self.assertIn("dictation.lock", text)
            self.assertIn("Quit Odicto", text)
            self.assertTrue(venv.is_dir())
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
            code, text = self.move()
            self.assertEqual(0, code, text)
            self.assertTrue(lock_path.is_file(), "the lock file is never deleted")
            self.assertFalse(venv.exists())

        # ---- Start Menu shortcuts ------------------------------------------------------------
        def shortcut_install(self):
            """A fake install folder with the three root launchers and the real icon."""
            root = self.work / "Odicto Home"
            (root / "assets").mkdir(parents=True)
            for name in ("start_dictation.bat", "setup.bat", "stop_dictation.bat"):
                (root / name).write_text("@echo off\r\n", encoding="ascii")
            shutil.copy(ROOT / "assets" / "odicto.ico", root / "assets" / "odicto.ico")
            return root

        READ_SHORTCUTS = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "@(Get-ChildItem -LiteralPath (Join-Path $env:T_PROGRAMS 'Odicto') -Filter *.lnk | Sort-Object Name | "
            "ForEach-Object { $l = $ws.CreateShortcut($_.FullName); "
            "[pscustomobject]@{ Name = $_.BaseName; Target = $l.TargetPath; Args = $l.Arguments; "
            "WorkDir = $l.WorkingDirectory; Icon = $l.IconLocation } }) | ConvertTo-Json -Compress")

        def test_start_menu_shortcuts_are_created_with_icon_and_working_directory(self):
            root, programs = self.shortcut_install(), self.work / "Programs"
            programs.mkdir()
            env = dict(T_ROOT=str(root), T_PROGRAMS=str(programs))
            code, text = _ps("New-OdictoStartMenuShortcuts -Root $env:T_ROOT -ProgramsDir $env:T_PROGRAMS", **env)
            self.assertEqual(0, code, text)
            self.assertEqual(str(programs / "Odicto"), text.splitlines()[-1])
            links = {row["Name"]: row for row in _ps_json(self.READ_SHORTCUTS, **env)}
            self.assertEqual({"Odicto", "Odicto Setup", "Stop Odicto"}, set(links))
            expected = {"Odicto": "start_dictation.bat", "Odicto Setup": "setup.bat", "Stop Odicto": "stop_dictation.bat"}
            for name, bat in expected.items():
                with self.subTest(shortcut=name):
                    self.assertEqual(str(root / bat).lower(), links[name]["Target"].lower())
                    self.assertEqual(str(root).lower(), links[name]["WorkDir"].lower())
                    self.assertEqual((str(root / "assets" / "odicto.ico") + ",0").lower(), links[name]["Icon"].lower())
                    self.assertEqual("", links[name]["Args"])

        def test_start_menu_shortcuts_are_idempotent_and_leave_other_files_alone(self):
            root, programs = self.shortcut_install(), self.work / "Programs"
            (programs / "Odicto").mkdir(parents=True)
            (programs / "Odicto" / "notes.txt").write_text("not ours")
            env = dict(T_ROOT=str(root), T_PROGRAMS=str(programs))
            for _ in range(2):
                code, text = _ps("New-OdictoStartMenuShortcuts -Root $env:T_ROOT -ProgramsDir $env:T_PROGRAMS", **env)
                self.assertEqual(0, code, text)
            self.assertEqual(["Odicto Setup.lnk", "Odicto.lnk", "Stop Odicto.lnk", "notes.txt"],
                             sorted(p.name for p in (programs / "Odicto").iterdir()))
            self.assertEqual(3, len(_ps_json(self.READ_SHORTCUTS, **env)))
            self.assertEqual("not ours", (programs / "Odicto" / "notes.txt").read_text())

        def test_start_menu_shortcuts_refresh_when_the_install_moves(self):
            programs = self.work / "Programs"
            programs.mkdir()
            first = self.shortcut_install()
            moved = self.work / "Moved Odicto"
            create = "New-OdictoStartMenuShortcuts -Root $env:T_ROOT -ProgramsDir $env:T_PROGRAMS"
            code, text = _ps(create, T_ROOT=str(first), T_PROGRAMS=str(programs))
            self.assertEqual(0, code, text)
            first.rename(moved)
            env = dict(T_ROOT=str(moved), T_PROGRAMS=str(programs))
            code, text = _ps(create, **env)
            self.assertEqual(0, code, text)
            links = _ps_json(self.READ_SHORTCUTS, **env)
            self.assertEqual(3, len(links))
            for row in links:
                self.assertTrue(row["Target"].lower().startswith(str(moved).lower()), row)
                self.assertEqual(str(moved).lower(), row["WorkDir"].lower())

        def test_start_menu_shortcuts_refuse_a_root_without_the_launchers(self):
            root, programs = self.shortcut_install(), self.work / "Programs"
            programs.mkdir()
            (root / "setup.bat").unlink()
            code, text = _ps("New-OdictoStartMenuShortcuts -Root $env:T_ROOT -ProgramsDir $env:T_PROGRAMS",
                             T_ROOT=str(root), T_PROGRAMS=str(programs))
            self.assertEqual(1, code, text)
            self.assertIn("Missing setup.bat", text)
            self.assertFalse((programs / "Odicto").exists(), "nothing is created when a launcher is missing")

        # ---- the real installer entry point --------------------------------------------------
        def installer(self, *args, env=None):
            (self.install / "scripts" / "windows").mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "install.ps1", self.install / "install.ps1")
            # The copy ends with a quiet process scan (see SCAN_NO_PROCESSES); the original is untouched.
            (self.install / "scripts" / "windows" / "install_helpers.ps1").write_text(
                HELPERS_PS1.read_text(encoding="utf-8") + "\n" + SCAN_NO_PROCESSES, encoding="utf-8")
            done = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self.install / "install.ps1"), "-NoUv", "-VenvOnly", *args],
                capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=self.work,
                env=dict(os.environ, **(env or {})))
            return done.returncode, done.stdout + done.stderr

        def test_installer_rejects_unsupported_python_before_touching_anything(self):
            venv = self.install_venv(self.broken_template)
            before = sorted(p.name for p in venv.iterdir())
            code, text = self.installer("-Python", self.fake_python("py313", "3 13 2"))
            self.assertNotEqual(0, code, text)
            self.assertIn("3.13.2", text)
            self.assertNotIn("Moved the old .venv", text)
            self.assertEqual(before, sorted(p.name for p in venv.iterdir()))
            self.assertEqual([], self.backups())
            self.assert_private_files_untouched()

        def test_installer_repairs_a_broken_venv_keeps_a_healthy_one_and_spares_secrets(self):
            if not _supported():
                self.fail("Run this suite with Python 3.10-3.12; the installer rebuild test needs one (got %s)"
                          % _python_version_text())
            old = self.install_venv(self.broken_template)
            (old / "marker.txt").write_text("old environment")
            code, text = self.installer("-Python", sys.executable)
            self.assertEqual(0, code, text)
            self.assertIn("Moved the old .venv", text)
            backups = self.backups()
            self.assertEqual(1, len(backups), text)
            self.assertEqual("old environment", (backups[0] / "marker.txt").read_text())
            rebuilt = subprocess.run([str(old / "Scripts" / "python.exe"), "-c", "import sys;print(sys.version_info[:2])"],
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(0, rebuilt.returncode, rebuilt.stderr)
            self.assertFalse((old / "marker.txt").exists())
            self.assert_private_files_untouched()
            # A venv that already runs is left alone.
            code, text = self.installer("-Python", sys.executable)
            self.assertEqual(0, code, text)
            self.assertIn("keeping it", text)
            self.assertEqual(1, len(self.backups()))
            self.assert_private_files_untouched()

        def test_working_venv_needs_no_base_python_to_be_found_or_installed(self):
            if not _supported():
                self.fail("Run this suite with Python 3.10-3.12 (got %s)" % _python_version_text())
            venv = self.install_venv(self.healthy_template)
            empty = self.work / "empty"
            empty.mkdir()
            system = os.environ.get("SystemRoot", r"C:\Windows")
            bare = {"PATH": system + r"\System32;" + system + r"\System32\WindowsPowerShell\v1.0",
                    "LOCALAPPDATA": str(empty), "ProgramFiles": str(empty), "USERPROFILE": str(empty)}
            code, text = self.installer(env=bare)
            self.assertEqual(0, code, text)
            self.assertIn("keeping it", text)
            self.assertNotIn("Locating an installed Python", text)
            self.assertNotIn("winget", text)
            self.assertEqual([], self.backups())
            self.assertTrue((venv / "pyvenv.cfg").is_file())
            self.assert_private_files_untouched()

    class TestWindowsLauncher(_InstallFolderCase):
        """scripts/windows/start_dictation.bat judges .venv by running it, before config."""

        @classmethod
        def setUpClass(cls):
            cls._shared = tempfile.TemporaryDirectory(prefix="odicto-launcher-template ")
            shared = Path(cls._shared.name)
            cls.healthy_template = shared / "healthy"
            cls.broken_template = shared / "broken"
            _make_venv(cls.healthy_template)
            shutil.copytree(cls.healthy_template, cls.broken_template)
            _vanish_home(cls.broken_template)

        @classmethod
        def tearDownClass(cls):
            cls._shared.cleanup()

        def launch(self, venv_template, *args, with_env=True):
            (self.install / "scripts" / "windows").mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "scripts" / "windows" / "start_dictation.bat",
                        self.install / "scripts" / "windows" / "start_dictation.bat")
            shutil.copytree(venv_template, self.install / ".venv")
            if not with_env:
                (self.install / ".env").unlink()
            done = subprocess.run(
                [str(self.install / "scripts" / "windows" / "start_dictation.bat"), *args],
                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=self.work)
            return done.returncode, done.stdout + done.stderr

        def test_broken_venv_gets_an_actionable_error_in_startup_mode(self):
            code, text = self.launch(self.broken_template, "/nostartup")
            self.assertEqual(1, code, text)
            self.assertIn("cannot run", text)
            self.assertIn("install.ps1", text)
            self.assertIn("Quit Odicto", text)
            self.assertNotIn("Config validation", text)
            self.assert_private_files_untouched()

        def test_broken_venv_is_reported_before_the_missing_env_check_in_normal_start(self):
            code, text = self.launch(self.broken_template, "/min", with_env=False)
            self.assertEqual(1, code, text)
            self.assertIn("cannot run", text)
            self.assertNotIn(".env not found", text)
            self.assertNotIn("Config validation", text)

        def test_working_venv_passes_the_gate_and_reaches_the_env_check(self):
            code, text = self.launch(self.healthy_template, "/nostartup", with_env=False)
            self.assertIn(".env not found", text)
            self.assertNotIn("cannot run", text)

else:

    def _bash(script, **env):
        environment = dict(os.environ, ODICTO_SH=str(HELPERS_SH), **env)
        done = subprocess.run(["bash", "-c", ". \"$ODICTO_SH\"; " + script], capture_output=True, text=True,
                              env=environment, timeout=180, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout.strip(), done.stderr.strip()

    class TestPosixInstaller(_InstallFolderCase):
        @classmethod
        def setUpClass(cls):
            cls._shared = tempfile.TemporaryDirectory(prefix="odicto-venv-template ")
            cls.healthy_template = Path(cls._shared.name) / "healthy"
            _make_venv(cls.healthy_template)

        @classmethod
        def tearDownClass(cls):
            cls._shared.cleanup()

        def setUp(self):
            super().setUp()
            (self.work / "fakes").mkdir()

        def fake_python(self, name, version):
            path = self.work / "fakes" / name
            path.write_text("#!/bin/sh\necho %s\n" % version)
            path.chmod(0o755)
            return str(path)

        def broken_venv(self):
            """A venv whose python link points at a removed interpreter."""
            venv = self.install / ".venv"
            (venv / "bin").mkdir(parents=True)
            os.symlink(str(self.work / "removed-base" / "python3"), str(venv / "bin" / "python"))
            (venv / "pyvenv.cfg").write_text("home = %s\n" % (self.work / "removed-base"))
            return venv

        def healthy_venv(self):
            shutil.copytree(self.healthy_template, self.install / ".venv", symlinks=True)
            return self.install / ".venv"

        def test_selection_skips_a_newer_default_and_prefers_the_highest_supported(self):
            code, out, err = _bash('odicto_find_python "" "$T1" "$T2" "$T3" "$T4"',
                                   T1=self.fake_python("py313", "3 13 2"), T2=self.fake_python("py310", "3 10 4"),
                                   T3=self.fake_python("py311", "3 11 9"), T4=str(self.work / "fakes" / "absent"))
            self.assertEqual((0, str(self.work / "fakes" / "py311")), (code, out), err)
            code, out, err = _bash('odicto_find_python "" "$T1" "$T2"', T1=self.fake_python("a313", "3 13 2"),
                                   T2=self.fake_python("a39", "3 9 18"))
            self.assertEqual(1, code, out)

        def test_explicit_unsupported_python_is_rejected_without_switching(self):
            installed = self.fake_python("py311", "3 11 9")
            code, out, err = _bash('odicto_resolve_python "$T1" "" "$T2"', T1=self.fake_python("py313", "3 13 2"), T2=installed)
            self.assertEqual(1, code)
            self.assertEqual("", out)
            self.assertIn("3.13.2", err)
            code, out, err = _bash('odicto_resolve_python 3.12 "" "$T2"', T2=installed)
            self.assertEqual(1, code)
            self.assertIn("no installed Python 3.12", err)
            code, out, err = _bash('odicto_resolve_python 3.11 "" "$T2"', T2=installed)
            self.assertEqual((0, installed), (code, out), err)
            code, out, err = _bash('odicto_resolve_python "$T1"', T1=sys.executable)
            if _supported():
                self.assertEqual((0, sys.executable), (code, out), err)
            else:
                self.assertEqual(1, code)

        def test_venv_state_is_decided_by_running_it(self):
            venv = self.install / ".venv"
            self.assertEqual("missing", _bash('odicto_venv_state "$T"', T=str(venv))[1])
            self.broken_venv()
            state = _bash('odicto_venv_state "$T"', T=str(venv))[1]
            self.assertTrue(state.startswith("broken "), state)
            self.assertIn("no longer exists", state)
            shutil.rmtree(venv)
            state = _bash('odicto_venv_state "$T"', T=str(self.healthy_venv()))[1]
            self.assertEqual(("healthy " if _supported() else "unsupported ") + _python_version_text(), state)

        def move(self, venv=None):
            return _bash('odicto_venv_move_aside "$R" "$V"', R=str(self.install), V=str(venv or self.install / ".venv"))

        def test_move_aside_keeps_everything_and_never_reuses_a_name(self):
            moved = []
            for _ in range(2):
                venv = self.broken_venv()
                (venv / "keep.txt").write_text("installed packages stand in here")
                code, out, err = self.move()
                self.assertEqual(0, code, err)
                moved.append(Path(out))
                self.assertFalse(venv.exists())
            self.assertNotEqual(moved[0], moved[1])
            for path in moved:
                self.assertTrue((path / "keep.txt").is_file())
            self.assertEqual(2, len(self.backups()))
            self.assert_private_files_untouched()

        def test_move_aside_refuses_unsafe_targets(self):
            other = self.install / "venv"
            other.mkdir()
            code, out, err = self.move(venv=other)
            self.assertEqual(1, code)
            self.assertTrue(other.is_dir())
            target = self.work / "link-target"
            target.mkdir()
            os.symlink(str(target), str(self.install / ".venv"))
            code, out, err = self.move()
            self.assertEqual(1, code)
            self.assertIn("symlink", err)
            self.assertEqual([], self.backups())

        def test_move_aside_refuses_while_a_process_runs_from_the_venv(self):
            venv = self.broken_venv()
            os.unlink(venv / "bin" / "python")
            runner = venv / "bin" / "odicto-fake-app"
            shutil.copy(shutil.which("sleep"), runner)
            proc = subprocess.Popen([str(runner), "60"])
            self.addCleanup(proc.kill)
            code, out, err = self.move()
            self.assertEqual(1, code, err)
            self.assertIn("Quit Odicto", err)
            self.assertTrue(venv.is_dir())
            proc.kill()
            proc.wait(timeout=30)
            code, out, err = self.move()
            self.assertEqual(0, code, err)

        # ---- a process started through another spelling of the install folder -----------------
        def linked_install(self):
            """A second spelling of the install folder: a symlink beside it."""
            link = self.work / "linked install"
            os.symlink(str(self.install), str(link))
            return link

        def assert_move_refused_for(self, proc, root):
            code, out, err = _bash('odicto_venv_move_aside "$R" "$V"', R=str(root), V=str(root / ".venv"))
            self.assertEqual(1, code, err)
            self.assertIn("PID %d" % proc.pid, err)
            self.assertIn("Quit Odicto", err)

        def test_move_aside_refuses_a_venv_program_started_through_a_symlink(self):
            # ps shows a command as it was typed, so <link>/.venv/... and <real>/.venv/... are
            # different text. The installer matches the physical and the given spelling.
            venv = self.broken_venv()
            os.unlink(venv / "bin" / "python")
            shutil.copy(shutil.which("sleep"), venv / "bin" / "odicto-fake-app")
            link = self.linked_install()
            cases = [  # (how the process is started, the install folder the installer is given)
                ("symlink path", link, link),
                ("physical path, symlinked install folder", self.install, link),
            ]
            if sys.platform.startswith("linux"):
                # /proc/<pid>/exe names the program by its physical path. macOS has no /proc, so
                # there a spelling the installer is not given stays unknown (dictation.lock remains).
                cases.append(("symlink path, physical install folder", link, self.install))
            for label, started, given in cases:
                with self.subTest(label):
                    proc = subprocess.Popen([str(started / ".venv" / "bin" / "odicto-fake-app"), "60"])
                    self.addCleanup(proc.kill)
                    try:
                        self.assert_move_refused_for(proc, given)
                    finally:
                        proc.kill()
                        proc.wait(timeout=30)
                    self.assertTrue(venv.is_dir())
            code, out, err = self.move()
            self.assertEqual(0, code, err)
            self.assertFalse(venv.exists())

        def test_move_aside_refuses_python_running_main_py_through_a_symlink(self):
            venv = self.broken_venv()
            (self.install / "main.py").write_text("import time\ntime.sleep(60)\n")
            link = self.linked_install()
            proc = subprocess.Popen([sys.executable, str(link / "main.py")])
            self.addCleanup(proc.kill)
            try:
                self.assert_move_refused_for(proc, link)
            finally:
                proc.kill()
                proc.wait(timeout=30)
            self.assertTrue(venv.is_dir())
            code, out, err = self.move()
            self.assertEqual(0, code, err)
            self.assertFalse(venv.exists())

        def test_move_aside_refuses_a_program_started_relative_to_the_install_folder(self):
            # scripts/posix/start_dictation.sh runs ".venv/bin/python main.py" from the install
            # folder: the command names that folder only through the process's working folder.
            venv = self.broken_venv()
            os.unlink(venv / "bin" / "python")
            shutil.copy(shutil.which("sleep"), venv / "bin" / "odicto-fake-app")
            (self.install / "main.py").write_text("import time\ntime.sleep(60)\n")
            cases = [
                (".venv program", [".venv/bin/odicto-fake-app", "60"]),
                ("python main.py", [sys.executable, "main.py"]),
            ]
            for label, args in cases:
                with self.subTest(label):
                    proc = subprocess.Popen(args, cwd=str(self.install))
                    self.addCleanup(proc.kill)
                    try:
                        self.assert_move_refused_for(proc, self.install)
                    finally:
                        proc.kill()
                        proc.wait(timeout=30)
                    self.assertTrue(venv.is_dir())
            # An editor with main.py open in the install folder is not a user: only a program named
            # python counts, not an argument that contains "python".
            (self.install / "install_python.sh").write_text("")
            editor = subprocess.Popen(["tail", "-f", "install_python.sh", "main.py"], cwd=str(self.install),
                                      stdout=subprocess.DEVNULL)
            self.addCleanup(editor.kill)
            try:
                code, out, err = self.move()
            finally:
                editor.kill()
                editor.wait(timeout=30)
            self.assertEqual(0, code, err)
            self.assertFalse(venv.exists())

        def test_move_aside_refuses_while_the_app_holds_dictation_lock(self):
            import fcntl
            venv = self.broken_venv()
            held = open(self.install / "dictation.lock", "a+b")   # the same call the app makes
            self.addCleanup(held.close)
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.move()
            self.assertEqual(1, code, err)
            self.assertIn("dictation.lock", err)
            self.assertTrue(venv.is_dir())
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            code, out, err = self.move()
            self.assertEqual(0, code, err)
            self.assertTrue((self.install / "dictation.lock").is_file())

        def installer(self, *args):
            (self.install / "scripts" / "posix").mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "install.sh", self.install / "install.sh")
            shutil.copy(HELPERS_SH, self.install / "scripts" / "posix" / "install_python.sh")
            done = subprocess.run(["bash", str(self.install / "install.sh"), "-NoUv", "-VenvOnly", *args],
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=self.work)
            return done.returncode, done.stdout + done.stderr

        def test_installer_rejects_unsupported_python_before_touching_anything(self):
            venv = self.broken_venv()
            code, text = self.installer("-Python", self.fake_python("py313", "3 13 2"))
            self.assertNotEqual(0, code, text)
            self.assertIn("3.13.2", text)
            self.assertTrue(os.path.islink(venv / "bin" / "python"))
            self.assertEqual([], self.backups())
            self.assert_private_files_untouched()

        def test_working_venv_needs_no_base_python_to_be_found_or_installed(self):
            if not _supported():
                self.fail("Run this suite with Python 3.10-3.12 (got %s)" % _python_version_text())
            self.healthy_venv()
            code, text = self.installer()
            self.assertEqual(0, code, text)
            self.assertIn("keeping it", text)
            self.assertNotIn("Locating an installed Python", text)
            self.assertEqual([], self.backups())
            self.assert_private_files_untouched()

        def test_installer_repairs_a_broken_venv_keeps_a_healthy_one_and_spares_secrets(self):
            if not _supported():
                self.fail("Run this suite with Python 3.10-3.12; the installer rebuild test needs one (got %s)"
                          % _python_version_text())
            venv = self.broken_venv()
            (venv / "marker.txt").write_text("old environment")
            code, text = self.installer("-Python", sys.executable)
            self.assertEqual(0, code, text)
            self.assertEqual(1, len(self.backups()), text)
            self.assertEqual("old environment", (self.backups()[0] / "marker.txt").read_text())
            self.assertEqual(0, subprocess.run([str(venv / "bin" / "python"), "-c", "pass"]).returncode)
            self.assert_private_files_untouched()
            code, text = self.installer("-Python", sys.executable)
            self.assertEqual(0, code, text)
            self.assertIn("keeping it", text)
            self.assertEqual(1, len(self.backups()))


if __name__ == "__main__":
    unittest.main()
