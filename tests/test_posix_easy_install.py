"""The one-line installer for macOS and Linux: install-odicto.sh, built from bootstrap.sh.

The built file runs here with `bash` from a pipe (Git Bash on Windows), with fake `uname`,
`curl`, `uv`, `sudo`, `apt-get`, `dpkg-query` and `ps`, and a fake managed `odicto` that
records its calls. The fake `uv` stands in for the verified bootstrap: it puts the fake
command where the real install puts it. Nothing here installs, starts or stops Odicto.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
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


builder = _load("odicto_posix_installer_tested", DISTRIBUTION / "build_posix_installer.py")


def find_bash() -> str:
    """Git Bash on Windows (`bash` on PATH there can be the WSL launcher), `bash` elsewhere."""
    if os.name == "nt":
        roots = []
        git = shutil.which("git")
        if git:
            roots.append(Path(git).resolve().parents[1])
        roots += [Path(os.environ.get(name, "")) / "Git" for name in ("ProgramFiles", "ProgramFiles(x86)")]
        for root in roots:
            for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
                if (root / relative).is_file():
                    return str(root / relative)
        raise RuntimeError("Git Bash is needed to run these tests")
    found = shutil.which("bash")
    if not found:
        raise RuntimeError("bash is needed to run these tests")
    return found


def posix(path) -> str:
    """A path as bash reads it: `C:\\a\\b` is `/c/a/b` in Git Bash."""
    text = Path(path).resolve().as_posix()
    if os.name == "nt":
        return "/" + text[0].lower() + text[2:]
    return text


def write_program(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").encode("ascii"))
    path.chmod(0o755)


# A fake managed command. Each call appends its path and its arguments to the log.
FAKE_ODICTO = """#!/bin/sh
printf 'odicto|%s|%s\\n' "$0" "$*" >> "$T_LOG"
case "$1" in
  wait-ready) echo "FAKE-WAIT-OUTPUT"; exit "${T_WAIT:-0}" ;;
  "$T_FAIL") echo "FAKE failure of $1"; exit 3 ;;
esac
exit 0
"""

# The fake `uv` is the verified bootstrap's stand-in. The bootstrap keeps its own uv in its cache folder
# and checks it with `--version` (the runner puts the fake there). On `run` it reads the Python program
# from stdin (as `uv run ... python -` does), then installs the fake command, unless it is told to fail.
FAKE_UV = """#!/bin/sh
printf 'uv|%s\\n' "$*" >> "$T_LOG"
[ "$1" = --version ] && { echo 'uv 0.0 (fake)'; exit 0; }
cat > /dev/null
[ "${T_UV_EXIT:-0}" = 0 ] || exit "$T_UV_EXIT"
if [ "${T_NO_INSTALL:-}" != 1 ]; then
  mkdir -p "$T_HOME/bin"
  cp "$T_FAKE_ODICTO" "$T_HOME/bin/odicto"
  chmod +x "$T_HOME/bin/odicto"
