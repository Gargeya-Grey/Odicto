"""Capture continuity with deterministic callback timing and no audio device."""
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

    def assert_rejected(self):
        with self.assertRaises(RuntimeError):
            self.recorder.stop()
        self.assertFalse(self.recorder.recording)
        self.assertIsNone(self.recorder.last_audio_array)

    def test_resumed_gap_during_capture_rejects_partial_audio(self):
        self.feed(0.064, 0.1)
        self.recorder.start()
        self.feed(0.128, 0.2)
        self.feed(4.128, 0.3)
        self.feed(4.192, 0.4)
        self.assert_rejected()

    def test_idle_discontinuity_discards_old_preroll(self):
        for overflow in (False, True):
            with self.subTest(overflow=overflow):
                # A fresh test recorder uses the same mocked endpoint only.
                self.recorder.close()
                self.now = 0.0
                self.recorder = AudioRecorder()
                self.addCleanup(self.recorder.close)
                self.feed(0.064, 0.1)
                self.feed(0.128 if overflow else 4.128, 0.2, overflow=overflow)
                self.recorder.start()
                self.assertTrue(self.recorder.stop())
                np.testing.assert_array_equal(
                    self.recorder.last_audio_array,
                    np.full(1024, 0.2, dtype=np.float32),
                )

    def test_input_overflow_during_capture_rejects_partial_audio(self):
        self.feed(0.064, 0.1)
        self.recorder.start()
        self.feed(0.128, 0.2, overflow=True)
        self.feed(0.192, 0.3)
        self.assert_rejected()

    def test_continuous_silence_preserves_all_samples(self):
        self.feed(0.064)
        self.recorder.start()
        for index in range(2, 8):
            self.feed(index * 0.064)
        self.assertFalse(self.recorder.stop())
        np.testing.assert_array_equal(
            self.recorder.last_audio_array, np.zeros(7 * 1024, dtype=np.float32)
        )


if __name__ == "__main__":
    unittest.main()
