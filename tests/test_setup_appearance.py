"""Setup page "Appearance" view: seeded controls, saving, and rejection of bad values."""

from __future__ import annotations

import http.client
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

import config
import setup_web

KEYS = ("HUD_WAVEFORM", "HUD_TEXTURE", "HUD_EDGE", "HUD_SHAPE", "HUD_SIZE")
DEFAULTS = {"HUD_WAVEFORM": "reach", "HUD_TEXTURE": "veins", "HUD_EDGE": "rim", "HUD_SHAPE": "leaf",
            "HUD_SIZE": "1x"}
OTHER = {"HUD_WAVEFORM": "stream", "HUD_TEXTURE": "plain", "HUD_EDGE": "soft", "HUD_SHAPE": "capsule",
         "HUD_SIZE": "1.5x"}


class _Roots(unittest.TestCase):
    """A temporary .env and prompt folder; the real ones are never touched."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = os.path.join(self.tmp.name, ".env")
        self.prompt_path = os.path.join(self.tmp.name, "prompt.txt")
        self.patches = [
            mock.patch.object(setup_web, "ENV_PATH", self.env_path),
            mock.patch.object(setup_web, "prompt_live_path", lambda: self.prompt_path),
            mock.patch.object(config, "_prompt_dir", lambda: self.tmp.name),
            mock.patch.object(setup_web, "restart_odicto", lambda: "restart stubbed"),
            mock.patch.object(setup_web, "app_version", lambda: "9.9.9"),
            mock.patch.object(setup_web, "_admin_mode_state", lambda: (True, False)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _write_env(self, values: dict) -> None:
        with open(self.env_path, "w", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write("%s=%s\n" % (key, value))


def _segment_html(page: str, key: str) -> str:
    start = page.index('data-hud-key="%s"' % key)
    return page[start:page.index("</div>", start)]


def _selected_values(page: str, key: str) -> list:
    """data-value of every segment of one control that carries the selected class."""
    found = []
    for match in re.finditer(r'<button type="button" class="([^"]*)" data-value="([^"]+)" aria-pressed="([^"]+)"',
                             _segment_html(page, key)):
        classes, value, pressed = match.groups()
        if "selected" in classes.split():
            found.append((value, pressed == "true"))
    return found


class TestAppearancePage(_Roots):
    def _hidden(self, page: str, key: str) -> str:
        match = re.search(r'<input type="hidden" name="%s" id="%s" value="([^"]*)">' % (key, key), page)
        self.assertIsNotNone(match, key)
        return match.group(1)

    def test_default_page_carries_defaults(self) -> None:
        page = setup_web._page()
        for key, value in DEFAULTS.items():
            self.assertEqual(self._hidden(page, key), value, key)
            self.assertEqual(_selected_values(page, key), [(value, True)], key)

    def test_saved_values_seed_inputs_and_selection(self) -> None:
        self._write_env(OTHER)
        page = setup_web._page()
        for key, value in OTHER.items():
            self.assertEqual(self._hidden(page, key), value, key)
            self.assertEqual(_selected_values(page, key), [(value, True)], key)

    def test_hand_edited_value_is_normalised_and_bad_value_shows_default(self) -> None:
        self._write_env({"HUD_WAVEFORM": " Stream ", "HUD_TEXTURE": "glossy", "HUD_EDGE": "", "HUD_SHAPE": "CAPSULE"})
        page = setup_web._page()
        self.assertEqual(self._hidden(page, "HUD_WAVEFORM"), "stream")
        self.assertEqual(self._hidden(page, "HUD_TEXTURE"), "veins")
        self.assertEqual(self._hidden(page, "HUD_EDGE"), "rim")
        self.assertEqual(self._hidden(page, "HUD_SHAPE"), "capsule")

    def test_each_size_seeds_the_third_segment_control(self) -> None:
        for index, size in enumerate(config.HUD_CHOICES["HUD_SIZE"]):
            self._write_env({"HUD_SIZE": size})
            page = setup_web._page()
            self.assertEqual(self._hidden(page, "HUD_SIZE"), size)
            self.assertEqual(_selected_values(page, "HUD_SIZE"), [(size, True)])
            self.assertRegex(page, r'data-hud-key="HUD_SIZE" style="--n:3; --i:%d"' % index)

    def test_the_size_control_has_three_labelled_segments(self) -> None:
        page = setup_web._page()
        self.assertIn("<h3>Size</h3>", page)
        names = re.findall(r'<span class="gear-name">([^<]+)</span>', _segment_html(page, "HUD_SIZE"))
        self.assertEqual(names, ["1x", "1.3x", "1.5x"])

    def test_a_hand_edited_size_is_normalised_and_a_bad_one_shows_one_x(self) -> None:
        self._write_env({"HUD_SIZE": " 1.3X "})
        self.assertEqual(self._hidden(setup_web._page(), "HUD_SIZE"), "1.3x")
        for bad in ("2x", "1.3", "huge", ""):
            self._write_env({"HUD_SIZE": bad})
            page = setup_web._page()
            self.assertEqual(self._hidden(page, "HUD_SIZE"), "1x", bad)
            self.assertEqual(_selected_values(page, "HUD_SIZE"), [("1x", True)], bad)

    def test_the_preview_follows_the_size_choice(self) -> None:
        page = setup_web._page()
        self.assertIn("HUD_SIZE:'size'", page)
        self.assertIn("const LEAF_SIZE = {'1x':1, '1.3x':1.3, '1.5x':1.5};", page)
        self.assertIn("LEAF_VIEW.W / 2 / size, LEAF_VIEW.BOTTOM / size", page)

    def test_every_option_has_a_segment_and_no_token_is_left(self) -> None:
        page = setup_web._page()
        self.assertEqual(re.findall(r"__[A-Z0-9_]+__", page), [])
        for key, allowed in config.HUD_CHOICES.items():
            values = re.findall(r'data-value="([^"]+)"', _segment_html(page, key))
            self.assertEqual(tuple(values), allowed, key)

    def test_view_and_copy_are_present(self) -> None:
        page = setup_web._page()
        self.assertIn('data-view="appearance"', page)
        self.assertIn('id="view-appearance"', page)
        self.assertIn("Choose how the dictation pill looks. Changes apply when you save.", page)
        for word in ("Listening", "Transcribing", "Done"):
            self.assertIn("'%s'" % word, page)

    def test_view_follows_shortcuts_in_the_nav(self) -> None:
        page = setup_web._page()
        self.assertLess(page.index('data-view="controls"'), page.index('data-view="appearance"'))
        self.assertLess(page.index('data-view="appearance"'), page.index('data-view="prompt"'))

    def test_preview_constants_are_exact(self) -> None:
        page = setup_web._page()
        self.assertIn(
            "const LEAF_K = {H:36, STEM_H:32, STEM_W:2, VOICE_W:84, STEM_GAP:8, TIP:58, TEXT_GAP:12, "
            "RIGHT_INSET:18, ROWS:5, PIX:2, PITCH:3, ROW_PITCH:4, HIST:24};", page)
        self.assertIn(
            "const LEAF_C = {jade:[142,220,187], warn:[242,194,122], danger:[242,163,155], ink:[231,229,223]};", page)

    def test_the_preview_script_holds_its_stop_and_reduced_motion_checks(self) -> None:
        """Source checks only: the gate has no browser to run the preview script."""
        page = setup_web._page()
        self.assertIn("(prefers-reduced-motion: reduce)", page)
        self.assertIn("CURRENT_VIEW === 'appearance' && !document.hidden && !LEAF.still", page)
        self.assertIn("cancelAnimationFrame(LEAF.raf)", page)
        self.assertIn("window.addEventListener('resize', leafPreviewSync)", page)


class TestAppearanceSave(_Roots):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_web._Handler)
        self.server.odicto_token = "test-token-abc"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _save(self, fields: dict) -> int:
        body = urllib.parse.urlencode({"LLM_PROVIDER": "none", **fields}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/save", body, {
                "Host": "127.0.0.1:%d" % self.port,
                "X-Odicto-Token": "test-token-abc",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def _stored(self) -> dict:
        return setup_web.read_env_raw()

    def _env_text(self) -> str:
        with open(self.env_path, encoding="utf-8") as handle:
            return handle.read()

    def test_keys_are_editable_and_in_the_template(self) -> None:
        for key in KEYS:
            self.assertIn(key, setup_web.EDITABLE_KEYS)
        with open(setup_web.ENV_EXAMPLE_PATH, encoding="utf-8") as handle:
            template_text = handle.read()
        for key in KEYS:
            self.assertRegex(template_text, r"(?m)^%s=" % key)

    def test_valid_values_are_written(self) -> None:
        self.assertEqual(self._save(OTHER), 200)
        stored = self._stored()
        for key, value in OTHER.items():
            self.assertEqual(stored.get(key), value, key)

    def test_value_is_normalised_before_writing(self) -> None:
        self.assertEqual(self._save({"HUD_WAVEFORM": " Stream ", "HUD_SHAPE": "CAPSULE"}), 200)
        stored = self._stored()
        self.assertEqual(stored.get("HUD_WAVEFORM"), "stream")
        self.assertEqual(stored.get("HUD_SHAPE"), "capsule")

    def test_invalid_value_is_not_written(self) -> None:
        self.assertEqual(self._save({"HUD_WAVEFORM": "zigzag", "HUD_TEXTURE": "plain"}), 200)
        stored = self._stored()
        self.assertNotIn("zigzag", self._env_text())
        self.assertEqual(stored.get("HUD_TEXTURE"), "plain")
        self.assertIn(stored.get("HUD_WAVEFORM", ""), ("", DEFAULTS["HUD_WAVEFORM"]))

    def test_a_size_is_written_normalised_and_a_bad_size_is_not(self) -> None:
        self.assertEqual(self._save({"HUD_SIZE": " 1.3X "}), 200)
        self.assertEqual(self._stored().get("HUD_SIZE"), "1.3x")
        for bad in ("2x", "1.4x", "1.3", "x", "1.3x1.5x"):
            self.assertEqual(self._save({"HUD_SIZE": bad}), 200)
            self.assertEqual(self._stored().get("HUD_SIZE"), "1.3x", bad)
            self.assertIn("HUD_SIZE=1.3x", self._env_text().splitlines())
        self.assertEqual(self._save({"HUD_SIZE": "1x"}), 200)
        self.assertEqual(self._stored().get("HUD_SIZE"), "1x")

    def test_a_size_with_a_newline_writes_nothing(self) -> None:
        for payload in ("1.5x\nLLM_PROVIDER=meta", "1.3x\r\nSETUP_PORT=1"):
            with self.subTest(payload=payload):
                self.assertEqual(self._save({"HUD_SIZE": payload}), 200)
                text = self._env_text()
                self.assertNotIn("LLM_PROVIDER=meta", text)
                self.assertNotIn("SETUP_PORT=1", text)
                self.assertIn(self._stored().get("HUD_SIZE", ""), ("", "1x"))

    def test_invalid_value_keeps_the_saved_one(self) -> None:
        self.assertEqual(self._save(OTHER), 200)
        self.assertEqual(self._save({"HUD_EDGE": "glow", "HUD_SHAPE": ""}), 200)
        stored = self._stored()
        self.assertEqual(stored.get("HUD_EDGE"), "soft")
        self.assertEqual(stored.get("HUD_SHAPE"), "capsule")

    def test_newline_injection_writes_nothing(self) -> None:
        payloads = ["reach\nLLM_PROVIDER=meta", "stream\r\nSETUP_PORT=1", "plain\nHUD_EDGE=soft"]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self._save({"HUD_WAVEFORM": payload, "HUD_TEXTURE": payload}), 200)
                text = self._env_text()
                self.assertNotIn("LLM_PROVIDER=meta", text)
                self.assertNotIn("SETUP_PORT=1", text)
                self.assertNotIn("HUD_EDGE=soft", text)
                stored = self._stored()
                self.assertEqual(stored.get("LLM_PROVIDER"), "none")
                for key in ("HUD_WAVEFORM", "HUD_TEXTURE"):
                    self.assertIn(stored.get(key, ""), ("", DEFAULTS[key]), key)

    def test_merge_env_rejects_an_invalid_value_directly(self) -> None:
        setup_web.merge_env({"HUD_SHAPE": "blob", "HUD_EDGE": "soft", "HUD_SIZE": "2x"})
        stored = self._stored()
        self.assertNotIn("blob", self._env_text())
        self.assertNotIn("2x", self._env_text())
        self.assertEqual(stored.get("HUD_EDGE"), "soft")
        self.assertEqual(stored.get("HUD_SIZE"), "1x")


if __name__ == "__main__":
    unittest.main()
