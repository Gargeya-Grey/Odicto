"""HUD appearance keys: defaults, normalising, blank and unknown values."""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import config

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "config.py"
)
_KEYS = ("HUD_WAVEFORM", "HUD_TEXTURE", "HUD_EDGE", "HUD_SHAPE", "HUD_SIZE")
_DEFAULTS = {"HUD_WAVEFORM": "reach", "HUD_TEXTURE": "veins", "HUD_EDGE": "rim", "HUD_SHAPE": "leaf",
             "HUD_SIZE": "1x"}


def _reload_with(env: dict):
    """Load a PRIVATE copy of config.py under a patched environment, without a real .env."""
    spec = importlib.util.spec_from_file_location("_config_hud_copy", _CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    clean = {k: v for k, v in os.environ.items() if k not in _KEYS}
    with mock.patch.dict(os.environ, clean, clear=True), \
            mock.patch.dict(os.environ, env), \
            mock.patch("dotenv.load_dotenv", lambda *a, **k: False), \
            redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class TestHudStyleConfig(unittest.TestCase):
    def test_defaults_table(self) -> None:
        for key, value in _DEFAULTS.items():
            self.assertEqual(config.ENV_DEFAULTS[key], value)
            self.assertEqual(config.HUD_CHOICES[key][0], value)

    def test_defaults_when_unset(self) -> None:
        cfg = _reload_with({})
        for key, value in _DEFAULTS.items():
            self.assertEqual(getattr(cfg.Config, key), value, key)
        self.assertEqual(cfg.Config.CONFIG_WARNINGS, [])

    def test_value_is_stripped_and_lowercased(self) -> None:
        cfg = _reload_with({"HUD_WAVEFORM": "  Stream ", "HUD_SHAPE": "CAPSULE", "HUD_EDGE": " Soft", "HUD_TEXTURE": "Plain ",
                            "HUD_SIZE": " 1.5X "})
        C = cfg.Config
        self.assertEqual(C.HUD_WAVEFORM, "stream")
        self.assertEqual(C.HUD_SHAPE, "capsule")
        self.assertEqual(C.HUD_EDGE, "soft")
        self.assertEqual(C.HUD_TEXTURE, "plain")
        self.assertEqual(C.HUD_SIZE, "1.5x")
        self.assertEqual(C.CONFIG_WARNINGS, [])

    def test_blank_reads_as_default_without_warning(self) -> None:
        cfg = _reload_with({key: "" for key in _KEYS} | {"HUD_TEXTURE": "   "})
        for key, value in _DEFAULTS.items():
            self.assertEqual(getattr(cfg.Config, key), value, key)
        self.assertEqual(cfg.Config.CONFIG_WARNINGS, [])

    def test_unknown_value_warns_once_and_uses_default(self) -> None:
        for key in _KEYS:
            cfg = _reload_with({key: "sharp"})
            self.assertEqual(getattr(cfg.Config, key), _DEFAULTS[key], key)
            warnings = [w for w in cfg.CONFIG_WARNINGS if key in w]
            self.assertEqual(len(warnings), 1, key)
            for allowed in config.HUD_CHOICES[key]:
                self.assertIn(allowed, warnings[0])
            self.assertEqual(len(cfg.CONFIG_WARNINGS), 1)

    def test_size_allows_exactly_the_three_scales(self) -> None:
        self.assertEqual(config.HUD_CHOICES["HUD_SIZE"], ("1x", "1.3x", "1.5x"))
        for value in ("1x", "1.3x", "1.5x"):
            cfg = _reload_with({"HUD_SIZE": value})
            self.assertEqual((cfg.Config.HUD_SIZE, cfg.Config.CONFIG_WARNINGS), (value, []))

    def test_an_unknown_size_uses_one_x_with_one_warning(self) -> None:
        for bad in ("2x", "1.4x", "big", "1,3x", "1.3", "0x"):
            cfg = _reload_with({"HUD_SIZE": bad})
            self.assertEqual(cfg.Config.HUD_SIZE, "1x", bad)
            self.assertEqual(len(cfg.CONFIG_WARNINGS), 1, bad)
            self.assertIn("HUD_SIZE", cfg.CONFIG_WARNINGS[0])
            self.assertIn("1.3x", cfg.CONFIG_WARNINGS[0])

    def test_explain_lists_each_key(self) -> None:
        text = repr(config.Config.explain())
        for label in ("Visual HUD waveform", "Visual HUD texture", "Visual HUD edge", "Visual HUD shape",
                      "Visual HUD size"):
            self.assertIn(label, text)

    def test_keys_are_not_in_import_snapshot(self) -> None:
        for key in _KEYS:
            self.assertNotIn(key, config._IMPORT_SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
