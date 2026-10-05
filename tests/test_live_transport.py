"""Live transport ownership tests using asynchronous fake connections only."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

import transcriber
from config import Config
from transcriber import GeminiLiveSession


class TestLiveTransport(unittest.TestCase):
    def test_overflow_requires_full_audio_fallback(self):
        session = GeminiLiveSession()
        for number in range(129):
            session.push_audio(np.full(1024, number, dtype=np.float32))
        session._final_parts.append("plausible but incomplete words")
        self.assertEqual(session._chunks.qsize(), 128)
        self.assertEqual(session._chunks.queue[0][0], 1)
        session.stop(timeout=0)
        self.assertTrue(session.needs_batch_fallback)

    def test_provider_error_requires_full_audio_fallback(self):
        session = GeminiLiveSession()
        session._final_parts.append("partial words")
        session._error = "transport disconnected"
        self.assertTrue(session.needs_batch_fallback)

    def test_successful_session_does_not_require_batch_fallback(self):
        session = GeminiLiveSession()
        session._final_parts.append("complete words")
        self.assertEqual(session.stop(timeout=0), "complete words")
        self.assertFalse(session.needs_batch_fallback)

    def test_timed_out_connect_and_send_are_cancelled_and_joined(self):
        for stall_at in ("connect", "send"):
            with self.subTest(stall_at=stall_at):
                entered, cancelled, released = (threading.Event() for _ in range(3))

                async def stall():
                    entered.set()
                    try:
                        while not released.is_set():
                            await asyncio.sleep(.005)
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise

                class Connection:
                    async def __aenter__(self):
                        if stall_at == "connect":
                            await stall()
                        return self

                    async def __aexit__(self, *args):
                        return False

                    async def send_realtime_input(self, **kwargs):
                        if "audio" in kwargs and stall_at == "send":
                            await stall()

                    async def receive(self):
                        while not released.is_set():
                            await asyncio.sleep(.005)
                        if False:
                            yield None

                client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
                    connect=lambda **kwargs: Connection())))
                session = GeminiLiveSession(client=client)
                with patch.object(transcriber, "_ensure_google_genai"), patch.object(
                    transcriber, "google_genai", MagicMock()
                ), patch.object(transcriber, "google_genai_types", MagicMock()), patch.object(
                    Config, "GEMINI_API_KEY", "synthetic-key"
                ):
                    try:
                        session.push_audio(np.ones(1024, dtype=np.float32))
                        session.start()
                        self.assertTrue(entered.wait(2), "fake transport was not reached")
                        session.stop(timeout=.03, final_wait_s=.01)
                        self.assertTrue(cancelled.is_set(), "timed-out I/O was not cancelled")
                        self.assertFalse(session._thread.is_alive(), "session worker survived stop")
                        self.assertTrue(session.needs_batch_fallback)
                        session.stop(timeout=.03)
                        self.assertFalse(session._thread.is_alive(), "repeat stop revived worker")
                    finally:
                        released.set()
                        session._thread.join(2)


if __name__ == "__main__":
    unittest.main()
