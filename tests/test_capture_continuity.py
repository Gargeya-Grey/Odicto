"""Capture continuity with deterministic callback timing and no audio device.

Contract: an input overflow or a callback gap never discards a capture. The
audio is kept and flagged in ``last_capture_gap``. Stop raises only when the
stream died and the session holds no audio at all.
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from recorder import AudioRecorder


class TestCaptureContinuity(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = 0.0
        self.stack.enter_context(patch("recorder.sd.InputStream"))
        self.stack.enter_context(patch("recorder.time.monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(patch("recorder.monotonic", side_effect=lambda: self.now))
        self.recorder = AudioRecorder()
        self.addCleanup(self.recorder.close)

    def feed(self, at, value=0.0, overflow=False):
        self.now = at
        chunk = np.full((1024, 1), value, dtype=np.float32)
        status = SimpleNamespace(input_overflow=True) if overflow else None
        self.recorder._callback(chunk, len(chunk), None, status)

    def expected(self, *values):
        return np.concatenate([np.full(1024, v, dtype=np.float32) for v in values])

    def test_resumed_gap_during_capture_keeps_audio_and_flags_it(self):
        self.feed(0.064, 0.1)
        self.recorder.start()
        self.feed(0.128, 0.2)
        self.feed(4.128, 0.3)
        self.feed(4.192, 0.4)
        self.assertTrue(self.recorder.stop())
        self.assertTrue(self.recorder.last_capture_gap)
        self.assertFalse(self.recorder.last_capture_limited)
        # Pre-roll and pre-gap audio survive; the ring clear only drops stale pre-roll.
        np.testing.assert_array_equal(
            self.recorder.last_audio_array, self.expected(0.1, 0.2, 0.3, 0.4)
        )

    def test_idle_gap_discards_old_preroll(self):
        self.feed(0.064, 0.1)
        self.feed(4.128, 0.2)
        self.recorder.start()
        self.assertTrue(self.recorder.stop())
        np.testing.assert_array_equal(self.recorder.last_audio_array, self.expected(0.2))
        # A gap before the session is not a gap in the session.
        self.assertFalse(self.recorder.last_capture_gap)

    def test_idle_overflow_keeps_preroll_unflagged(self):
        self.feed(0.064, 0.1)
        self.feed(0.128, 0.2, overflow=True)
        self.recorder.start()
        self.assertTrue(self.recorder.stop())
        np.testing.assert_array_equal(self.recorder.last_audio_array, self.expected(0.1, 0.2))
        self.assertFalse(self.recorder.last_capture_gap)

    def test_input_overflow_during_capture_keeps_audio_and_flags_it(self):
        self.feed(0.064, 0.1)
        self.recorder.start()
        self.feed(0.128, 0.2, overflow=True)
        self.feed(0.192, 0.3)
        self.assertTrue(self.recorder.stop())
        self.assertTrue(self.recorder.last_capture_gap)
        self.assertFalse(self.recorder.recording)
        np.testing.assert_array_equal(
            self.recorder.last_audio_array, self.expected(0.1, 0.2, 0.3)
        )

    def test_dead_stream_with_audio_keeps_audio_and_flags_it(self):
        self.feed(0.064, 0.1)
        self.recorder.start()
        self.feed(0.128, 0.2)
        self.now = 5.0  # no callback since 0.128 s: the stream died
        self.assertTrue(self.recorder.stop())
        self.assertTrue(self.recorder.last_capture_gap)
        np.testing.assert_array_equal(self.recorder.last_audio_array, self.expected(0.1, 0.2))

    def test_dead_stream_without_audio_raises(self):
        self.now = 1.0
        self.recorder.start()  # empty ring: no pre-roll
        self.now = 5.0
        with self.assertRaisesRegex(RuntimeError, "Microphone interrupted"):
            self.recorder.stop()
        self.assertFalse(self.recorder.recording)
        self.assertIsNone(self.recorder.last_audio_array)
        self.assertTrue(self.recorder.last_capture_gap)

    def test_live_stream_without_audio_returns_false(self):
        self.now = 1.0
        self.recorder.start()
        self.assertFalse(self.recorder.stop())
        self.assertIsNone(self.recorder.last_audio_array)
        self.assertFalse(self.recorder.last_capture_gap)

    def test_continuous_silence_preserves_all_samples(self):
        self.feed(0.064)
        self.recorder.start()
        for index in range(2, 8):
            self.feed(index * 0.064)
        self.assertFalse(self.recorder.stop())
        self.assertFalse(self.recorder.last_capture_gap)
        np.testing.assert_array_equal(
            self.recorder.last_audio_array, np.zeros(7 * 1024, dtype=np.float32)
        )


if __name__ == "__main__":
    unittest.main()
