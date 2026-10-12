"""Relocation regressions: public launchers and data paths work outside the repo CWD."""
from pathlib import Path
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paths import ROOT


class TestProjectLayout(unittest.TestCase):
    def test_settings_and_lock_still_use_install_root(self):
        import config
        import main
        import setup_web
        from platforms import base

        self.assertEqual(ROOT, Path(base.install_root()))
        self.assertEqual(ROOT / ".env", Path(setup_web.ENV_PATH))
        self.assertEqual(ROOT / ".env.example", Path(setup_web.ENV_EXAMPLE_PATH))
        self.assertEqual(ROOT / "prompt.txt", Path(config.prompt_live_path()))
        self.assertEqual(ROOT / "dictation.log", Path(main._session_log_path()))
        self.assertEqual(ROOT / "dictation.pid", Path(base.pid_file_path()))

    def test_root_has_three_launchers_per_platform(self):
        from windows_admin import HELPER_PARTS

        root_files = {p.name for p in ROOT.iterdir() if p.is_file()}
        self.assertEqual({"start_dictation.bat", "setup.bat", "stop_dictation.bat"},
                         {n for n in root_files if n.endswith(".bat")})
        self.assertEqual({"start_dictation.sh", "setup.sh", "stop_dictation.sh"},
                         {n for n in root_files if n.endswith(".sh")} - {"install.sh"})
        for name in ("start_dictation", "setup", "stop_dictation"):
            self.assertTrue((ROOT / "scripts" / "windows" / (name + ".bat")).is_file(), name)
            self.assertTrue((ROOT / "scripts" / "posix" / (name + ".sh")).is_file(), name)
        # The admin-mode helper is internal: it lives under scripts, never at the root.
        self.assertTrue(ROOT.joinpath(*HELPER_PARTS).is_file())
        self.assertEqual("admin_mode.ps1", HELPER_PARTS[-1])

    def test_setup_asset_loads_outside_repo(self):
        import setup_web

        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as outside, patch.object(setup_web, "_TEMPLATE_CACHE", None):
            try:
                os.chdir(outside)
                self.assertEqual(
                    (ROOT / "assets" / "setup_template.html").read_text(encoding="utf-8"),
                    setup_web._load_template(),
                )
            finally:
                os.chdir(original_cwd)

    def test_cli_help_runs_outside_repo_without_starting_app(self):
        with tempfile.TemporaryDirectory() as outside:
            result = subprocess.run(
                [sys.executable, str(ROOT / "odicto.py"), "--help"],
                cwd=outside, capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("remove-autostart", result.stdout)

    def test_main_launcher_resolves_code_without_binding_hooks(self):
        original_path = sys.path[:]
        run_launcher = runpy.run_path
        try:
            with patch("runpy.run_path") as execute:
                # Invoke the real wrapper while intercepting only application execution.
                run_launcher(str(ROOT / "main.py"), run_name="__main__")
                execute.assert_called_once_with(str(ROOT / "app" / "main.py"), run_name="__main__")
                self.assertEqual(str(ROOT / "app"), sys.path[0])
        finally:
            sys.path[:] = original_path
