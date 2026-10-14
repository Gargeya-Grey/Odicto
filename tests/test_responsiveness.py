"""Hotkey responsiveness on Windows: OS modifier truth and the owner's process settings.

Fault kinds covered: a key-up the keyboard library never saw (stale table), a failing
Win32 call, and a process that Windows would page out or throttle. No real hook is
installed; the process-settings check runs in a child process.
"""
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_state import AppState
from config import Config

WINDOWS = sys.platform == "win32"
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "lwin": 0x5B, "rwin": 0x5C}


def fake_os_state(*down):
    codes = {VK[k] for k in down}
    return lambda: (lambda vk: -32768 if vk in codes else 0)


@unittest.skipUnless(WINDOWS, "Windows backend")
class TestModifierTruth(unittest.TestCase):
    def setUp(self):
        import platforms.windows as win
        self.win = win
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # The keyboard library's table missed a Shift key-up: it still says Shift is down.
        self.table = {"shift", "f6"}
        self.stack.enter_context(patch.object(win, "_keyboard_is_pressed", side_effect=lambda k: k in self.table))

    def test_modifiers_follow_the_os_not_a_stale_table(self):
        self.stack.enter_context(patch.object(self.win, "_async_key_state", fake_os_state("ctrl")))
        self.assertTrue(self.win.is_pressed("ctrl"))
        self.assertFalse(self.win.is_pressed("shift"), "a lost Shift key-up must not count as held")
        self.assertFalse(self.win.is_pressed("alt"))

    def test_either_windows_key_counts_as_cmd(self):
        self.stack.enter_context(patch.object(self.win, "_async_key_state", fake_os_state("rwin")))
        self.assertTrue(self.win.is_pressed("cmd"))
        self.assertTrue(self.win.is_pressed("windows"))

    def test_other_keys_still_use_the_keyboard_library(self):
        self.stack.enter_context(patch.object(self.win, "_async_key_state", fake_os_state()))
        self.assertTrue(self.win.is_pressed("f6"))
        self.assertFalse(self.win.is_pressed("grave"))

    def test_a_failing_os_call_falls_back_to_the_table(self):
        self.stack.enter_context(patch.object(self.win, "_async_key_state", side_effect=OSError("no user32")))
        self.assertTrue(self.win.is_pressed("shift"))
        self.assertFalse(self.win.is_pressed("ctrl"))

    def test_the_real_os_call_answers(self):
        # Nothing is held while the suite runs headless; the call must work and return a bool.
        self.stack.close()
        self.assertIsInstance(self.win.is_pressed("ctrl"), bool)


@unittest.skipUnless(WINDOWS, "Windows backend")
class TestStaleShiftKeepsRawChordRaw(unittest.TestCase):
    """The owner's report: the raw chord started AI mode. Drive the real hook handler."""

    def setUp(self):
        import platforms.windows as win
        from tests.test_reliability import app_fixture

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app = app_fixture()
        self.app.state = AppState.IDLE
        self.app._runtime_enabled = True
        self.app._live_key_held = False
        self.app._pressed_mods_at_press = ()
        self.app.pid_file = "unused-test-pid"
        self.hooks = {}
        for name, value in {"HOTKEY": "ctrl+grave", "AI_HOTKEY": "shift+grave",
                            "HOTKEY_TOGGLE": True, "RESET_CONTEXT_HOTKEY": "", "LIVE_HOTKEY": "f7",
                            "CTRL_KEEP_CONTEXT_KEYS": ("f6",), "PLAY_AUDIO_CUES": False,
                            "RETRIGGER_COOLDOWN_MS": 0}.items():
            self.stack.enter_context(patch.object(Config, name, value))
        self.stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
        self.stack.enter_context(patch("main.platforms.hook_key",
                                       side_effect=lambda key, fn, **kw: self.hooks.update({key: fn})))
        self.stack.enter_context(patch("main.platforms.unhook_all"))
        self.stack.enter_context(patch("main.platforms.lock_is_held", return_value=False))
        # The real Windows is_pressed, over a stale library table and a fake OS state.
        self.stack.enter_context(patch.object(win, "_keyboard_is_pressed", side_effect=lambda k: k == "shift"))
        self.stack.enter_context(patch.object(win, "_async_key_state", fake_os_state("ctrl")))
        self.stack.enter_context(patch("main.platforms.is_pressed", win.is_pressed))
        self.app._start_hotkey_worker()
        self.addCleanup(self.app._shutdown)
        self.app._bind_hotkeys()

    def test_raw_chord_with_a_stale_shift_starts_raw_dictation(self):
        chosen, done = [], threading.Event()
        def press(use_llm):
            chosen.append(use_llm)
            done.set()
        self.app.on_press = press
        self.assertFalse(self.hooks["grave"](SimpleNamespace(event_type="down")), "the chord is suppressed")
        self.assertTrue(done.wait(1))
        self.assertEqual(chosen, [False], "Ctrl+key with a lost Shift key-up must stay raw dictation")


