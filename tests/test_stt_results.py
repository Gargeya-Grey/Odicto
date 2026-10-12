"""Provider-result handling without network, model loading, or microphone input."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

import transcriber
from config import Config


class TestSpeechResults(unittest.TestCase):
    @contextmanager
    def backend(self, provider, payload=None, error=None):
        if provider == "gemini":
            client = MagicMock()
            client.interactions.create.return_value = SimpleNamespace(**(payload or {}))
            client.interactions.create.side_effect = error
            with patch.object(transcriber, "_ensure_google_genai"), patch.object(
                transcriber, "google_genai", MagicMock()
            ), patch.object(Config, "GEMINI_API_KEY", "synthetic-key"), patch.object(
                transcriber, "get_genai_client", return_value=client
            ):
                backend = transcriber.GeminiTranscriber()
            boundary = patch.object(client.interactions, "create", client.interactions.create)
        else:
            backend = transcriber.CloudTranscriber(provider)
            backend._endpoint = lambda: ("https://invalid.example", "synthetic-key", "test-model")
            boundary = patch.object(transcriber, "_post_bytes", return_value=payload, side_effect=error)
        with boundary, patch.object(backend, "_whisper_fallback", return_value="fallback words") as fallback:
            yield backend, fallback

    def test_valid_empty_text_does_not_invoke_another_model(self):
        for provider in ("groq", "openrouter", "gemini"):
            for empty in ("", " \n\t"):
                with self.subTest(provider=provider, text=repr(empty)):
                    key = "output_text" if provider == "gemini" else "text"
                    with self.backend(provider, {key: empty}) as (backend, fallback):
                        self.assertEqual(backend.transcribe(np.zeros(16000, dtype=np.float32)), "")
                        fallback.assert_not_called()

    def test_recognized_words_are_preserved(self):
        for provider in ("groq", "openrouter", "gemini"):
            with self.subTest(provider=provider):
                key = "output_text" if provider == "gemini" else "text"
                with self.backend(provider, {key: "  Spoken words.  "}) as (backend, fallback):
                    self.assertEqual(backend.transcribe(np.ones(16000, dtype=np.float32)), "Spoken words.")
                    fallback.assert_not_called()

    def test_missing_or_invalid_text_still_falls_back(self):
        for provider in ("groq", "openrouter", "gemini"):
            key = "output_text" if provider == "gemini" else "text"
            for payload in ({}, {key: None}, {key: 123}):
                with self.subTest(provider=provider, payload=payload):
                    with self.backend(provider, payload) as (backend, fallback):
                        self.assertEqual(backend.transcribe(np.ones(16000, dtype=np.float32)), "fallback words")
                        fallback.assert_called_once()

    def test_provider_failure_still_falls_back(self):
        for provider in ("groq", "openrouter", "gemini"):
            with self.subTest(provider=provider):
                with self.backend(provider, error=RuntimeError("unavailable")) as (backend, fallback):
                    self.assertEqual(backend.transcribe(np.ones(16000, dtype=np.float32)), "fallback words")
                    fallback.assert_called_once()

    def test_gemini_unary_request_has_finite_timeout(self):
        with self.backend("gemini", {"output_text": "words"}) as (backend, fallback):
            backend.transcribe(np.ones(16000, dtype=np.float32))
            self.assertEqual(backend._client.interactions.create.call_args.kwargs.get("timeout"), 15.0)
            fallback.assert_not_called()

    def test_shared_gemini_client_disables_retries_without_live_lifetime_timeout(self):
        factory = MagicMock()
        with patch.object(transcriber, "_ensure_google_genai"), patch.object(
            transcriber, "google_genai", SimpleNamespace(Client=factory)
        ):
            transcriber.get_genai_client("synthetic-key")
        options = factory.call_args.kwargs.get("http_options", {})
        self.assertEqual(options.get("retry_options"), {"attempts": 0})
        self.assertNotIn("timeout", options, "unary deadline must not end live dictation")

    def test_installed_gemini_sdk_applies_timeout_and_one_attempt(self):
        import httpx
        from google import genai

        requests = []

        def transport(request):
            requests.append(request.extensions["timeout"])
            raise httpx.ReadTimeout("synthetic stalled provider")

        clients = []

        def factory(**kwargs):
            options = dict(kwargs.get("http_options", {}))
            options["client_args"] = {"transport": httpx.MockTransport(transport)}
            kwargs["http_options"] = options
            client = genai.Client(**kwargs)
            clients.append(client)
            return client

        try:
            with patch.object(transcriber, "_ensure_google_genai"), patch.object(
                transcriber, "google_genai", SimpleNamespace(Client=factory)
            ), patch.object(Config, "GEMINI_API_KEY", "synthetic-key"):
                backend = transcriber.GeminiTranscriber()
            with patch.object(backend, "_whisper_fallback", return_value="fallback words") as fallback:
                self.assertEqual(backend.transcribe(np.zeros(16000, dtype=np.float32)), "fallback words")
                fallback.assert_called_once()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0], dict(connect=15.0, read=15.0, write=15.0, pool=15.0))
        finally:
            for client in clients:
                client.close()
