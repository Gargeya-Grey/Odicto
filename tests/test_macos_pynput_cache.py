"""Tests for the macOS main-thread Carbon layout cache around pynput.

Uses fake ``pynput`` submodules in ``sys.modules`` so every check runs on
Windows, Linux, and macOS without opening a window or touching HIToolbox.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import types
import unittest
from unittest import mock

from platforms import macos


def _build_fake_pynput(state: dict):
    """Create isolated fake ``pynput`` modules that record Carbon call threads."""
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

    darwin_util.keycode_context = orig_keycode_context
    darwin_util.keycode_to_string = orig_keycode_to_string
    darwin_util.get_unicode_to_keycode_map = orig_get_unicode_to_keycode_map

    # Mirror ``from pynput._util.darwin import keycode_context, get_unicode_to_keycode_map``
    # inside ``pynput/keyboard/_darwin.py``.
    darwin_kb.keycode_context = orig_keycode_context
    darwin_kb.get_unicode_to_keycode_map = orig_get_unicode_to_keycode_map

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


class TestMacosPynputMainThreadCache(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "context": (42, b"layout-v1"),
            "chars_by_vk": {50: "`", 0: "a"},
            "context_calls": [],
            "map_calls": [],
            "to_string_calls": [],
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

    def test_warms_cache_on_main_thread_and_serves_background_threads(self) -> None:
        main_tid = threading.get_ident()
        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
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

        self.assertEqual(kb_ctx, (42, b"layout-v1"))
        self.assertEqual(util_ctx, (42, b"layout-v1"))
        self.assertEqual(util_map, {"`": 50, "a": 0})
        self.assertEqual(len(self.state["context_calls"]), warm_ctx_count)
        self.assertEqual(len(self.state["map_calls"]), warm_map_count)

    def test_main_thread_calls_refresh_cached_layout(self) -> None:
        with mock.patch.dict(sys.modules, self.modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
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
        fake_qt_core = types.ModuleType("PySide6.QtCore")
        modules = {**self.modules, "PySide6.QtCore": fake_qt_core}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "_pynput", self.fake_kb_pkg):
            self._run_in_thread(macos._install_pynput_main_thread_cache)

            def bg_read():
                with self.darwin_kb.keycode_context() as ctx:
                    return ctx, self.darwin_kb.get_unicode_to_keycode_map()

            bg_ctx, bg_map = self._run_in_thread(bg_read)

        self.assertEqual(bg_ctx, (None, None))
        self.assertEqual(bg_map, {})
        self.assertEqual(self.state["context_calls"], [])
        self.assertEqual(self.state["map_calls"], [])

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
