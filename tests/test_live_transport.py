"""Live transport ownership tests using asynchronous fake connections only."""
import asyncio
import threading
import time
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

    def test_rejected_key_is_reported_at_once_and_only_once(self):
        class ApiError(Exception):
            def __init__(self, code, message):
                super().__init__(f"{code} None. {message}")
                self.code, self.message = code, message

        class InvalidStatus(Exception):  # the websocket client keeps the status on .response
            def __init__(self, status):
                super().__init__(f"server rejected WebSocket connection: HTTP {status}")
                self.response = SimpleNamespace(status_code=status)

        wrapped = RuntimeError("connect failed")
        wrapped.__cause__ = InvalidStatus(401)
        cases = (
            (ApiError(1008, "API key not valid. Please pass a valid API key."), True),
            (ApiError(401, "unauthorized"), True),
            (InvalidStatus(401), True),
            (wrapped, True),
            (ApiError(403, "API key expired."), True),
            (ApiError(403, "Permission denied on the Live model."), False),  # one model, not the key
            (InvalidStatus(403), False),
            (ApiError(1011, "internal error"), False),
            (ApiError(1008, "policy violation"), False),
            (RuntimeError("socket closed"), False),
        )
        for error, rejected in cases:
            with self.subTest(error=str(error)):
                seen = []
                session = GeminiLiveSession(on_error=seen.append)
                session._record_error(error)
                session._record_error(RuntimeError("later error"))
                self.assertEqual(seen, [rejected])
                self.assertEqual(session.auth_rejected, rejected)
                self.assertTrue(session.needs_batch_fallback)

    def test_later_key_rejection_is_not_hidden_by_an_earlier_transport_error(self):
        seen = []
        session = GeminiLiveSession(on_error=seen.append)
        key_error = RuntimeError("1008 None. API key not valid.")
        key_error.code = 1008
        session._record_error(RuntimeError("socket closed"))
        session._record_error(key_error)
        session._record_error(key_error)  # no third call: nothing new
        self.assertEqual(seen, [False, True])
        self.assertTrue(session.auth_rejected)
        self.assertEqual(session._error, "RuntimeError")  # the first diagnostic stays

    def test_startup_failures_reach_the_app_and_the_log_before_stop(self):
        import contextlib
        import io
        cases = {
            "google-genai not installed": dict(sdk=None, key="synthetic-key", client="ok"),
            "GEMINI_API_KEY empty": dict(sdk=MagicMock(), key="", client="ok"),
            "no Gemini client": dict(sdk=MagicMock(), key="synthetic-key", client=None),
        }
        for message, case in cases.items():
            with self.subTest(message=message):
                seen = []
                output = io.StringIO()
                session = GeminiLiveSession(on_error=seen.append)
                with patch.object(transcriber, "_ensure_google_genai"), patch.object(
                    transcriber, "google_genai", case["sdk"]
                ), patch.object(transcriber, "google_genai_types", case["sdk"]), patch.object(
                    Config, "GEMINI_API_KEY", case["key"]
                ), patch.object(
                    transcriber, "get_genai_client", return_value=case["client"]
                ), contextlib.redirect_stdout(output):
                    session.start()
                    session._thread.join(2)
                    self.assertEqual(seen, [False])  # told while recording, before stop
                    self.assertIn(message, output.getvalue())  # logged before stop
                    session.stop(timeout=0)
                self.assertEqual(output.getvalue().count(message), 1, "logged twice")

    def test_connect_failure_is_logged_before_stop_without_provider_text(self):
        import contextlib
        import io

        class Connection:
            async def __aenter__(self):
                raise RuntimeError("HTTP 500 dictated secret words")

            async def __aexit__(self, *args):
                return False

        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
            connect=lambda **kwargs: Connection())))
        output = io.StringIO()
        seen = []
        session = GeminiLiveSession(client=client, on_error=seen.append)
        with patch.object(transcriber, "_ensure_google_genai"), patch.object(
            transcriber, "google_genai", MagicMock()
        ), patch.object(transcriber, "google_genai_types", MagicMock()), patch.object(
            Config, "GEMINI_API_KEY", "synthetic-key"
        ), patch.object(Config, "LOG_TRANSCRIPTS", False), contextlib.redirect_stdout(output):
            session.start()
            session._thread.join(2)
            self.assertEqual(seen, [False])
            self.assertIn("RuntimeError (HTTP 500)", output.getvalue())
            self.assertNotIn("secret", output.getvalue())
            session.stop(timeout=0)
        self.assertEqual(output.getvalue().count("Gemini Live STT error"), 1)

    def _stop_timing(self, after_end, final_wait_s=30.0, hold=60.0, end_delay=0.0,
                     end_fails=False, gaps=None, before=(), then_raise=None):
        """stop() on a fake connection that sends ``after_end`` once the activity has ended,
        then stays silent, as the real service does.

        Like the SDK, ``receive()`` ends after a turn_complete message and must be called
        again. ``before`` messages come while the user is still speaking. ``gaps`` gives the
        pause before each later message (default 20 ms). ``end_delay`` makes the activity_end
        send slow; ``end_fails`` makes it raise; ``then_raise`` is raised by the stream after
        its messages. The wait is long and the outcome is judged by events: the 15 s join
        below is only a deadlock guard, not a timing claim."""
        ended = threading.Event()
        released = threading.Event()
        queued = list(after_end)
        state = {"before_sent": False, "sent": 0}

        def message(final=None, interim=None, **flags):
            server = SimpleNamespace(interim_input_transcription=SimpleNamespace(text=interim) if interim else None,
                                     turn_complete=False, generation_complete=False,
                                     input_transcription=SimpleNamespace(text=final) if final else None)
            for name, value in flags.items():
                setattr(server, name, value)
            return SimpleNamespace(server_content=server)

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send_realtime_input(self, **kwargs):
                if "activity_end" in kwargs:
                    ended.set()  # the service has it as soon as the call starts
                    if end_delay:
                        await asyncio.sleep(end_delay)
                    if end_fails:
                        raise RuntimeError("send failed")

            async def receive(self):
                if not state["before_sent"]:
                    state["before_sent"] = True
                    for item in before:
                        yield message(**item)
                while not ended.is_set():
                    await asyncio.sleep(.005)
                while queued:
                    item = queued.pop(0)
                    await asyncio.sleep(gaps[state["sent"]] if gaps else .02)
                    state["sent"] += 1
                    yield message(**item)
                    if item.get("turn_complete"):
                        return  # the SDK ends receive() at the end of a model turn
                if then_raise is not None:
                    raise then_raise
                started = asyncio.get_running_loop().time()
                while not released.is_set() and asyncio.get_running_loop().time() - started < hold:
                    await asyncio.sleep(.01)

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
                result = []
                runner = threading.Thread(
                    target=lambda: result.append(session.stop(timeout=60.0, final_wait_s=final_wait_s)))
                runner.start()
                runner.join(15)  # deadlock guard only
                self.assertFalse(runner.is_alive(), "stop() did not come back")
                return result[0], session
            finally:
                released.set()
                session._thread.join(3)

    WHOLE = "The first half and the second half."

    def test_stop_returns_at_the_first_final_after_the_end(self):
        """The measured order: the whole final, generation_complete, then a voice-activity
        message. The final alone ends the 30 s wait."""
        text, session = self._stop_timing(
            [dict(final=self.WHOLE), dict(generation_complete=True), dict()])
        self.assertEqual(text, self.WHOLE)
        self.assertFalse(session.needs_batch_fallback)

    def test_completion_before_the_final_does_not_end_the_wait(self):
        text, session = self._stop_timing([dict(generation_complete=True), dict(final=self.WHOLE)])
        self.assertEqual(text, self.WHOLE)
        self.assertFalse(session.needs_batch_fallback)

    def test_a_turn_that_ends_before_the_final_is_listened_past(self):
        """receive() ends at turn_complete; the SDK sets no order against the transcription."""
        text, session = self._stop_timing([dict(turn_complete=True), dict(final=self.WHOLE)])
        self.assertEqual(text, self.WHOLE)
        self.assertFalse(session.needs_batch_fallback)

    def test_a_final_during_the_speech_does_not_end_the_wait(self):
        text, _ = self._stop_timing([dict(final="And the rest.")],
                                    before=[dict(final="Said while speaking.")])
        self.assertEqual(text, "Said while speaking. And the rest.")

    def test_a_final_that_arrives_while_activity_end_is_sent_counts(self):
        """The service answers the end before the send call returns."""
        text, session = self._stop_timing([dict(final=self.WHOLE)], end_delay=.5, gaps=[.01])
        self.assertEqual(text, self.WHOLE)
        self.assertFalse(session.needs_batch_fallback)

    def test_a_failed_activity_end_requires_the_full_recording(self):
        """Without the end the service may have sent text for only part of the speech."""
        _, session = self._stop_timing([dict(final="Only the start.")], end_fails=True)
        self.assertTrue(session.needs_batch_fallback)

    def test_a_stream_error_is_an_error_even_when_it_is_a_timeout(self):
        _, session = self._stop_timing([dict(generation_complete=True)], final_wait_s=5.0,
                                       then_raise=asyncio.TimeoutError())
        self.assertTrue(session.needs_batch_fallback)
        self.assertEqual(session._error, "TimeoutError")

    class ApiError(Exception):  # the SDK raises websocket close codes as APIError(code, ...)
        def __init__(self, code):
            super().__init__(f"{code} None.")
            self.code = code

    def test_a_close_after_stop_without_a_final_uses_the_full_recording(self):
        """A normal close (1000) is no error; an abnormal one is. Neither brought the final,
        so the draft is not trusted."""
        for code, is_error in ((1000, False), (1011, True)):
            with self.subTest(code=code):
                _, session = self._stop_timing([dict(generation_complete=True)],
                                               final_wait_s=5.0, then_raise=self.ApiError(code))
                self.assertEqual(bool(session._error), is_error)
                self.assertTrue(session.needs_batch_fallback)

    def test_a_normal_close_while_speaking_is_an_error(self):
        """The speech after the close was never sent."""
        session = GeminiLiveSession()
        error = self.ApiError(1000)

        class Closing:
            def receive(self):
                async def close():
                    raise error
                    yield
                return close()

        with self.assertRaises(self.ApiError):
            asyncio.run(session._recv_loop(Closing()))

    def test_the_wait_running_out_returns_what_arrived(self):
        text, _ = self._stop_timing([dict(generation_complete=True)], final_wait_s=.6)
        self.assertEqual(text, "")  # the app falls back to the draft or the batch call

    def test_a_receive_that_yields_nothing_while_speaking_requires_the_full_recording(self):
        """Defensive: the real SDK raises on a close instead (see the close tests)."""
        session = GeminiLiveSession()

        class Closed:
            def receive(self):
                async def nothing():
                    return
                    yield
                return nothing()

        asyncio.run(session._recv_loop(Closed()))
        self.assertTrue(session.needs_batch_fallback)

    def test_failing_error_callback_does_not_break_the_session(self):
        def boom(_rejected):
            raise RuntimeError("callback bug")

        session = GeminiLiveSession(on_error=boom)
        session._record_error(RuntimeError("x"))
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
