"""Installer PATH hint for POSIX shells when ~/.local/bin is not yet on PATH."""
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "tools" / "distribution" / "manager.py"


def _load_manager():
    spec = importlib.util.spec_from_file_location("odicto_path_hint_tested", MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {spec.name: module}):
        spec.loader.exec_module(module)
    return module


class TestPathHint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = _load_manager()

    def test_returns_none_when_command_dir_is_on_path(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        for path_value in (
            "/usr/bin:/Users/alice/.local/bin:/bin",
            "/Users/alice/.local/bin/:/usr/bin",
            "~/.local/bin:/usr/bin",
            "/Users/alice/./.local/bin:/usr/bin",
        ):
            env = {"PATH": path_value, "HOME": "/Users/alice", "SHELL": "/bin/zsh"}
            self.assertIsNone(
                self.manager.path_hint(command, env=env, platform="darwin"),
                f"expected None for PATH={path_value!r}",
            )

    def test_macos_zsh_suggests_zprofile_with_leading_newline(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/zsh"}
        hint = self.manager.path_hint(command, env=env, platform="darwin")
        self.assertIsNotNone(hint)
        self.assertIn(
            "printf '\\n%s\\n' 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zprofile",
            hint,
        )

    def test_zsh_honors_zdotdir_on_macos_and_linux_with_safe_quoting(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        mac_hint = self.manager.path_hint(
            command,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/Users/alice",
                "SHELL": "/bin/zsh",
                "ZDOTDIR": "/Users/alice/.config/zsh",
            },
            platform="darwin",
        )
        self.assertIsNotNone(mac_hint)
        self.assertIn(">> /Users/alice/.config/zsh/.zprofile", mac_hint)

        linux_hint = self.manager.path_hint(
            PurePosixPath("/home/alice/.local/bin/odicto"),
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/alice",
                "SHELL": "/usr/bin/zsh",
                "ZDOTDIR": "/home/alice/my zsh config",
            },
            platform="linux",
        )
        self.assertIsNotNone(linux_hint)
        self.assertIn(">> '/home/alice/my zsh config/.zshrc'", linux_hint)

        same_as_home_hint = self.manager.path_hint(
            command,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/Users/alice",
                "SHELL": "/bin/zsh",
                "ZDOTDIR": "/Users/alice",
            },
            platform="darwin",
        )
        self.assertIsNotNone(same_as_home_hint)
        self.assertIn(">> ~/.zprofile", same_as_home_hint)

    def test_macos_bash_preserves_existing_login_profile_precedence(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/bash"}
        cases = [
            (set(), "~/.bash_profile"),
            ({"/Users/alice/.bash_login"}, "~/.bash_login"),
            ({"/Users/alice/.profile"}, "~/.profile"),
            ({"/Users/alice/.bash_login", "/Users/alice/.profile"}, "~/.bash_login"),
            ({"/Users/alice/.bash_profile", "/Users/alice/.bash_login"}, "~/.bash_profile"),
        ]
        for existing, expected_profile in cases:
            hint = self.manager.path_hint(
                command,
                env=env,
                platform="darwin",
                profile_exists=existing.__contains__,
            )
            self.assertIsNotNone(hint)
            self.assertIn(
                f"printf '\\n%s\\n' 'export PATH=\"$HOME/.local/bin:$PATH\"' >> {expected_profile}",
                hint,
                f"existing={existing!r}",
            )

    def test_linux_zsh_and_bash_suggest_rc_files(self):
        command = PurePosixPath("/home/alice/.local/bin/odicto")
        zsh_hint = self.manager.path_hint(
            command,
            env={"PATH": "/usr/bin:/bin", "HOME": "/home/alice", "SHELL": "/usr/bin/zsh"},
            platform="linux",
        )
        bash_hint = self.manager.path_hint(
            command,
            env={"PATH": "/usr/bin:/bin", "HOME": "/home/alice", "SHELL": "/bin/bash"},
            platform="linux",
        )
        self.assertIsNotNone(zsh_hint)
        self.assertIn(">> ~/.zshrc", zsh_hint)
        self.assertIsNotNone(bash_hint)
        self.assertIn(">> ~/.bashrc", bash_hint)

    def test_fish_suggests_fish_add_path(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/opt/homebrew/bin/fish"}
        hint = self.manager.path_hint(command, env=env, platform="darwin")
        self.assertIsNotNone(hint)
        self.assertIn('fish_add_path "$HOME/.local/bin"', hint)
        self.assertNotIn("export PATH", hint)

    def test_unknown_or_missing_shell_falls_back_to_dot_profile(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        for shell in ("", "/bin/sh", "/usr/bin/dash"):
            env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": shell}
            hint = self.manager.path_hint(command, env=env, platform="darwin")
            self.assertIsNotNone(hint)
            self.assertIn(">> ~/.profile", hint)

    def test_windows_returns_none(self):
        command = Path(r"C:\Users\alice\.local\bin\odicto.cmd")
        env = {"PATH": r"C:\Windows\System32", "SHELL": "/bin/zsh"}
        self.assertIsNone(self.manager.path_hint(command, env=env, platform="win32"))

    def test_relative_command_path_returns_none(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/zsh"}
        for command in (PurePosixPath("odicto"), PurePosixPath("bin/odicto")):
            self.assertIsNone(self.manager.path_hint(command, env=env, platform="darwin"))

    def test_custom_path_with_spaces_and_metacharacters_is_safely_quoted(self):
        tricky_dir = "/Users/alice/my dir/$(id)/bin"
        command = PurePosixPath(tricky_dir) / "odicto"
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/zsh"}
        hint = self.manager.path_hint(command, env=env, platform="darwin")
        self.assertIsNotNone(hint)
        command_line = hint.splitlines()[-1].strip()
        tokens = shlex.split(command_line)
        self.assertEqual(tokens[0], "printf")
        self.assertEqual(tokens[1], r"\n%s\n")
        self.assertIn(shlex.quote(tricky_dir), tokens[2])
        self.assertEqual(tokens[-2:], [">>", "~/.zprofile"])

    def test_path_hint_is_read_only_with_respect_to_shell_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_home = Path(directory)
            bash_login = fake_home / ".bash_login"
            bash_login.write_text('export EXISTING="keep"', encoding="utf-8")
            posix_home = PurePosixPath("/Users/alice")
            command = posix_home / ".local" / "bin" / "odicto"

            def check_fixture(path: str) -> bool:
                rel = PurePosixPath(path).relative_to(posix_home)
                return (fake_home / Path(*rel.parts)).exists()

            hint = self.manager.path_hint(
                command,
                env={"PATH": "/usr/bin:/bin", "HOME": str(posix_home), "SHELL": "/bin/bash"},
                platform="darwin",
                profile_exists=check_fixture,
            )
            self.assertIsNotNone(hint)
            self.assertIn(">> ~/.bash_login", hint)
            self.assertEqual(bash_login.read_text(encoding="utf-8"), 'export EXISTING="keep"')
            self.assertFalse((fake_home / ".bash_profile").exists())
            self.assertFalse((fake_home / ".profile").exists())

    def test_install_prints_path_hint_when_missing_and_omits_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "managed-home"
            home.mkdir()
            record = {"version": "v1.7.0", "directory": "v1.7.0-test"}
            fake_cmd = PurePosixPath("/Users/alice/.local/bin/odicto")

            def run_install(env_path: str) -> str:
                out = io.StringIO()
                mgr = self.manager.Manager(home=home, speech_preparer=lambda *_: None)
                with patch.object(self.manager, "Manager", return_value=mgr), \
                     patch.object(mgr, "update", return_value=record), \
                     patch.object(mgr, "active", return_value=None), \
                     patch.object(mgr, "initialize_settings"), \
                     patch.object(mgr, "release_path", return_value=home), \
                     patch.object(mgr, "environment", return_value={}), \
                     patch.object(mgr, "install_launchers"), \
                     patch.object(mgr, "expose_command", return_value=fake_cmd), \
                     patch.object(mgr, "retire_old_releases"), \
                     patch.object(mgr, "delete_trash"), \
                     patch.object(self.manager.sys, "platform", "darwin"), \
                     patch.dict(self.manager.os.environ, {
                         "PATH": env_path,
                         "HOME": "/Users/alice",
                         "SHELL": "/bin/zsh",
                         "ZDOTDIR": "",
                     }, clear=False), \
                     patch("sys.stdout", out):
                    code = self.manager.main(["--home", str(home), "install"])
                self.assertEqual(code, 0)
                return out.getvalue()

            missing_output = run_install("/usr/bin:/bin")
            self.assertIn("Command installed at /Users/alice/.local/bin/odicto", missing_output)
            self.assertIn(
                "printf '\\n%s\\n' 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zprofile",
                missing_output,
            )

            present_output = run_install("/usr/bin:/Users/alice/.local/bin:/bin")
            self.assertIn("Command installed at /Users/alice/.local/bin/odicto", present_output)
            self.assertNotIn("~/.zprofile", present_output)


if os.name != "nt":
    class TestPathHintLiveShells(unittest.TestCase):
        """Run the printed copy-paste commands against disposable profiles and open fresh shells."""

        @classmethod
        def setUpClass(cls):
            cls.manager = _load_manager()

        def _make_fake_launcher(self, home: Path) -> Path:
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "odicto"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)
            return launcher

        def test_macos_bash_hint_preserves_bash_login_and_no_trailing_newline(self):
            bash = shutil.which("bash") or "/bin/bash"
            with tempfile.TemporaryDirectory() as directory:
                fake_home = Path(directory).resolve()
                launcher = self._make_fake_launcher(fake_home)
                bash_login = fake_home / ".bash_login"
                # Intentionally omit trailing newline.
                bash_login.write_text('export ODICTO_ORIG="from_bash_login"', encoding="utf-8")
                env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_home), "SHELL": bash}

                hint = self.manager.path_hint(launcher, env=env, platform="darwin")
                self.assertIsNotNone(hint)
                cmd = hint.splitlines()[-1].strip()
                self.assertTrue(cmd.endswith(">> ~/.bash_login"), cmd)
                # Installer helper itself must remain read-only.
                self.assertEqual(bash_login.read_text(encoding="utf-8"), 'export ODICTO_ORIG="from_bash_login"')

                apply_res = subprocess.run([bash, "-c", cmd], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(apply_res.returncode, 0, apply_res.stdout + apply_res.stderr)
                self.assertFalse((fake_home / ".bash_profile").exists())

                fresh = subprocess.run(
                    [bash, "-l", "-c", 'printf "%s|%s\\n" "$ODICTO_ORIG" "$(command -v odicto)"'],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
                self.assertEqual(fresh.stdout.strip(), f"from_bash_login|{launcher}")

        def test_macos_bash_hint_preserves_dot_profile_when_no_bash_profile_or_login(self):
            bash = shutil.which("bash") or "/bin/bash"
            with tempfile.TemporaryDirectory() as directory:
                fake_home = Path(directory).resolve()
                launcher = self._make_fake_launcher(fake_home)
                dot_profile = fake_home / ".profile"
                dot_profile.write_text('export ODICTO_ORIG="from_profile"', encoding="utf-8")
                env = {"PATH": "/usr/bin:/bin", "HOME": str(fake_home), "SHELL": bash}

                hint = self.manager.path_hint(launcher, env=env, platform="darwin")
                self.assertIsNotNone(hint)
                cmd = hint.splitlines()[-1].strip()
                self.assertTrue(cmd.endswith(">> ~/.profile"), cmd)

                apply_res = subprocess.run([bash, "-c", cmd], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(apply_res.returncode, 0, apply_res.stdout + apply_res.stderr)
                self.assertFalse((fake_home / ".bash_profile").exists())
                self.assertFalse((fake_home / ".bash_login").exists())

                fresh = subprocess.run(
                    [bash, "-l", "-c", 'printf "%s|%s\\n" "$ODICTO_ORIG" "$(command -v odicto)"'],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
                self.assertEqual(fresh.stdout.strip(), f"from_profile|{launcher}")

        if shutil.which("zsh"):
            def test_macos_zsh_hint_honors_zdotdir_and_no_trailing_newline(self):
                zsh = shutil.which("zsh")
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    fake_home = root / "home"
                    zdotdir = root / "my zsh config"
                    zdotdir.mkdir(parents=True)
                    launcher = self._make_fake_launcher(fake_home)
                    zprofile = zdotdir / ".zprofile"
                    # Intentionally omit trailing newline.
                    zprofile.write_text('export ODICTO_ORIG="from_zdotdir"', encoding="utf-8")
                    env = {
                        "PATH": "/usr/bin:/bin",
                        "HOME": str(fake_home),
                        "SHELL": zsh,
                        "ZDOTDIR": str(zdotdir),
                    }

                    hint = self.manager.path_hint(launcher, env=env, platform="darwin")
                    self.assertIsNotNone(hint)
                    cmd = hint.splitlines()[-1].strip()
                    self.assertIn(shlex.quote(str(zprofile)), cmd)
                    # Installer helper itself must remain read-only.
                    self.assertEqual(zprofile.read_text(encoding="utf-8"), 'export ODICTO_ORIG="from_zdotdir"')

                    apply_res = subprocess.run([zsh, "-c", cmd], env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(apply_res.returncode, 0, apply_res.stdout + apply_res.stderr)
                    self.assertFalse((fake_home / ".zprofile").exists())

                    fresh = subprocess.run(
                        [zsh, "-l", "-c", 'printf "%s|%s\\n" "$ODICTO_ORIG" "$(command -v odicto)"'],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
                    self.assertEqual(fresh.stdout.strip(), f"from_zdotdir|{launcher}")


if __name__ == "__main__":
    unittest.main()
