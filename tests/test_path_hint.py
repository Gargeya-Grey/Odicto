"""Installer PATH hint for POSIX shells when ~/.local/bin is not yet on PATH."""
import importlib.util
import io
from pathlib import Path, PurePosixPath
import shlex
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

    def test_macos_zsh_suggests_zprofile_copy_paste_command(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/zsh"}
        hint = self.manager.path_hint(command, env=env, platform="darwin")
        self.assertIsNotNone(hint)
        self.assertIn("echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zprofile", hint)

    def test_macos_bash_suggests_bash_profile(self):
        command = PurePosixPath("/Users/alice/.local/bin/odicto")
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/alice", "SHELL": "/bin/bash"}
        hint = self.manager.path_hint(command, env=env, platform="darwin")
        self.assertIsNotNone(hint)
        self.assertIn("echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bash_profile", hint)

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
        self.assertIn(shlex.quote(tricky_dir), tokens[2])
        self.assertEqual(tokens[-2:], [">>", "~/.zprofile"])

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
                     }, clear=False), \
                     patch("sys.stdout", out):
                    code = self.manager.main(["--home", str(home), "install"])
                self.assertEqual(code, 0)
                return out.getvalue()

            missing_output = run_install("/usr/bin:/bin")
            self.assertIn("Command installed at /Users/alice/.local/bin/odicto", missing_output)
            self.assertIn("echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zprofile", missing_output)

            present_output = run_install("/usr/bin:/Users/alice/.local/bin:/bin")
            self.assertIn("Command installed at /Users/alice/.local/bin/odicto", present_output)
            self.assertNotIn("~/.zprofile", present_output)


if __name__ == "__main__":
    unittest.main()
