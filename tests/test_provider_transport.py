"""Shared HTTP pools, request budgets, polish sizing and prewarm. No network."""
import contextlib
import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import numpy as np

import http_clients
import refiner
import transcriber
from config import Config
from refiner import TextRefiner, _MetaClient
from tests.test_reliability import refiner_fixture


def _reply(content="An answer.", finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=None,
    )


def _assert_full_timeout(case, timeout, read=None):
    case.assertIsInstance(timeout, httpx.Timeout, f"not an httpx.Timeout: {timeout!r}")
    for phase in ("connect", "read", "write", "pool"):
        case.assertIsNotNone(getattr(timeout, phase), f"{phase} timeout is unset")
    case.assertLessEqual(timeout.connect, 5.0)
    if read is not None:
        case.assertAlmostEqual(timeout.read, read, places=1)
        case.assertAlmostEqual(timeout.write, read, places=1)


class TestSharedClients(unittest.TestCase):
    def test_full_timeout_sets_every_phase(self):
        t = http_clients.full_timeout(3.0, 20.0)
        self.assertEqual((t.connect, t.read, t.write, t.pool), (3.0, 20.0, 20.0, 3.0))
        t = http_clients.full_timeout(1, 2, write=4, pool=5)
        self.assertEqual((t.connect, t.read, t.write, t.pool), (1.0, 2.0, 4.0, 5.0))

    def test_singletons_are_reused(self):
        self.assertIs(http_clients.shared_httpx_client(), http_clients.shared_httpx_client())
        self.assertIs(http_clients.shared_requests_session(), http_clients.shared_requests_session())

    def test_httpx_pool_keeps_idle_connections_for_two_minutes(self):
        factory = MagicMock()
        with patch.object(http_clients, "_httpx_client", None), patch("httpx.Client", factory):
            first = http_clients.shared_httpx_client()
            second = http_clients.shared_httpx_client()
        self.assertIs(first, second)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["limits"].keepalive_expiry, 120)

    def test_concurrent_first_use_builds_one_client(self):
        created = []

        def slow_factory(**kwargs):
            time.sleep(0.02)
            created.append(object())
            return created[-1]

        with patch.object(http_clients, "_httpx_client", None), patch("httpx.Client", slow_factory):
            threads = [threading.Thread(target=http_clients.shared_httpx_client) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(2)
        self.assertEqual(len(created), 1)

    def test_requests_adapter_has_no_retries(self):
        adapter = http_clients.shared_requests_session().get_adapter("https://example.invalid/")
        self.assertEqual(adapter.max_retries.total, 0)


class TestSpeechTransport(unittest.TestCase):
    def test_post_bytes_reuses_shared_client_with_stt_budget(self):
        client = MagicMock()
        client.post.return_value = SimpleNamespace(status_code=200, content=b'{"text": "hi"}', text="")
        with patch.object(transcriber, "shared_httpx_client", return_value=client), patch.object(
            Config, "STT_DEADLINE_SECONDS", 12.0
        ):
            self.assertEqual(transcriber._post_bytes("https://x.invalid/a", b"d", {}), {"text": "hi"})
            transcriber._post_bytes("https://x.invalid/a", b"d", {}, timeout=7.0)
        self.assertEqual(client.post.call_count, 2)
        _assert_full_timeout(self, client.post.call_args_list[0].kwargs["timeout"], read=12.0)
        _assert_full_timeout(self, client.post.call_args_list[1].kwargs["timeout"], read=7.0)

    def test_post_bytes_http_error_is_runtime_error(self):
        client = MagicMock()
        client.post.return_value = SimpleNamespace(status_code=401, content=b"", text="bad key")
        with patch.object(transcriber, "shared_httpx_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                transcriber._post_bytes("https://x.invalid/a", b"d", {})

    def test_cloud_transcriber_passes_stt_deadline(self):
        backend = transcriber.CloudTranscriber("groq")
        backend._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
        with patch.object(transcriber, "_post_bytes", return_value={"text": "ok"}) as post, patch.object(
            Config, "STT_DEADLINE_SECONDS", 9.0
        ):
            self.assertEqual(backend.transcribe(np.ones(1600, dtype=np.float32)), "ok")
        self.assertEqual(post.call_args.kwargs["timeout"], 9.0)

    def test_gemini_stt_timeout_never_exceeds_deadline(self):
        client = MagicMock()
        client.interactions.create.return_value = SimpleNamespace(output_text="words")
        with patch.object(transcriber, "_ensure_google_genai"), patch.object(
            transcriber, "google_genai", MagicMock()
        ), patch.object(Config, "GEMINI_API_KEY", "synthetic-key"), patch.object(
            transcriber, "get_genai_client", return_value=client
        ):
            backend = transcriber.GeminiTranscriber()
        with patch.object(Config, "STT_DEADLINE_SECONDS", 6.0):
            backend.transcribe(np.ones(1600, dtype=np.float32))
        self.assertEqual(client.interactions.create.call_args.kwargs["timeout"], 6.0)

    def test_shared_genai_client_keeps_idle_connections(self):
        factory = MagicMock()
        with patch.object(transcriber, "_ensure_google_genai"), patch.object(
            transcriber, "google_genai", SimpleNamespace(Client=factory)
        ):
            first = transcriber.get_genai_client("synthetic-key-2")
            self.assertIs(transcriber.get_genai_client("synthetic-key-2"), first)
        factory.assert_called_once()
        options = factory.call_args.kwargs["http_options"]
        self.assertEqual(options["retry_options"], {"attempts": 0})
        self.assertEqual(options["client_args"]["limits"].keepalive_expiry, 120)

    def test_refiner_and_stt_share_one_gemini_client(self):
        factory = MagicMock()
        with patch.object(transcriber, "_ensure_google_genai"), patch.object(
            transcriber, "google_genai", SimpleNamespace(Client=factory)
        ), patch.object(refiner, "google_genai", SimpleNamespace(Client=factory)), patch.object(
            Config, "GEMINI_API_KEY", "synthetic-key-3"
        ):
            stt = transcriber.GeminiTranscriber()
            ai = refiner._GeminiClient("synthetic-key-3", "m")
        factory.assert_called_once()
        self.assertIs(ai.client, stt._client)

    def test_installed_sdk_sends_one_ai_request_bounded_by_timeout(self):
        from google import genai

        seen = []

        def transport(request):
            seen.append(request.extensions["timeout"])
            raise httpx.ReadTimeout("synthetic stalled provider")

        clients = []

        def factory(**kwargs):
            options = dict(kwargs["http_options"])
            options["client_args"] = {"transport": httpx.MockTransport(transport)}
            clients.append(genai.Client(api_key=kwargs["api_key"], http_options=options))
            return clients[-1]

        try:
            with patch.object(refiner, "google_genai", SimpleNamespace(Client=factory)):
                ai = refiner._GeminiClient("synthetic-key-4", "m")
            with self.assertRaises(RuntimeError):
                ai.create_interaction("q", 10, system_instruction="s", timeout=7.0)
            self.assertEqual(len(seen), 1, "SDK retried the AI request")
            self.assertEqual(seen[0]["read"], 7.0)
        finally:
            for client in clients:
                client.close()

    def test_groq_and_gemini_upload_flac_openrouter_keeps_wav(self):
        audio = np.ones(1600, dtype=np.float32) * 0.1
        bodies = {}
        for kind in ("groq", "openrouter"):
            backend = transcriber.CloudTranscriber(kind)
            backend._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
            with patch.object(transcriber, "_post_bytes", return_value={"text": "ok"}) as post:
                backend.transcribe(audio)
            bodies[kind] = post.call_args.args[1]
        self.assertIn(b'filename="clip.flac"', bodies["groq"])
        self.assertIn(b"Content-Type: audio/flac", bodies["groq"])
        self.assertIn(b'"format": "wav"', bodies["openrouter"])

    def test_gemini_upload_falls_back_to_wav_when_flac_fails(self):
        backend = transcriber.GeminiTranscriber.__new__(transcriber.GeminiTranscriber)
        backend._client = MagicMock()
        backend._client.interactions.create.return_value = SimpleNamespace(output_text="w")
        with patch.object(transcriber, "float32_to_flac_bytes", side_effect=RuntimeError("x")):
            self.assertEqual(backend.transcribe(np.ones(1600, dtype=np.float32)), "w")
        self.assertEqual(
            backend._client.interactions.create.call_args.kwargs["input"][0]["mime_type"], "audio/wav"
        )

    def test_flac_round_trips_with_soundfile(self):
        import soundfile as sf

        audio = (np.sin(np.linspace(0, 200, 16000)) * 0.5).astype(np.float32)
        data, fmt = transcriber.encode_upload_audio(audio, 16000, prefer_flac=True)
        self.assertEqual(fmt, "flac")
        self.assertTrue(data.startswith(b"fLaC"))
        decoded, rate = sf.read(io.BytesIO(data), dtype="float32")
        self.assertEqual(rate, 16000)
        self.assertEqual(decoded.shape, audio.shape)
        self.assertLess(float(np.max(np.abs(decoded - audio))), 1e-3)
        self.assertLess(len(data), len(transcriber.float32_to_wav_bytes(audio, 16000)))

    def test_flac_failure_falls_back_to_wav(self):
        audio = np.ones(1600, dtype=np.float32) * 0.1
        with patch.object(transcriber, "float32_to_flac_bytes", side_effect=RuntimeError("no libsndfile")):
            data, fmt = transcriber.encode_upload_audio(audio, 16000, prefer_flac=True)
        self.assertEqual(fmt, "wav")
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertEqual(transcriber.encode_upload_audio(audio, 16000)[1], "wav")

    def test_local_fallback_uses_lazy_whisper(self):
        with patch.object(transcriber, "WhisperTranscriber") as whisper:
            whisper.return_value.transcribe.return_value = "local words"
            backend = transcriber.CloudTranscriber("openrouter")
            self.assertEqual(backend.local_fallback(np.ones(10, dtype=np.float32)), "local words")
            backend.local_fallback(np.ones(10, dtype=np.float32))
        whisper.assert_called_once()

    def test_whisper_local_fallback_is_its_own_transcribe(self):
        whisper = transcriber.WhisperTranscriber.__new__(transcriber.WhisperTranscriber)
        with patch.object(whisper, "transcribe", return_value="w") as transcribe:
            self.assertEqual(whisper.local_fallback("clip"), "w")
            self.assertIsNone(whisper.prewarm())
        transcribe.assert_called_once_with("clip")

    def test_default_cloud_call_still_falls_back_to_whisper(self):
        audio = np.ones(1600, dtype=np.float32) * 0.1
        with patch.object(transcriber, "WhisperTranscriber") as whisper, patch.object(
            transcriber, "_post_bytes", side_effect=RuntimeError("timed out")
        ):
            whisper.return_value.transcribe.return_value = "local words"
            backend = transcriber.CloudTranscriber("groq")
            backend._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
            self.assertEqual(backend.transcribe(audio), "local words")
        whisper.return_value.transcribe.assert_called_once()

    def test_caller_owned_fallback_raises_instead_of_running_whisper(self):
        audio = np.ones(1600, dtype=np.float32) * 0.1
        with patch.object(transcriber, "WhisperTranscriber") as whisper, patch.object(
            transcriber, "_post_bytes", side_effect=RuntimeError("timed out")
        ):
            cloud = transcriber.CloudTranscriber("groq")
            cloud._endpoint = lambda: ("https://x.invalid/a", "synthetic-key", "m")
            with self.assertRaisesRegex(transcriber.LocalFallbackDisabled, "timed out"):
                cloud.transcribe(audio, allow_local_fallback=False)
            gemini = transcriber.GeminiTranscriber.__new__(transcriber.GeminiTranscriber)
            gemini._client = None
            with self.assertRaises(transcriber.LocalFallbackDisabled):
                gemini.transcribe(audio, allow_local_fallback=False)
        whisper.assert_not_called()

    def test_abandoned_gemini_reply_does_not_advance_server_memory(self):
        client = refiner._GeminiClient.__new__(refiner._GeminiClient)
        client.model = "m"
        client._last_interaction_id = "earlier"
        client.client = MagicMock()
        client.client.interactions.create.return_value = SimpleNamespace(output_text="a", id="new")
        client.create_interaction("q", 10, keep_history=True, system_instruction="s",
                                  commit_guard=lambda apply: None)  # guard refuses
        self.assertEqual(client._last_interaction_id, "earlier")
        client.create_interaction("q", 10, keep_history=True, system_instruction="s")
        self.assertEqual(client._last_interaction_id, "new")

    def test_gemini_id_commit_is_atomic_with_reset_and_abandon(self):
        # Race P2-b: pause the worker at the exact commit point (inside the
        # locked step), reset / abandon from another thread, then release. The
        # other call must wait, then land wholly after: reset clears both
        # memories; abandon leaves one complete, consistent exchange.
        for action, expected_id, expected_turns in (("reset_context", None, 0),
                                                    ("abandon_inflight", "new", 2)):
            with self.subTest(action=action):
                ai = refiner_fixture("gemini")
                client = refiner._GeminiClient.__new__(refiner._GeminiClient)
                client.model = "m"
                client._last_interaction_id = "earlier"
                client.client = MagicMock()
                client.client.interactions.create.return_value = SimpleNamespace(output_text="answer", id="new")
                ai.client = client
                at_commit, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                real_record = ai._record_reply

                def paused_record(reply, keep_history, turn=None, generation=None, commits=()):
                    def paused_apply(apply):
                        def run():
                            at_commit.set()
                            release.wait(5)
                            apply()
                        return run
                    real_record(reply, keep_history, turn, generation,
                                tuple(paused_apply(a) for a in commits))

                with patch.object(ai, "_record_reply", side_effect=paused_record), \
                        contextlib.redirect_stdout(io.StringIO()):
                    worker = threading.Thread(target=ai.refine, args=("question",),
                                              kwargs={"keep_history": True})
                    worker.start()
                    self.assertTrue(at_commit.wait(1))
                    other = threading.Thread(target=getattr(ai, action))
                    other.start()
                    other.join(0.1)
                    self.assertTrue(other.is_alive(), f"{action} must wait for the atomic commit")
                    release.set()
                    worker.join(2)
                    other.join(2)
                self.assertEqual(client._last_interaction_id, expected_id)
                self.assertEqual(len(ai.conversation_history), expected_turns)

    def test_gemini_reply_abandoned_before_commit_keeps_old_conversation(self):
        ai = refiner_fixture("gemini")
        client = refiner._GeminiClient.__new__(refiner._GeminiClient)
        client.model = "m"
        client._last_interaction_id = "earlier"
        client.client = MagicMock()

        def reply_after_abandon(**kwargs):
            ai.abandon_inflight()  # lands while the request is in flight
            return SimpleNamespace(output_text="late", id="new")

        client.client.interactions.create.side_effect = reply_after_abandon
        ai.client = client
        with contextlib.redirect_stdout(io.StringIO()):
            ai.refine("question", keep_history=True)
        self.assertEqual(client._last_interaction_id, "earlier")
        self.assertEqual(ai.conversation_history, [])

    def test_pinned_generation_survives_abandon_before_refine_starts(self):
        ai = refiner_fixture()
        token = ai.history_generation()
        ai.abandon_inflight()  # lands before the worker reaches refine()
        with ai.pinned_generation(token):
            self.assertEqual(ai.refine("question", keep_history=True), "Hello world.")
        self.assertEqual(ai.conversation_history, [])
        self.assertEqual(ai.refine("question", keep_history=True), "Hello world.")
        self.assertEqual(len(ai.conversation_history), 2)  # unpinned default unchanged

    def test_whisper_decodes_never_run_concurrently(self):
        active, peak, lock = [0], [0], threading.Lock()

        def decode(audio, **kwargs):
            def segments():
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
                yield SimpleNamespace(text="words")
            return segments(), None

        engines = []
        for _ in range(2):
            engine = transcriber.WhisperTranscriber.__new__(transcriber.WhisperTranscriber)
            engine.model = MagicMock()
            engine.model.transcribe.side_effect = decode
            engines.append(engine)
        audio = np.ones(1600, dtype=np.float32) * 0.1
        workers = [threading.Thread(target=e.transcribe, args=(audio,)) for e in engines * 2]
        for w in workers:
            w.start()
        for w in workers:
            w.join(2)
        self.assertEqual(peak[0], 1)
        self.assertEqual(engines[0].transcribe(audio, allow_local_fallback=False), "words")


class TestPrewarm(unittest.TestCase):
    def test_cloud_prewarm_is_non_blocking_rate_limited_and_never_raises(self):
        entered, release = threading.Event(), threading.Event()

        def stalled(*args):
            entered.set()
            release.wait(2)
            raise OSError("network down")

        backend = transcriber.CloudTranscriber("groq")
        backend._endpoint = lambda: ("https://api.example.invalid/v1/audio", "synthetic-key", "m")
        try:
            with patch.object(transcriber, "warm_httpx_origin", side_effect=stalled) as warm:
                started = time.monotonic()
                backend.prewarm()
                backend.prewarm()
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(entered.wait(2))
                release.set()
                time.sleep(0.05)
                self.assertEqual(warm.call_count, 1, "second prewarm inside 20 s must not fire")
        finally:
            release.set()

    def test_prewarm_survives_endpoint_and_thread_errors(self):
        backend = transcriber.CloudTranscriber("groq")
        backend._endpoint = MagicMock(side_effect=RuntimeError("bad config"))
        backend.prewarm()
        backend._endpoint = lambda: ("https://api.example.invalid/v1", "k", "m")
        with patch("http_clients.threading.Thread", side_effect=RuntimeError("no threads")):
            backend.prewarm()

    def test_gemini_stt_prewarm_uses_shared_client_and_swallows_errors(self):
        backend = transcriber.GeminiTranscriber.__new__(transcriber.GeminiTranscriber)
        backend._client = MagicMock()
        backend._prewarmer = http_clients.Prewarmer()
        done = threading.Event()
        backend._client.models.get.side_effect = lambda **kw: (done.set(), (_ for _ in ()).throw(OSError("x")))
        backend.prewarm()
        self.assertTrue(done.wait(2))
        self.assertIn("http_options", backend._client.models.get.call_args.kwargs["config"])

    def test_refiner_prewarm_targets_provider_host_only(self):
        r = refiner_fixture("openrouter")
        r.client.base_url = "https://openrouter.ai/api/v1/"
        done = threading.Event()
        with patch.object(refiner, "warm_httpx_origin", side_effect=lambda url: (done.set(), 1 / 0)) as warm:
            started = time.monotonic()
            r.prewarm()
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(done.wait(2))
        warm.assert_called_once_with("https://openrouter.ai/api/v1/")
        r.client.chat.completions.create.assert_not_called()

    def test_refiner_prewarm_is_noop_for_local_and_none(self):
        for provider in ("ollama", "none"):
            r = refiner_fixture(provider)
            with patch.object(r._prewarmer, "fire") as fire:
                r.prewarm()
            fire.assert_not_called()

    def test_meta_prewarm_uses_shared_session(self):
        client = _MetaClient("synthetic-key", "https://api.meta.example/v1", "m")
        self.assertIs(client._session, http_clients.shared_requests_session())
        self.assertNotIn("Authorization", client._session.headers, "key must not live on the shared session")
        with patch.object(refiner, "warm_requests_origin") as warm:
            client.prewarm()
        warm.assert_called_once_with("https://api.meta.example/v1")


class TestOpenAIClients(unittest.TestCase):
    def test_every_openai_client_uses_shared_http_client(self):
        shared = http_clients.shared_httpx_client()
        cases = [
            ("ollama", {}),
            ("groq", {"GROQ_API_KEY": "synthetic-key"}),
            ("openrouter", {"OPENROUTER_API_KEY": "synthetic-key"}),
        ]
        for provider, keys in cases:
            with self.subTest(provider=provider):
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(Config, "LLM_PROVIDER", provider))
                    for name, value in keys.items():
                        stack.enter_context(patch.object(Config, name, value))
                    stack.enter_context(patch.object(refiner, "prefetch_openrouter_catalog"))
                    factory = stack.enter_context(patch("refiner.OpenAI"))
                    factory.return_value.chat.completions.create.return_value = _reply()
                    r = TextRefiner()
                    self.assertEqual(r.refine("a question"), "An answer.")
                self.assertIs(factory.call_args.kwargs["http_client"], shared)
                self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                _assert_full_timeout(
                    self, factory.return_value.chat.completions.create.call_args.kwargs["timeout"]
                )

    def test_test_provider_pings_use_shared_client_and_full_timeouts(self):
        shared = http_clients.shared_httpx_client()
        for provider in ("ollama", "groq", "openrouter"):
            with self.subTest(provider=provider):
                with patch("refiner.OpenAI") as factory, patch.object(
                    refiner, "ensure_openrouter_catalog"
                ), patch.object(refiner, "peek_openrouter_reasoning", return_value=None):
                    factory.return_value.chat.completions.create.return_value = _reply("OK")
                    self.assertEqual(refiner.test_provider(provider, "synthetic-key", "m"), "ok")
                self.assertIs(factory.call_args.kwargs["http_client"], shared)
                _assert_full_timeout(
                    self, factory.return_value.chat.completions.create.call_args.kwargs["timeout"]
                )


class TestAIBudget(unittest.TestCase):
    def openrouter(self):
        r = refiner_fixture("openrouter")
        r.model = "vendor/model"
        return r

    def test_first_attempt_gets_whole_budget(self):
        r = self.openrouter()
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 17.0):
            self.assertEqual(r.refine("question"), "Hello world.")
        _assert_full_timeout(self, r.client.chat.completions.create.call_args.kwargs["timeout"], read=17.0)

    def test_empty_content_retry_gets_remaining_budget(self):
        r = self.openrouter()
        r.client.chat.completions.create.side_effect = [_reply(None, "length"), _reply("Second.")]
        with patch.object(refiner, "_remaining", return_value=11.0):
            self.assertEqual(r.refine("question"), "Second.")
        calls = r.client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        _assert_full_timeout(self, calls[1].kwargs["timeout"], read=11.0)

    def test_empty_content_retry_skipped_when_budget_spent(self):
        r = self.openrouter()
        r.client.chat.completions.create.return_value = _reply(None, "length")
        with patch.object(refiner, "_remaining", return_value=2.0):
            self.assertEqual(r.refine("question"), "question")
        r.client.chat.completions.create.assert_called_once()
        self.assertEqual(r.last_notice, "ai_fallback")

    def test_mandatory_reasoning_retry_respects_deadline(self):
        refiner.reset_openrouter_effort_cache()
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("reasoning is mandatory for this model")
        try:
            with self.assertRaises(RuntimeError):
                refiner._openrouter_create(
                    client, {"model": "vendor/strict"}, timeout=http_clients.full_timeout(5, 5),
                    deadline=time.monotonic() + 1.0,
                )
            client.chat.completions.create.assert_called_once()

            client.reset_mock()
            refiner.reset_openrouter_effort_cache()
            client.chat.completions.create.side_effect = [RuntimeError("reasoning is mandatory"), _reply()]
            refiner._openrouter_create(
                client, {"model": "vendor/strict"}, timeout=http_clients.full_timeout(5, 30),
                deadline=time.monotonic() + 20.0,
            )
            retry_timeout = client.chat.completions.create.call_args_list[1].kwargs["timeout"]
            _assert_full_timeout(self, retry_timeout)
            self.assertLessEqual(retry_timeout.read, 20.0)
        finally:
            refiner.reset_openrouter_effort_cache()

    def test_meta_and_gemini_receive_the_budget(self):
        r = refiner_fixture("meta")
        r.client = MagicMock(spec=_MetaClient)
        r.client.create_responses.return_value = "Meta answer."
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 25.0):
            self.assertEqual(r.refine("question"), "Meta answer.")
        self.assertEqual(r.client.create_responses.call_args.kwargs["timeout"], (5.0, 25.0))

        g = refiner_fixture("gemini")
        g.client = refiner._GeminiClient.__new__(refiner._GeminiClient)
        g.client.model, g.client._last_interaction_id = "m", None
        g.client.client = MagicMock()
        g.client.client.interactions.create.return_value = SimpleNamespace(output_text="G.", id="i")
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 25.0):
            self.assertEqual(g.refine("question"), "G.")
        self.assertEqual(g.client.client.interactions.create.call_args.kwargs["timeout"], 25.0)


