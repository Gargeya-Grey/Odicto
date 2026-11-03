"""X11 grab and replay decisions without a display, hooks or injected input."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, patch

from platforms import _x11


def bitmap(*codes):
    result = bytearray(32)
    for code in codes:
        result[code // 8] |= 1 << (code % 8)
    return result


class TestX11Backend(unittest.TestCase):
    def fixture(self):
        constants = SimpleNamespace(KeyPress=2, KeyRelease=3, AnyModifier=32768,
            AnyKey=0, GrabModeAsync=1, GrabModeSync=0, CurrentTime=0,
            ReplayKeyboard=5, SyncKeyboard=3, AsyncKeyboard=4, GrabSuccess=0,
            ControlMask=4, ShiftMask=1, LockMask=2, Mod1Mask=8, Mod4Mask=64)
        display = MagicMock()
        display.query_keymap.return_value = bitmap()
        display.pending_events.return_value = 0
        display.get_modifier_mapping.return_value = [[], [], [], [], [77], [], [], []]
        display.screen.return_value.root.grab_keyboard.return_value = 0
        names = {"grave": 49, "F8": 74, "Control_L": 37, "Control_R": 105, "Num_Lock": 77}
        keysyms = SimpleNamespace(string_to_keysym=lambda name: names.get(name, 0))
        display.keysym_to_keycode.side_effect = lambda symbol: symbol
        return _x11._Backend(display, constants, keysyms)

    def event(self, backend, kind, code=74, state=0):
        return SimpleNamespace(type=backend.X.KeyPress if kind == "down" else backend.X.KeyRelease,
                               detail=code, state=state, time=123)

    def test_bind_uses_synchronous_passive_grab_without_starting_real_thread(self):
        backend = self.fixture()
        callback = MagicMock()
        with patch.object(_x11.threading, "Thread") as thread:
            backend.bind("f8", callback, True)
        self.assertEqual([call.args[1] for call in backend.root.grab_key.call_args_list], [0, 2, 16, 18])
        self.assertNotIn(backend.X.AnyModifier, [call.args[1] for call in backend.root.grab_key.call_args_list])
        thread.return_value.start.assert_called_once()
        self.assertEqual(backend.handlers[74], (callback, True, "f8"))

    def test_shared_primary_registers_only_configured_chords_and_lock_variants(self):
        backend = self.fixture()
        with patch.object(_x11.threading, "Thread"):
            backend.bind("grave", MagicMock(), True, modifier_sets=(("ctrl",), ("ctrl", "shift")))
        self.assertEqual({call.args[1] for call in backend.root.grab_key.call_args_list},
                         {4, 5, 6, 7, 20, 21, 22, 23})
        self.assertFalse(any(call.args[1] & backend.X.Mod1Mask for call in backend.root.grab_key.call_args_list))

    def test_production_binder_supplies_raw_ai_and_polish_chords_only_to_x11(self):
        from config import Config
        from tests.test_reliability import app_fixture
        app = app_fixture()
        with patch.multiple(Config, HOTKEY="ctrl+grave", AI_HOTKEY="ctrl+shift+grave",
                            POLISH_HOTKEY="ctrl+shift+p", LIVE_HOTKEY="f7", CANCEL_HOTKEY="esc",
                            RESET_CONTEXT_HOTKEY="f5"), patch("main.ensure_can_bind_hotkeys"), \
                patch("main.platforms.hotkey_backend_name", return_value="x11"), \
                patch("main.platforms.hook_key") as hook:
            app._bind_hotkeys()
        chords = {call.args[0]: call.kwargs["modifier_sets"] for call in hook.call_args_list}
        self.assertEqual(chords, {"grave": (("ctrl",), ("ctrl", "shift")),
                                 "p": (("ctrl", "shift"),), "f7": ((),), "esc": ((),), "f5": ((),)})

    def test_grab_conflict_is_reported_and_partial_grabs_released(self):
        backend = self.fixture()
        backend.root.grab_key.side_effect = lambda *args, **kwargs: kwargs["onerror"](RuntimeError(), None)
        with self.assertRaisesRegex(RuntimeError, "Another application owns"):
            backend.bind("f8", MagicMock(), True)
        backend.root.ungrab_key.assert_called_once_with(74, backend.X.AnyModifier)
        self.assertFalse(backend.handlers)
        self.assertIsNone(backend.thread)

    def test_nonmatching_primary_is_replayed_to_focused_application(self):
        backend = self.fixture()
        callback = MagicMock(return_value=True)
        backend.handlers[74] = (callback, True, "f8")
        backend.handle(self.event(backend, "down", state=backend.X.ShiftMask))
        backend.display.allow_events.assert_called_once_with(backend.X.ReplayKeyboard, 123)
        self.assertFalse(backend.suppressed)

    def test_suppressed_hotkey_down_and_up_do_not_replay(self):
        backend = self.fixture()
        callback = MagicMock(return_value=False)
        backend.handlers[74] = (callback, True, "f8")
        backend.handle(self.event(backend, "down"))
        self.assertEqual(backend.suppressed, {74})
        backend.handle(self.event(backend, "up"))
        self.assertEqual([call.args[0].event_type for call in callback.call_args_list], ["down", "up"])
        self.assertFalse(backend.suppressed)
        self.assertNotIn(backend.X.ReplayKeyboard,
                         [call.args[0] for call in backend.display.allow_events.call_args_list])
        backend.display.ungrab_keyboard.assert_called_once_with(123)

    def test_unrelated_key_during_hold_is_replayed_and_grab_resumed(self):
        backend = self.fixture()
        backend.handlers[74] = (MagicMock(return_value=False), True, "f8")
        backend.handle(self.event(backend, "down"))
        backend.handle(self.event(backend, "down", code=38))
        backend.display.allow_events.assert_any_call(backend.X.ReplayKeyboard, 123)
        backend.root.grab_keyboard.assert_called_once()
        self.assertEqual(backend.suppressed, {74})

    def test_failed_keyboard_regrab_raises_for_real_integer_status(self):
        backend = self.fixture()
        backend.suppressed.add(74)
        backend.root.grab_keyboard.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "could not resume"):
            backend._replay(123)

    def test_release_poll_notifies_once_after_replayed_key(self):
        backend = self.fixture()
        callback = MagicMock(return_value=True)
        backend.handlers[74] = (callback, False, "f8")
        backend.handle(self.event(backend, "down"))
        backend.poll_releases()
        backend.poll_releases()
        self.assertEqual([call.args[0].event_type for call in callback.call_args_list], ["down", "up"])

    def test_autorepeat_release_keeps_held_state(self):
        backend = self.fixture()
        callback = MagicMock(return_value=False)
        backend.handlers[74] = (callback, True, "f8")
        backend.handle(self.event(backend, "down"))
        backend.display.query_keymap.return_value = bitmap(74)
        backend.handle(self.event(backend, "up"))
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(backend.suppressed, {74})

    def test_modifier_snapshot_uses_server_event_state(self):
        backend = self.fixture()
        states = []
        def callback(event):
            states.append((backend.is_pressed("ctrl"), backend.is_pressed("shift")))
            return False
        backend.handlers[74] = (callback, True, "f8")
        backend.handle(self.event(backend, "down", state=backend.X.ControlMask))
        self.assertEqual(states, [(True, False)])

    def test_side_exclusive_modifiers_use_distinct_keycodes(self):
        backend = self.fixture()
        backend.display.query_keymap.return_value = bitmap(37)
        with patch.object(_x11, "_get_backend", return_value=backend):
            self.assertTrue(_x11.is_pressed_exclusive("left ctrl"))
            self.assertFalse(_x11.is_pressed_exclusive("right ctrl"))

    def test_close_releases_server_grabs_and_connection(self):
        backend = self.fixture()
        backend.close()
        backend.root.ungrab_key.assert_called_once_with(backend.X.AnyKey, backend.X.AnyModifier)
        backend.display.ungrab_keyboard.assert_called_once()
        backend.display.close.assert_called_once()

    def test_wayland_and_missing_display_fail_before_loading_xlib(self):
        for environment in ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, {}):
            with self.subTest(environment=environment), patch.dict(_x11.os.environ, environment, clear=True):
                with self.assertRaises(RuntimeError):
                    _x11.require_session()

    def test_text_injection_uses_private_stdin_and_preserves_newlines(self):
        with patch.dict(_x11.os.environ, {"DISPLAY": ":99"}, clear=True), \
                patch.object(_x11.shutil, "which", return_value="/bin/xdotool"), \
                patch.object(_x11.subprocess, "run") as run:
            self.assertTrue(_x11.send_text_bulk("private words\n--not-a-flag"))
        typed = [call for call in run.call_args_list if call.args[0][1] == "type"]
        self.assertEqual([call.args[0][-2:] for call in typed], [["--file", "-"], ["--file", "-"]])
        self.assertEqual([call.kwargs["input"] for call in typed], [b"private words", b"--not-a-flag"])
        self.assertEqual(run.call_args_list[1].args[0], ["/bin/xdotool", "key", "--clearmodifiers", "Return"])
        self.assertFalse(any("private words" in argument for call in run.call_args_list for argument in call.args[0]))
        self.assertTrue(all("shell" not in call.kwargs for call in run.call_args_list))

    def test_unsafe_characters_request_explicit_copy_before_any_input(self):
        from platforms.base import TextDeliveryUnavailable
        for text in ("safe prefix नमस्ते\nend", "café", "tab\ttext", "emoji 🙂"):
            with self.subTest(text=text), patch.object(_x11.subprocess, "run") as run:
                with self.assertRaises(TextDeliveryUnavailable):
                    _x11.send_text_bulk(text)
                run.assert_not_called()

    def test_failed_injection_raises_to_prevent_duplicate_fallback(self):
        with patch.dict(_x11.os.environ, {"DISPLAY": ":99"}, clear=True), \
                patch.object(_x11.shutil, "which", return_value="/bin/xdotool"), \
                patch.object(_x11.subprocess, "run", side_effect=_x11.subprocess.TimeoutExpired("xdotool", 1)):
            with self.assertRaisesRegex(RuntimeError, "possibly partial"):
                _x11.send_text_bulk("typed partly")