@unittest.skipUnless(WINDOWS, "Windows backend")
class TestKeepResponsive(unittest.TestCase):
    def test_settings_apply_in_a_real_process(self):
        # Start the child as the admin-mode logon task does: below-normal CPU, low I/O.
        code = (
            "import json, sys, psutil; sys.path.insert(0, sys.argv[1]);"
            "p = psutil.Process(); p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS); p.ionice(psutil.IOPRIO_LOW);"
            "import platforms.windows as w; notes = w.keep_responsive();"
            "print(json.dumps({'notes': notes, 'above': p.nice() == psutil.ABOVE_NORMAL_PRIORITY_CLASS,"
            " 'io_normal': p.ionice() == psutil.IOPRIO_NORMAL}))"
        )
        out = subprocess.run([sys.executable, "-c", code, APP_DIR], capture_output=True,
                             text=True, timeout=60, check=True)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(result["notes"], ["priority above normal", "power throttling off",
                                           "memory and I/O priority normal", "192 MB kept resident"])
        self.assertTrue(result["above"])
        self.assertTrue(result["io_normal"])

    def test_never_raises_without_kernel32(self):
        import platforms.windows as win
        with patch.object(win, "_kernel32", side_effect=OSError("gone")):
            notes = win.keep_responsive()
        self.assertEqual(len(notes), 1)
        self.assertIn("unavailable", notes[0])

    def test_each_failure_is_reported_and_the_rest_still_run(self):
        import platforms.windows as win
        k32 = MagicMock()
        k32.SetPriorityClass.return_value = 0
        k32.SetProcessInformation.side_effect = AttributeError("old Windows")
        k32.SetProcessWorkingSetSizeEx.return_value = 1
        with patch.object(win, "_kernel32", return_value=k32):
            notes = win.keep_responsive(resident_mb=64)
        self.assertTrue(notes[0].startswith("priority unchanged"))
        self.assertTrue(notes[1].startswith("power throttling unchanged"))
        self.assertTrue(notes[2].startswith("memory"))
        self.assertEqual(notes[3], "64 MB kept resident")
        low = 64 * 1024 * 1024
        args = k32.SetProcessWorkingSetSizeEx.call_args[0]
        self.assertEqual((args[1], args[2], args[3]), (low, low * 4, 0x1 | 0x8))


    def test_windows_10_retries_power_throttling_without_the_timer_flag(self):
        import platforms.windows as win
        k32 = MagicMock()
        masks = []
        def set_info(proc, cls, ref, size):
            if cls != 4:
                return 1
            masks.append(ref._obj.ControlMask)
            return 0 if ref._obj.ControlMask & 0x4 else 1  # Windows 10 refuses the 0x4 flag
        k32.SetProcessInformation.side_effect = set_info
        with patch.object(win, "_kernel32", return_value=k32):
            notes = win.keep_responsive()
        self.assertEqual(masks, [0x5, 0x1])
        self.assertEqual(notes[1], "power throttling off")


class TestOwnerAppliesSettings(unittest.TestCase):
    def test_claim_install_applies_and_logs_once_the_lock_is_held(self):
        import main
        with patch.object(main, "acquire_single_instance_lock", return_value=True), \
                patch.object(main.platforms, "kill_other_odicto_processes"), \
                patch.object(main.platforms, "keep_responsive", return_value=["a", "b"]) as keep, \
                patch("builtins.print") as out:
            self.assertTrue(main.claim_install("unused-test-pid"))
        keep.assert_called_once_with()
        out.assert_any_call("Responsiveness: a; b.", flush=True)

    def test_no_settings_without_the_lock(self):
        import main
        with patch.object(main, "acquire_single_instance_lock", return_value=False), \
                patch.object(main.platforms, "keep_responsive") as keep:
            self.assertFalse(main.claim_install("unused-test-pid"))
        keep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
