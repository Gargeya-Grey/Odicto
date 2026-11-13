"""The managed install downloads the speech model. No real model is fetched and no Odicto command runs."""
import contextlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import odicto
from config import Config
from tests import test_managed_distribution as fixtures

dist = fixtures.dist


class TestInstallStep(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestManagedDistribution(
            methodName="test_new_install_creates_settings_once_from_the_example")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.prepared = []

    def preparer(self, error=None):
        def prepare(directory, environment):
            self.prepared.append((directory, dict(environment), (self.manager.data / ".env").exists(),
                                  (directory / ".odicto-ready.json").exists()))
            if error:
                raise error
        self.manager.speech_preparer = prepare

    def run_main(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(dist, "Manager", return_value=self.manager), \
                patch.object(self.manager, "install_launchers") as launchers, \
                patch.object(self.manager, "expose_command", return_value=Path("odicto")), \
                patch.object(self.manager, "install_shortcuts", return_value=[]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = dist.main(["--home", str(self.manager.home), *arguments])
        return code, output.getvalue(), errors.getvalue(), launchers

    def test_install_gets_the_model_after_the_settings_exist(self):
        self.preparer()
        code, output, _, launchers = self.run_main("install")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.prepared), 1)
        directory, environment, settings_existed, release_ready = self.prepared[0]
        self.assertEqual(directory, self.manager.release_path(self.manager.active()))
        self.assertEqual(environment["ODICTO_DATA_ROOT"], str(self.manager.data))
        self.assertEqual(environment["ODICTO_MANAGED_HOME"], str(self.manager.home))
        self.assertTrue(settings_existed)
        self.assertTrue(release_ready)
        self.assertRegex(output, r"Getting the speech model\s+\.\.\. done\n")
        self.assertLess(output.index("Getting the speech model"), output.index("Command installed"))
        self.assertIn(f"Odicto {self.manager.active()['version']} is active.", output)
        launchers.assert_called_once()
        self.assertNotIn("not downloaded", output)

    def test_a_failed_download_prints_one_line_and_the_install_goes_on(self):
        errors = (subprocess.CalledProcessError(1, "prepare"), subprocess.TimeoutExpired("prepare", 900),
                  OSError("no python"), dist.DistributionError("synthetic"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.preparer(error)
                code, output, stderr, launchers = self.run_main("install")
                self.assertEqual(code, 0, stderr)
                self.assertRegex(output, r"Getting the speech model\s+\.\.\. skipped\n")
                self.assertIn("The speech model was not downloaded now. Odicto downloads it at its first start.\n",
                              output)
                self.assertIn(f"Odicto {self.manager.active()['version']} is active.", output)
                launchers.assert_called_once()
                self.assertEqual(stderr, "")

    def test_only_install_gets_the_model(self):
        self.preparer()
        self.assertEqual(self.run_main("update")[0], 0)
        self.assertEqual(self.prepared, [])

    def test_the_step_is_listed_with_its_plain_name(self):
        self.assertEqual(dist._UPDATE_STEPS["speech"], "Getting the speech model")

    def test_the_default_step_runs_the_hidden_command_with_the_managed_environment(self):
        directory = self.fixture.manager.releases / "v1.2.0-aaaaaaaaaaaa-bbbbbbbb"
        environment = {"ODICTO_DATA_ROOT": "synthetic data", "ODICTO_MANAGED_HOME": "synthetic home"}
        with patch.object(dist.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            dist.Manager._download_speech_model(self.manager, directory, environment)
        command = run.call_args.args[0]
        self.assertEqual(command, [str(self.manager._python(directory)), str(directory / "odicto.py"),
                                   "_prepare-speech"])
        kwargs = run.call_args.kwargs
        self.assertEqual((kwargs["cwd"], kwargs["env"], kwargs["timeout"], kwargs["check"]),
                         (directory, environment, 900, True))
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(Path(kwargs["stdout"].name), self.manager.update_log)
        self.assertIn("speech model", self.manager.update_log.read_text(encoding="utf-8"))

    def test_the_default_step_is_what_a_plain_manager_uses_and_is_injectable(self):
        plain = dist.Manager(self.manager.home)
        self.assertEqual(plain.speech_preparer.__func__, dist.Manager._download_speech_model)
        marker = object()
        self.assertIs(dist.Manager(self.manager.home, speech_preparer=marker).speech_preparer, marker)

    def test_a_timeout_or_error_of_the_default_step_reaches_the_install_as_an_exception(self):
        directory = self.manager.releases / "v1.2.0-aaaaaaaaaaaa-bbbbbbbb"
        for error in (subprocess.TimeoutExpired("prepare", 900), subprocess.CalledProcessError(1, "prepare")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(dist.subprocess, "run", side_effect=error), self.assertRaises(type(error)):
                self.manager._download_speech_model(directory, {})


class TestPrepareSpeechCommand(unittest.TestCase):
    """The hidden `odicto.py _prepare-speech` command."""

    def run_command(self, size, download_model):
        utils = type(sys)("faster_whisper.utils")
        utils.download_model = download_model
        package = type(sys)("faster_whisper")
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"faster_whisper": package, "faster_whisper.utils": utils}), \
                patch.object(Config, "WHISPER_MODEL_SIZE", size), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = odicto.cmd_prepare_speech(None)
        return code, output.getvalue(), errors.getvalue()

    def test_an_existing_folder_is_a_local_model_and_nothing_downloads(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            code, output, errors = self.run_command(folder, lambda *a, **k: calls.append((a, k)))
        self.assertEqual((code, errors, calls), (0, "", []))
        self.assertIn("local folder", output)

    def test_a_cached_model_needs_no_download(self):
        calls = []
        code, output, _ = self.run_command("tiny.en", lambda size, **k: calls.append((size, k)) or "path")
        self.assertEqual((code, calls), (0, [("tiny.en", {"local_files_only": True})]))
        self.assertIn("ready", output)

    def test_a_model_that_is_not_cached_is_downloaded_with_download_model(self):
        calls = []

        def download(size, **kwargs):
            calls.append((size, kwargs))
            if kwargs.get("local_files_only"):
                raise OSError("not cached")
            return "path"
        code, output, errors = self.run_command("base.en", download)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(calls, [("base.en", {"local_files_only": True}), ("base.en", {})])

    def test_a_failure_prints_one_line_with_the_class_and_no_path_or_key(self):
        def download(size, **kwargs):
            raise RuntimeError(r"C:\Users\someone\secret path and key=sk-123456")
        code, output, errors = self.run_command("tiny.en", download)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "The speech model was not downloaded (RuntimeError).\n")
        self.assertEqual(output, "")
        for leak in ("someone", "sk-123456", "Traceback"):
            self.assertNotIn(leak, errors)

    def test_the_command_is_dispatched_only_as_the_single_hidden_argument(self):
        with patch.object(sys, "argv", ["odicto.py", "_prepare-speech"]), \
                patch.object(odicto, "cmd_prepare_speech", return_value=7) as handler:
            self.assertEqual(odicto.main(), 7)
        handler.assert_called_once_with(None)
        for hidden in ("_prepare-speech",):
            self.assertNotIn(hidden, dist._app_parser().format_help())
        self.assertFalse(re.search(r"_prepare-speech", dist._guide(Path("home"))))


if __name__ == "__main__":
    unittest.main()
