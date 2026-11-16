"""The bootstrap keeps uv outside the managed home, and the manager copies it into the home.

The bootstrap runs under bash with fake uname, curl, sudo, apt-get, dpkg-query and uv programs and a
temporary HOME. No real installer, package manager or uv runs. The manager tests use temporary homes.
The hand-over tests let the fake uv run a small driver, which calls the real manager (new design) or a
fixture of the 1.5.x manager (old design) with the environment the bootstrap built.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = ROOT / "tools" / "distribution"
SPEC = importlib.util.spec_from_file_location("odicto_private_uv_manager", DISTRIBUTION / "manager.py")
dist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dist
SPEC.loader.exec_module(dist)

UV_NAME = dist.PRIVATE_UV_NAMES[0]
UVX_NAME = dist.PRIVATE_UV_NAMES[1]
FAKE_UV = "#!/bin/sh\n" + '''echo "uv $*" >> "$ODICTO_TEST_LOG"
case "$1" in --version) if [ -n "$ODICTO_TEST_UV_BROKEN" ]; then exit 1; fi; echo "uv 0.0"; exit 0 ;; esac
echo "uv-path=$PATH" >> "$ODICTO_TEST_LOG"
echo "bootstrap-uv=${ODICTO_BOOTSTRAP_UV-unset}" >> "$ODICTO_TEST_LOG"
if [ -n "$ODICTO_TEST_DRIVER" ] && [ "$1" = run ]; then
  # Stand in for "uv run ... python - ARGS": run the driver with ARGS, in the environment the bootstrap built.
  while [ "$1" != python ]; do shift; done
  shift; shift
  cat > /dev/null
  exec "$ODICTO_TEST_PYTHON" "$ODICTO_TEST_DRIVER" "$@"
fi
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
FORGET = ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "WSL_DISTRO_NAME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
          "CARGO_HOME", "UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL", "UV_NO_MODIFY_PATH", "CARGO_DIST_FORCE_INSTALL_DIR",
          "ODICTO_DATA_ROOT", "ODICTO_MANAGED_HOME", "ODICTO_WSL_INTEROP_FILE", "ODICTO_BOOTSTRAP_UV")

# The managers that the fake uv runs after the bootstrap. Both read --home from their arguments.
DRIVER_NEW = '''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("odicto_driver_manager", os.environ["ODICTO_TEST_MANAGER"])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
home = Path(sys.argv[sys.argv.index("--home") + 1])
manager = module.Manager(home)
manager.initialize()
manager.install_launchers()
print("new manager installed", home)
'''
# The 1.5.1 manager (tools/distribution/manager.py at v1.5.1). Only the three lines that matter here are
# kept: initialize() refuses a non-empty home that has no marker, uv comes from PATH (shutil.which), and
# the launcher bakes that absolute path. PATH is searched by hand so the fixture runs the same on Windows.
DRIVER_OLD = '''
import os, sys
from pathlib import Path
home = Path(sys.argv[sys.argv.index("--home") + 1]).resolve()
marker = home / ".odicto-managed.json"
if home.exists() and not marker.exists() and any(home.iterdir()):
    raise SystemExit("Destination is not an empty managed installation; existing files were preserved")
home.mkdir(parents=True, exist_ok=True)
marker.write_text("{}", encoding="utf-8")
uv = None
for folder in os.environ["PATH"].split(os.pathsep):
    candidate = Path(folder) / "uv"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        uv = str(candidate)
        break
if uv is None:
    raise SystemExit("uv was not found; rerun the bootstrap installer")
(home / "bin").mkdir()
(home / "bin" / "odicto").write_text("exec " + uv + " run launcher.py\\n", encoding="utf-8")
print("old manager installed", home)
'''


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

    def home_names(self) -> list[str]:
        return sorted(path.name for path in self.home.iterdir())


class TestBootstrapUvFolder(Bootstrap):
    def check_install(self, result, folder: Path) -> list[str]:
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue((folder / "uv").is_file())
        lines = self.lines()
        where = posix(folder)
        for name in ("UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL"):
            self.assertIn(f"installer {name}={where}", lines)
        for name in ("UV_NO_MODIFY_PATH", "INSTALLER_NO_MODIFY_PATH", "UV_DISABLE_UPDATE"):
            self.assertIn(f"installer {name}=1", lines)
        self.assertIn("installer CARGO_DIST_FORCE_INSTALL_DIR=unset", lines)
        self.assertFalse([line for line in lines if line.startswith("PATH-UV-USED")], "a uv on PATH was used")
        self.assertTrue([line for line in lines if line.startswith("uv run --python 3.12 --no-project")], lines)
        return lines

    def test_linux_installs_uv_in_the_user_cache_and_writes_nothing_else(self):
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_DATA_HOME=posix(self.root / "data"),
                                    CARGO_DIST_FORCE_INSTALL_DIR=posix(self.root / "elsewhere"))
        lines = self.check_install(result, self.home / ".cache" / "odicto-bootstrap" / "uv")
        self.assertEqual(self.home_names(), [".cache"], "the bootstrap wrote into HOME: no profile, no .local")
        self.assertFalse((self.root / "data").exists(), "the bootstrap made the managed home")
        self.assertIn("curl --proto =https --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o", " ".join(lines))

    def test_xdg_cache_home_is_used_only_when_it_is_absolute(self):
        cache = self.root / "cache"
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_CACHE_HOME=posix(cache))
        self.check_install(result, cache / "odicto-bootstrap" / "uv")
        self.assertEqual(self.home_names(), [])
        self.log.write_text("")
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_CACHE_HOME="relative/cache")
        self.check_install(result, self.home / ".cache" / "odicto-bootstrap" / "uv")

    def test_macos_installs_uv_in_the_library_caches_folder(self):
        result = self.run_bootstrap(system="Darwin")
        self.check_install(result, self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv")
        self.assertEqual(self.home_names(), ["Library"])
        self.assertEqual(sorted(path.name for path in (self.home / "Library").iterdir()), ["Caches"])

    def test_a_uv_on_path_is_ignored_even_when_the_bootstrap_folder_has_none(self):
        # The PATH stub is named uv and logs PATH-UV-USED; check_install fails if it ran.
        result = self.run_bootstrap(system="Darwin")
        self.assertTrue((self.stubs / "uv").is_file())
        self.check_install(result, self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv")

    def test_the_managed_home_is_never_touched_nor_its_uv_run(self):
        # A uv in <home>/tools/uv could be anything. Before Odicto owns the home, the bootstrap must not
        # run it, add to the folder, or make the home (it would get the umask's mode, not Odicto's policy).
        data = self.root / "data"
        planted = data / "odicto" / "tools" / "uv"
        planted.mkdir(parents=True)
        self.write(planted / "uv", "#!/bin/sh\necho \"HOME-UV-USED $*\" >> \"$ODICTO_TEST_LOG\"\nexit 0\n", 0o755)
        (data / "odicto" / "note.txt").write_text("keep", encoding="utf-8")
        before = sorted(str(path.relative_to(data)) for path in data.rglob("*"))
        result = self.run_bootstrap(WAYLAND_DISPLAY="", XDG_DATA_HOME=posix(data))
        self.check_install(result, self.home / ".cache" / "odicto-bootstrap" / "uv")
        self.assertFalse([line for line in self.lines() if "HOME-UV-USED" in line])
        self.assertEqual(sorted(str(path.relative_to(data)) for path in data.rglob("*")), before)
        self.assertEqual((data / "odicto" / "note.txt").read_text(encoding="utf-8"), "keep")

    def test_a_home_argument_is_passed_on_and_the_folder_is_not_made(self):
        for index, form in enumerate(("--home", "--home=")):
            with self.subTest(form=form):
                self.log.write_text("")
                shutil.rmtree(self.home / "Library", ignore_errors=True)
                chosen = self.root / f"chosen {index}"
                arguments = ["--home", posix(chosen)] if form == "--home" else [f"--home={posix(chosen)}"]
                result = self.run_bootstrap(system="Darwin", arguments=arguments)
                lines = self.check_install(result, self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv")
                self.assertFalse(chosen.exists(), "the bootstrap made the managed home")
                run = [line for line in lines if line.startswith("uv run")][0]
                self.assertIn(shlex.join(arguments).replace("'", ""), run.replace("'", ""))

    def test_the_manager_gets_the_bootstrap_uv_in_its_environment(self):
        folder = self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
        result = self.run_bootstrap(system="Darwin")
        self.check_install(result, folder)
        lines = self.lines()
        self.assertIn(f"bootstrap-uv={posix(folder)}/uv", lines)
        self.assertTrue([line for line in lines if line.startswith(f"uv-path={posix(folder)}:")], lines)

    def test_a_working_uv_in_the_bootstrap_folder_is_reused_and_nothing_is_downloaded(self):
        folder = self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
        folder.mkdir(parents=True)
        shutil.copy(self.root / "fake-uv", folder / "uv")
        (folder / "uv").chmod(0o755)
        result = self.run_bootstrap(system="Darwin")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse([line for line in self.lines() if line.startswith(("curl", "installer"))])
        self.assertTrue([line for line in self.lines() if line.startswith("uv run")])

    def test_a_broken_uv_in_the_bootstrap_folder_is_installed_again(self):
        folder = self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
        folder.mkdir(parents=True)
        shutil.copy(self.root / "fake-uv", folder / "uv")
        (folder / "uv").chmod(0o755)
        result = self.run_bootstrap(system="Darwin", ODICTO_TEST_UV_BROKEN="1")
        # The fake that the installer copies is broken too, so the bootstrap runs it: this checks the install step.
        self.assertTrue([line for line in self.lines() if line.startswith("curl")], self.output(result))

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
        result = self.run_bootstrap(WAYLAND_DISPLAY="")
        self.assertEqual(result.returncode, 0, self.output(result))
        lines = self.lines()
        self.assertIn("installer stdin=0", lines)
        sudo = [line for line in lines if line.startswith("sudo ")]
        self.assertEqual(len(sudo), 2, lines)
        for line in sudo:
            self.assertTrue(line.endswith("stdin=0"), line)


class TestBootstrapHandsOverToTheManager(Bootstrap):
    """The fake uv runs a driver instead of the downloaded manager, in the environment the bootstrap built."""

    def hand_over(self, driver: str, arguments, **environment):
        self.write(self.root / "driver.py", textwrap.dedent(driver), 0o644)
        return self.run_bootstrap(system="Darwin", arguments=arguments, ODICTO_TEST_DRIVER=str(self.root / "driver.py"),
                                  ODICTO_TEST_PYTHON=sys.executable, ODICTO_TEST_MANAGER=str(DISTRIBUTION / "manager.py"),
                                  **environment)

    def test_the_new_manager_copies_the_bootstrap_uv_into_a_home_it_made_private(self):
        chosen = self.root / "chosen"
        result = self.hand_over(DRIVER_NEW, ["--home", str(chosen)])
        self.assertEqual(result.returncode, 0, self.output(result))
        folder = self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
        private = chosen / "tools" / "uv" / UV_NAME
        self.assertEqual(private.read_bytes(), (folder / "uv").read_bytes())
        self.assertTrue((folder / "uv").is_file(), "the bootstrap folder keeps its uv")
        self.assertTrue((chosen / ".odicto-managed.json").is_file())
        if os.name != "nt":
            for path in (chosen, chosen / "tools", chosen / "tools" / "uv"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
        launcher = (chosen / "bin" / "odicto").read_text(encoding="utf-8")
        self.assertIn(shlex.quote(str(private)), launcher)
        self.assertNotIn(".cache", launcher)
        self.assertNotIn("Caches", launcher)

    def test_a_foreign_tools_uv_in_the_home_is_refused_and_never_adopted(self):
        # Finding: the bootstrap used to put tools/uv in the home first, so a foreign tools/uv got a marker.
        chosen = self.root / "chosen"
        (chosen / "tools" / "uv").mkdir(parents=True)
        (chosen / "tools" / "uv" / "uv").write_text("foreign", encoding="utf-8")
        result = self.hand_over(DRIVER_NEW, ["--home", str(chosen)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an empty managed installation", self.output(result))
        self.assertFalse((chosen / ".odicto-managed.json").exists())
        self.assertEqual((chosen / "tools" / "uv" / "uv").read_text(encoding="utf-8"), "foreign")

    def test_an_old_manager_is_not_refused_and_its_launcher_stays_valid(self):
        # v1.5.x knows no tools/uv: it needs an empty or absent home and a uv on PATH.
        chosen = self.root / "chosen"
        result = self.hand_over(DRIVER_OLD, ["--home", str(chosen)])
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertNotIn("not an empty", self.output(result))
        folder = self.home / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
        launcher = (chosen / "bin" / "odicto").read_text(encoding="utf-8")
        baked = launcher.split()[1]
        self.assertEqual(Path(baked).resolve(), (folder / "uv").resolve())
        self.assertTrue(Path(baked).is_file(), "the path that the old launcher holds must stay valid")
        self.assertEqual(sorted(path.name for path in chosen.iterdir()), [".odicto-managed.json", "bin"])


class TestWslAndWayland(Bootstrap):
    def packages(self) -> str:
        return " ".join(line for line in self.lines() if line.startswith("sudo apt-get install"))

    def wsl_variants(self):
        marker = self.root / "WSLInterop"
        marker.write_text("")
        return ({"WSL_DISTRO_NAME": "Ubuntu"}, {"ODICTO_WSL_INTEROP_FILE": posix(marker)})

    def test_wsl_with_a_wayland_variable_and_an_x_display_goes_on_and_adds_the_alsa_plugins(self):
        for wsl in self.wsl_variants():
            with self.subTest(wsl=wsl):
                self.log.write_text("")
                result = self.run_bootstrap(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0", **wsl)
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertIn("libasound2-plugins", self.packages())
                shown = result.stdout.decode()
                self.assertIn("Odicto in WSL is for testing", shown)
                self.assertIn("shortcuts work only while a Linux window has focus", shown)
                self.assertIn("types only into Linux (X11) apps, not into Windows apps", shown)
                self.assertNotIn("Wayland session", self.output(result))

    def test_wsl_with_only_an_x_display_goes_on(self):
        for wsl in self.wsl_variants():
            with self.subTest(wsl=wsl):
                self.log.write_text("")
                result = self.run_bootstrap(DISPLAY=":0", **wsl)
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertIn("libasound2-plugins", self.packages())

    def test_wsl_without_a_display_stops_before_sudo_and_names_the_fix(self):
        # The exact environment of a WSL without WSLg: no DISPLAY, no WAYLAND_DISPLAY, no XDG_SESSION_TYPE.
        # Odicto cannot start there, so the install must not go on to sudo and the download.
        for wsl in self.wsl_variants():
            for extra in ({}, {"WAYLAND_DISPLAY": "wayland-0"}, {"XDG_SESSION_TYPE": "tty"}):
                with self.subTest(wsl=wsl, extra=extra):
                    self.log.write_text("")
                    result = self.run_bootstrap(**wsl, **extra)
                    message = result.stderr.decode()
                    self.assertEqual(result.returncode, 1, self.output(result))
                    self.assertIn("wsl --update", message)
                    self.assertIn("Nothing was installed", message)
                    self.assertEqual(self.lines(), [], "sudo, apt-get or curl ran")
                    self.assertEqual(self.home_names(), [])

    def test_a_plain_x11_session_does_not_add_the_alsa_plugins_or_the_wsl_lines(self):
        result = self.run_bootstrap(XDG_SESSION_TYPE="x11", DISPLAY=":0")
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


class ManagerCase(unittest.TestCase):
    def setUp(self):
        elevation = patch("tools.distribution.managed_admin.elevated", return_value=False)
        elevation.start()
        self.addCleanup(elevation.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="odicto-find-uv-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"

    def make_source(self, with_uvx: bool = False) -> Path:
        """A stand-in for the uv the bootstrap started with: a file that can run, never run by these tests."""
        folder = self.root / "bootstrap-uv"
        folder.mkdir(exist_ok=True)
        source = folder / UV_NAME
        source.write_bytes(b"#!/bin/sh\nexit 0\n")
        source.chmod(0o755)
        if with_uvx:
            (folder / UVX_NAME).write_bytes(b"#!/bin/sh\nexit 0\n")
            (folder / UVX_NAME).chmod(0o755)
        return source

    def private_uv(self) -> Path:
        path = self.home / "tools" / "uv" / UV_NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def environment(self, **values):
        base = {"PATH": ""}
        base.update(values)
        return patch.dict(os.environ, base)


class TestFindUv(ManagerCase):
    def test_the_private_uv_is_found_with_an_empty_path(self):
        private = self.private_uv()
        with self.environment():
            self.assertEqual(dist.find_uv(self.home), str(private))

    def test_the_private_uv_wins_over_a_uv_on_path(self):
        private = self.private_uv()
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), str(private))

    def test_a_uv_on_path_is_used_when_the_home_has_none(self):
        self.home.mkdir(parents=True)
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), "/synthetic/uv")
        with self.environment():
            self.assertIsNone(dist.find_uv(self.home))

    def test_a_private_uv_that_is_not_a_file_is_not_used(self):
        (self.home / "tools" / "uv" / UV_NAME).mkdir(parents=True)
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), "/synthetic/uv")


class TestEnsurePrivateUv(ManagerCase):
    def test_the_bootstrap_uv_is_copied_into_private_folders_of_an_initialized_home(self):
        source = self.make_source(with_uvx=True)
        manager = dist.Manager(self.home)
        manager.initialize()
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)):
            private = manager.ensure_private_uv()
        self.assertEqual(private, self.home / "tools" / "uv" / UV_NAME)
        self.assertEqual(private.read_bytes(), source.read_bytes())
        self.assertTrue((self.home / "tools" / "uv" / UVX_NAME).is_file())
        self.assertEqual(sorted(path.name for path in (self.home / "tools" / "uv").iterdir()),
                         sorted((UV_NAME, UVX_NAME)), "a temporary file was left")
        self.assertEqual(sorted(path.name for path in (self.home / "tools").iterdir()), ["uv"])
        if os.name != "nt":
            for folder in (self.home, self.home / "tools", self.home / "tools" / "uv"):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder)
            self.assertTrue(os.access(private, os.X_OK))
        self.assertEqual(dist.find_uv(self.home), str(private))

    def test_a_copy_that_is_there_is_kept(self):
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)):
            private = manager.ensure_private_uv()
            source.write_bytes(b"a newer uv")
            before = private.stat().st_mtime_ns
            self.assertEqual(manager.ensure_private_uv(), private)
        self.assertEqual(private.read_bytes(), b"#!/bin/sh\nexit 0\n")
        self.assertEqual(private.stat().st_mtime_ns, before)

    def test_a_uv_on_path_is_copied_when_no_bootstrap_variable_is_set(self):
        # A home made after an older bootstrap, which left uv on PATH and set no variable.
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        with self.environment(), patch.object(dist.shutil, "which", return_value=str(source)):
            private = manager.ensure_private_uv()
        self.assertEqual(private.read_bytes(), source.read_bytes())

    def test_a_missing_or_unusable_source_copies_nothing_and_makes_no_folder(self):
        manager = dist.Manager(self.home)
        manager.initialize()
        for value in ("", "relative/uv", str(self.root / "no" / UV_NAME), str(self.root)):
            with self.subTest(value=value), self.environment(ODICTO_BOOTSTRAP_UV=value):
                self.assertIsNone(manager.ensure_private_uv())
                self.assertFalse((self.home / "tools").exists())

    def test_a_tools_entry_that_is_not_a_real_folder_is_refused_and_kept(self):
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        (self.home / "tools").write_text("mine", encoding="utf-8")
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)), self.assertRaises(dist.DistributionError):
            manager.ensure_private_uv()
        self.assertEqual((self.home / "tools").read_text(encoding="utf-8"), "mine")

    def test_a_failed_copy_leaves_no_file_and_find_uv_falls_back_to_path(self):
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)), patch.object(
                dist.shutil, "copyfile", side_effect=OSError("disk full")):
            self.assertIsNone(manager.ensure_private_uv())
        self.assertEqual(list((self.home / "tools" / "uv").iterdir()), [])
        with patch.object(dist.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(dist.find_uv(self.home), "/synthetic/uv")

    def test_dependencies_and_launchers_use_the_copy_without_path(self):
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        release = self.home / "release"
        (release / "tools" / "distribution").mkdir(parents=True)
        (release / "tools" / "distribution" / "requirements.lock").write_text("", encoding="utf-8")
        private = self.home / "tools" / "uv" / UV_NAME
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)), patch.object(dist.Manager, "_install_steps") as steps:
            manager._install_dependencies(release, {})
            self.assertEqual(steps.call_args.args[0], str(private))
            manager.install_launchers()
        launcher = (self.home / "bin" / "odicto").read_text(encoding="utf-8")
        self.assertIn(shlex.quote(str(private)), launcher)
        self.assertIn(f"& '{private}' run", (self.home / "bin" / "odicto.ps1").read_text(encoding="utf-8"))

    def test_launchers_alone_also_make_the_copy(self):
        # An install that finds the newest release already active stages nothing, so only the launchers run.
        source = self.make_source()
        manager = dist.Manager(self.home)
        manager.initialize()
        with self.environment(ODICTO_BOOTSTRAP_UV=str(source)):
            manager.install_launchers()
        self.assertTrue((self.home / "tools" / "uv" / UV_NAME).is_file())

    def test_a_home_that_holds_tools_uv_is_not_empty_for_the_installer(self):
        # Nothing but Odicto makes tools/uv now, so a home that has it is somebody else's.
        private = self.private_uv()
        with self.assertRaises(dist.DistributionError):
            dist.Manager(self.home).initialize()
        self.assertFalse((self.home / ".odicto-managed.json").exists())
        self.assertEqual(private.read_bytes(), b"#!/bin/sh\nexit 0\n")

    def test_an_owned_home_that_has_tools_uv_still_initializes(self):
        manager = dist.Manager(self.home)
        manager.initialize()
        self.private_uv()
        manager.initialize()
        self.assertTrue((self.home / "tools" / "uv" / UV_NAME).is_file())


class TestLauncherThroughALink(ManagerCase):
    """expose_command makes ~/.local/bin/odicto a link to <home>/bin/odicto. The launcher must not use $0."""

    # The POSIX launcher of d053167 and before, which took its folder from $0.
    OLD_LAUNCHER = ('#!/bin/sh\nset -eu\n'
                    'script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                    'exec {uv} run --python 3.12 --no-project --no-config "$script_dir/../launcher.py" "$@"\n')

    def setUp(self):
        super().setUp()
        self.bash = find_bash()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        # A uv that prints one argument per line.
        (self.tools / "fake-uv").write_bytes(b'#!/bin/sh\nfor argument do printf "%s\\n" "$argument"; done\n')
        (self.tools / "fake-uv").chmod(0o755)
        self.manager = dist.Manager(self.home)
        self.manager.initialize()
        with patch.object(dist, "bootstrap_uv", return_value=None), patch.object(
                dist, "find_uv", return_value=posix(self.tools / "fake-uv")):
            self.manager.install_launchers()
        self.launcher = self.home / "bin" / "odicto"

    def run_through(self, path: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.bash, posix(path), *arguments], capture_output=True, timeout=60,
                              env=dict(os.environ, PATH=os.pathsep.join([str(Path(self.bash).parent), os.environ.get("PATH", "")])))

    def link_in_another_folder(self) -> Path:
        """~/.local/bin/odicto: a link on macOS and Linux. Git Bash on Windows copies instead; a copy in
        another folder fails the same way when the script reads its folder from $0."""
        folder = self.root / "user" / ".local" / "bin"
        folder.mkdir(parents=True)
        done = subprocess.run([self.bash, "-c", 'ln -s "$1" "$2"', "bash", posix(self.launcher), posix(folder / "odicto")],
                              capture_output=True, timeout=60,
                              env=dict(os.environ, PATH=os.pathsep.join([str(Path(self.bash).parent), os.environ.get("PATH", "")])))
        self.assertEqual(done.returncode, 0, done.stderr)
        return folder / "odicto"

    def test_the_launcher_holds_absolute_quoted_paths_and_never_reads_dollar_zero(self):
        text = self.launcher.read_text(encoding="utf-8")
        self.assertNotIn("$0", text)
        self.assertNotIn("dirname", text)
        self.assertIn(shlex.quote(str(self.home / "launcher.py")), text)
        self.assertIn(shlex.quote(posix(self.tools / "fake-uv")), text)

    def test_the_launcher_works_through_a_link_in_another_folder(self):
        link = self.link_in_another_folder()
        done = self.run_through(link, "version", "--flag", "a b")
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace"))
        lines = done.stdout.decode().replace("\r", "").splitlines()
        self.assertEqual(lines, ["run", "--python", "3.12", "--no-project", "--no-config",
                                 str(self.home / "launcher.py"), "version", "--flag", "a b"])

    def test_the_launcher_works_directly_and_a_home_with_a_space_stays_one_word(self):
        spaced = self.root / "a home with spaces"
        manager = dist.Manager(spaced)
        manager.initialize()
        with patch.object(dist, "bootstrap_uv", return_value=None), patch.object(
                dist, "find_uv", return_value=posix(self.tools / "fake-uv")):
            manager.install_launchers()
        done = self.run_through(spaced / "bin" / "odicto", "status")
        self.assertEqual(done.stdout.decode().replace("\r", "").splitlines()[-2:], [str(spaced / "launcher.py"), "status"])

    def test_the_test_would_catch_the_old_launcher(self):
        # Control: the launcher text of d053167, run through the same link, cannot find launcher.py.
        link = self.link_in_another_folder()
        link.write_bytes(self.OLD_LAUNCHER.format(uv=shlex.quote(posix(self.tools / "fake-uv"))).encode())
        done = self.run_through(link, "version")
        lines = done.stdout.decode().replace("\r", "").splitlines()
        self.assertNotIn(str(self.home / "launcher.py"), lines)
        self.assertTrue(any(line.endswith("launcher.py") and ".local" in line for line in lines), lines)

    def test_the_windows_launchers_do_not_depend_on_a_link(self):
        # Windows: the user-level command is a one-line .cmd that calls the absolute <home>\bin\odicto.cmd,
        # which finds odicto.ps1 through %~dp0, and the script finds launcher.py through $PSScriptRoot.
        command = (self.home / "bin" / "odicto.cmd").read_bytes().decode()
        self.assertIn('-File "%~dp0odicto.ps1"', command)
        script = (self.home / "bin" / "odicto.ps1").read_text(encoding="utf-8")
        self.assertIn("Split-Path -Parent $PSScriptRoot", script)
        source = (DISTRIBUTION / "manager.py").read_text(encoding="utf-8")
        self.assertIn("""target = str(self.home / "bin" / "odicto.cmd")""", source)


class TestRefreshAfterStart(ManagerCase):
    """A home set up by an older manager gets the private uv and link-safe launchers at its next start."""

    def setUp(self):
        super().setUp()
        self.source = self.make_source()
        self.manager = dist.Manager(self.home)
        self.manager.initialize()
        old = TestLauncherThroughALink.OLD_LAUNCHER.format(uv=shlex.quote(str(self.source))).encode()
        launcher = self.home / "bin" / "odicto"
        launcher.parent.mkdir()
        launcher.write_bytes(old)
        marker = json.loads(self.manager.marker.read_text(encoding="utf-8"))
        marker["launchers"] = {"bin/odicto": hashlib.sha256(old).hexdigest()}
        dist.atomic_json(self.manager.marker, marker)
        self.old = old

    def refresh(self, **values):
        with self.environment(**values):
            self.manager._refresh_launchers()
        return (self.home / "bin" / "odicto").read_bytes()

    def test_an_old_launcher_is_rewritten_and_the_uv_copied_under_a_uv_launcher(self):
        text = self.refresh(UV=str(self.source))
        posix_host = os.name != "nt"
        self.assertEqual(text != self.old, posix_host)
        self.assertEqual((self.home / "tools" / "uv" / UV_NAME).is_file(), posix_host)
        if posix_host:
            self.assertIn(shlex.quote(str(self.home / "launcher.py")), text.decode())
            self.assertIn(shlex.quote(str(self.home / "tools" / "uv" / UV_NAME)), text.decode())

    def test_nothing_happens_outside_a_uv_launcher(self):
        with patch.object(dist.Manager, "install_launchers") as install:
            self.assertEqual(self.refresh(), self.old)
        install.assert_not_called()

    def test_current_launchers_with_a_private_uv_are_left_alone(self):
        with self.environment(UV=str(self.source)):
            self.manager.install_launchers()
        with patch.object(dist.Manager, "install_launchers") as install:
            self.refresh(UV=str(self.source))
        install.assert_not_called()

    def test_a_foreign_launcher_is_kept_and_the_start_is_not_disturbed(self):
        (self.home / "bin" / "odicto").write_bytes(b"#!/bin/sh\necho mine\n")
        self.assertEqual(self.refresh(UV=str(self.source)), b"#!/bin/sh\necho mine\n")

    def test_a_busy_manager_lock_skips_the_refresh(self):
        with dist.file_lock(self.manager.home / "manager.lock"):
            self.assertEqual(self.refresh(UV=str(self.source)), self.old)

    def test_the_uv_variable_is_a_source_for_the_copy(self):
        with self.environment(UV=str(self.source)):
            self.assertEqual(dist.bootstrap_uv(), self.source)


class TestVerifiersStayIdentical(unittest.TestCase):
    def test_the_shell_and_powershell_bootstraps_embed_the_same_python(self):
        shell = (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8")
        powershell = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertEqual(shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0],
                         powershell.split("$bootstrap = @'\n", 1)[1].split("\n'@", 1)[0])

    def test_the_powershell_bootstrap_forbids_a_path_change_and_ignores_path_uv(self):
        powershell = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        for needle in ("UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL", "UV_NO_MODIFY_PATH", "INSTALLER_NO_MODIFY_PATH",
                       "UV_DISABLE_UPDATE", r"Odicto-bootstrap\uv", "uv.exe", "ODICTO_BOOTSTRAP_UV"):
            self.assertIn(needle, powershell)
        self.assertNotIn("Get-Command uv", powershell)
        self.assertNotIn(".local", powershell)
        self.assertNotIn(".cargo", powershell)
        # It never makes or reads the managed home: no code line before the payload names it.
        code = [line for line in powershell.split("$bootstrap = @'")[0].splitlines()
                if not line.strip().startswith("#")]
        for line in code:
            for needle in ("managedHome", "tools"):
                self.assertNotIn(needle, line)

    def test_the_powershell_bootstrap_restores_path_in_a_finally_block(self):
        powershell = (DISTRIBUTION / "bootstrap.ps1").read_text(encoding="utf-8")
        tail = powershell.split("$managerArgs = @()", 1)[1]
        self.assertLess(tail.index("$savedPath = $env:PATH"), tail.index("try {"))
        self.assertLess(tail.index("try {"), tail.index("$env:PATH = $uvDirectory"))
        self.assertLess(tail.index("} finally {"), tail.rindex("$env:PATH = $savedPath"))

    def test_the_shell_bootstrap_never_names_the_managed_home(self):
        shell = (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8").split("<<'PY'", 1)[0]
        for needle in ("default_home", "XDG_DATA_HOME", "Application Support", "--home", "mkdir -p \"$home"):
            self.assertNotIn(needle, shell)


if os.name == "nt":
    class TestPowerShellBootstrapPath(unittest.TestCase):
        """One PowerShell process runs bootstrap.ps1 twice, with a fake uv.exe: once it succeeds, once it
        fails. PATH and ODICTO_BOOTSTRAP_UV must be as they were after each. No network, no real uv."""

        SOURCE = textwrap.dedent('''
            using System; using System.IO;
            class Fake {
                static int Main(string[] args) {
                    string log = Environment.GetEnvironmentVariable("ODICTO_TEST_LOG");
                    File.AppendAllText(log, "args=" + string.Join(" ", args) + "\\n");
                    if (args.Length > 0 && args[0] == "--version") { Console.WriteLine("uv 0.0"); return 0; }
                    File.AppendAllText(log, "path=" + Environment.GetEnvironmentVariable("PATH") + "\\n");
                    File.AppendAllText(log, "bootstrap-uv=" + Environment.GetEnvironmentVariable("ODICTO_BOOTSTRAP_UV") + "\\n");
                    Console.In.ReadToEnd();
                    string code = Environment.GetEnvironmentVariable("ODICTO_TEST_UV_EXIT");
                    return string.IsNullOrEmpty(code) ? 0 : int.Parse(code);
                }
            }
            ''')

        def test_path_and_the_variable_are_restored_after_success_and_after_failure(self):
            with tempfile.TemporaryDirectory(prefix="odicto-ps-bootstrap-") as directory:
                root = Path(directory).resolve()
                source = root / "fake.cs"
                source.write_text(self.SOURCE, encoding="ascii")
                compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
                folder = root / "local" / "Odicto-bootstrap" / "uv"
                folder.mkdir(parents=True)
                built = subprocess.run([str(compiler), "/nologo", f"/out:{folder / 'uv.exe'}", str(source)],
                                       capture_output=True, text=True, timeout=120)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                log = root / "log"
                log.write_text("")
                runner = root / "run.ps1"
                runner.write_text(textwrap.dedent(f"""
                    $ErrorActionPreference = 'Stop'
                    $env:LOCALAPPDATA = '{root / "local"}'
                    $env:ODICTO_TEST_LOG = '{log}'
                    $env:ODICTO_BOOTSTRAP_UV = 'before-value'
                    $path = $env:PATH
                    & '{DISTRIBUTION / "bootstrap.ps1"}'
                    Write-Output ('success-path-restored=' + ($env:PATH -ceq $path))
                    Write-Output ('success-variable=' + $env:ODICTO_BOOTSTRAP_UV)
                    $env:ODICTO_TEST_UV_EXIT = '3'
                    try {{ & '{DISTRIBUTION / "bootstrap.ps1"}'; Write-Output 'failure-did-not-throw' }}
                    catch {{ Write-Output ('failure-message=' + $_.Exception.Message) }}
                    Write-Output ('failure-path-restored=' + ($env:PATH -ceq $path))
                    Write-Output ('failure-variable=' + $env:ODICTO_BOOTSTRAP_UV)
                    """), encoding="ascii")
                done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                       "-File", str(runner)], capture_output=True, text=True, timeout=120)
                output = done.stdout + done.stderr
                self.assertEqual(done.returncode, 0, output)
                for expected in ("success-path-restored=True", "success-variable=before-value",
                                 "failure-message=Odicto managed installation failed", "failure-path-restored=True",
                                 "failure-variable=before-value"):
                    self.assertIn(expected, output)
                lines = log.read_text(encoding="utf-8").splitlines()
                self.assertTrue([line for line in lines if line.startswith("path=" + str(folder) + os.pathsep)], lines)
                self.assertIn(f"bootstrap-uv={folder / 'uv.exe'}", lines)


if __name__ == "__main__":
    unittest.main()
