"""Shutdown boundaries with providers/input replaced; no microphone or hooks."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.test_reliability import app_fixture


class TestShutdown(unittest.TestCase):
    def test_incomplete_live_audio_rejects_both_final_and_preview(self):
        app = app_fixture()
        app.live_preview = "incomplete preview"
        session = MagicMock()
        session.stop.return_value = "incomplete final"
        session.needs_batch_fallback = True
        audio = [0.1, 0.2]
        with patch.object(app, "process_and_paste") as pipeline:
            app._finish_live_session(session, audio, app._live_epoch)
        pipeline.assert_called_once_with(audio, False, "", False, "", live=True)

    def test_provider_finishing_after_quit_cannot_insert(self):
        for use_llm in (False, True):
            with self.subTest(use_llm=use_llm), tempfile.TemporaryDirectory() as directory:
                app = app_fixture()
                app.pid_file = os.path.join(directory, "dictation.pid")
                app.ollama_process = None
                provider = app.transcriber.transcribe
                if use_llm:
                    app.refiner = MagicMock()
                    provider = app.refiner.refine
                def finish_after_quit(*args, **kwargs):
                    app._shutdown()
                    return "must not be inserted"
                provider.side_effect = finish_after_quit
                with patch("main.platforms.unhook_all"), patch("main.paste_text") as paste:
                    app.process_and_paste([0.1], use_llm, pre_context="selected")
                paste.assert_not_called()
                self.assertFalse(app.ready)

    def test_nonowner_shutdown_preserves_runtime_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            app = app_fixture()
            app.pid_file = os.path.join(directory, "dictation.pid")
            with open(app.pid_file, "w") as f:
                f.write("12345")
            with patch("main.platforms.unhook_all"):
                app._shutdown()
            self.assertTrue(os.path.exists(app.pid_file))

    def test_owner_shutdown_removes_only_own_pid(self):
        for owner in (str(os.getpid()), "12345"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                app = app_fixture()
                app._runtime_enabled = True
                app.pid_file = os.path.join(directory, "dictation.pid")
                with open(app.pid_file, "w") as f:
                    f.write(owner)
                with patch("main.platforms.unhook_all"), patch("main.platforms.lock_is_held", return_value=True):
                    app._shutdown()
                self.assertEqual(os.path.exists(app.pid_file), owner != str(os.getpid()))

    def test_live_final_after_quit_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = app_fixture()
            app.pid_file = os.path.join(directory, "dictation.pid")
            epoch = app._live_epoch
            session = MagicMock()
            session.stop.side_effect = lambda **kwargs: (app._shutdown(), "late final")[1]
            with patch("main.platforms.unhook_all"), patch.object(app, "process_and_paste") as pipeline:
                app._finish_live_session(session, [0.1], epoch)
            pipeline.assert_not_called()
