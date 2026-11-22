"""Setup page Save keeps every API key whatever the AI provider is."""

from __future__ import annotations

import http.client
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

import setup_web

KEYS = {
    "GEMINI_API_KEY": "gem-test-key",
    "OPENROUTER_API_KEY": "or-test-key",
    "META_API_KEY": "meta-test-key",
    "GROQ_API_KEY": "groq-test-key",
}


class SetupKeySaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = os.path.join(self.tmp.name, ".env")
        self.prompt_path = os.path.join(self.tmp.name, "prompt.txt")
        self.patches = [
            mock.patch.object(setup_web, "ENV_PATH", self.env_path),
            mock.patch.object(setup_web, "prompt_live_path", lambda: self.prompt_path),
            mock.patch.object(setup_web, "restart_odicto", lambda: "restart stubbed"),
        ]
        for p in self.patches:
            p.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_web._Handler)
        self.server.odicto_token = "test-token-abc"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _save(self, fields: dict) -> int:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "POST",
                "/save",
                body,
                {
                    "Host": f"127.0.0.1:{self.port}",
                    "X-Odicto-Token": "test-token-abc",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def _stored(self) -> dict:
        return setup_web.read_env_raw()

    def test_save_clears_effort_and_answer_cap_and_retains_literal_guidance(self):
        guidance = "Keep ${HOME}, don't change #42.\nUse <plain> English."
        self._save({"OPENROUTER_REASONING_EFFORT": "high", "LLM_MAX_TOKENS": "512"})
        self._save({"OPENROUTER_REASONING_EFFORT": "", "LLM_MAX_TOKENS": "",
                    "POLISH_PROMPT": guidance})
        saved = self._stored()
        self.assertEqual(saved["OPENROUTER_REASONING_EFFORT"], "")
        self.assertEqual(saved["LLM_MAX_TOKENS"], "0")
        self.assertEqual(urllib.parse.unquote(saved["POLISH_PROMPT_ENCODED"]), guidance)
        self.assertIn("&lt;plain&gt; English.", setup_web._page())
        self._save({"POLISH_PROMPT": ""})
        self.assertFalse(self._stored()["POLISH_PROMPT_ENCODED"])

    def test_partial_save_preserves_ai_prompt_and_refuses_invalid_cap(self):
        self._save({"SYSTEM_PROMPT": "Original instructions", "LLM_MAX_TOKENS": "256"})
        with mock.patch.object(setup_web, "restart_odicto") as restart:
            self._save({"LLM_MAX_TOKENS": "-5"})
            restart.assert_not_called()
        self.assertEqual(self._stored()["LLM_MAX_TOKENS"], "256")
        self._save({"POLISH_PROMPT": "Fix punctuation."})
        self.assertEqual(self._stored()["SYSTEM_PROMPT_FILE"], "prompt.txt")
        with open(self.prompt_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "Original instructions")

    def test_keys_are_saved_when_the_ai_provider_is_none(self) -> None:
        fields = {"LLM_PROVIDER": "none", "STT_PROVIDER": "gemini", **KEYS}
        self.assertEqual(self._save(fields), 200)
        stored = self._stored()
        for name, value in KEYS.items():
            self.assertEqual(stored.get(name), value, name)

    def test_saved_keys_survive_a_later_save_that_shows_them_masked(self) -> None:
        self.assertEqual(self._save({"LLM_PROVIDER": "none", **KEYS}), 200)
        masked = {name: setup_web._MASKED for name in KEYS}
        self.assertEqual(self._save({"LLM_PROVIDER": "none", **masked}), 200)
        stored = self._stored()
        for name, value in KEYS.items():
            self.assertEqual(stored.get(name), value, name)

    def test_a_blank_key_field_never_erases_a_saved_key(self) -> None:
        self.assertEqual(self._save({"LLM_PROVIDER": "none", **KEYS}), 200)
        blank = {name: "" for name in KEYS}
        self.assertEqual(self._save({"LLM_PROVIDER": "none", **blank}), 200)
        stored = self._stored()
        for name, value in KEYS.items():
            self.assertEqual(stored.get(name), value, name)

    def test_keys_are_saved_when_the_ai_provider_is_set(self) -> None:
        fields = {"LLM_PROVIDER": "gemini", **KEYS}
        self.assertEqual(self._save(fields), 200)
        stored = self._stored()
        for name, value in KEYS.items():
            self.assertEqual(stored.get(name), value, name)


if __name__ == "__main__":
    unittest.main()
