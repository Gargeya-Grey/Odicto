"""Resting resource use: what the app loads, how many threads it starts, what the HUD repaints.

Every test here pins a behaviour that keeps Odicto cheap while it waits in the
background. None of them touches a hook, a microphone or the network.
Fresh interpreters (subprocesses) are used wherever "already imported" would
hide the answer.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from paths import ROOT

APP_DIR = Path(__file__).resolve().parent.parent / "app"
SDKS = ("openai", "google", "pydantic", "httpx", "requests", "PySide6", "faster_whisper", "ctranslate2")

_PROBE = r"""
import json, os, sys
sys.path.insert(0, {app!r})
# Skip .env so the developer's real file cannot override the variables the probe sets.
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
{body}
loaded = sorted({{m.split('.')[0] for m in sys.modules}} & set({sdks!r}))
print(json.dumps({{"loaded": loaded, "env": os.environ.get("OPENBLAS_NUM_THREADS"), "extra": {extra}}}))
"""


def _probe(body: str = "import main", extra: str = "None", **env) -> dict:
    """Run ``body`` in a fresh interpreter and report which SDKs it loaded."""
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("OPENBLAS", "LLM_", "STT_", "LIVE_STT", "GEMINI_", "GROQ_",
                                  "OPENROUTER_", "META_", "OLLAMA_"))}
    clean["QT_QPA_PLATFORM"] = "offscreen"
    for key, value in env.items():  # None removes a key the runner's own environment holds
        if value is None:
            clean.pop(key, None)
        else:
            clean[key] = value
    code = _PROBE.format(app=str(APP_DIR), body=body, sdks=SDKS, extra=extra)
    result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=clean,
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestBlasThreads(unittest.TestCase):
    """numpy's OpenBLAS must not size itself to every CPU thread (about 480 MB committed)."""

    def test_importing_main_limits_openblas_to_one_thread(self):
        self.assertEqual(_probe("import main")["env"], "1")

    def test_an_explicit_environment_value_wins(self):
        self.assertEqual(_probe("import main", OPENBLAS_NUM_THREADS="3")["env"], "3")

    def test_numpy_is_imported_after_the_limit_is_set(self):
        # Reading the variable from a fresh interpreter proves ordering: numpy's own
        # import is the first thing in main that could create the thread pool.
        info = _probe("import main, numpy\n", extra="os.environ.get('OPENBLAS_NUM_THREADS')")
        self.assertEqual(info["extra"], "1")

    @unittest.skipUnless(sys.platform == "win32", "private bytes are a Windows measure")
    def test_importing_main_commits_little_memory_on_windows(self):
        info = _probe("import main, psutil", extra="round(psutil.Process().memory_info().private / 1048576)")
        # Before the limit this was about 510 MB; after it, about 30.
        self.assertLess(info["extra"], 150)


class TestSdksLoadOnlyWhenConfigured(unittest.TestCase):
    """A provider SDK is imported only when that provider is configured."""

    BUILD = (
        "import main\n"
        "from config import Config\n"
        "from recorder import AudioRecorder\n"
        "from refiner import TextRefiner\n"
        "from transcriber import CloudTranscriber, GeminiTranscriber, WhisperTranscriber\n"
        "provider = Config.effective_stt_provider()\n"
        "if provider == 'gemini': GeminiTranscriber()\n"
        "elif provider in ('groq', 'openrouter'): CloudTranscriber(provider)\n"
        "else: pass\n"
        "TextRefiner()\n"
    )

    def test_importing_the_app_loads_no_provider_sdk_and_no_whisper(self):
        self.assertEqual(_probe("import main")["loaded"], [])

    def test_cloud_speech_does_not_load_whisper_until_it_is_needed(self):
        loaded = _probe(self.BUILD, STT_PROVIDER="groq", GROQ_API_KEY="test-key", LLM_PROVIDER="none")["loaded"]
        self.assertNotIn("faster_whisper", loaded)
        self.assertNotIn("ctranslate2", loaded)
        self.assertNotIn("openai", loaded)
        self.assertNotIn("google", loaded)

    def test_gemini_speech_loads_the_gemini_sdk_only(self):
        loaded = _probe(self.BUILD, STT_PROVIDER="gemini", GEMINI_API_KEY="test-key", LLM_PROVIDER="none")["loaded"]
        self.assertIn("google", loaded)
        self.assertNotIn("openai", loaded)
        self.assertNotIn("faster_whisper", loaded)

    def test_an_openai_compatible_chat_provider_loads_openai_only(self):
        loaded = _probe(self.BUILD, STT_PROVIDER="groq", LLM_PROVIDER="groq", GROQ_API_KEY="test-key")["loaded"]
        self.assertIn("openai", loaded)
        self.assertNotIn("google", loaded)
        self.assertNotIn("faster_whisper", loaded)

    def test_no_provider_configured_loads_no_sdk(self):
        loaded = _probe(self.BUILD, STT_PROVIDER="groq", GROQ_API_KEY="test-key", LLM_PROVIDER="none")["loaded"]
        self.assertEqual([m for m in loaded if m in ("openai", "google", "pydantic", "faster_whisper")], [])


