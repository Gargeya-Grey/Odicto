"""Gemini AI request bounds, with provider calls replaced by local doubles."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from refiner import _GeminiClient
from tests.test_reliability import refiner_fixture


class TestProviderDeadlines(unittest.TestCase):
    def client(self):
        client = _GeminiClient.__new__(_GeminiClient)
        client.model = "test-model"
        client._last_interaction_id = None
        client.client = MagicMock()
        client.client.interactions.create.return_value = SimpleNamespace(output_text="answer", id="id")
        return client

    def test_gemini_ai_defaults_to_finite_request_timeout(self):
        client = self.client()
        self.assertEqual(client.create_interaction("question", 10, system_instruction="test"), "answer")
        self.assertEqual(client.client.interactions.create.call_args.kwargs.get("timeout"), 30.0)

    def test_explicit_polish_timeout_stays_two_seconds(self):
        client = self.client()
        client.create_interaction("words", 10, system_instruction="test", timeout=2.0)
        self.assertEqual(client.client.interactions.create.call_args.kwargs.get("timeout"), 2.0)

    def test_gemini_timeout_keeps_raw_text_and_drops_pending_history(self):
        refiner = refiner_fixture("gemini")
        refiner.client = self.client()
        refiner.client.client.interactions.create.side_effect = TimeoutError("synthetic timeout")
        with patch.object(Config, "effective_system_prompt", return_value="test"):
            self.assertEqual(refiner.refine("raw words", keep_history=True), "raw words")
        self.assertEqual(refiner.last_notice, "ai_fallback")
        self.assertEqual(refiner.conversation_history, [])
        self.assertIsNone(refiner.client._last_interaction_id)
