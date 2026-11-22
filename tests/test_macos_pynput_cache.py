"""Tests for the macOS main-thread Carbon layout cache & live refresh around pynput.

Uses fake ``pynput``, ``PySide6.QtCore``, and clipboard modules so every check
runs on Windows, Linux, and macOS without opening a window or touching HIToolbox.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import types
import unittest
from unittest import mock

import typer
from platforms import clipboard as clip_mod, macos
from tests.test_clipboard_safety import FakeClipboard


def _build_fake_pynput(state: dict):
    """Create isolated fake ``pynput`` modules that record Carbon & Controller calls."""
    pynput_mod = types.ModuleType("pynput")
    util_mod = types.ModuleType("pynput._util")
    darwin_util = types.ModuleType("pynput._util.darwin")
    kb_mod = types.ModuleType("pynput.keyboard")
    darwin_kb = types.ModuleType("pynput.keyboard._darwin")

    @contextlib.contextmanager
    def orig_keycode_context():
        state["context_calls"].append(threading.get_ident())
        yield state["context"]

    def orig_keycode_to_string(ctx, kc):
        state["to_string_calls"].append((threading.get_ident(), ctx, kc))
        return state["chars_by_vk"].get(kc, "")

    def orig_get_unicode_to_keycode_map():
        state["map_calls"].append(threading.get_ident())
        with darwin_util.keycode_context() as ctx:
            return {
                darwin_util.keycode_to_string(ctx, kc): kc
                for kc in sorted(state["chars_by_vk"])
            }

    class FakeKey:
        cmd = "<cmd>"
        ctrl = "<ctrl>"
        shift = "<shift>"
        alt = "<alt>"
        backspace = "<backspace>"

    class FakeController:
        def __init__(self):
            self._mapping = darwin_kb.get_unicode_to_keycode_map()
            state["controllers"].append(dict(self._mapping))

        def press(self, key):
            vk = self._mapping.get(key) if isinstance(key, str) else key
            state["key_events"].append(("press", key, vk))

        def release(self, key):
            vk = self._mapping.get(key) if isinstance(key, str) else key
            state["key_events"].append(("release", key, vk))

        def type(self, text):
            state["typed"].append((text, dict(self._mapping)))

    darwin_util.keycode_context = orig_keycode_context
    darwin_util.keycode_to_string = orig_keycode_to_string
    darwin_util.get_unicode_to_keycode_map = orig_get_unicode_to_keycode_map

    # Mirror ``from pynput._util.darwin import keycode_context, get_unicode_to_keycode_map``
    # inside ``pynput/keyboard/_darwin.py``.
    darwin_kb.keycode_context = orig_keycode_context
    darwin_kb.get_unicode_to_keycode_map = orig_get_unicode_to_keycode_map
    darwin_kb.Controller = FakeController
    darwin_kb.Key = FakeKey

    kb_mod.Controller = FakeController
    kb_mod.Key = FakeKey
    util_mod.darwin = darwin_util
    kb_mod._darwin = darwin_kb
    pynput_mod._util = util_mod
    pynput_mod.keyboard = kb_mod

    modules = {
        "pynput": pynput_mod,
        "pynput._util": util_mod,
        "pynput._util.darwin": darwin_util,
        "pynput.keyboard": kb_mod,
        "pynput.keyboard._darwin": darwin_kb,
    }
    return modules, darwin_util, darwin_kb, kb_mod, orig_keycode_context, orig_get_unicode_to_keycode_map


def _fake_qt_core(has_instance: bool = True):
    qt_core = types.ModuleType("PySide6.QtCore")

    class _FakeQCoreApplication:
        @staticmethod
        def instance():
            return object() if has_instance else None

    qt_core.QCoreApplication = _FakeQCoreApplication
    return qt_core


class TestMacosPynputMainThreadCache(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "context": (42, b"us-layout"),
            "chars_by_vk": {8: "c", 9: "v", 50: "`"},
            "context_calls": [],
            "map_calls": [],
            "to_string_calls": [],
            "controllers": [],
            "key_events": [],
            "typed": [],
        }
        (
            self.modules,
            self.darwin_util,
            self.darwin_kb,
            self.fake_kb_pkg,
            self.orig_ctx,
            self.orig_map,
        ) = _build_fake_pynput(self.state)

    def _run_in_thread(self, fn):
        result = {}
        error = {}

        def target():
            try:
                result["value"] = fn()
            except BaseException as exc:
                error["exc"] = exc

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        if "exc" in error:
            raise error["exc"]
        return result.get("value")

    def test_warms_cache_on_main_thread_and_serves_cached_when_qt_loop_not_pumping(self) -> None:
        main_tid = threading.get_ident()
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)):
            macos._install_pynput_main_thread_cache()

            self.assertTrue(all(tid == main_tid for tid in self.state["context_calls"]))
            self.assertEqual(self.state["map_calls"], [main_tid])
            warm_ctx_count = len(self.state["context_calls"])
            warm_map_count = len(self.state["map_calls"])

            def bg_read():
                with self.darwin_kb.keycode_context() as kb_ctx:
                    with self.darwin_util.keycode_context() as util_ctx:
                        kb_map = self.darwin_kb.get_unicode_to_keycode_map()
                        util_map = self.darwin_util.get_unicode_to_keycode_map()
                        kb_map["mutated"] = 999
                        return kb_ctx, util_ctx, util_map

            kb_ctx, util_ctx, util_map = self._run_in_thread(bg_read)

        self.assertEqual(kb_ctx, (42, b"us-layout"))
        self.assertEqual(util_ctx, (42, b"us-layout"))
        self.assertEqual(util_map, {"c": 8, "v": 9, "`": 50})
        self.assertEqual(len(self.state["context_calls"]), warm_ctx_count)
        self.assertEqual(len(self.state["map_calls"]), warm_map_count)

    def test_background_refreshes_live_layout_on_main_thread_after_layout_switch(self) -> None:
        """Simulates switching U.S. -> Dvorak while Qt HUD loop is running."""
        main_tid = threading.get_ident()
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}

        def fake_main_thread_dispatch(fn, timeout_s=0.5):
            self.assertNotEqual(threading.get_ident(), main_tid)
            return True, fn()

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", side_effect=fake_main_thread_dispatch):
            macos._install_pynput_main_thread_cache()

            self.state["context"] = (77, b"dvorak-layout")
            self.state["chars_by_vk"] = {34: "c", 47: "v", 50: "`"}

            def bg_copy_paste():
                with self.darwin_kb.keycode_context() as ctx:
                    return ctx, self.darwin_kb.get_unicode_to_keycode_map()

            bg_ctx, bg_map = self._run_in_thread(bg_copy_paste)

        self.assertEqual(bg_ctx, (77, b"dvorak-layout"))
        self.assertEqual(bg_map, {"c": 34, "v": 47, "`": 50})

    def test_copy_and_paste_use_fresh_dvorak_keycodes_after_layout_switch(self) -> None:
        main_tid = threading.get_ident()
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}

        def fake_main_thread_dispatch(fn, timeout_s=0.5):
            self.assertNotEqual(threading.get_ident(), main_tid)
            return True, fn()

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", side_effect=fake_main_thread_dispatch), \
                mock.patch("platforms.macos.time.sleep"):
            macos._install_pynput_main_thread_cache()
            warm_map_count = len(self.state["map_calls"])

            # Switch layout from U.S. (c=8, v=9) to plain Dvorak (c=34, v=47).
            self.state["context"] = (77, b"dvorak-layout")
            self.state["chars_by_vk"] = {34: "c", 47: "v", 50: "`"}

            self._run_in_thread(macos.send_copy)
            self._run_in_thread(macos.send_paste)

        self.assertEqual(len(self.state["map_calls"]), warm_map_count + 2)
        self.assertIn(("press", "c", 34), self.state["key_events"])
        self.assertIn(("press", "v", 47), self.state["key_events"])

    def test_copy_and_paste_fail_without_sending_shortcut_on_layout_timeout(self) -> None:
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)):
            macos._install_pynput_main_thread_cache()

            # Simulate layout change while main-thread lookup subsequently times out.
            self.state["context"] = (77, b"dvorak-layout")
            self.state["chars_by_vk"] = {34: "c", 47: "v", 50: "`"}

            with self.assertRaises(RuntimeError):
                self._run_in_thread(macos.send_copy)
            with self.assertRaises(RuntimeError):
                self._run_in_thread(macos.send_paste)

        self.assertEqual(self.state["controllers"], [])
        self.assertEqual(self.state["key_events"], [])

    def _fake_typer_env(self, clip: FakeClipboard):
        stack = contextlib.ExitStack()
        for target, kwargs in (
            ("typer.clipboard_read", {"side_effect": clip.read}),
            ("typer.clipboard_write", {"side_effect": clip.write}),
            ("typer.clipboard_snapshot", {"side_effect": clip.snapshot}),
            ("typer.clipboard_restore", {"side_effect": clip.restore}),
            ("typer.clipboard_restore_result", {"side_effect": clip.restore_result}),
            ("typer.clipboard_change_token", {"side_effect": lambda: clip.token}),
            ("typer.clipboard_write_text_token", {"side_effect": clip.write_token}),
            ("typer.clipboard_read_text_token", {"side_effect": clip.read_token}),
            ("typer.foreground_is_terminal", {"return_value": False}),
            ("typer.foreground_is_ide_host", {"return_value": False}),
            ("typer.force_release_modifiers", {}),
            ("typer.wm_copy_foreground", {"return_value": False}),
            ("typer._wait_modifiers_up", {}),
            ("typer.time.sleep", {}),
        ):
            stack.enter_context(mock.patch(target, **kwargs))
        stack.enter_context(mock.patch.object(clip_mod, "_win_api", side_effect=AssertionError("real clipboard")))
        stack.enter_context(mock.patch.object(clip_mod, "_text_restore", side_effect=AssertionError("real clipboard")))
        stack.enter_context(mock.patch.object(clip_mod, "_text_read", side_effect=AssertionError("real clipboard")))
        return stack

    def test_paste_text_restores_clipboard_and_reports_failure_on_layout_timeout(self) -> None:
        clip = FakeClipboard("user clip")
        delivered = []
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}
        typer._reset_restore_now_for_tests()
        self.addCleanup(typer._reset_restore_now_for_tests)

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)), \
                mock.patch("typer.send_paste", side_effect=macos.send_paste), \
                self._fake_typer_env(clip):
            macos._install_pynput_main_thread_cache()
            self.state["context"] = (77, b"dvorak-layout")
            self.state["chars_by_vk"] = {34: "c", 47: "v", 50: "`"}

            with self.assertRaises(RuntimeError):
                self._run_in_thread(
                    lambda: typer.paste_text(
                        "dictated text",
                        on_delivered=lambda: delivered.append("delivered"),
                    )
                )

        self.assertEqual(self.state["controllers"], [])
        self.assertEqual(self.state["key_events"], [])
        self.assertEqual(delivered, [])
        self.assertEqual(clip.text, "user clip")
        self.assertEqual(len(clip.restores), 1)
        self.assertFalse(typer.last_paste_restore_failed())

    def test_get_selected_text_restores_clipboard_without_shortcut_on_layout_timeout(self) -> None:
        clip = FakeClipboard("user clip")
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}

        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)), \
                mock.patch("typer.send_copy", side_effect=macos.send_copy), \
                self._fake_typer_env(clip):
            macos._install_pynput_main_thread_cache()
            self.state["context"] = (77, b"dvorak-layout")
            self.state["chars_by_vk"] = {34: "c", 47: "v", 50: "`"}

            selected = self._run_in_thread(lambda: typer.get_selected_text(timeout=0.05))

        self.assertEqual(selected, "")
        self.assertEqual(self.state["controllers"], [])
        self.assertEqual(self.state["key_events"], [])
        self.assertEqual(clip.text, "user clip")
        self.assertEqual(len(clip.restores), 1)

    def test_main_thread_calls_refresh_cached_layout(self) -> None:
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)):
            macos._install_pynput_main_thread_cache()

            self.state["context"] = (77, b"layout-v2")
            self.state["chars_by_vk"] = {12: "q"}

            with self.darwin_util.keycode_context() as refreshed_ctx:
                self.assertEqual(refreshed_ctx, (77, b"layout-v2"))
            refreshed_map = self.darwin_kb.get_unicode_to_keycode_map()
            self.assertEqual(refreshed_map, {"q": 12})

            ctx_count_before_bg = len(self.state["context_calls"])
            map_count_before_bg = len(self.state["map_calls"])

            def bg_read():
                with self.darwin_kb.keycode_context() as ctx:
                    return ctx, self.darwin_util.get_unicode_to_keycode_map()

            bg_ctx, bg_map = self._run_in_thread(bg_read)

        self.assertEqual(bg_ctx, (77, b"layout-v2"))
        self.assertEqual(bg_map, {"q": 12})
        self.assertEqual(len(self.state["context_calls"]), ctx_count_before_bg)
        self.assertEqual(len(self.state["map_calls"]), map_count_before_bg)

    def test_background_install_with_qt_loaded_skips_carbon_when_cache_empty(self) -> None:
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(True)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg), \
                mock.patch.object(macos, "_run_on_main_thread", return_value=(False, None)):
            self._run_in_thread(macos._install_pynput_main_thread_cache)

            def bg_read():
                with self.darwin_kb.keycode_context() as ctx:
                    return ctx, self.darwin_kb.get_unicode_to_keycode_map()

            bg_ctx, bg_map = self._run_in_thread(bg_read)

        self.assertEqual(bg_ctx, (None, None))
        self.assertEqual(bg_map, {})
        self.assertEqual(self.state["context_calls"], [])
        self.assertEqual(self.state["map_calls"], [])

    def test_background_without_qt_app_reads_live_layout_directly(self) -> None:
        modules = {**self.modules, "PySide6.QtCore": _fake_qt_core(False)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
            macos._install_pynput_main_thread_cache()
            self.state["context"] = (88, b"dvorak-no-hud")
            self.state["chars_by_vk"] = {34: "c", 47: "v"}

            def bg_read():
                with self.darwin_kb.keycode_context() as ctx:
                    return ctx, self.darwin_kb.get_unicode_to_keycode_map()

            bg_ctx, bg_map = self._run_in_thread(bg_read)

        self.assertEqual(bg_ctx, (88, b"dvorak-no-hud"))
        self.assertEqual(bg_map, {"c": 34, "v": 47})

    def test_idempotent_installation_does_not_double_wrap(self) -> None:
        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
            macos._install_pynput_main_thread_cache()
            first_ctx_fn = self.darwin_util.keycode_context
            first_map_fn = self.darwin_util.get_unicode_to_keycode_map
            calls_after_first = (
                len(self.state["context_calls"]),
                len(self.state["map_calls"]),
            )

            macos._install_pynput_main_thread_cache()
            self.assertIs(self.darwin_util.keycode_context, first_ctx_fn)
            self.assertIs(self.darwin_util.get_unicode_to_keycode_map, first_map_fn)
            self.assertEqual(
                (len(self.state["context_calls"]), len(self.state["map_calls"])),
                calls_after_first,
            )

    def test_noop_off_darwin_or_without_pynput(self) -> None:
        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
            macos._install_pynput_main_thread_cache()
        self.assertIs(self.darwin_util.keycode_context, self.orig_ctx)
        self.assertIs(self.darwin_util.get_unicode_to_keycode_map, self.orig_map)

        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", None):
            macos._install_pynput_main_thread_cache()
        self.assertIs(self.darwin_util.keycode_context, self.orig_ctx)
        self.assertIs(self.darwin_util.get_unicode_to_keycode_map, self.orig_map)


if __name__ == "__main__":
    unittest.main()
