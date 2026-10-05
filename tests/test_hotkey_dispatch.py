"""Production action dispatch with fake hooks and no microphone or input injection."""
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_state import AppState
from config import Config
from tests.test_reliability import app_fixture


class TestHotkeyDispatch(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app = app_fixture()
        self.app.state = AppState.IDLE
        self.app._runtime_enabled = True
        self.app._live_key_held = False
        self.app._pressed_mods_at_press = ()
        self.app.pid_file = "unused-test-pid"
        self.held = {"ctrl"}
        self.hooks = {}
        for name, value in {"HOTKEY": "ctrl+grave", "AI_HOTKEY": "ctrl+shift+grave",
                            "HOTKEY_TOGGLE": True, "RESET_CONTEXT_HOTKEY": "", "LIVE_HOTKEY": "f7",
                            "CTRL_KEEP_CONTEXT_KEYS": ("f6",), "PLAY_AUDIO_CUES": False,
                            "RETRIGGER_COOLDOWN_MS": 0}.items():
            self.stack.enter_context(patch.object(Config, name, value))
        self.stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
        self.stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: self.hooks.update({key: fn})))
        self.stack.enter_context(patch("main.platforms.is_pressed", side_effect=lambda key: key in self.held))
        self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.stack.enter_context(patch("main.platforms.lock_is_held", return_value=False))
        self.app._start_hotkey_worker()
        self.addCleanup(self.app._shutdown)
        self.app._bind_hotkeys()

    def key(self, kind="down", key="grave"):
        return self.hooks[key](SimpleNamespace(event_type=kind))

    def test_slow_reconnect_returns_hook_immediately_and_orders_stop(self):
        entered, resume, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        calls = []
        def start():
            calls.append("start")
            entered.set()
            self.assertTrue(resume.wait(2))
        def stop():
            calls.append("stop")
            stopped.set()
        self.app.recorder.start.side_effect = start
        self.app.on_release = stop
        before = time.monotonic()
        self.assertFalse(self.key())
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.key(), "key repeat stays suppressed")
        self.key("up")
        self.key()
        self.assertEqual(calls, ["start"])
        resume.set()
        self.assertTrue(stopped.wait(1))
        self.assertEqual(calls, ["start", "stop"])

    def test_queued_capture_preserves_its_modifier_snapshot(self):
        entered, resume, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        observed = []
        def press(use_llm):
            entered.set()
            self.assertTrue(resume.wait(2))
            observed.append((use_llm, self.app._pressed_mods_at_press))
            done.set()
        self.app.on_press = press
        self.held = {"ctrl", "shift", "f6"}
        self.key()
        self.assertTrue(entered.wait(1))
        self.held = set()
        self.assertTrue(self.key(), "an unrelated key event must pass through")
        resume.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(observed, [(True, ("ctrl", "shift", "f6"))])

    def test_shutdown_skips_queued_actions(self):
        entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def blocked_press(use_llm):
            entered.set()
            resume.wait(2)
            finished.set()
        self.app.on_press = MagicMock(side_effect=blocked_press)
        self.key()
        self.assertTrue(entered.wait(1))
        self.key("up")
        self.key()
        self.app._shutdown()
        resume.set()
        self.assertTrue(finished.wait(1))
        self.app._hotkey_worker.join(1)
        self.assertFalse(self.app._hotkey_worker.is_alive())
        self.assertEqual(self.app.on_press.call_count, 1)

    def test_live_recording_is_announced_only_after_reconnect(self):
        observed = []
        self.app.recorder.start.side_effect = lambda: observed.append(self.app.state)
        with patch.object(Config, "effective_live_stt_provider", return_value="whisper"):
            self.app.on_live_toggle()
        self.assertEqual(observed, [AppState.IDLE])
        self.assertEqual(self.app.state, AppState.RECORDING)


if __name__ == "__main__":
    unittest.main()