class TestPolishSizing(unittest.TestCase):
    def test_polish_wait_scales_with_length_and_is_capped(self):
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 30.0):
            self.assertEqual(refiner.polish_wait_seconds("short"), 2.0)
            self.assertAlmostEqual(refiner.polish_wait_seconds("x" * 1000), 5.0)
        with patch.object(Config, "LLM_DEADLINE_SECONDS", 4.0):
            self.assertEqual(refiner.polish_wait_seconds("x" * 5000), 4.0)

    def test_polish_skips_text_over_max_chars(self):
        r = refiner_fixture()
        r.last_notice = "stale"
        with patch.object(Config, "POLISH_MAX_CHARS", 10):
            self.assertEqual(r.polish("x" * 11), "x" * 11)
        self.assertEqual(r.last_notice, "")
        r.client.chat.completions.create.assert_not_called()
        self.assertFalse(r._polish_lock.locked())

    def test_polish_limit_zero_means_no_limit_and_request_uses_scaled_wait(self):
        r = refiner_fixture()
        text = "word " * 200
        with patch.object(Config, "POLISH_MAX_CHARS", 0), patch.object(Config, "LLM_DEADLINE_SECONDS", 30.0):
            self.assertEqual(r.polish(text), "Hello world.")
        _assert_full_timeout(self, r.client.chat.completions.create.call_args.kwargs["timeout"], read=5.0)

    def test_polish_waits_scaled_time_not_two_seconds(self):
        r = refiner_fixture()
        text = "y" * 750  # 1 + 750/250 = 4 s
        waits = []
        real_event = threading.Event

        class RecordingEvent(real_event):
            def wait(self, timeout=None):
                waits.append(timeout)
                return super().wait(timeout)

        with patch.object(Config, "POLISH_MAX_CHARS", 1200), patch.object(
            Config, "LLM_DEADLINE_SECONDS", 30.0
        ), patch("refiner.threading.Event", RecordingEvent):
            self.assertEqual(r.polish(text), "Hello world.")
        # Thread.start() also waits on an Event (timeout None); ignore it.
        self.assertEqual([w for w in waits if w is not None], [4.0])


