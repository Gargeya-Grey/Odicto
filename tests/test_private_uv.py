"""The bootstrap installs its own uv in the managed home, and tells WSL from a Wayland desktop.

The bootstrap runs under bash with fake uname, curl, sudo, apt-get, dpkg-query and uv programs and a
temporary HOME. No real installer, package manager or uv runs. The manager tests use temporary homes.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = ROOT / "tools" / "distribution"
SPEC = importlib.util.spec_from_file_location("odicto_private_uv_manager", DISTRIBUTION / "manager.py")
dist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dist
SPEC.loader.exec_module(dist)

UV_NAME = "uv.exe" if os.name == "nt" else "uv"
FAKE_UV = "#!/bin/sh\n" + '''echo "uv $*" >> "$ODICTO_TEST_LOG"
case "$1" in --version) echo "uv 0.0"; exit 0 ;; esac
echo "uv-path=$PATH" >> "$ODICTO_TEST_LOG"
cat > /dev/null
exit 0
'''
# What the Astral installer would do: report its options and its stdin, then put uv in UV_INSTALL_DIR.
FAKE_INSTALLER = "#!/bin/sh\n" + '''{
for name in UV_INSTALL_DIR UV_UNMANAGED_INSTALL UV_NO_MODIFY_PATH INSTALLER_NO_MODIFY_PATH UV_DISABLE_UPDATE CARGO_DIST_FORCE_INSTALL_DIR; do
  eval "echo \\"installer $name=\\${$name-unset}\\""
done
echo "installer stdin=$(cat | wc -c | tr -d ' ')"
} >> "$ODICTO_TEST_LOG"
if [ -z "$ODICTO_TEST_INSTALLER_FAILS" ]; then
  mkdir -p "$UV_INSTALL_DIR"
  cp "$ODICTO_TEST_FAKE_UV" "$UV_INSTALL_DIR/uv"
  chmod +x "$UV_INSTALL_DIR/uv"
fi
'''
STUBS = {
    "curl": '''echo "curl $*" >> "$ODICTO_TEST_LOG"
for argument do destination=$argument; done
cp "$ODICTO_TEST_INSTALLER" "$destination"
''',
    "sudo": 'echo "sudo $* stdin=$(cat | wc -c | tr -d \' \')" >> "$ODICTO_TEST_LOG"\n',
    "apt-get": 'echo "apt-get $*" >> "$ODICTO_TEST_LOG"\n',
    "dpkg-query": "exit 0\n",
    # A uv that is already on PATH. The bootstrap must not use it.
    "uv": 'echo "PATH-UV-USED $*" >> "$ODICTO_TEST_LOG"\nexit 0\n',
}
FORGET = ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "WSL_DISTRO_NAME", "XDG_DATA_HOME", "CARGO_HOME",
          "UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL", "UV_NO_MODIFY_PATH", "CARGO_DIST_FORCE_INSTALL_DIR",
          "ODICTO_DATA_ROOT", "ODICTO_MANAGED_HOME", "ODICTO_WSL_INTEROP_FILE")


def find_bash() -> str:
    """Real bash. On Windows that is Git Bash, never the WSL launcher in System32."""
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"
    # Git\usr\bin\bash.exe reads no login profile; the one in Git\bin would put its own tools before the stubs.
    roots = []
    for program in (shutil.which("bash"), shutil.which("git")):
        if program:
            path = Path(program)
            in_usr = path.parent.name.lower() == "bin" and path.parent.parent.name.lower() == "usr"
            roots.append(path.parents[2] if in_usr else path.parents[1])
    roots.append(Path(r"C:\Program Files\Git"))
    windows = os.environ.get("SystemRoot", r"C:\Windows").lower()
    for root in roots:
        candidate = root / "usr" / "bin" / "bash.exe"
        if candidate.is_file() and not str(candidate).lower().startswith(windows):
            return str(candidate)
    raise AssertionError("Git Bash was not found")


def posix(path) -> str:
    """The form bash reads: /c/Users/... for Git Bash on Windows, the path itself elsewhere."""
    text = Path(path).as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-private-uv-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.log = self.root / "log"
        self.log.write_text("")
        for name, body in STUBS.items():
            self.write(self.stubs / name, "#!/bin/sh\n" + body, 0o755)
        self.write(self.root / "fake-uv", FAKE_UV, 0o755)
        self.write(self.root / "fake-installer", FAKE_INSTALLER, 0o755)
        # bash cannot read a script with CRLF line ends, which a Windows checkout has.
        self.write(self.root / "bootstrap.sh", (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8"), 0o755)
        self.bash = find_bash()

    @staticmethod
    def write(path: Path, text: str, mode: int) -> None:
        path.write_bytes(text.replace("\r\n", "\n").encode())
        path.chmod(mode)

    def uname(self, system: str) -> None:
        self.write(self.stubs / "uname", f"#!/bin/sh\nprintf '{system}\\n'\n", 0o755)

    def run_bootstrap(self, system="Linux", arguments=(), **environment):
        self.uname(system)
        env = {key: value for key, value in os.environ.items() if key not in FORGET}
        tools = [str(self.stubs)]
        if os.name == "nt":
            tools += [str(Path(self.bash).parent)]
        else:
            tools += ["/usr/bin", "/bin"]
        env.update(PATH=os.pathsep.join(tools), HOME=posix(self.home), TMPDIR=posix(self.root),
                   ODICTO_TEST_LOG=posix(self.log), ODICTO_TEST_FAKE_UV=posix(self.root / "fake-uv"),
                   ODICTO_TEST_INSTALLER=posix(self.root / "fake-installer"),
                   # No WSL, unless the test says so: a developer may run these tests inside WSL.
                   ODICTO_WSL_INTEROP_FILE=posix(self.root / "no-such-file"))
        env.update(environment)
        return subprocess.run([self.bash, posix(self.root / "bootstrap.sh"), *arguments], env=env,
                              input=b"THE REST OF A PIPED SCRIPT\n", capture_output=True, timeout=60)

    def lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def output(self, result) -> str:
        return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


class TestPrivateUv(Bootstrap):
    def check_private_install(self, result, private: Path) -> list[str]:
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue((private / "uv").is_file())
        lines = self.lines()
        folder = posix(private)
        for name in ("UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL"):
            self.assertIn(f"installer {name}={folder}", lines)
        for name in ("UV_NO_MODIFY_PATH", "INSTALLER_NO_MODIFY_PATH", "UV_DISABLE_UPDATE"):
            self.assertIn(f"installer {name}=1", lines)
        self.assertIn("installer CARGO_DIST_FORCE_INSTALL_DIR=unset", lines)
        self.assertFalse([line for line in lines if line.startswith("PATH-UV-USED")], "a uv on PATH was used")
        self.assertTrue([line for line in lines if line.startswith("uv run --python 3.12 --no-project")], lines)
        return lines

    def test_linux_installs_uv_under_the_default_home_and_changes_no_profile(self):
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_DATA_HOME=posix(self.root / "data"),
                                    CARGO_DIST_FORCE_INSTALL_DIR=posix(self.root / "elsewhere"))
        lines = self.check_private_install(result, self.root / "data" / "odicto" / "tools" / "uv")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [], "the bootstrap wrote into HOME")
        self.assertEqual(sorted(path.name for path in (self.root / "data").iterdir()), ["odicto"])
        self.assertIn("curl --proto =https --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o", " ".join(lines))

    def test_macos_home_with_a_space_holds_uv(self):
        result = self.run_bootstrap(system="Darwin")
        private = self.home / "Library" / "Application Support" / "Odicto" / "tools" / "uv"
        self.check_private_install(result, private)
        self.assertEqual([path.name for path in self.home.iterdir()], ["Library"])

    def test_a_uv_on_path_is_ignored_even_when_the_private_one_is_missing(self):
        # The PATH stub is named uv and logs PATH-UV-USED; check_private_install fails if it ran.
        result = self.run_bootstrap(system="Darwin")
        self.assertTrue((self.stubs / "uv").is_file())
        self.check_private_install(result, self.home / "Library" / "Application Support" / "Odicto" / "tools" / "uv")

    def test_the_home_argument_picks_the_folder_in_both_forms(self):
        for index, form in enumerate(("--home", "--home=")):
            with self.subTest(form=form):
                self.log.write_text("")
                chosen = self.root / f"chosen {index}"
                arguments = ["--home", posix(chosen)] if form == "--home" else [f"--home={posix(chosen)}"]
                result = self.run_bootstrap(system="Darwin", arguments=arguments)
                lines = self.check_private_install(result, chosen / "tools" / "uv")
                self.assertFalse((self.home / "Library").exists())
                run = [line for line in lines if line.startswith("uv run")][0]
                self.assertIn(shlex.join(arguments).replace("'", ""), run.replace("'", ""))

    def test_a_working_private_uv_is_reused_and_nothing_is_downloaded(self):
        private = self.home / "Library" / "Application Support" / "Odicto" / "tools" / "uv"
        private.mkdir(parents=True)
        shutil.copy(self.root / "fake-uv", private / "uv")
        (private / "uv").chmod(0o755)
        result = self.run_bootstrap(system="Darwin")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse([line for line in self.lines() if line.startswith(("curl", "installer"))])
        self.assertTrue([line for line in self.lines() if line.startswith("uv run")])

    def test_a_failed_uv_install_stops_with_a_message_and_does_not_fall_back_to_another_uv(self):
        cargo = self.home / ".cargo" / "bin"
        cargo.mkdir(parents=True)
        shutil.copy(self.root / "fake-uv", cargo / "uv")
        (cargo / "uv").chmod(0o755)
        result = self.run_bootstrap(system="Darwin", ODICTO_TEST_INSTALLER_FAILS="1")
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertIn("was not installed", result.stderr.decode())
        self.assertFalse([line for line in self.lines() if line.startswith(("uv run", "PATH-UV-USED"))])

    def test_nothing_reads_the_script_when_it_comes_through_a_pipe(self):
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_DATA_HOME=posix(self.root / "data"))
        self.assertEqual(result.returncode, 0, self.output(result))
        lines = self.lines()
        self.assertIn("installer stdin=0", lines)
        sudo = [line for line in lines if line.startswith("sudo ")]
        self.assertEqual(len(sudo), 2, lines)
        for line in sudo:
            self.assertTrue(line.endswith("stdin=0"), line)


class TestWslAndWayland(Bootstrap):
    def packages(self) -> str:
        return " ".join(line for line in self.lines() if line.startswith("sudo apt-get install"))

    def test_wsl_with_a_wayland_variable_and_an_x_display_goes_on_and_adds_the_alsa_plugins(self):
        for wsl in ({"WSL_DISTRO_NAME": "Ubuntu"}, {"ODICTO_WSL_INTEROP_FILE": None}):
            with self.subTest(wsl=wsl):
                self.log.write_text("")
                wsl = dict(wsl)
                if "ODICTO_WSL_INTEROP_FILE" in wsl:
                    marker = self.root / "WSLInterop"
                    marker.write_text("")
                    wsl["ODICTO_WSL_INTEROP_FILE"] = posix(marker)
                result = self.run_bootstrap(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0",
                                            XDG_DATA_HOME=posix(self.root / "data"), **wsl)
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertIn("libasound2-plugins", self.packages())
                shown = result.stdout.decode()
                self.assertIn("Odicto in WSL is for testing", shown)
                self.assertIn("shortcuts work only while a Linux window has focus", shown)
                self.assertIn("types only into Linux (X11) apps, not into Windows apps", shown)
                self.assertNotIn("Wayland session", self.output(result))

    def test_wsl_without_a_display_stops_before_sudo_and_names_the_fix(self):
        result = self.run_bootstrap(WAYLAND_DISPLAY="wayland-0", WSL_DISTRO_NAME="Ubuntu")
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertIn("wsl --update", result.stderr.decode())
        self.assertIn("Nothing was installed", result.stderr.decode())
        self.assertEqual(self.lines(), [])

    def test_a_plain_x11_session_does_not_add_the_alsa_plugins_or_the_wsl_lines(self):
        result = self.run_bootstrap(XDG_SESSION_TYPE="x11", DISPLAY=":0", XDG_DATA_HOME=posix(self.root / "data"))
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("sudo apt-get install", self.packages())
        self.assertNotIn("libasound2-plugins", self.packages())
        self.assertNotIn("WSL", self.output(result))

    def test_a_wayland_desktop_stops_before_sudo_with_advice_that_is_true_on_new_ubuntu(self):
        for session in ({"XDG_SESSION_TYPE": "wayland"}, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}):
            with self.subTest(session=session):
                self.log.write_text("")
                result = self.run_bootstrap(**session)
                message = result.stderr.decode()
                self.assertEqual(result.returncode, 1, self.output(result))
                self.assertEqual(self.lines(), [], "sudo, apt-get or curl ran")
                self.assertIn("Nothing was installed", message)
                self.assertIn("If your login screen offers a session with Xorg in its name", message)
                self.assertIn("Ubuntu 25.10 and newer have no Xorg session", message)
                self.assertNotIn("choose a session with Xorg", message)
                self.assertNotIn("Log out", message)


class TestManagerFindsUv(unittest.TestCase):
    def setUp(self):
        elevation = patch("tools.distribution.managed_admin.elevated", return_value=False)
        elevation.start()
        self.addCleanup(elevation.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-find-uv-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "home"

    def private_uv(self) -> Path:
        path = self.home / "tools" / "uv" / UV_NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_the_private_uv_is_found_with_an_empty_path(self):
        private = self.private_uv()
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(dist.find_uv(self.home), str(private))

    def test_the_private_uv_wins_over_a_uv_on_path(self):
        private = self.private_uv()
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), str(private))

    def test_a_uv_on_path_is_used_when_the_home_has_none(self):
        self.home.mkdir(parents=True)
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), "/synthetic/uv")
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertIsNone(dist.find_uv(self.home))

    def test_a_private_uv_that_is_not_a_file_is_not_used(self):
        (self.home / "tools" / "uv" / UV_NAME).mkdir(parents=True)
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), "/synthetic/uv")

    def test_dependencies_and_launchers_use_the_private_uv_without_path(self):
        private = self.private_uv()
        manager = dist.Manager(self.home)
        manager.initialize()
        release = self.home / "release"
        (release / "tools" / "distribution").mkdir(parents=True)
        (release / "tools" / "distribution" / "requirements.lock").write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"PATH": ""}), patch.object(dist.Manager, "_install_steps") as steps:
            manager._install_dependencies(release, {})
            self.assertEqual(steps.call_args.args[0], str(private))
            manager.install_launchers()
        launcher = (self.home / "bin" / "odicto").read_text(encoding="utf-8")
        self.assertIn(shlex.quote(str(private)), launcher)
        self.assertIn(f"& '{private}' run", (self.home / "bin" / "odicto.ps1").read_text(encoding="utf-8"))

    def test_a_home_that_holds_only_the_private_uv_is_still_empty_for_the_installer(self):
        private = self.private_uv()
        dist.Manager(self.home).initialize()
        self.assertTrue((self.home / ".odicto-managed.json").is_file())
        self.assertTrue(private.is_file())

    def test_any_other_file_in_the_home_still_stops_the_installer(self):
        for other in ("note.txt", "tools/other"):
            with self.subTest(other=other):
                if self.home.exists():
                    shutil.rmtree(self.home)
                self.private_uv()
                target = self.home / other
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("keep", encoding="utf-8")
                with self.assertRaises(dist.DistributionError):
                    dist.Manager(self.home).initialize()
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")


class TestVerifiersStayIdentical(unittest.TestCase):
    def test_the_shell_and_powershell_bootstraps_embed_the_same_python(self):
        shell = (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8")
        powershell = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertEqual(shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0],
                         powershell.split("$bootstrap = @'\n", 1)[1].split("\n'@", 1)[0])

    def test_the_powershell_bootstrap_forbids_a_path_change_and_ignores_path_uv(self):
        powershell = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        for needle in ("UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL", "UV_NO_MODIFY_PATH", "UV_DISABLE_UPDATE",
                       r"tools\uv", "uv.exe"):
            self.assertIn(needle, powershell)
        self.assertNotIn("Get-Command uv", powershell)
        self.assertNotIn(".local", powershell)
        self.assertNotIn(".cargo", powershell)


if __name__ == "__main__":
    unittest.main()
