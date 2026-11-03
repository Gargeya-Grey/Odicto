"""Selection polish upgrade and setup persistence without runtime side effects."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import config
import setup_web
from tests.test_equivalence import _render_page


def private_config(values):
    spec = importlib.util.spec_from_file_location("_polish_upgrade_config", config.__file__)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, values, clear=True), patch("dotenv.load_dotenv", return_value=False):
        spec.loader.exec_module(module)
    module.load_env_file_keys = lambda: {}
    return module


class TestPolishUpgrade(unittest.TestCase):
    def test_implicit_default_yields_to_each_existing_f8_binding_without_mutation(self):
        cases = ({"HOTKEY": "ctrl+f8", "AI_HOTKEY": "ctrl+shift+f8"},
                 {"LIVE_HOTKEY": "f8"}, {"RESET_CONTEXT_HOTKEY": "f8"},
                 {"CANCEL_HOTKEY": "f8"}, {"CTRL_KEEP_CONTEXT_KEYS": "f8"})
        for values in cases:
            with self.subTest(values=values):
                module = private_config(values)
                with redirect_stdout(io.StringIO()):
                    module.Config.validate()
                    self.assertEqual(module.Config.effective_polish_hotkey(), "")
                self.assertEqual(module.Config.POLISH_HOTKEY, "f8")
                self.assertEqual(len([w for w in module.Config.CONFIG_WARNINGS
                    if "POLISH_HOTKEY default conflicts" in w]), 1)

    def test_explicit_conflicting_shortcut_is_rejected(self):
        for key in ("LIVE_HOTKEY", "RESET_CONTEXT_HOTKEY", "CANCEL_HOTKEY", "CTRL_KEEP_CONTEXT_KEYS"):
            with self.subTest(key=key):
                module = private_config({"POLISH_HOTKEY": "f8", key: "f8"})
                with self.assertRaisesRegex(ValueError, "different primary key"):
                    module.Config.validate()

    def test_explicit_blank_and_valid_chord_remain_intentional(self):
        for value in ("", "ctrl+shift+p"):
            with self.subTest(value=value):
                module = private_config({"POLISH_HOTKEY": value})
                module.Config.validate()
                self.assertEqual(module.Config.effective_polish_hotkey(), value)

    def test_page_preserves_disabled_or_explicit_invalid_shortcut_for_editing(self):
        for value in ("", "f7"):
            with self.subTest(value=value):
                page = _render_page({"env": {"POLISH_HOTKEY": value}})
                self.assertIn('id="POLISH_HOTKEY" value="' + value + '"', page)

    def test_page_disables_implicit_default_when_existing_shortcut_owns_f8(self):
        page = _render_page({"env": {"LIVE_HOTKEY": "f8"}})
        self.assertIn('id="POLISH_HOTKEY" value=""', page)

    def test_disable_survives_save_unsubmitted_save_reload_and_config_load(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory, ".env")
            with ExitStack() as stack:
                stack.enter_context(patch.object(setup_web, "ENV_PATH", str(env)))
                stack.enter_context(patch.object(setup_web, "prompt_live_path", return_value=str(Path(directory, "prompt.txt"))))
                stack.enter_context(patch.object(setup_web, "_page", return_value="saved page"))
                stack.enter_context(patch.object(setup_web, "restart_odicto", return_value="restart stubbed"))
                stack.enter_context(patch.object(config.Config, "HOTKEY", "ctrl+grave"))
                stack.enter_context(patch.object(config.Config, "AI_HOTKEY", "ctrl+shift+grave"))
                stack.enter_context(patch.object(config.Config, "LIVE_HOTKEY", "f7"))
                handler = object.__new__(setup_web._Handler)
                handler._inject_token = lambda body: body
                handler._send = Mock()
                handler._handle_save({"POLISH_HOTKEY": [""], "SYSTEM_PROMPT": [config.DEFAULT_SYSTEM_PROMPT]})
                self.assertEqual(setup_web.read_env()["POLISH_HOTKEY"], "")
                setup_web.merge_env({"POLISH_MODEL": "custom/model"})
                values = setup_web.read_env()
                self.assertEqual(values["POLISH_HOTKEY"], "")
            self.assertEqual(private_config({"POLISH_HOTKEY": values["POLISH_HOTKEY"]}).Config.effective_polish_hotkey(), "")

    def test_disabled_shortcut_does_not_bind_f8(self):
        from tests.test_reliability import app_fixture
        import main
        hooks = []
        with patch.object(config.Config, "POLISH_HOTKEY", ""), patch.object(
            config.Config, "HOTKEY", "ctrl+grave"
        ), patch.object(config.Config, "AI_HOTKEY", "ctrl+shift+grave"), patch.object(
            config.Config, "LIVE_HOTKEY", "f7"
        ), patch.object(config.Config, "RESET_CONTEXT_HOTKEY", "f5"), patch.object(
            config.Config, "CANCEL_HOTKEY", "esc"
        ), patch("main.ensure_can_bind_hotkeys"), patch(
            "main.platforms.hook_key", side_effect=lambda key, *args, **kwargs: hooks.append(key)
        ):
            app_fixture()._bind_hotkeys()
        self.assertNotIn("f8", hooks)

    def test_invalid_shortcut_leaves_settings_and_changed_or_restored_prompt_untouched(self):
        for prompt in ("changed private prompt", config.DEFAULT_SYSTEM_PROMPT):
            with self.subTest(prompt_kind="default" if prompt == config.DEFAULT_SYSTEM_PROMPT else "custom"):
                with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    env = Path(directory, ".env")
                    live_prompt = Path(directory, "prompt.txt")
                    env.write_text("HOTKEY=ctrl+grave\nPOLISH_HOTKEY=f8\n", encoding="utf-8")
                    live_prompt.write_text("original synthetic prompt", encoding="utf-8")
                    before = env.read_bytes(), live_prompt.read_bytes()
                    stack.enter_context(patch.object(setup_web, "ENV_PATH", str(env)))
                    stack.enter_context(patch.object(setup_web, "prompt_live_path", return_value=str(live_prompt)))
                    page = stack.enter_context(patch.object(setup_web, "_page", return_value="error page"))
                    restart = stack.enter_context(patch.object(setup_web, "restart_odicto"))
                    stack.enter_context(patch.object(config.Config, "HOTKEY", "ctrl+grave"))
                    stack.enter_context(patch.object(config.Config, "AI_HOTKEY", "ctrl+shift+grave"))
                    stack.enter_context(patch.object(config.Config, "LIVE_HOTKEY", "f7"))
                    handler = object.__new__(setup_web._Handler)
                    handler._inject_token = lambda body: body
                    handler._send = Mock()
                    handler._handle_save({"POLISH_HOTKEY": ["f7"], "SYSTEM_PROMPT": [prompt]})
                    self.assertEqual((env.read_bytes(), live_prompt.read_bytes()), before)
                    self.assertIn("Hotkey invalid", page.call_args.args[0])
                    restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