fi
exit 0
"""

FAKES = {
    "uname": "#!/bin/sh\nprintf '%s\\n' \"$T_SYSTEM\"\n",
    # A download here means the bootstrap did not find the fake `uv` in its cache folder.
    "curl": "#!/bin/sh\nprintf 'curl|%s\\n' \"$*\" >> \"$T_LOG\"\nexit 1\n",
    # A uv on PATH is never used: the bootstrap installs and runs its own.
    "uv": "#!/bin/sh\nprintf 'path-uv|%s\\n' \"$*\" >> \"$T_LOG\"\nexit 9\n",
    "private-uv": FAKE_UV,
    # sudo and apt-get read all of stdin, as a greedy child would on a pipe.
    "sudo": "#!/bin/sh\nprintf 'sudo|%s\\n' \"$*\" >> \"$T_LOG\"\ncat > /dev/null\nexit 0\n",
    "apt-get": "#!/bin/sh\nprintf 'apt-get|%s\\n' \"$*\" >> \"$T_LOG\"\ncat > /dev/null\nexit 0\n",
    "dpkg-query": "#!/bin/sh\nfor last; do :; done\n"
                  "[ \"$last\" = \"${T_MISSING:-}\" ] && exit 1\nprintf installed\n",
    "ps": "#!/bin/sh\ncat \"$T_PS\" 2>/dev/null\nexit 0\n",
}

# The pasted line is `curl ... | bash`: the shell reads the script from a pipe.
RUNNER = ('export HOME="$T_ROOT/home" PATH="$T_FAKES:/usr/bin:/bin:$PATH" XDG_SESSION_TYPE="${T_SESSION:-x11}"\n'
          # No WSL: a developer may run these tests inside WSL, and the bootstrap checks for it.
          'export ODICTO_WSL_INTEROP_FILE="$T_ROOT/no-such-file"\n'
          'unset XDG_DATA_HOME XDG_CACHE_HOME WAYLAND_DISPLAY DISPLAY WSL_DISTRO_NAME\n'
          'for d in "$HOME/.cache/odicto-bootstrap/uv" "$HOME/Library/Caches/Odicto-bootstrap/uv"; do\n'
          '  mkdir -p "$d" && cp "$T_FAKES/private-uv" "$d/uv" && chmod +x "$d/uv"\n'
          'done\n'
          'cat "$T_SCRIPT" | bash -s -- "$@"\n')


class TestPosixInstallerBuild(unittest.TestCase):
    def setUp(self):
        self.bootstrap = (DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8")
        self.built = builder.build(self.bootstrap).decode("ascii")
        self.body = self.bootstrap.split("\n", 1)[1]

    def test_holds_the_bootstrap_body_unchanged_inside_the_function(self):
        self.assertTrue(self.bootstrap.startswith("#!/usr/bin/env bash\n"))
        self.assertEqual(self.built.count(self.body), 1)
        self.assertIn("odicto_bootstrap() {\n" + self.body + "}\n", self.built)
        # The bootstrap's own stop messages and exit codes stay.
        self.assertIn('echo "Nothing was installed." >&2\n      exit 1\n', self.built)

    def test_one_shebang_and_the_last_line_is_the_call(self):
        self.assertTrue(self.built.startswith("#!/usr/bin/env bash\n"))
        self.assertEqual(self.built.count("#!/"), 1)
        self.assertEqual(self.built.splitlines()[-1], 'odicto_install_main "$@"')
        self.assertEqual(self.built.count('\nodicto_install_main "$@"'), 1)
        self.assertTrue(self.built.endswith("\n"))

    def test_lf_lines_and_plain_ascii(self):
        self.assertNotIn("\r", self.built)
        self.built.encode("ascii")

    def test_nothing_runs_at_the_top_level_before_the_last_line(self):
        # After the bootstrap function, each line that starts in column 0 is a comment, a function
        # header, a closing brace or the call. Everything else is indented inside a function.
        after = self.built.split(self.body + "}\n", 1)[1]
        for line in after.splitlines():
            if not line or line[0] in " #":
                continue
            self.assertRegex(line, r'^(odicto_\w+\(\) \{.*|\}|odicto_install_main "\$@")$')
        before = self.built.split("odicto_bootstrap() {\n", 1)[0]
        for line in before.splitlines():
            self.assertTrue(not line or line.startswith("#"), line)

    def test_cli_writes_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "install-odicto.sh"
            self.assertEqual(builder.main([str(target)]), 0)
            self.assertEqual(target.read_bytes(), self.built.encode("ascii"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main([]), 2)

    def test_the_bootstrap_with_crlf_builds_the_same_file(self):
        self.assertEqual(builder.build(self.bootstrap.replace("\n", "\r\n")), builder.build(self.bootstrap))


class TestPosixInstallerRuns(unittest.TestCase):
    """Runs the built file from a pipe, with fakes. Linux and macOS differ only by `uname`."""

    @classmethod
    def setUpClass(cls):
        cls.bash = find_bash()
        cls.script = builder.build((DISTRIBUTION / "bootstrap.sh").read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.fakes = self.root / "fakes"
        self.fakes.mkdir()
        for name, text in FAKES.items():
            write_program(self.fakes / name, text)
        self.fake_odicto = self.root / "fake-odicto"
        write_program(self.fake_odicto, FAKE_ODICTO)
        self.log = self.root / "log.txt"
        self.ps = self.root / "ps.txt"

    def managed(self, system="Linux") -> Path:
        if system == "Darwin":
            return self.home / "Library" / "Application Support" / "Odicto"
        return self.home / ".local" / "share" / "odicto"

    def install_existing(self, system="Linux") -> Path:
        home = self.managed(system)
        (home / "bin").mkdir(parents=True)
        shutil.copyfile(self.fake_odicto, home / "bin" / "odicto")
        (home / "bin" / "odicto").chmod(0o755)
        return home

    def run_installer(self, *args, system="Linux", script=None, **extra):
        self.log.unlink(missing_ok=True)
        installer = self.root / "install-odicto.sh"
        installer.write_bytes(self.script if script is None else script)
        environment = dict(os.environ, T_ROOT=posix(self.root), T_FAKES=posix(self.fakes),
                           T_SCRIPT=posix(installer), T_LOG=posix(self.log), T_PS=posix(self.ps),
                           T_SYSTEM=system, T_FAKE_ODICTO=posix(self.fake_odicto),
                           T_HOME=extra.pop("home", None) or posix(self.managed(system)))
        environment.update({key: str(value) for key, value in extra.items()})
        done = subprocess.run([self.bash, "-c", RUNNER, "sh", *args], env=environment, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return done

    def records(self):
        if not self.log.exists():
            return []
        return [line.split("|", 2) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def calls(self):
        """The arguments of each call of the fake managed command, in order."""
        return [row[2] for row in self.records() if row[0] == "odicto"]

    def output(self, done):
        return done.stdout + done.stderr

    def test_new_install_runs_autostart_then_start_then_the_check_then_setup(self):
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls(), ["autostart", "start", "wait-ready --timeout 5 --report-waiting", "setup"])
        # The bootstrap (the fake uv) runs first, and no download starts: uv is found.
        self.assertEqual([row[0] for row in self.records()][0], "uv")
        self.assertNotIn("curl", [row[0] for row in self.records()])
        self.assertNotIn("path-uv", [row[0] for row in self.records()])
        out = self.output(done)
        for text in ("Step 1 of 3", "Step 2 of 3: Starting Odicto", "Step 3 of 3", "Odicto is ready.", "Try it now:",
                     "It starts by itself when you sign in.", "Your browser shows Odicto Setup."):
            self.assertIn(text, out)
        self.assertNotIn("FAKE-WAIT-OUTPUT", out)  # the answer of the check is not shown
        self.assertNotIn("not ready to dictate yet", out)
        self.assertNotIn("When it is ready:", out)

    def test_every_command_is_called_by_its_full_path_in_the_managed_home(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                done = self.run_installer(system=system)
                self.assertEqual(done.returncode, 0, self.output(done))
                paths = {row[1] for row in self.records() if row[0] == "odicto"}
                self.assertEqual(paths, {posix(self.managed(system)) + "/bin/odicto"})
                self.assertIn('"' + posix(self.managed(system)) + '/bin/odicto" stop', self.output(done))
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_macos_uses_its_application_support_home_with_the_space_in_it(self):
        done = self.run_installer(system="Darwin")
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertTrue((self.managed("Darwin") / "bin" / "odicto").is_file())
        self.assertEqual(self.calls(), ["autostart", "start", "wait-ready --timeout 5 --report-waiting", "setup"])

    def test_the_home_argument_goes_to_the_bootstrap_and_to_the_commands(self):
        other = self.root / "other home"
        done = self.run_installer("--home", posix(other), home=posix(other))
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertIn("--home " + posix(other),
                      [row[1] for row in self.records() if row[0] == "uv" and row[1].startswith("run ")][0])
        self.assertEqual({row[1] for row in self.records() if row[0] == "odicto"}, {posix(other) + "/bin/odicto"})
        self.assertFalse(self.managed().exists())

    def test_an_existing_install_keeps_its_sign_in_choice(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                self.install_existing(system)
                done = self.run_installer(system=system)
                self.assertEqual(done.returncode, 0, self.output(done))
                self.assertEqual(self.calls(), ["start", "wait-ready --timeout 5 --report-waiting", "setup"])
                self.assertNotIn("sign in", self.output(done))
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_a_pending_sign_in_receipt_is_tried_again(self):
        home = self.install_existing()
        (home / "autostart.json").write_text('{"path": "x", "pending": true}', encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls()[:2], ["autostart", "start"])

    def test_a_finished_receipt_with_no_pending_flag_keeps_the_choice(self):
        home = self.install_existing()
        (home / "autostart.json").write_text('{"path": "x", "sha256": "y"}', encoding="ascii")
        done = self.run_installer()
        self.assertEqual(self.calls()[:1], ["start"], self.output(done))

    def source_entry(self, system="Linux") -> Path:
        if system == "Darwin":
            return self.home / "Library" / "LaunchAgents" / "com.odicto.plist"
        return self.home / ".config" / "autostart" / "odicto.desktop"

    def test_a_source_start_entry_stops_before_anything_starts(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                entry = self.source_entry(system)
                entry.parent.mkdir(parents=True)
                entry.write_text("source copy", encoding="ascii")
                done = self.run_installer(system=system)
                self.assertEqual(done.returncode, 0, self.output(done))
                self.assertEqual(self.calls(), [])
                out = self.output(done)
                self.assertIn("another copy of Odicto is on this computer", out)
                self.assertIn("Two copies must never run together, so the new copy was not started.", out)
                self.assertIn("https://github.com/Gargeya-Grey/Odicto#moving-from-a-source-checkout", out)
                self.assertIn(str(entry.name), out)
                self.assertNotIn("Step 2 of 3", out)
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_the_managed_start_entry_is_not_a_source_copy(self):
        for entry in (self.home / ".config" / "autostart" / "odicto-managed.desktop",
                      self.home / "Library" / "LaunchAgents" / "com.odicto.managed.plist"):
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("managed", encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls()[:2], ["autostart", "start"])

    def test_source_entries_after_a_finished_migration_do_not_block(self):
        self.install_existing()
        entry = self.source_entry()
        entry.parent.mkdir(parents=True)
        entry.write_text("source copy", encoding="ascii")
        receipt = self.managed() / "migration.json"
        receipt.write_text('{"state": "complete"}', encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls(), ["start", "wait-ready --timeout 5 --report-waiting", "setup"])
        receipt.write_text('{"state": "copying"}', encoding="ascii")
        done = self.run_installer()
        self.assertEqual(self.calls(), [])
        self.assertIn("another copy of Odicto is on this computer", self.output(done))

    def source_checkout(self) -> Path:
        checkout = self.root / "src checkout" / "Odicto"
        checkout.mkdir(parents=True)
        (checkout / "main.py").write_text("", encoding="ascii")
        (checkout / "odicto.py").write_text("", encoding="ascii")
        return checkout

    def test_a_running_source_owner_stops_before_anything_starts(self):
        checkout = self.source_checkout()
        self.ps.write_text("  101 /usr/sbin/cron -f\n  4242 " + posix(checkout) + "/.venv/bin/python -u "
                           + posix(checkout) + "/main.py\n", encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls(), [])
        out = self.output(done)
        self.assertIn("That copy is running now: " + posix(checkout) + "/main.py", out)
        self.assertIn("another copy of Odicto is on this computer", out)
        self.assertIn("https://github.com/Gargeya-Grey/Odicto#moving-from-a-source-checkout", out)

    def test_python_options_and_launchers_before_the_script_do_not_hide_a_source_owner(self):
        checkout = self.source_checkout()
        for command in ("/usr/bin/python3 -W ignore " + posix(checkout) + "/main.py",
                        "/usr/bin/python3.12 -X utf8 -u " + posix(checkout) + "/main.py --debug",
                        "/usr/bin/env python3 " + posix(checkout) + "/main.py",
                        "/home/me/.local/bin/uv run python " + posix(checkout) + "/main.py",
                        "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python "
                        + posix(checkout) + "/main.py"):
            with self.subTest(command=command):
                self.ps.write_text("  4242 " + command + "\n", encoding="ascii")
                done = self.run_installer()
                self.assertEqual(self.calls(), [], self.output(done))
                self.assertIn("That copy is running now: " + posix(checkout) + "/main.py", self.output(done))

    def test_a_running_app_main_of_a_source_copy_is_found_too(self):
        checkout = self.source_checkout()
        (checkout / "app").mkdir()
        (checkout / "app" / "main.py").write_text("", encoding="ascii")
        (checkout / "app" / "odicto.py").write_text("", encoding="ascii")
        self.ps.write_text("  4242 /usr/bin/python3 " + posix(checkout) + "/app/main.py\n", encoding="ascii")
        done = self.run_installer()
        self.assertEqual(self.calls(), [], self.output(done))

    def test_a_process_that_is_not_an_odicto_owner_does_not_block(self):
        checkout = self.source_checkout()
        other = self.root / "other project"
        other.mkdir()
        (other / "main.py").write_text("", encoding="ascii")  # no odicto.py beside it
        self.ps.write_text("\n".join([
            "  1 /usr/bin/python3 " + posix(other) + "/main.py",
            "  2 vim " + posix(checkout) + "/main.py",          # an editor, not a Python
            "  3 /usr/bin/python3 -m http.server",
            "  4 /usr/bin/python3 " + posix(checkout) + "/mainxpy",
            "  5 /usr/bin/python3 /does/not/exist/main.py",
            "  6 /usr/bin/less " + posix(checkout) + "/main.py /usr/bin/python3",  # Python after the script
        ]) + "\n", encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls()[:2], ["autostart", "start"])

    def test_the_managed_owner_itself_is_not_another_copy(self):
        home = self.install_existing()
        release = home / "releases" / "v1.5.1" / "app"
        release.mkdir(parents=True)
        (release / "main.py").write_text("", encoding="ascii")
        (release / "odicto.py").write_text("", encoding="ascii")
        self.ps.write_text("  4242 " + posix(home) + "/python/bin/python3 " + posix(release) + "/main.py\n",
                           encoding="ascii")
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls()[:1], ["start"])

    def test_a_ready_owner_gets_the_ready_summary(self):
        done = self.run_installer(T_WAIT=0)
        self.assertEqual(done.returncode, 0, self.output(done))
        self.assertEqual(self.calls().count("wait-ready --timeout 5 --report-waiting"), 1)
        self.assertIn("Odicto is ready.", self.output(done))

    def test_a_waiting_owner_gets_the_waiting_summary_and_setup_still_opens(self):
        for code in (20, 21):
            with self.subTest(code=code):
                done = self.run_installer(T_WAIT=code)
                self.assertEqual(done.returncode, 0, self.output(done))
                self.assertEqual(self.calls()[-2:], ["wait-ready --timeout 5 --report-waiting", "setup"])
                out = self.output(done)
                self.assertIn("Odicto is running. It is not ready to dictate yet.", out)
                self.assertIn("The lines above say what it waits for. It gets ready by itself.", out)
                self.assertIn("When it is ready:", out)
                self.assertIn("  1. Click in any text box, for example a new email.", out)
                self.assertNotIn("Try it now:", out)
                self.assertNotIn("Odicto is ready.", out)
                self.assertIn("Your browser shows Odicto Setup.", out)
                self.assertNotIn("Some steps did not finish.", out)
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_any_other_answer_of_the_check_leaves_readiness_unconfirmed(self):
        # Only exit 0 may say "ready": an owner that ended after the start answers 1.
        for code in (1, 3, 19, 22, 75, 127):
            with self.subTest(code=code):
                done = self.run_installer(T_WAIT=code)
                self.assertEqual(done.returncode, 0, self.output(done))
                self.assertEqual(self.calls()[-1], "setup")
                out = self.output(done)
                self.assertIn("Odicto started. It did not confirm that it is ready.", out)
                self.assertIn("If dictation does not work, run this line to start Odicto again:", out)
                self.assertIn("Try it now:", out)
                self.assertNotIn("Odicto is ready.", out)
                self.assertNotIn("not ready to dictate yet", out)
                self.assertNotIn("Some steps did not finish.", out)
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_a_failed_start_says_so_and_claims_no_setup(self):
        done = self.run_installer(T_FAIL="start")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.calls(), ["autostart", "start"])  # no check, no setup
        out = self.output(done)
        self.assertIn("Odicto did not start. The lines above say why.", out)
        self.assertIn("After you fix that, paste the install line again. It starts Odicto and opens Setup.", out)
        self.assertNotIn("Step 3 of 3", out)
        self.assertNotIn("Odicto is ready.", out)
        self.assertNotIn("Your browser shows", out)
        self.assertNotIn("another copy", out)
        self.assertNotIn("Privacy & Security", out)  # the Mac text is for a Mac

    def test_a_failed_start_on_a_mac_names_the_two_permissions(self):
        done = self.run_installer(system="Darwin", T_FAIL="start")
        self.assertEqual(done.returncode, 1)
        out = self.output(done)
        self.assertIn("Accessibility and Input Monitoring", out)
        self.assertNotIn("Your browser shows", out)

    def test_a_failed_autostart_still_starts_and_says_so(self):
        done = self.run_installer(T_FAIL="autostart")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.calls(), ["autostart", "start", "wait-ready --timeout 5 --report-waiting", "setup"])
        out = self.output(done)
        self.assertIn("It does not start by itself when you sign in.", out)
        self.assertNotIn("It starts by itself", out)
        self.assertIn("Odicto is running. Some steps did not finish.", out)
        self.assertNotIn("Odicto is ready.", out)

    def test_a_failed_setup_says_so_and_does_not_claim_ready(self):
        done = self.run_installer(T_FAIL="setup")
        self.assertEqual(done.returncode, 1)
        out = self.output(done)
        self.assertIn("Odicto Setup did not open.", out)
        self.assertIn("Odicto is running. Some steps did not finish.", out)
        self.assertNotIn("Odicto is ready.", out)
        self.assertNotIn("Your browser shows", out)

    def test_an_unconfirmed_or_waiting_owner_with_a_failed_step_exits_non_zero(self):
        for code, text in ((1, "Odicto started. It did not confirm that it is ready."),
                           (20, "Odicto is running. It is not ready to dictate yet.")):
            with self.subTest(code=code):
                done = self.run_installer(T_FAIL="setup", T_WAIT=code)
                self.assertEqual(done.returncode, 1)
                out = self.output(done)
                self.assertIn(text, out)
                self.assertIn("Some steps did not finish.", out)
                self.assertIn("Odicto Setup did not open.", out)
                shutil.rmtree(self.home)
                self.home.mkdir()

    def test_a_failed_bootstrap_runs_no_odicto_command_and_keeps_its_exit_code(self):
        done = self.run_installer(T_UV_EXIT=7)
        self.assertEqual(done.returncode, 7)
        self.assertEqual(self.calls(), [])
        out = self.output(done)
        self.assertIn("Odicto was not installed. The lines above say why.", out)
        self.assertNotIn("stop it first", out)
        self.assertNotIn("Step 2 of 3", out)

    def test_a_failed_repair_says_to_stop_odicto_first(self):
        self.install_existing()
        done = self.run_installer(T_UV_EXIT=7)
        self.assertEqual(done.returncode, 7)
        self.assertEqual(self.calls(), [])
        self.assertIn("If Odicto is open, stop it first:", self.output(done))

    def test_a_wayland_desktop_stops_in_the_bootstrap_before_anything_else(self):
        done = self.run_installer(T_SESSION="wayland")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.records(), [])
        out = self.output(done)
        self.assertIn("Nothing was installed.", out)
        self.assertIn("Odicto was not installed. The lines above say why.", out)

    def test_an_install_that_leaves_no_command_is_an_error(self):
        done = self.run_installer(T_NO_INSTALL=1)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertIn("is missing.", self.output(done))

    def test_missing_packages_go_through_sudo_and_a_greedy_sudo_cannot_eat_the_script(self):
        done = self.run_installer(T_MISSING="xdotool")
        self.assertEqual(done.returncode, 0, self.output(done))
        rows = [row for row in self.records() if row[0] in ("sudo", "apt-get")]
        self.assertEqual([row[1] for row in rows],
                         ["apt-get update", "apt-get install -y xdotool"])
        # The script went on after sudo read all of its stdin.
        self.assertEqual(self.calls()[:2], ["autostart", "start"])
        self.assertIn("Odicto is ready.", self.output(done))

    def test_a_script_cut_before_its_last_line_runs_nothing(self):
        lines = self.script.decode("ascii").splitlines(keepends=True)
        self.assertEqual(lines[-1].strip(), 'odicto_install_main "$@"')
        for name, cut in (("no call", b"".join(line.encode("ascii") for line in lines[:-1])),
                          ("half of a function", b"".join(line.encode("ascii") for line in lines[:len(lines) // 2]))):
            with self.subTest(cut=name):
                done = self.run_installer(script=cut)
                self.assertEqual(self.records(), [])
                self.assertNotIn("Step 1 of 3", self.output(done))
                self.assertFalse(self.managed().exists())

    def test_the_built_file_parses_in_this_bash(self):
        installer = self.root / "install-odicto.sh"
        installer.write_bytes(self.script)
        done = subprocess.run([self.bash, "-n", posix(installer)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class TestWorkflowsUseTheInstaller(unittest.TestCase):
    def setUp(self):
        self.release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_only_a_stable_release_publishes_the_file_after_it_is_built(self):
        self.assertLess(self.release.index("build_posix_installer.py out/install-odicto.sh"),
                        self.release.index("- name: Publish the GitHub Release"))
        publish = self.release.split("- name: Publish the GitHub Release", 1)[1]
        prerelease, stable = publish.split("-eq 'true')", 1)[1].split("gh release create", 1)[0].split("else {", 1)
        self.assertIn("'out/install-odicto.sh'", stable)
        self.assertNotIn("install-odicto.sh", prerelease)
        self.assertNotIn("install-odicto.sh", publish.split("$assets = @(", 1)[1].splitlines()[0])
        # It travels with the Windows installer, as the same kind of asset.
        self.assertIn("'out/Install-Odicto.cmd'", stable)

    def test_every_macos_and_linux_ci_job_runs_the_built_file_from_a_pipe(self):
        step = self.ci.split("- name: One-line installer, run from a pipe with fakes", 1)[1]
        step = step.split("- name:", 1)[0]
        self.assertIn("if: runner.os != 'Windows'", step)
        self.assertIn("build_posix_installer.py", step)
        self.assertIn('cat "$work/install-odicto.sh" | env -i', step)


if __name__ == "__main__":
    unittest.main()
