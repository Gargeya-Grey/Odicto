"""Production action dispatch with fake hooks and no microphone or input injection."""
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_state import AppState
from config import Config, normalize_modifier_name
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
                            "CTRL_KEEP_CONTEXT_KEYS": ("f6",), "POLISH_HOTKEY": "f8", "PLAY_AUDIO_CUES": False,
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

    def test_polish_key_suppresses_repeat_and_dispatches_once_per_tap(self):
        self.held = set()
        with patch.object(self.app, "_dispatch_hotkey") as dispatch:
            self.assertFalse(self.key("down", "f8"))
            self.assertFalse(self.key("down", "f8"))
            self.assertFalse(self.key("up", "f8"))
            dispatch.assert_called_once_with("polish")
            self.assertFalse(self.key("down", "f8"))
            self.assertEqual(dispatch.call_count, 2)

    def test_bare_polish_key_does_not_capture_modified_application_shortcuts(self):
        for modifier in ("shift", "ctrl", "alt", "cmd"):
            with self.subTest(modifier=modifier), patch.object(self.app, "_dispatch_hotkey") as dispatch:
                self.held = {modifier}
                self.assertTrue(self.key("down", "f8"))
                self.assertTrue(self.key("up", "f8"))
                dispatch.assert_not_called()

    def test_accepted_key_repeats_stay_suppressed_after_modifiers_change(self):
        for key, initial in (("grave", {"ctrl"}), ("f8", set())):
            with self.subTest(key=key), patch.object(self.app, "_dispatch_hotkey") as dispatch:
                self.held = initial
                self.assertFalse(self.key("down", key))
                self.held = {"shift"}
                self.assertFalse(self.key("down", key))
                self.assertFalse(self.key("up", key))
                self.assertEqual(dispatch.call_count, 1)

    def test_slow_reconnect_returns_hook_immediately_and_orders_stop(self):
        entered, resume, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        calls, seen = [], {}
        def start():
            calls.append("start")
            seen["thread"] = threading.get_ident()
            entered.set()
            # Set only after the hook calls below return: run inline, this wait times out.
            seen["resumed"] = resume.wait(5)
        def stop():
            calls.append("stop")
            stopped.set()
        self.app.recorder.start.side_effect = start
        self.app.on_release = stop
        self.assertFalse(self.key())
        self.assertTrue(entered.wait(1))
        self.assertNotEqual(seen["thread"], threading.get_ident(), "a slow start must not run on the hook thread")
        self.assertFalse(self.key(), "key repeat stays suppressed")
        self.key("up")
        self.key()
        self.assertEqual(calls, ["start"])
        resume.set()
        self.assertTrue(stopped.wait(1))
        self.assertTrue(seen["resumed"], "the hook must return while start() is still running")
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
        self.key("up")
        self.held = set()
        self.assertTrue(self.key(), "an unrelated physical key press must pass through")
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