class TestTranscriptLogging(unittest.TestCase):
    def run_refine(self, log_transcripts):
        r = refiner_fixture()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Config, "LOG_TRANSCRIPTS", log_transcripts), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            r.refine("SECRET-QUESTION", context="SECRET-SELECTION")
            r.polish("SECRET-DICTATION")
        return out.getvalue() + err.getvalue()

    def test_private_text_stays_out_of_logs_by_default(self):
        printed = self.run_refine(False)
        self.assertIn("Context: <16 chars>", printed)
        self.assertNotIn("SECRET", printed)

    def test_opt_in_logs_selection(self):
        self.assertIn("SECRET-SELECTION", self.run_refine(True))

    def test_meta_empty_reply_dump_respects_flag(self):
        client = _MetaClient.__new__(_MetaClient)
        client.model = "m"
        client.base_url = "https://api.meta.example/v1"
        data = {"status": "completed", "output": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "SECRET-ECHO"}]}]}
        for flag in (False, True):
            with self.subTest(flag=flag):
                out = io.StringIO()
                with patch.object(client, "_post_payload", return_value=data), patch.object(
                    Config, "LOG_TRANSCRIPTS", flag
                ), contextlib.redirect_stdout(out):
                    self.assertIsNone(client.create_responses([], timeout=(1, 2)))
                self.assertEqual("SECRET-ECHO" in out.getvalue(), flag)


if __name__ == "__main__":
    unittest.main()
