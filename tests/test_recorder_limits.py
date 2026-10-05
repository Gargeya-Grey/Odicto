"""Capture length limit (max_seconds) without an audio device."""
import threading
import unittest
from unittest.mock import patch

import numpy as np

from recorder import AudioRecorder

RATE = 16000
BLOCK = 1024


class TestRecorderLimits(unittest.TestCase):
    def make(self, max_seconds):
        with patch("recorder.sd.InputStream"):
            recorder = AudioRecorder(sample_rate=RATE, channels=1, max_seconds=max_seconds)
        self.addCleanup(recorder.close)
        return recorder

    def feed(self, recorder, blocks, value=0.5):
        for _ in range(blocks):
            recorder._callback(np.full((BLOCK, 1), value, np.float32), BLOCK, None, None)

    def test_limit_stops_appending_at_max_seconds(self):
        recorder = self.make(max_seconds=0.5)  # 8000 samples
        recorder.start()
        with patch("recorder.threading.Thread"):
            self.feed(recorder, 20)
        self.assertTrue(recorder.recording, "the limit never stops capture by itself")
        self.assertTrue(recorder.stop())
        self.assertEqual(len(recorder.last_audio_array), 8000)
        self.assertTrue(recorder.last_capture_limited)
        self.assertFalse(recorder.last_capture_gap)
        # The ring keeps running after the session limit.
        self.assertGreater(sum(len(c) for c in recorder._ring), 8000)

    def test_limit_callback_fires_once_off_callback_thread_without_locks(self):
        recorder = self.make(max_seconds=0.5)
        fired = []
        done = threading.Event()

        def on_limit():
            lock_free = recorder._lock.acquire(blocking=False)
            if lock_free:
                recorder._lock.release()
            fired.append((threading.get_ident(), threading.current_thread().daemon, lock_free))
            done.set()

        recorder.set_limit_callback(on_limit)
        recorder.start()
        self.feed(recorder, 30)
        self.assertTrue(done.wait(2.0))
        recorder.stop()
        self.assertEqual(len(fired), 1)
        ident, daemon, lock_free = fired[0]
        self.assertNotEqual(ident, threading.get_ident())
        self.assertTrue(daemon)
        self.assertTrue(lock_free)

    def test_limit_callback_fires_once_per_session(self):
        recorder = self.make(max_seconds=0.25)
        calls = []
        recorder.set_limit_callback(lambda: calls.append(1))
        with patch("recorder.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            for _ in range(2):
                recorder.start()
                self.feed(recorder, 10)
                recorder.stop()
        self.assertEqual(len(calls), 2)

    def test_limit_listeners_stop_with_the_session(self):
        recorder = self.make(max_seconds=0.1)  # 1600 samples
        seen = []
        recorder.add_chunk_listener(lambda c: seen.append(len(c)))
        recorder.start()
        with patch("recorder.threading.Thread"):
            self.feed(recorder, 5)
        recorder.stop()
        self.assertEqual(sum(seen), 1600)

    def test_preroll_longer_than_limit_is_trimmed_and_flagged(self):
        recorder = self.make(max_seconds=0.1)
        recorder.set_limit_callback(lambda: None)
        with patch("recorder.threading.Thread") as thread:
            self.feed(recorder, 6)  # idle ring: 6144 samples
            recorder.start()
            self.feed(recorder, 1)
        self.assertTrue(recorder.stop())
        self.assertEqual(len(recorder.last_audio_array), 1600)
        self.assertTrue(recorder.last_capture_limited)
        thread.assert_called_once()

    def test_zero_means_unlimited(self):
        recorder = self.make(max_seconds=0)
        recorder.set_limit_callback(lambda: self.fail("no limit configured"))
        recorder.start()
        with patch("recorder.threading.Thread") as thread:
            self.feed(recorder, 200)  # ~12.8 s
        self.assertTrue(recorder.stop())
        self.assertEqual(len(recorder.last_audio_array), 200 * BLOCK)
        self.assertFalse(recorder.last_capture_limited)
        thread.assert_not_called()

    def test_default_constructor_is_unlimited(self):
        with patch("recorder.sd.InputStream"):
            recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        self.assertEqual(recorder.max_seconds, 0)

    def test_flags_reset_on_next_start(self):
        recorder = self.make(max_seconds=0.1)
        recorder.start()
        with patch("recorder.threading.Thread"):
            self.feed(recorder, 3)
        recorder._session_gap = True
        recorder.stop()
        self.assertTrue(recorder.last_capture_limited)
        self.assertTrue(recorder.last_capture_gap)
        recorder.start()
        self.assertFalse(recorder.last_capture_limited)
        self.assertFalse(recorder.last_capture_gap)
        recorder.stop()
        self.assertFalse(recorder.last_capture_limited)
        self.assertFalse(recorder.last_capture_gap)


if __name__ == "__main__":
    unittest.main()
