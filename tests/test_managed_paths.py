"""Managed releases keep mutable state and writer leases outside immutable code."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from paths import ROOT
from state_lock import StateLease


class TestManagedPaths(unittest.TestCase):
    def test_code_version_and_mutable_files_use_separate_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / ".env").write_text("POLISH_HOTKEY=\n", encoding="utf-8")
            (data / "prompt.txt").write_text("synthetic private prompt", encoding="utf-8")
            environment = dict(os.environ, ODICTO_DATA_ROOT=str(data))
            program = "import sys,json;sys.path.insert(0,'app');from paths import ROOT,DATA_ROOT;from platforms import base;from config import Config,_prompt_dir;from version import app_version;print(json.dumps([str(ROOT),str(DATA_ROOT),base.install_root(),base.code_root(),_prompt_dir(),Config.POLISH_HOTKEY,app_version()]))"
            result = subprocess.run([sys.executable, "-c", program], cwd=ROOT, env=environment,
                                    capture_output=True, text=True, check=True, timeout=20)
            values = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(values[:6], [str(ROOT), str(data), str(data), str(ROOT), str(data), ""])
            self.assertEqual(values[6], (ROOT / "VERSION").read_text().strip())

    def test_state_lease_blocks_activation_until_writer_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.lock"
            with StateLease(path):
                with self.assertRaises(OSError):
                    with StateLease(path):
                        self.fail("Two writers acquired the same lease")
            with StateLease(path):
                self.assertTrue(path.exists())

    def test_unusable_environment_does_not_stop_or_spawn_owner(self):
        import setup_web
        problem = MagicMock(severity="error", message="unsupported input session")
        with patch.object(setup_web.sys, "platform", "linux"), patch(
            "platforms.preflight.environment_problems", return_value=[problem]
        ), patch("platforms.kill_other_odicto_processes") as stop, patch("platforms.spawn_detached") as start:
            with self.assertRaisesRegex(RuntimeError, "unsupported input session"):
                setup_web.restart_odicto()
        stop.assert_not_called()
        start.assert_not_called()