class TestStartupLogsVersion(unittest.TestCase):
    def test_startup_logs_the_odicto_version(self):
        from main import DictationApp
        from version import app_version

        out = io.StringIO()
        with patch("main.Config.SHOW_VISUAL_INDICATOR", False), patch("threading.Thread"), redirect_stdout(out):
            DictationApp()
        self.assertIn(f"Odicto {app_version()}", out.getvalue().splitlines())


# --------------------------------------------------------------------------- backup Whisper
class _FakeTimer:
    """Stands in for threading.Timer: records the call, never sleeps, fires on demand."""

    made: list = []

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, tuple(args)
        self.cancelled = False
        self.daemon = False
        self.name = ""
        _FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        with redirect_stdout(io.StringIO()):
            self.function(*self.args)


class TestBackupWhisperIsReleasedWhenIdle(unittest.TestCase):
    """A cloud setup frees the backup Whisper (about 740 MB) 10 idle minutes after its last use."""

    def setUp(self):
        import numpy as np
        import transcriber

        self.transcriber = transcriber
        self.audio = np.ones(10, dtype=np.float32)
        _FakeTimer.made = []
        for patcher in (patch.object(transcriber.threading, "Timer", _FakeTimer),
                        patch.object(transcriber, "WhisperTranscriber")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.whisper = transcriber.WhisperTranscriber
        self.whisper.return_value.transcribe.return_value = "local words"
        self.backend = transcriber.CloudTranscriber("groq")

    def test_a_use_starts_one_ten_minute_daemon_timer(self):
        self.assertEqual(self.backend.local_fallback(self.audio), "local words")
        self.whisper.assert_called_once_with(backup=True)
        self.assertEqual(len(_FakeTimer.made), 1)
        self.assertEqual(_FakeTimer.made[0].interval, 600.0)
        self.assertTrue(_FakeTimer.made[0].daemon)

    def test_the_timer_releases_the_model_and_the_next_failure_loads_it_again(self):
        self.backend.local_fallback(self.audio)
        _FakeTimer.made[-1].fire()
        self.assertIsNone(self.backend._whisper)
        self.whisper.return_value.release.assert_called_once_with()
        self.assertEqual(self.backend.local_fallback(self.audio), "local words")
        self.assertEqual(self.whisper.call_count, 2)

    def test_a_newer_use_restarts_the_timer(self):
        self.backend.local_fallback(self.audio)
        first = _FakeTimer.made[0]
        self.backend.local_fallback(self.audio)
        self.assertTrue(first.cancelled)
        first.fire()  # a stale timer that fired anyway must not release
        self.assertIsNotNone(self.backend._whisper)
        _FakeTimer.made[-1].fire()
        self.assertIsNone(self.backend._whisper)
        self.assertEqual(self.whisper.call_count, 1)

    def test_a_running_decode_is_never_released(self):
        import threading

        entered, finish = threading.Event(), threading.Event()
        calls = []

        def transcribe(audio):
            calls.append(audio)
            if len(calls) == 2:
                entered.set()
                finish.wait(5)
            return "w"

        self.whisper.return_value.transcribe.side_effect = transcribe
        self.backend.local_fallback(self.audio)
        worker = threading.Thread(target=self.backend.local_fallback, args=(self.audio,), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(5))
        _FakeTimer.made[0].fire()  # the first use's timer fires during the second decode
        self.assertIsNotNone(self.backend._whisper)
        finish.set()
        worker.join(5)
        _FakeTimer.made[-1].fire()
        self.assertIsNone(self.backend._whisper)

    def test_a_failed_decode_still_starts_the_timer(self):
        self.whisper.return_value.transcribe.side_effect = RuntimeError("decode failed")
        with self.assertRaises(RuntimeError):
            self.backend.local_fallback(self.audio)
        self.assertEqual(len(_FakeTimer.made), 1)
        _FakeTimer.made[0].fire()
        self.assertIsNone(self.backend._whisper)

    def test_gemini_shares_the_same_release(self):
        backend = self.transcriber.GeminiTranscriber.__new__(self.transcriber.GeminiTranscriber)
        backend.local_fallback(self.audio)
        _FakeTimer.made[-1].fire()
        self.assertIsNone(backend._whisper)


class TestBackupOnlyMklSetting(unittest.TestCase):
    """MKL's buffer cache keeps ~120 MB per released model; only a backup-only Whisper turns it off."""

    VAR, MARK = "MKL_DISABLE_FAST_MM", "ODICTO_SET_MKL_DISABLE_FAST_MM"

    def _decide(self, stt="groq", live="groq", loaded=False, preset=None):
        import transcriber

        with patch.dict(os.environ), \
                patch.object(transcriber, "_ctranslate2_loaded", return_value=loaded), \
                patch.object(transcriber.Config, "effective_stt_provider", return_value=stt), \
                patch.object(transcriber.Config, "effective_live_stt_provider", return_value=live):
            os.environ.pop(self.VAR, None)
            os.environ.pop(self.MARK, None)
            if preset is not None:
                os.environ[self.VAR] = preset
            transcriber._backup_only_mkl_settings()
            return os.environ.get(self.VAR), os.environ.get(self.MARK)

    def test_a_cloud_only_setup_turns_the_cache_off_and_marks_it(self):
        self.assertEqual(self._decide(), ("1", "1"))

    def test_a_primary_or_live_whisper_keeps_the_cache(self):
        self.assertEqual(self._decide(stt="whisper", live="whisper"), (None, None))
        self.assertEqual(self._decide(stt="groq", live="whisper"), (None, None))

    def test_too_late_once_ctranslate2_is_loaded(self):
        self.assertEqual(self._decide(loaded=True), (None, None))

    def test_a_value_the_user_set_wins_and_is_not_marked(self):
        self.assertEqual(self._decide(preset="0"), ("0", None))

    def test_only_a_backup_load_applies_the_setting(self):
        import types
        from unittest.mock import MagicMock

        import transcriber

        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = MagicMock()
        missing = object()
        saved = sys.modules.get("faster_whisper", missing)
        sys.modules["faster_whisper"] = fake

        def restore():
            if saved is missing:
                sys.modules.pop("faster_whisper", None)
            else:
                sys.modules["faster_whisper"] = saved

        self.addCleanup(restore)
        for backup, calls in ((True, 1), (False, 0)):
            with patch.object(transcriber, "WhisperModel", None), \
                    patch.object(transcriber, "_backup_only_mkl_settings") as decide, \
                    patch.object(transcriber, "whisper_device_attempts", return_value=[("cpu", "int8")]), \
                    patch.object(transcriber.Config, "WHISPER_DEVICE", "cpu"), \
                    redirect_stdout(io.StringIO()):
                transcriber.WhisperTranscriber(backup=backup)
            self.assertEqual(decide.call_count, calls, "backup=%s" % backup)


class TestChildOdictoDropsTheInheritedSetting(unittest.TestCase):
    """The tray's setup page and the Odicto its Save starts inherit the parent's environment."""

    def test_a_marked_value_is_removed_before_anything_loads(self):
        info = _probe("import main",
                      extra="[os.environ.get('MKL_DISABLE_FAST_MM'), os.environ.get('ODICTO_SET_MKL_DISABLE_FAST_MM')]",
                      MKL_DISABLE_FAST_MM="1", ODICTO_SET_MKL_DISABLE_FAST_MM="1")
        self.assertEqual(info["extra"], [None, None])

    def test_a_value_the_user_set_stays(self):
        info = _probe("import main", extra="os.environ.get('MKL_DISABLE_FAST_MM')",
                      MKL_DISABLE_FAST_MM="1", ODICTO_SET_MKL_DISABLE_FAST_MM=None)
        self.assertEqual(info["extra"], "1")


class TestPrimaryWhisperStaysLoaded(unittest.TestCase):
    """STT_PROVIDER=whisper keeps its model: releasing it would slow every dictation."""

    def test_the_dictation_and_live_whisper_are_not_backups(self):
        import ast

        import transcriber

        self.assertFalse(issubclass(transcriber.WhisperTranscriber, transcriber._LocalWhisperFallback))
        tree = ast.parse((APP_DIR / "main.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and getattr(node.func, "id", None) == "WhisperTranscriber"]
        self.assertGreaterEqual(len(calls), 2, "the dictation engine and the live engine")
        for call in calls:
            self.assertEqual((call.args, call.keywords), ([], []), ast.dump(call))

    def test_release_drops_the_model(self):
        import transcriber

        whisper = transcriber.WhisperTranscriber.__new__(transcriber.WhisperTranscriber)
        whisper.model = object()
        whisper.release()
        self.assertIsNone(whisper.model)


# --------------------------------------------------------------------------- tray icon
def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class TestTrayIcon(unittest.TestCase):
    def setUp(self):
        _qt_app()

    def _copy_asset(self, root: Path, name: str) -> None:
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "assets" / name).write_bytes((ROOT / "assets" / name).read_bytes())

    def test_the_shipped_logo_loads_and_is_the_graphite_square(self):
        from indicator import logo_icon

        icon = logo_icon()
        self.assertFalse(icon.isNull())
        image = icon.pixmap(64, 64).toImage()
        centre = image.pixelColor(32, 8)  # inside the rounded square, above the jade mark
        self.assertEqual((centre.red(), centre.green(), centre.blue()), (0x1B, 0x1D, 0x20))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)  # rounded corner stays transparent

    def test_the_logo_is_found_from_the_install_root_not_the_working_directory(self):
        from indicator import logo_icon

        with tempfile.TemporaryDirectory() as elsewhere:
            previous = os.getcwd()
            os.chdir(elsewhere)
            try:
                self.assertFalse(logo_icon().isNull())
            finally:
                os.chdir(previous)

    def test_the_root_comes_from_paths_root(self):
        import indicator

        with tempfile.TemporaryDirectory() as root:
            with patch.object(indicator, "ROOT", Path(root)):
                self.assertTrue(indicator.logo_icon().isNull())  # nothing under this root
            self._copy_asset(Path(root), "odicto-icon.svg")
            with patch.object(indicator, "ROOT", Path(root)):
                self.assertFalse(indicator.logo_icon().isNull())

    def test_png_is_used_when_the_svg_is_missing_or_damaged(self):
        from indicator import logo_icon

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            self._copy_asset(base, "odicto-icon-256.png")
            self.assertFalse(logo_icon(base).isNull())  # svg missing
            (base / "assets" / "odicto-icon.svg").write_text("not an svg", encoding="utf-8")
            icon = logo_icon(base)  # svg damaged
            self.assertFalse(icon.isNull())
            self.assertFalse(icon.pixmap(32, 32).isNull())

    def test_a_damaged_logo_alone_is_a_null_icon_not_an_error(self):
        from indicator import logo_icon

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "assets").mkdir()
            (Path(root) / "assets" / "odicto-icon.svg").write_text("not an svg", encoding="utf-8")
            (Path(root) / "assets" / "odicto-icon-256.png").write_bytes(b"not a png")
            self.assertTrue(logo_icon(Path(root)).isNull())

    def test_the_tray_icon_is_the_logo_and_falls_back_to_a_plain_icon(self):
        import indicator

        hud = _make_hud()
        try:
            icon = hud._make_tray_icon()
            self.assertFalse(icon.isNull())
            self.assertEqual(icon.pixmap(64, 64).toImage().pixelColor(32, 8).red(), 0x1B)
            with tempfile.TemporaryDirectory() as empty, patch.object(indicator, "ROOT", Path(empty)):
                fallback = hud._make_tray_icon()
            self.assertFalse(fallback.isNull())  # the tray still gets an icon
            self.assertNotEqual(fallback.pixmap(24, 24).toImage().pixelColor(12, 12).red(), 0x1B)
        finally:
            _close_hud(hud)