class TestPolishModifierAliases(unittest.TestCase):
    def bind(self, chord, physical):
        stack = ExitStack()
        self.addCleanup(stack.close)
        hooks, app = {}, app_fixture()
        stack.enter_context(patch.multiple(Config, HOTKEY="ctrl+grave", AI_HOTKEY="ctrl+shift+grave",
            POLISH_HOTKEY=chord, RESET_CONTEXT_HOTKEY="", LIVE_HOTKEY="f7", CANCEL_HOTKEY="esc",
            CTRL_KEEP_CONTEXT_KEYS=("f6",)))
        stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
        stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: hooks.update({key: fn})))

        def pressed(key):
            requested = normalize_modifier_name(key)
            if requested.startswith(("left ", "right ")):
                return requested in physical
            return any(normalize_modifier_name(value.removeprefix("left ").removeprefix("right ")) == requested
                       for value in physical)

        stack.enter_context(patch("main.platforms.is_pressed", side_effect=pressed))
        stack.enter_context(patch("main.platforms.is_pressed_exclusive", side_effect=lambda key: key in physical))
        dispatch = stack.enter_context(patch.object(app, "_dispatch_hotkey"))
        app._bind_hotkeys()
        return hooks[chord.split("+")[-1]], dispatch

    def test_supported_modifier_aliases_dispatch_through_production_binder(self):
        for modifier in ("ctrl", "control", "shift", "alt", "option", "cmd", "command", "win", "windows", "super"):
            with self.subTest(modifier=modifier):
                handler, dispatch = self.bind(modifier + "+p", {normalize_modifier_name(modifier)})
                self.assertFalse(handler(SimpleNamespace(event_type="down")))
                self.assertFalse(handler(SimpleNamespace(event_type="up")))
                dispatch.assert_called_once_with("polish")

    def test_side_modifiers_accept_only_the_requested_side(self):
        for alias in ("ctrl", "control", "shift", "alt", "option", "cmd", "command", "win", "windows", "super"):
            for side, opposite in (("left", "right"), ("right", "left")):
                requested = normalize_modifier_name(side + " " + alias)
                other = normalize_modifier_name(opposite + " " + alias)
                for physical, accepted in (({requested}, True), ({other}, False), ({requested, other}, False)):
                    with self.subTest(alias=alias, side=side, physical=physical):
                        handler, dispatch = self.bind(side + " " + alias + "+p", physical)
                        self.assertEqual(handler(SimpleNamespace(event_type="down")), not accepted)
                        self.assertEqual(handler(SimpleNamespace(event_type="up")), not accepted)
                        self.assertEqual(dispatch.call_count, int(accepted))

    def test_alias_chord_repeats_remain_suppressed_after_modifier_release(self):
        physical = {"left windows"}
        handler, dispatch = self.bind("left win+p", physical)
        self.assertFalse(handler(SimpleNamespace(event_type="down")))
        physical.clear()
        self.assertFalse(handler(SimpleNamespace(event_type="down")))
        self.assertFalse(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_called_once_with("polish")
        self.assertTrue(handler(SimpleNamespace(event_type="down")))

    def test_generic_chords_still_require_exact_modifier_families(self):
        for physical, accepted in (({"ctrl", "shift"}, True), ({"ctrl"}, False),
                                   ({"ctrl", "shift", "alt"}, False)):
            handler, dispatch = self.bind("control+shift+p", physical)
            self.assertEqual(handler(SimpleNamespace(event_type="down")), not accepted)
            self.assertEqual(dispatch.call_count, int(accepted))

    def test_x11_masks_accept_the_same_modifier_aliases(self):
        from tests.test_x11_backend import TestX11Backend
        backend = TestX11Backend().fixture()
        for alias, mask in (("control", 4), ("option", 8), ("command", 64), ("super", 64), ("left win", 64)):
            with self.subTest(alias=alias):
                self.assertIn(mask, backend.masks(((alias,),)))

    def test_non_modifier_prefix_is_rejected_before_binding(self):
        with self.assertRaisesRegex(ValueError, "Unsupported shortcut modifier"):
            Config.validate_polish_hotkey("f6+p", "ctrl+grave")

    def test_conflicting_exclusive_modifier_sides_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "both exclusive modifier sides"):
            Config.validate_polish_hotkey("left control+right ctrl+p", "ctrl+grave")


