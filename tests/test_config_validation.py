import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import config


_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "config.py")


def _reload_with(env: dict):
    """Load a PRIVATE copy of config.py under patched env vars.

    The shared sys.modules['config'] and its Config class are never replaced.
    """
    spec = importlib.util.spec_from_file_location("_config_private_copy", _CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    # Skip .env so the developer's real file cannot override the patched values.
    with mock.patch.dict(os.environ, env), mock.patch("dotenv.load_dotenv", lambda *a, **k: False),             redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class TestConfigValidation(unittest.TestCase):
    def test_bad_int_names_key_and_falls_back(self) -> None:
        cfg = _reload_with({"MIN_HOLD_MS": "abc"})
        self.assertEqual(cfg.Config.MIN_HOLD_MS, 80)
        self.assertTrue(any("MIN_HOLD_MS" in w and "abc" in w for w in cfg.Config.CONFIG_WARNINGS))
        self.assertIs(cfg.Config.CONFIG_WARNINGS, cfg.CONFIG_WARNINGS)

    def test_bad_float_names_key_and_falls_back(self) -> None:
        cfg = _reload_with({"STT_DEADLINE_SECONDS": "soon"})
        self.assertEqual(cfg.Config.STT_DEADLINE_SECONDS, 20.0)
        self.assertTrue(any("STT_DEADLINE_SECONDS" in w for w in cfg.CONFIG_WARNINGS))

    def test_out_of_range_is_clamped_with_warning(self) -> None:
        cfg = _reload_with({"MAX_RECORDING_SECONDS": "99999", "LLM_DEADLINE_SECONDS": "0.5",
                            "PASTE_DELAY_SECONDS": "0"})
        self.assertEqual(cfg.Config.MAX_RECORDING_SECONDS, 7200)
        self.assertEqual(cfg.Config.LLM_DEADLINE_SECONDS, 3.0)
        self.assertEqual(cfg.Config.PASTE_DELAY_SECONDS, 0.15)
        for key in ("MAX_RECORDING_SECONDS", "LLM_DEADLINE_SECONDS", "PASTE_DELAY_SECONDS"):
            self.assertTrue(any(key in w for w in cfg.CONFIG_WARNINGS), key)

    def test_defaults(self) -> None:
        d = config.ENV_DEFAULTS
        self.assertEqual(d["PASTE_DELAY_SECONDS"], "1.0")
        self.assertEqual(d["MAX_RECORDING_SECONDS"], "600")
        self.assertEqual(d["STT_DEADLINE_SECONDS"], "20")
        self.assertEqual(d["LLM_DEADLINE_SECONDS"], "30")
        self.assertEqual(d["CANCEL_HOTKEY"], "esc")
        self.assertEqual(d["LOG_TRANSCRIPTS"], "false")
        self.assertEqual(d["POLISH_MAX_CHARS"], "1200")

    def test_default_attributes_when_env_unset(self) -> None:
        cfg = _reload_with({
            "PASTE_DELAY_SECONDS": "1.0", "MAX_RECORDING_SECONDS": "600",
            "STT_DEADLINE_SECONDS": "20", "LLM_DEADLINE_SECONDS": "30",
            "CANCEL_HOTKEY": " ESC ", "LOG_TRANSCRIPTS": "false", "POLISH_MAX_CHARS": "1200",
        })
        C = cfg.Config
        self.assertEqual(C.PASTE_DELAY_SECONDS, 1.0)
        self.assertEqual(C.MAX_RECORDING_SECONDS, 600)
        self.assertEqual(C.STT_DEADLINE_SECONDS, 20.0)
        self.assertEqual(C.LLM_DEADLINE_SECONDS, 30.0)
        self.assertEqual(C.CANCEL_HOTKEY, "esc")
        self.assertIs(C.LOG_TRANSCRIPTS, False)
        self.assertEqual(C.POLISH_MAX_CHARS, 1200)
        self.assertEqual(C.CONFIG_WARNINGS, [])

    def test_log_transcripts_parses_bool(self) -> None:
        self.assertIs(_reload_with({"LOG_TRANSCRIPTS": "true"}).Config.LOG_TRANSCRIPTS, True)
        self.assertIs(_reload_with({"LOG_TRANSCRIPTS": "no"}).Config.LOG_TRANSCRIPTS, False)

    def test_blank_cancel_hotkey_disables(self) -> None:
        self.assertEqual(_reload_with({"CANCEL_HOTKEY": ""}).Config.CANCEL_HOTKEY, "")


if __name__ == "__main__":
    unittest.main()