# --------------------------------------------------------------------------- HUD idle
def _make_hud(**attrs):
    from unittest.mock import MagicMock

    from app_state import AppState
    from indicator import DictationIndicator

    _qt_app()
    app = MagicMock()
    app.state = AppState.IDLE
    app.last_status = None
    app.use_llm = False
    app.ready = True
    app.recorder = None
    app.live_active = False
    app.live_preview = ""
    app.status_detail = None
    for name, value in attrs.items():
        setattr(app, name, value)
    return DictationIndicator(app)


def _close_hud(hud) -> None:
    hud._tick.stop()
    hud._hide_timer.stop()
    if getattr(hud, "_tray", None) is not None:
        hud._tray.hide()
    hud.close()


def _settle(hud) -> None:
    """Jump to the end of every animation the way real time would, then tick once."""
    for tween in (hud._w_tw, hud._h_tw):
        tween.snap(tween.b)
    hud._phase = "open"
    hud._phase_t0 = time.monotonic() - 10
    hud._content_fade = hud._glyph_fade = hud._check_progress = 1.0
    hud._resize_hold = None
    hud._on_tick()


class TestHudRestsWhenIdle(unittest.TestCase):
    def test_the_paint_timer_stops_when_the_pill_is_hidden_and_resumes_when_shown(self):
        from indicator import GuiState

        hud = _make_hud()
        try:
            self.assertTrue(hud._tick.isActive())  # the boot entrance animates
            hud._do_hide()
            hud._phase_t0 -= 10  # the exit has long finished
            hud._on_tick()
            self.assertEqual(hud._phase, "hidden")
            self.assertFalse(hud._tick.isActive())
            hud._transition_to(GuiState.RECORDING)
            self.assertTrue(hud._tick.isActive())
            self.assertEqual(hud._tick.interval(), 16)
        finally:
            _close_hud(hud)

    def test_a_standing_error_repaints_slowly_and_wakes_on_any_change(self):
        from app_state import AppState
        from indicator import GuiState

        hud = _make_hud(ready=False, last_status="init_error", status_detail="mic")
        try:
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.ERROR)
            _settle(hud)
            self.assertTrue(hud._tick.isActive())
            self.assertEqual(hud._tick.interval(), 250)
            hud.app.status_detail = "hotkeys"  # a retry reports a new reason
            hud._sync_from_app()
            self.assertEqual(hud._tick.interval(), 16)  # the width change animates at full speed
            _settle(hud)
            hud.app.state = AppState.RECORDING
            hud._sync_from_app()
            self.assertEqual(hud.gui_state, GuiState.RECORDING)
            _settle(hud)
            self.assertEqual(hud._tick.interval(), 16)  # recording is never slowed
        finally:
            _close_hud(hud)

    def test_the_slow_frames_are_identical_to_the_fast_ones(self):
        hud = _make_hud(ready=False, last_status="init_error", status_detail="mic")
        try:
            hud._sync_from_app()
            _settle(hud)
            self.assertEqual(hud._tick.interval(), 250)
            first = hud.grab().toImage()
            for _ in range(40):  # what the 60 FPS timer would have drawn meanwhile
                hud._on_tick()
            second = hud.grab().toImage()
            self.assertEqual(first, second)
        finally:
            _close_hud(hud)

    def test_a_pill_that_is_still_animating_keeps_the_fast_tick(self):
        hud = _make_hud(ready=False, last_status="init_error", status_detail="mic")
        try:
            hud._sync_from_app()
            _settle(hud)
            hud._glyph_fade = 0.2  # a cross-fade in progress
            hud._on_tick()
            self.assertEqual(hud._tick.interval(), 16)
        finally:
            _close_hud(hud)


if __name__ == "__main__":
    unittest.main()