class TestMacModifierSides(unittest.TestCase):
    def setUp(self):
        from platforms import macos
        self.mac = macos
        self.native_query = macos._modifier_key_state
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(macos, "_pynput", SimpleNamespace(KeyCode=type("FakeKeyCode", (), {}))))
        stack.enter_context(patch.object(macos, "_pressed_tokens", set()))
        stack.enter_context(patch.object(macos, "_pressed_keys", {}))
        stack.enter_context(patch.object(macos, "_hooks", {}))
        stack.enter_context(patch.object(macos, "_modifier_key_state", return_value=None))

    def translated_chord(self, chord, sides, accepted, character="π", virtual_key=35,
                         released_character="p", action="polish", dictation="ctrl+grave",
                         cancel="esc", reset=""):
        mac, app = self.mac, app_fixture()
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(Config, HOTKEY=dictation, AI_HOTKEY="ctrl+shift+" + dictation.split("+")[-1],
                POLISH_HOTKEY=chord, RESET_CONTEXT_HOTKEY=reset, LIVE_HOTKEY="f7", CANCEL_HOTKEY=cancel,
                CTRL_KEEP_CONTEXT_KEYS=("f6",)))
            stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
            stack.enter_context(patch("main.platforms.hotkey_backend_name", return_value="pynput"))
            stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: mac.hook_key(
                key, fn, kw["suppress"], **({"can_decline": kw["can_decline"]} if "can_decline" in kw else {}))))
            stack.enter_context(patch("main.platforms.is_pressed", side_effect=mac.is_pressed))
            stack.enter_context(patch("main.platforms.is_pressed_exclusive", side_effect=mac.is_pressed_exclusive))
            stack.enter_context(patch.object(mac, "_listener", object()))
            stack.enter_context(patch.object(mac, "_primary_virtual_key", side_effect=lambda token: {"p": 35, "grave": 50}.get(token)))
            dispatch = stack.enter_context(patch.object(app, "_dispatch_hotkey"))
            app._bind_hotkeys()
            modifiers = [SimpleNamespace(name={56: "shift", 59: "ctrl"}.get(code, "alt"),
                                         value=SimpleNamespace(vk=code)) for code in sides]
            for modifier in modifiers:
                mac._on_press(modifier)
            key = mac._pynput.KeyCode()
            key.char, key.vk = character, virtual_key
            event = object()
            for _ in range(2):
                mac._on_press(key)
                self.assertIs(mac._intercept(None, event), None if accepted else event)
            for modifier in modifiers:
                mac._on_release(modifier)
            key.char = released_character
            mac._on_release(key)
            self.assertIs(mac._intercept(None, event), None if accepted else event)
            self.assertEqual(dispatch.call_count, int(accepted))
            if accepted:
                self.assertEqual(dispatch.call_args.args[0], action)
                if action == "primary":
                    self.assertTrue(dispatch.call_args.args[2])
            self.assertFalse(mac.is_pressed("p"))
            self.assertFalse(mac._pressed_tokens & {"p", "grave", "~"})
            self.assertFalse(mac._pressed_keys)

    def test_option_translated_character_keeps_physical_primary_and_release(self):
        self.translated_chord("option+p", (58,), True)

    def test_shifted_symbol_polish_is_not_shadowed_by_dictation_primary(self):
        self.translated_chord("shift+~", (56,), True, "~", 50, "`")

    def test_ai_chord_still_works_when_shifted_symbol_has_a_polish_binding(self):
        self.translated_chord("shift+~", (59, 56), True, "~", 50, "`", "primary")

    def test_polish_symbol_does_not_also_dispatch_passthrough_cancel(self):
        self.translated_chord("shift+~", (56,), True, "~", 50, "`",
                              dictation="ctrl+p", cancel="grave")

    def test_polish_symbol_release_does_not_dispatch_passthrough_reset(self):
        self.translated_chord("shift+~", (56,), True, "~", 50, "`",
                              dictation="ctrl+p", reset="grave")

    def test_unmatched_symbol_release_clears_pressed_state_when_modifiers_change(self):
        self.translated_chord("shift+~", (56, 58), False, "~", 50, "`", dictation="ctrl+p")

    def observer_ownership(self, observer, failed_polish=False):
        mac, app = self.mac, app_fixture()
        with ExitStack() as stack:
            stack.enter_context(patch.multiple(Config, HOTKEY="ctrl+p", AI_HOTKEY="ctrl+shift+p",
                POLISH_HOTKEY="shift+~", RESET_CONTEXT_HOTKEY="grave" if observer == "reset" else "",
                CANCEL_HOTKEY="grave" if observer == "cancel" else "esc", LIVE_HOTKEY="f7",
                CTRL_KEEP_CONTEXT_KEYS=("f6",)))
            stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
            stack.enter_context(patch("main.platforms.hotkey_backend_name", return_value="pynput"))
            stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: mac.hook_key(key, fn, **kw)))
            stack.enter_context(patch("main.platforms.is_pressed", side_effect=mac.is_pressed))
            stack.enter_context(patch("main.platforms.is_pressed_exclusive", side_effect=mac.is_pressed_exclusive))
            stack.enter_context(patch.object(mac, "_listener", object()))
            stack.enter_context(patch.object(mac, "_primary_virtual_key", side_effect=lambda token: {"p": 35, "grave": 50}.get(token)))
            dispatch = stack.enter_context(patch.object(app, "_dispatch_hotkey"))
            app._bind_hotkeys()
            shift = SimpleNamespace(name="shift", value=SimpleNamespace(vk=56))
            key, event = mac._pynput.KeyCode(), object()
            key.vk = 50
            if failed_polish:
                mac._on_press(shift)
                key.char = "~"
                dispatch.side_effect = RuntimeError("Synthetic dispatch failure")
                mac._on_press(key)
                self.assertIs(mac._intercept(None, event), event)
                dispatch.side_effect = None
                mac._on_release(shift)
                key.char = "`"
                mac._on_release(key)
                self.assertIs(mac._intercept(None, event), event)
                self.assertEqual([call.args[0] for call in dispatch.call_args_list], ["polish"])
            else:
                key.char = "`"
                mac._on_press(key)
                self.assertIs(mac._intercept(None, event), event)
                mac._on_press(shift)
                key.char = "~"
                for _ in range(2):
                    mac._on_press(key)
                    self.assertIs(mac._intercept(None, event), event)
                mac._on_release(shift)
                key.char = "`"
                mac._on_release(key)
                self.assertIs(mac._intercept(None, event), event)
                expected = ["cancel"] * 3 if observer == "cancel" else ["reset"]
                self.assertEqual([call.args[0] for call in dispatch.call_args_list], expected)
            self.assertFalse(mac._pressed_keys)
            self.assertFalse(mac._pressed_tokens & {"grave", "~"})
            dispatch.reset_mock()
            mac._on_press(shift)
            key.char = "~"
            mac._on_press(key)
            mac._on_release(key)
            mac._on_release(shift)
            dispatch.assert_called_once_with("polish")

    def test_cancel_owns_repeats_and_release_after_modifier_changes(self):
        self.observer_ownership("cancel")

    def test_reset_owns_repeats_and_release_after_modifier_changes(self):
        self.observer_ownership("reset")

    def test_failed_polish_press_cannot_redirect_release_to_reset(self):
        self.observer_ownership("reset", failed_polish=True)

    def test_unowned_press_cannot_select_a_changed_character_release_action(self):
        mac = self.mac
        reset = MagicMock(return_value=True)
        with patch.object(mac, "_listener", object()), patch.object(mac, "_primary_virtual_key", return_value=None):
            mac.hook_key("~", reset, False)
            key = mac._pynput.KeyCode()
            key.vk, key.char = 50, "`"
            mac._on_press(key)
            key.char = "~"
            mac._on_release(key)
        reset.assert_not_called()

    def test_side_specific_option_translated_character_dispatches_once(self):
        self.translated_chord("right option+p", (61,), True)

    def test_side_specific_option_rejects_opposite_and_both_sides(self):
        for sides in ((58,), (58, 61)):
            with self.subTest(sides=sides):
                self.translated_chord("right option+p", sides, False)

    def test_physical_codes_preserve_generic_and_side_specific_state(self):
        for family, left, right in (("ctrl", 59, 62), ("shift", 56, 60), ("alt", 58, 61), ("cmd", 55, 54)):
            with self.subTest(family=family):
                left_key = SimpleNamespace(name=family, value=SimpleNamespace(vk=left))
                right_key = SimpleNamespace(name=family + "_r", value=SimpleNamespace(vk=right))
                self.mac._on_press(left_key)
                self.assertTrue(self.mac.is_pressed(family))
                self.assertTrue(self.mac.is_pressed_exclusive("left " + family))
                self.assertFalse(self.mac.is_pressed_exclusive("right " + family))
                self.mac._on_press(right_key)
                self.assertTrue(self.mac.is_pressed(family))
                self.assertFalse(self.mac.is_pressed_exclusive("left " + family))
                self.assertFalse(self.mac.is_pressed_exclusive("right " + family))
                self.mac._on_release(left_key)
                self.assertTrue(self.mac.is_pressed(family))
                self.assertTrue(self.mac.is_pressed_exclusive("right " + family))
                self.mac._on_release(right_key)
                self.assertFalse(self.mac.is_pressed(family))

    def test_aliases_read_the_same_physical_command_side(self):
        key = SimpleNamespace(name="cmd", value=SimpleNamespace(vk=55))
        self.mac._on_press(key)
        for alias in ("cmd", "command", "win", "windows", "super"):
            self.assertTrue(self.mac.is_pressed(alias))
            self.assertTrue(self.mac.is_pressed_exclusive("left " + alias))
            self.assertFalse(self.mac.is_pressed_exclusive("right " + alias))
        self.mac._on_release(key)

    def test_native_modifier_state_overrides_ambiguous_pynput_release_events(self):
        import sys
        pressed = {59, 62}
        quartz = SimpleNamespace(kCGEventSourceStateCombinedSessionState=0,
            CGEventSourceKeyState=lambda state, code: code in pressed)
        with patch.dict(sys.modules, Quartz=quartz), patch.object(self.mac, "_modifier_key_state", self.native_query):
            self.mac._on_press(SimpleNamespace(name="ctrl", value=SimpleNamespace(vk=59)))
            self.mac._on_press(SimpleNamespace(name="ctrl_r", value=SimpleNamespace(vk=62)))
            self.assertTrue(self.mac.is_pressed("control"))
            self.assertFalse(self.mac.is_pressed_exclusive("left ctrl"))
            self.assertFalse(self.mac.is_pressed_exclusive("right ctrl"))
            pressed.remove(59)
            self.mac._on_press(SimpleNamespace(name="ctrl", value=SimpleNamespace(vk=59)))
            self.assertFalse(self.mac.is_pressed("left control"))
            self.assertTrue(self.mac.is_pressed_exclusive("right control"))
            pressed.clear()
            self.assertFalse(self.mac.is_pressed("ctrl"))

    def test_unhook_clears_tracked_modifier_state(self):
        with patch.object(self.mac, "_listener", None):
            self.mac._on_press(SimpleNamespace(name="shift", value=SimpleNamespace(vk=56)))
            self.mac.unhook_all()
            self.assertFalse(self.mac.is_pressed("shift"))


if __name__ == "__main__":
    unittest.main()
