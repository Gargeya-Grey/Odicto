"""Regression tests for macOS hotkey selection (Setup UI) and triggering (main + macos backend)."""
from __future__ import annotations

from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse
from http.server import ThreadingHTTPServer

import config
from config import Config, normalize_modifier_name
from tests.test_reliability import app_fixture

ROOT = Path(__file__).resolve().parents[1]


class TestLiveHotkeyModifiers(unittest.TestCase):
    def _bind_live(self, live_hotkey: str, physical: set[str]):
        app = app_fixture()
        hooks = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.multiple(
            Config,
            HOTKEY="ctrl+grave",
            AI_HOTKEY="ctrl+shift+grave",
            POLISH_HOTKEY="f8",
            RESET_CONTEXT_HOTKEY="",
            LIVE_HOTKEY=live_hotkey,
            CANCEL_HOTKEY="esc",
            CTRL_KEEP_CONTEXT_KEYS=("f6",),
        ))
        stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
        stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: hooks.update({key: (fn, kw)})))

        def pressed(key):
            requested = normalize_modifier_name(key)
            if requested.startswith(("left ", "right ")):
                return requested in physical
            return any(
                normalize_modifier_name(v.removeprefix("left ").removeprefix("right ")) == requested
                for v in physical
            )

        stack.enter_context(patch("main.platforms.is_pressed", side_effect=pressed))
        stack.enter_context(patch("main.platforms.is_pressed_exclusive", side_effect=lambda key: key in physical))
        dispatch = stack.enter_context(patch.object(app, "_dispatch_hotkey"))
        app._bind_hotkeys()
        return hooks, dispatch

    def test_live_hotkey_with_modifier_does_not_trigger_on_bare_key(self):
        physical: set[str] = set()
        hooks, dispatch = self._bind_live("ctrl+q", physical)
        self.assertIn("q", hooks)
        handler, kwargs = hooks["q"]
        # Bare 'q' must pass through (return True) and NOT trigger live dictation!
        self.assertTrue(handler(SimpleNamespace(event_type="down")))
        self.assertTrue(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_not_called()

        # Extra modifier (Cmd+Ctrl+Q) must also pass through.
        physical.update({"ctrl", "cmd"})
        self.assertTrue(handler(SimpleNamespace(event_type="down")))
        self.assertTrue(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_not_called()

        # Exact Ctrl+Q triggers once, suppresses down + repeat + up.
        physical.clear()
        physical.add("ctrl")
        self.assertFalse(handler(SimpleNamespace(event_type="down")))
        self.assertFalse(handler(SimpleNamespace(event_type="down")))
        physical.clear()
        self.assertFalse(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_called_once_with("live")
        # Subsequent bare 'q' still passes through cleanly.
        self.assertTrue(handler(SimpleNamespace(event_type="down")))

    def test_live_hotkey_side_specific_modifier(self):
        for physical, accepted in (({"right alt"}, True), ({"left alt"}, False), ({"left alt", "right alt"}, False)):
            with self.subTest(physical=physical):
                hooks, dispatch = self._bind_live("right option+q", set(physical))
                handler, _ = hooks["q"]
                self.assertEqual(handler(SimpleNamespace(event_type="down")), not accepted)
                self.assertEqual(handler(SimpleNamespace(event_type="up")), not accepted)
                self.assertEqual(dispatch.call_count, int(accepted))

    def test_live_hotkey_validation_rejects_bad_modifiers_bare_letters_and_save_conflicts(self):
        Config.validate_live_hotkey("f7", "ctrl+grave")
        Config.validate_live_hotkey("ctrl+q", "ctrl+grave")
        Config.validate_live_hotkey("", "ctrl+grave")

        with self.assertRaisesRegex(ValueError, "Unsupported shortcut modifier"):
            Config.validate_live_hotkey("f6+q", "ctrl+grave")
        with self.assertRaisesRegex(ValueError, "both exclusive modifier sides"):
            Config.validate_live_hotkey("left ctrl+right ctrl+q", "ctrl+grave")
        with self.assertRaisesRegex(ValueError, "modifier"):
            Config.validate_live_hotkey("q", "ctrl+grave")
        with self.assertRaisesRegex(ValueError, "different primary key"):
            Config.validate_live_hotkey("ctrl+grave", "ctrl+grave")

        import setup_web
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            prompt_path = os.path.join(tmp, "prompt.txt")
            with patch.object(setup_web, "ENV_PATH", env_path), \
                 patch.object(setup_web, "prompt_live_path", lambda: prompt_path), \
                 patch.object(setup_web, "restart_odicto") as restart:
                server = ThreadingHTTPServer(("127.0.0.1", 0), setup_web._Handler)
                server.odicto_token = "tok-live"
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    body = urllib.parse.urlencode({
                        "HOTKEY": "ctrl+q",
                        "AI_HOTKEY": "ctrl+shift+q",
                        "LIVE_HOTKEY": "q",
                    }).encode("utf-8")
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    try:
                        conn.request(
                            "POST",
                            "/save",
                            body,
                            {
                                "Host": f"127.0.0.1:{port}",
                                "X-Odicto-Token": "tok-live",
                                "Content-Type": "application/x-www-form-urlencoded",
                            },
                        )
                        resp = conn.getresponse()
                        html_body = resp.read().decode("utf-8")
                    finally:
                        conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                restart.assert_not_called()
                self.assertIn("Hotkey invalid:", html_body)


class TestExactPrimaryChordMatching(unittest.TestCase):
    def _bind_primary(self, hotkey: str, ai_hotkey: str, physical: set[str]):
        app = app_fixture()
        hooks = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.multiple(
            Config,
            HOTKEY=hotkey,
            AI_HOTKEY=ai_hotkey,
            AI_MODIFIER="",
            POLISH_HOTKEY="f8",
            RESET_CONTEXT_HOTKEY="",
            LIVE_HOTKEY="f7",
            CANCEL_HOTKEY="esc",
            CTRL_KEEP_CONTEXT_KEYS=("f6",),
        ))
        stack.enter_context(patch("main.ensure_can_bind_hotkeys"))
        stack.enter_context(patch("main.platforms.hook_key", side_effect=lambda key, fn, **kw: hooks.update({key: fn})))

        def pressed(key):
            try:
                requested = normalize_modifier_name(key)
            except ValueError:
                return key in physical
            if requested.startswith(("left ", "right ")):
                return requested in physical
            return any(
                normalize_modifier_name(v.removeprefix("left ").removeprefix("right ")) == requested
                for v in physical
            )

        stack.enter_context(patch("main.platforms.is_pressed", side_effect=pressed))
        stack.enter_context(patch("main.platforms.is_pressed_exclusive", side_effect=lambda key: key in physical))
        dispatch = stack.enter_context(patch.object(app, "_dispatch_hotkey"))
        app._bind_hotkeys()
        return hooks[hotkey.split("+")[-1]], dispatch

    def test_primary_chord_rejects_extra_modifiers_like_cmd_ctrl_q(self):
        physical: set[str] = {"ctrl", "cmd"}
        handler, dispatch = self._bind_primary("ctrl+q", "ctrl+shift+q", physical)
        # Cmd+Ctrl+Q (macOS Lock Screen) must NOT match ctrl+q!
        self.assertTrue(handler(SimpleNamespace(event_type="down")))
        self.assertTrue(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_not_called()

        # Alt+Ctrl+Q must NOT match ctrl+q either.
        physical.clear()
        physical.update({"ctrl", "alt"})
        self.assertTrue(handler(SimpleNamespace(event_type="down")))
        dispatch.assert_not_called()

        # Exact Ctrl+Q matches dictation (False).
        physical.clear()
        physical.add("ctrl")
        self.assertFalse(handler(SimpleNamespace(event_type="down")))
        self.assertFalse(handler(SimpleNamespace(event_type="up")))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0], "primary")
        self.assertFalse(dispatch.call_args.args[2])

    def test_primary_chord_honors_side_specific_modifiers(self):
        for physical, expected in (
            ({"left ctrl"}, False),
            ({"right ctrl"}, None),
            ({"left ctrl", "right ctrl"}, None),
            ({"left ctrl", "shift"}, True),
            ({"right ctrl", "shift"}, None),
        ):
            with self.subTest(physical=physical):
                handler, dispatch = self._bind_primary("left ctrl+q", "left ctrl+shift+q", set(physical))
                res = handler(SimpleNamespace(event_type="down"))
                if expected is None:
                    self.assertTrue(res)
                    dispatch.assert_not_called()
                else:
                    self.assertFalse(res)
                    dispatch.assert_called_once()
                    self.assertEqual(dispatch.call_args.args[2], expected)


class TestMacOSHotkeyBackend(unittest.TestCase):
    def setUp(self):
        from platforms import macos
        self.mac = macos
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(macos, "_pynput", SimpleNamespace(KeyCode=type("FakeKeyCode", (), {}))))
        stack.enter_context(patch.object(macos, "_pressed_tokens", set()))
        stack.enter_context(patch.object(macos, "_pressed_keys", {}))
        stack.enter_context(patch.object(macos, "_hooks", {}))
        stack.enter_context(patch.object(macos, "_modifier_key_state", return_value=None))

    def test_key_aliases_map_browser_names_to_pynput_names(self):
        self.assertEqual(self.mac._canonical_token("page up"), "page_up")
        self.assertEqual(self.mac._canonical_token("page down"), "page_down")
        self.assertEqual(self.mac._canonical_token("capslock"), "caps_lock")
        self.assertEqual(self.mac._canonical_token("caps lock"), "caps_lock")
        self.assertEqual(self.mac._canonical_token("return"), "enter")
        self.assertEqual(self.mac._canonical_token("escape"), "esc")

    def test_ctrl_letter_control_char_resolves_without_virtual_key(self):
        called = []
        with patch.object(self.mac, "_listener", object()), \
             patch.object(self.mac, "_primary_virtual_key", return_value=None):
            self.mac.hook_key("q", lambda ev: (called.append((ev.event_type, ev.key)), False)[1], True, can_decline=True)
            key = self.mac._pynput.KeyCode()
            key.vk = 12
            key.char = "\x11"  # ASCII DC1 produced by Ctrl+Q on macOS
            self.mac._on_press(key)
            self.mac._on_release(key)
        self.assertEqual(called, [("down", "q"), ("up", "q")])

    def test_passive_virtual_key_refresh_tracks_layout_switch(self):
        called = []
        accept = [False]
        with patch.object(self.mac, "_listener", object()), \
             patch.object(self.mac, "_primary_virtual_key", return_value=12):
            self.mac.hook_key(
                "q",
                lambda ev: (called.append((ev.event_type, ev.key)), not accept[0])[1],
                True,
                can_decline=True,
            )
            # Layout switched so 'q' is now at vk=39 (Dvorak). Normal typing sees 'q' at vk=39.
            typed_q = self.mac._pynput.KeyCode()
            typed_q.vk, typed_q.char = 39, "q"
            self.mac._on_press(typed_q)
            self.mac._on_release(typed_q)
            self.assertEqual(self.mac._hooks["q"]["virtual_key"], 39)

            # Subsequent Option+Q at vk=39 produces 'œ' but matches refreshed virtual_key=39 -> 'q'.
            called.clear()
            accept[0] = True
            opt_q = self.mac._pynput.KeyCode()
            opt_q.vk, opt_q.char = 39, "œ"
            self.mac._on_press(opt_q)
            self.mac._on_release(opt_q)
            self.assertEqual(called, [("down", "q"), ("up", "q")])

    def test_self_synthesized_injected_events_are_ignored_while_external_events_pass(self):
        called = []
        with patch.object(self.mac, "_listener", object()), \
             patch.object(self.mac, "_primary_virtual_key", return_value=12):
            self.mac.hook_key("q", lambda ev: (called.append(ev.event_type), False)[1], True)
            key = self.mac._pynput.KeyCode()
            key.vk, key.char = 12, "q"
            event = object()

            with self.mac._suppress_self_events():
                self.mac._on_press(key, True)
                self.assertIs(self.mac._intercept(None, event), event)
                self.mac._on_release(key, True)
                self.assertIs(self.mac._intercept(None, event), event)
            self.assertEqual(called, [])

            # Outside _suppress_self_events, hardware (injected=False) and 3rd-party injected events work.
            self.mac._on_press(key, False)
            self.assertIsNone(self.mac._intercept(None, event))
            self.mac._on_release(key, False)
            self.assertEqual(called, ["down", "up"])

    def test_event_tap_disabled_by_timeout_reenables_tap(self):
        fake_tap = object()
        fake_quartz = SimpleNamespace(CGEventTapEnable=MagicMock())
        event = object()
        with patch.object(self.mac, "_event_tap", fake_tap), \
             patch.dict("sys.modules", {"Quartz": fake_quartz}):
            returned = self.mac._intercept(0xFFFFFFFE, event)
        self.assertIs(returned, event)
        fake_quartz.CGEventTapEnable.assert_called_once_with(fake_tap, True)

    def test_force_release_modifiers_releases_right_side_and_quartz_active_modifiers(self):
        released = []
        fake_keys = SimpleNamespace(
            ctrl="ctrl", ctrl_l="ctrl_l", ctrl_r="ctrl_r",
            shift="shift", shift_l="shift_l", shift_r="shift_r",
            alt="alt", alt_l="alt_l", alt_r="alt_r",
            cmd="cmd", cmd_l="cmd_l", cmd_r="cmd_r",
        )
        fake_controller = SimpleNamespace(release=lambda k: released.append(k))
        fake_pynput = SimpleNamespace(Key=fake_keys, Controller=lambda: fake_controller)
        active = {"right ctrl", "left alt"}
        with patch.object(self.mac, "_pynput", fake_pynput), \
             patch.object(self.mac, "_modifier_key_state", side_effect=lambda tok: tok in active), \
             patch.object(self.mac.time, "sleep"):
            self.mac.force_release_modifiers()
        self.assertIn("ctrl_r", released)
        self.assertIn("alt_l", released)
        self.assertNotIn("shift_l", released)
        self.assertNotIn("shift_r", released)

    def test_quartz_cmd_chord_posts_private_session_events(self):
        posted = []
        flags_set = []
        fake_source = object()

        def make_ev(src, vk, is_down):
            return {"src": src, "vk": vk, "down": is_down}

        fake_quartz = SimpleNamespace(
            kCGEventSourceStatePrivate=99,
            kCGSessionEventTap=1,
            kCGEventFlagMaskCommand=0x100000,
            CGEventSourceCreate=lambda state: (state, fake_source),
            CGEventCreateKeyboardEvent=make_ev,
            CGEventSetFlags=lambda ev, flag: flags_set.append((ev["vk"], ev["down"], flag)),
            CGEventPost=lambda tap, ev: posted.append((tap, ev["vk"], ev["down"], ev["src"])),
        )
        with patch.object(self.mac.sys, "platform", "darwin"), \
             patch.dict("sys.modules", {"Quartz": fake_quartz}), \
             patch.object(self.mac.time, "sleep"):
            ok = self.mac._quartz_cmd_chord(9)
        self.assertTrue(ok)
        self.assertEqual(flags_set, [(9, True, 0x100000), (9, False, 0x100000)])
        self.assertEqual(posted, [
            (1, 9, True, (99, fake_source)),
            (1, 9, False, (99, fake_source)),
        ])

    def test_apply_window_exstyles_configures_transparent_nonactivating_overlay(self):
        clear_color = object()
        calls = []

        class FakeNSWindow:
            def setOpaque_(self, val): calls.append(("opaque", val))
            def setBackgroundColor_(self, val): calls.append(("bg", val))
            def setHasShadow_(self, val): calls.append(("shadow", val))
            def setHidesOnDeactivate_(self, val): calls.append(("hides", val))
            def setIgnoresMouseEvents_(self, val): calls.append(("mouse", val))
            def setLevel_(self, val): calls.append(("level", val))
            def setCollectionBehavior_(self, val): calls.append(("behavior", val))
            def orderFrontRegardless(self): calls.append(("orderFront", True))
            def setStyleMask_(self, val): raise AssertionError("must not mutate styleMask (causes black border)")

        nswin = FakeNSWindow()
        fake_objc = SimpleNamespace(objc_object=MagicMock(return_value=SimpleNamespace(window=lambda: nswin)))
        fake_appkit = SimpleNamespace(NSColor=SimpleNamespace(clearColor=lambda: clear_color))
        fake_qtgui = SimpleNamespace(QGuiApplication=SimpleNamespace(platformName=lambda: "cocoa"))
        fake_widget = SimpleNamespace(winId=lambda: 12345)
        with patch.object(self.mac.sys, "platform", "darwin"), \
             patch.dict("sys.modules", {"objc": fake_objc, "AppKit": fake_appkit, "PySide6.QtGui": fake_qtgui}):
            self.mac.apply_window_exstyles(fake_widget)
        self.assertIn(("opaque", False), calls)
        self.assertIn(("bg", clear_color), calls)
        self.assertIn(("shadow", False), calls)
        self.assertIn(("hides", False), calls)
        self.assertIn(("mouse", True), calls)
        self.assertIn(("level", 25), calls)
        self.assertIn(("orderFront", True), calls)

        # Offscreen QPA must never dereference winId() as an NSView*.
        fake_objc.objc_object.reset_mock()
        offscreen_qtgui = SimpleNamespace(QGuiApplication=SimpleNamespace(platformName=lambda: "offscreen"))
        with patch.object(self.mac.sys, "platform", "darwin"), \
             patch.dict("sys.modules", {"objc": fake_objc, "AppKit": fake_appkit, "PySide6.QtGui": offscreen_qtgui}):
            self.mac.apply_window_exstyles(fake_widget)
        fake_objc.objc_object.assert_not_called()

    def test_hud_begin_show_skips_raise_on_macos(self):
        import indicator
        calls = []
        fake_hud = SimpleNamespace(
            _phase="hidden",
            _phase_t0=0.0,
            _enter_base=None,
            _entry_offset=0.0,
            _glyph_fade=1.0,
            _droop_from=0.0,
            _droop_goal=lambda: 0.0,
            _w_tw=SimpleNamespace(snap=lambda _: None),
            _h_tw=SimpleNamespace(snap=lambda _: None),
            _label_target=90.0,
            _pill_h=30.0,
            _fit_canvas=lambda shrink=False: None,
            _appear_target=0.0,
            _reposition_bottom_center=lambda: None,
            setWindowOpacity=lambda _: None,
            show=lambda: calls.append("show"),
            _apply_win32_exstyles=lambda: calls.append("exstyles"),
            raise_=lambda: calls.append("raise"),
        )
        with patch.object(indicator.sys, "platform", "darwin"):
            indicator.DictationIndicator._begin_show(fake_hud, 1.0)
        self.assertEqual(calls, ["show", "exstyles"])

        calls.clear()
        fake_hud._phase = "hidden"
        with patch.object(indicator.sys, "platform", "win32"):
            indicator.DictationIndicator._begin_show(fake_hud, 2.0)
        self.assertEqual(calls, ["show", "exstyles", "raise"])

    def test_hud_live_notices_reflect_configured_live_hotkey(self):
        import indicator
        with patch.object(indicator.sys, "platform", "darwin"):
            self.assertEqual(indicator._format_hotkey_label("alt+l"), "Option+L")
            self.assertEqual(indicator._format_hotkey_label("f7"), "F7")
            self.assertIn(
                "when you tap Option+L.",
                indicator.live_failure_message("live_unavailable", "groq", live_hotkey="alt+l"),
            )
            self.assertIn(
                "through Groq when you tap Option+L.",
                indicator.live_failure_message("live_key_rejected", "groq", live_hotkey="alt+l"),
            )
        with patch.object(indicator.sys, "platform", "win32"):
            self.assertEqual(indicator._format_hotkey_label("alt+l"), "Alt+L")


