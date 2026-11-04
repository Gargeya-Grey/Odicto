"""Setup page "Colour" panel: the seeded page, saving THEME_COLOR, and rejection of bad values."""

from __future__ import annotations

import http.client
import re
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

import config
import setup_web
import theme
from tests.test_setup_appearance import _Roots


def _swatches(page: str) -> dict:
    """data-theme -> (selected class, aria-pressed) of each preset swatch."""
    found = {}
    for classes, name, pressed in re.findall(
            r'<button type="button" class="([^"]*)" data-theme="([a-z]+)"[^>]*aria-pressed="(true|false)"', page):
        found[name] = ("selected" in classes.split(), pressed == "true")
    return found


def _custom(page: str) -> str:
    match = re.search(r'<label class="([^"]*)" title="Custom colour"', page)
    assert match, "no custom swatch"
    return match.group(1)


def _root_vars(page: str) -> tuple:
    match = re.search(r"--h: ([0-9.]+); --k: ([0-9.]+);", page)
    assert match, "no --h and --k in the page"
    return match.group(1), match.group(2)


class TestColourPanelSeed(_Roots):
    def _hidden(self, page: str) -> str:
        match = re.search(r'<input type="hidden" name="THEME_COLOR" id="THEME_COLOR" value="([^"]*)">', page)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_the_default_page_is_forest_with_no_token_left(self) -> None:
        page = setup_web._page()
        self.assertEqual(re.findall(r"__[A-Z0-9_]+__", page), [])
        self.assertEqual(self._hidden(page), "forest")
        self.assertEqual(_root_vars(page), ("165", "1"))
        self.assertEqual({n: v for n, v in _swatches(page).items()},
                         {n: (n == "forest", n == "forest") for n in theme.PRESETS})
        self.assertNotIn("filled", _custom(page).split())
        self.assertIn('id="theme_name" aria-live="polite">Forest<', page)

    def test_every_preset_seeds_the_page_variables_and_the_selected_swatch(self) -> None:
        for name, (hue, k) in theme.PRESETS.items():
            with self.subTest(theme=name):
                self._write_env({"THEME_COLOR": name})
                page = setup_web._page()
                self.assertEqual(self._hidden(page), name)
                self.assertEqual(_root_vars(page), (f"{hue:g}", f"{k:g}"))
                self.assertEqual(_swatches(page), {n: (n == name, n == name) for n in theme.PRESETS})
                self.assertIn(">%s<" % name.capitalize(), page.split('id="theme_name"')[1][:80])

    def test_a_custom_colour_seeds_the_variables_the_name_and_the_picker(self) -> None:
        self._write_env({"THEME_COLOR": "#C06A8F"})
        page = setup_web._page()
        self.assertEqual(self._hidden(page), "#c06a8f")
        want = theme.page_theme("#c06a8f")
        self.assertEqual(_root_vars(page), (want["--h"], want["--k"]))
        self.assertEqual(_swatches(page), {n: (False, False) for n in theme.PRESETS})
        self.assertEqual(sorted(_custom(page).split()), ["custom", "filled", "selected", "swatch"])
        self.assertIn('id="theme_custom" value="#c06a8f"', page)
        self.assertIn(">#C06A8F<", page.split('id="theme_name"')[1][:80])

    def test_a_bad_saved_value_shows_forest(self) -> None:
        for bad in ("teal", "#abc", "", "#12345g", "forest\nX=1"):
            with self.subTest(bad=bad):
                self._write_env({"THEME_COLOR": bad.replace("\n", " ")})
                page = setup_web._page()
                self.assertEqual(self._hidden(page), "forest")
                self.assertEqual(_root_vars(page), ("165", "1"))

    def test_a_hand_edited_value_is_normalised(self) -> None:
        self._write_env({"THEME_COLOR": " Ocean "})
        self.assertEqual(self._hidden(setup_web._page()), "ocean")

    def test_the_picker_opens_on_the_accent_of_a_preset(self) -> None:
        self._write_env({"THEME_COLOR": "ocean"})
        jade = theme.hud_palette("ocean").jade
        self.assertIn('id="theme_custom" value="#%02x%02x%02x"' % jade, setup_web._page())

    def test_the_colour_panel_sits_above_the_pill_options(self) -> None:
        page = setup_web._page()
        self.assertLess(page.index('id="h_appearance_preview"'), page.index('id="h_appearance_colour"'))
        self.assertLess(page.index('id="h_appearance_colour"'), page.index('id="h_appearance_style"'))
        self.assertIn("The leaf, its glow, and the accents on this page.", page)

    def test_the_page_script_applies_a_choice_to_the_page_and_the_preview(self) -> None:
        page = setup_web._page()
        for text in ("function initThemePicker()", "root.setProperty('--h'", "root.setProperty('--k'",
                     "LEAF.pal = themePalette(theme);", "initThemePicker();\ninitLeafPreview();"):
            self.assertIn(text, page)


class TestColourSave(_Roots):
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

    def test_the_key_is_editable_and_in_the_example_file(self) -> None:
        self.assertIn("THEME_COLOR", setup_web.EDITABLE_KEYS)
        with open(setup_web.ENV_EXAMPLE_PATH, encoding="utf-8") as handle:
            self.assertRegex(handle.read(), r"(?m)^THEME_COLOR=")

    def test_a_preset_and_a_custom_colour_are_written_lower_case(self) -> None:
        self.assertEqual(self._save({"THEME_COLOR": " Plum "}), 200)
        self.assertEqual(self._stored().get("THEME_COLOR"), "plum")
        self.assertEqual(self._save({"THEME_COLOR": "#C06A8F"}), 200)
        self.assertEqual(self._stored().get("THEME_COLOR"), "#c06a8f")
        self.assertIn("THEME_COLOR=#c06a8f", self._env_text().splitlines())

    def test_a_bad_value_is_never_written_and_the_stored_value_stays(self) -> None:
        self.assertEqual(self._save({"THEME_COLOR": "ocean"}), 200)
        for bad in ("teal", "#abc", "c06a8f", "#12345g", "#1234567", "", "forest\nLLM_PROVIDER=meta", "<b>"):
            with self.subTest(bad=bad):
                self.assertEqual(self._save({"THEME_COLOR": bad}), 200)
                self.assertEqual(self._stored().get("THEME_COLOR"), "ocean")
                self.assertNotIn("LLM_PROVIDER=meta", self._env_text())
                self.assertNotIn("teal", self._env_text())

    def test_merge_env_rejects_an_invalid_colour_directly(self) -> None:
        setup_web.merge_env({"THEME_COLOR": "#nothex", "HUD_EDGE": "soft"})
        self.assertNotIn("nothex", self._env_text())
        self.assertIn(self._stored().get("THEME_COLOR", ""), ("", "forest"))
        self.assertEqual(self._stored().get("HUD_EDGE"), "soft")

    def test_a_saved_colour_comes_back_on_the_next_page(self) -> None:
        self.assertEqual(self._save({"THEME_COLOR": "ember"}), 200)
        page = setup_web._page()
        self.assertEqual(_root_vars(page), ("32", "1"))
        self.assertEqual(_swatches(page)["ember"], (True, True))

    def test_the_defaults_table_and_the_setup_page_agree_on_the_default(self) -> None:
        self.assertEqual(config.ENV_DEFAULTS["THEME_COLOR"], "forest")


if __name__ == "__main__":
    unittest.main()
