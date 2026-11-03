"""Personal spelling hints across speech backends; no network or global hooks."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from config import Config
import transcriber
from tests.test_reliability import refiner_fixture


class TestPersonalVocabulary(unittest.TestCase):
    def test_normalizes_separators_unicode_and_duplicate_spellings(self):
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", " Odicto, odicto\nCafe\u0301，Café\r\nTwinAatma "):
            self.assertEqual(Config.gemini_transcribe_vocabulary(), ["Odicto", "Café", "TwinAatma"])

    def test_setup_exposes_vocabulary_for_all_engines_and_polish_page(self):
        from html.parser import HTMLParser
        from tests.test_equivalence import _render_page
        from setup_web import _format_env_assignment
        page = _render_page({"env": {"STT_PROVIDER": "groq",
            "GEMINI_TRANSCRIBE_VOCABULARY": _format_env_assignment(
                "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto\nGargeya").partition("=")[2]}})
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__(); self.sections = []; self.controls = {}; self.ids = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs: self.ids.append(attrs["id"])
                if tag == "section": self.sections.append(attrs.get("id", ""))
                if attrs.get("id") in ("GEMINI_TRANSCRIBE_VOCABULARY", "POLISH_DICTATION"):
                    self.controls[attrs["id"]] = list(self.sections)
            def handle_endtag(self, tag):
                if tag == "section": self.sections.pop()
        controls = Controls(); controls.feed(page)
        self.assertEqual(len(controls.ids), len(set(controls.ids)))
        self.assertIn("view-speech", controls.controls["GEMINI_TRANSCRIBE_VOCABULARY"])
        self.assertNotIn("stt-gemini-fields", controls.controls["GEMINI_TRANSCRIBE_VOCABULARY"])
        self.assertIn("view-polish", controls.controls["POLISH_DICTATION"])
        self.assertIn("Odicto\nGargeya</textarea>", page)

    def test_setup_saves_multiline_vocabulary_as_one_safe_assignment(self):
        import io
        from dotenv import dotenv_values
        from setup_web import _format_env_assignment
        text = 'Odicto\nGargeya\nPOLISH_DICTATION=true'
        assignment = _format_env_assignment("GEMINI_TRANSCRIBE_VOCABULARY", text)
        values = dotenv_values(stream=io.StringIO(assignment))
        self.assertEqual(values, {"GEMINI_TRANSCRIBE_VOCABULARY": text})

    def test_gemini_terms_keep_first_hundred(self):
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", ",".join(f"Term{i}" for i in range(103))):
            self.assertEqual(Config.gemini_transcribe_vocabulary(), [f"Term{i}" for i in range(100)])

    def test_whisper_prompt_keeps_whole_terms_within_utf8_budget(self):
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto," + "語" * 80):
            self.assertEqual(Config.speech_vocabulary_prompt(), "Odicto")

    def test_whisper_budget_counts_encoded_bytes_including_separators(self):
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", "語" * 74 + ",x"):
            self.assertEqual(Config.speech_vocabulary_prompt(), "語" * 74)

    def test_empty_vocabulary_has_empty_prompt(self):
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", " ,\n， "):
            self.assertEqual(Config.speech_vocabulary_prompt(), "")

    def test_whisper_receives_spelling_hints_without_replacing_transcript(self):
        engine = transcriber.WhisperTranscriber.__new__(transcriber.WhisperTranscriber)
        engine.model = MagicMock()
        engine.model.transcribe.return_value = ([SimpleNamespace(text="different words")], None)
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto, TwinAatma"):
            self.assertEqual(engine.transcribe(np.ones(1600, dtype=np.float32)), "different words")
        self.assertEqual(engine.model.transcribe.call_args.kwargs["initial_prompt"], "Odicto, TwinAatma")

    def test_empty_vocabulary_does_not_add_whisper_prompt(self):
        engine = transcriber.WhisperTranscriber.__new__(transcriber.WhisperTranscriber)
        engine.model = MagicMock()
        engine.model.transcribe.return_value = ([SimpleNamespace(text="words")], None)
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", ""):
            self.assertEqual(engine.transcribe(np.ones(1600, dtype=np.float32)), "words")
        self.assertNotIn("initial_prompt", engine.model.transcribe.call_args.kwargs)

    def test_groq_upload_includes_vocabulary_prompt(self):
        backend = transcriber.CloudTranscriber("groq")
        backend._endpoint = lambda: ("https://example.invalid/transcribe", "synthetic-key", "whisper-large-v3-turbo")
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto, TwinAatma"), patch.object(
            transcriber, "_post_bytes", return_value={"text": "Odicto works"}
        ) as post:
            self.assertEqual(backend.transcribe(np.ones(1600, dtype=np.float32)), "Odicto works")
        body = post.call_args.args[1]
        self.assertIn(b'name="prompt"', body)
        self.assertIn(b"Odicto, TwinAatma", body)

    def test_polish_receives_vocabulary_and_leaves_input_and_ai_memory_separate(self):
        editor = refiner_fixture()
        editor.conversation_history = [{"role": "user", "content": "unrelated prior request"}]
        text = "Odicto is very very good.\n\nI feels happy!"
        with patch.object(Config, "GEMINI_TRANSCRIBE_VOCABULARY", "Odicto, TwinAatma"):
            self.assertEqual(editor.polish(text), "Hello world.")
        request = editor.client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["messages"][1], {"role": "user", "content": text})
        self.assertIn("Odicto", request["messages"][0]["content"])
        self.assertIn("TwinAatma", request["messages"][0]["content"])
        self.assertNotIn("unrelated prior request", str(request))
        self.assertEqual(editor.conversation_history, [{"role": "user", "content": "unrelated prior request"}])