class TestSetupHotkeyRecorderJS(unittest.TestCase):
    def _run_js_scenario(self, scenario_js: str) -> dict:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node must be available for JS hotkey recorder tests")
        template = (ROOT / "assets" / "setup_template.html").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", template, re.S)
        self.assertIsNotNone(match)
        full_js = match.group(1)
        start = full_js.index("// ------------------------------------------------------------ hotkey capture")
        end = full_js.index("// ------------------------------------------------------------ status + test")
        recorder_js = full_js[start:end]

        harness = f"""
const listeners = {{}};
const winListeners = {{}};
let dirtyCount = 0;
let updateKeycapsCount = 0;
let updateMetersCount = 0;
const inputs = {{
  HOTKEY: {{ value: 'ctrl+grave' }},
  AI_HOTKEY: {{ value: 'ctrl+shift+grave' }},
  LIVE_HOTKEY: {{ value: 'f7' }},
  POLISH_HOTKEY: {{ value: 'f8' }},
}};
function el(id) {{ return inputs[id] || null; }}
function markDirty() {{ dirtyCount += 1; }}
function updateKeycaps() {{ updateKeycapsCount += 1; }}
function updateMeters() {{ updateMetersCount += 1; }}
const document = {{
  addEventListener(type, fn) {{ listeners[type] = fn; }},
  removeEventListener(type, fn) {{ if (listeners[type] === fn) delete listeners[type]; }},
}};
const window = {{
  addEventListener(type, fn) {{ winListeners[type] = fn; }},
  removeEventListener(type, fn) {{ if (winListeners[type] === fn) delete winListeners[type]; }},
}};
function makeBtn(targetId) {{
  const classes = new Set();
  return {{
    textContent: 'Change',
    getAttribute(name) {{ return name === 'data-record-for' ? targetId : null; }},
    classList: {{
      add(c) {{ classes.add(c); }},
      remove(c) {{ classes.delete(c); }},
      has(c) {{ return classes.has(c); }},
    }},
  }};
}}
function fire(type, props) {{
  const fn = listeners[type];
  if (!fn) return false;
  let prevented = false;
  fn(Object.assign({{
    key: '', code: '', ctrlKey: false, shiftKey: false, altKey: false, metaKey: false,
    preventDefault() {{ prevented = true; }},
    stopPropagation() {{}},
  }}, props));
  return prevented;
}}
{recorder_js}
const result = (function() {{
  {scenario_js}
}})();
process.stdout.write(JSON.stringify(result));
"""
        proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_js_recorder_handles_ctrl_q_without_overlap_and_clears_stale_modifiers(self):
        res = self._run_js_scenario("""
          // Scenario 1: Ctrl already held (missed Control keydown) -> pressing q captures ctrl+q immediately
          const btn1 = makeBtn('HOTKEY');
          recordHotkey(btn1);
          fire('keydown', { key: 'q', code: 'KeyQ', ctrlKey: true });
          const afterMissedModDown = {
            value: inputs.HOTKEY.value,
            stillRecording: !!hotkeyRecorder,
          };

          // Scenario 2: Press Meta, release Meta, then press Ctrl+Q -> must NOT retain stale cmd
          const btn2 = makeBtn('LIVE_HOTKEY');
          recordHotkey(btn2);
          fire('keydown', { key: 'Meta', code: 'MetaLeft', metaKey: true });
          fire('keyup', { key: 'Meta', code: 'MetaLeft', metaKey: false });
          fire('keydown', { key: 'Control', code: 'ControlLeft', ctrlKey: true });
          fire('keydown', { key: 'q', code: 'KeyQ', ctrlKey: true });
          const afterStaleMeta = {
            value: inputs.LIVE_HOTKEY.value,
            stillRecording: !!hotkeyRecorder,
          };
          return { afterMissedModDown, afterStaleMeta };
        """)
        self.assertEqual(res["afterMissedModDown"], {"value": "ctrl+q", "stillRecording": False})
        self.assertEqual(res["afterStaleMeta"], {"value": "ctrl+q", "stillRecording": False})

    def test_js_recorder_commits_immediately_on_cmd_and_option_chords_without_6s_hang(self):
        res = self._run_js_scenario("""
          function capture(targetId, events) {
            const btn = makeBtn(targetId);
            recordHotkey(btn);
            for (const ev of events) fire(ev.type, ev);
            return { value: inputs[targetId].value, stillRecording: !!hotkeyRecorder };
          }
          return {
            cmdK: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Meta', code: 'MetaLeft', metaKey: true },
              { type: 'keydown', key: 'k', code: 'KeyK', metaKey: true },
              // macOS never sends keyup for 'k' while Meta is held!
            ]),
            optionQ: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Alt', code: 'AltLeft', altKey: true },
              { type: 'keydown', key: 'œ', code: 'KeyQ', altKey: true },
            ]),
            optionSpace: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Alt', code: 'AltLeft', altKey: true },
              { type: 'keydown', key: '\\u00a0', code: 'Space', altKey: true },
            ]),
            optionDeadE: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Alt', code: 'AltLeft', altKey: true },
              { type: 'keydown', key: 'Dead', code: 'KeyE', altKey: true },
            ]),
            shift1: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Shift', code: 'ShiftLeft', shiftKey: true },
              { type: 'keydown', key: '!', code: 'Digit1', shiftKey: true },
            ]),
            ctrlPageUp: capture('LIVE_HOTKEY', [
              { type: 'keydown', key: 'Control', code: 'ControlLeft', ctrlKey: true },
              { type: 'keydown', key: 'PageUp', code: 'PageUp', ctrlKey: true },
            ]),
          };
        """)
        self.assertEqual(res["cmdK"], {"value": "cmd+k", "stillRecording": False})
        self.assertEqual(res["optionQ"], {"value": "alt+q", "stillRecording": False})
        self.assertEqual(res["optionSpace"], {"value": "alt+space", "stillRecording": False})
        self.assertEqual(res["optionDeadE"], {"value": "alt+e", "stillRecording": False})
        self.assertEqual(res["shift1"], {"value": "shift+1", "stillRecording": False})
        self.assertEqual(res["ctrlPageUp"], {"value": "ctrl+page_up", "stillRecording": False})

    def test_js_recorder_escape_cancels_and_auto_syncs_ai_hotkey_primary(self):
        res = self._run_js_scenario("""
          // Bare Escape cancels and restores previous value without marking dirty
          const btnEsc = makeBtn('HOTKEY');
          recordHotkey(btnEsc);
          fire('keydown', { key: 'Escape', code: 'Escape' });
          const afterEsc = {
            hotkey: inputs.HOTKEY.value,
            dirtyCount,
            stillRecording: !!hotkeyRecorder,
          };

          // Changing HOTKEY to ctrl+q auto-syncs AI_HOTKEY primary from grave -> q
          const btnH = makeBtn('HOTKEY');
          recordHotkey(btnH);
          fire('keydown', { key: 'q', code: 'KeyQ', ctrlKey: true });
          const afterSync = {
            hotkey: inputs.HOTKEY.value,
            aiHotkey: inputs.AI_HOTKEY.value,
            dirtyCount,
          };

          // Changing HOTKEY to ctrl+shift+q (same as AI_HOTKEY) keeps AI_HOTKEY distinct
          const btnH2 = makeBtn('HOTKEY');
          recordHotkey(btnH2);
          fire('keydown', { key: 'q', code: 'KeyQ', ctrlKey: true, shiftKey: true });
          const afterDistinct = {
            hotkey: inputs.HOTKEY.value,
            aiHotkey: inputs.AI_HOTKEY.value,
          };
          return { afterEsc, afterSync, afterDistinct };
        """)
        self.assertEqual(res["afterEsc"], {"hotkey": "ctrl+grave", "dirtyCount": 0, "stillRecording": False})
        self.assertEqual(res["afterSync"]["hotkey"], "ctrl+q")
        self.assertEqual(res["afterSync"]["aiHotkey"], "ctrl+shift+q")
        self.assertEqual(res["afterSync"]["dirtyCount"], 1)
        self.assertEqual(res["afterDistinct"]["hotkey"], "ctrl+shift+q")
        self.assertNotEqual(res["afterDistinct"]["aiHotkey"], "ctrl+shift+q")
        self.assertTrue(res["afterDistinct"]["aiHotkey"].endswith("+q"))


if __name__ == "__main__":
    unittest.main()
