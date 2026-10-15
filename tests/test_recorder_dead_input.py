"""A stream that keeps calling back with near-zero samples is reconnected at the next hotkey."""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from recorder import AudioRecorder


def _feed(recorder, level, seconds=1.5):
    """Push `seconds` of constant-peak audio through the real callback."""
    for _ in range(int(16000 * seconds / 1024) + 1):
        chunk = np.full((1024, 1), level, np.float32)
        recorder._callback(chunk, 1024, None, None)


class TestDeadInputReconnect(unittest.TestCase):
    def _deliver_on_start(self, recorder, stream):
        stream.start.side_effect = lambda: recorder._callback(
            np.full((1024, 1), 0.05, np.float32), 1024, None, None
        )

    def test_near_silent_ring_reconnects_before_capture(self):
        old, new = MagicMock(), MagicMock()
        with patch("recorder.sd.InputStream", side_effect=[old, new]) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 3e-5)
                self._deliver_on_start(recorder, new)
                recorder.start()
                old.close.assert_called_once_with(ignore_errors=False)
                self.assertEqual(opened.call_count, 2)
                self.assertIs(recorder._stream, new)
                self.assertTrue(recorder.recording)
            finally:
                recorder.close()

    def test_quiet_but_live_room_noise_keeps_the_stream(self):
        with patch("recorder.sd.InputStream", return_value=MagicMock()) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 3e-3)
                recorder.start()
                self.assertEqual(opened.call_count, 1)
                self.assertTrue(recorder.recording)
            finally:
                recorder.close()

    def test_one_loud_chunk_in_a_quiet_ring_keeps_the_stream(self):
        with patch("recorder.sd.InputStream", return_value=MagicMock()) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 3e-5)
                recorder._callback(np.full((1024, 1), 0.2, np.float32), 1024, None, None)
                recorder.start()
                self.assertEqual(opened.call_count, 1)
            finally:
                recorder.close()

    def test_short_ring_is_not_judged(self):
        with patch("recorder.sd.InputStream", return_value=MagicMock()) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 0.0, seconds=0.5)
                recorder.start()
                self.assertEqual(opened.call_count, 1)
            finally:
                recorder.close()

    def test_threshold_margin_is_pinned(self):
        # The real fault peaked near 4.6e-4; 1.2e-3 is a live, quiet room.
        for level, reopens in ((4.6e-4, 2), (1.2e-3, 1)):
            with self.subTest(level=level):
                old, new = MagicMock(), MagicMock()
                with patch("recorder.sd.InputStream", side_effect=[old, new]) as opened:
                    recorder = AudioRecorder()
                    try:
                        _feed(recorder, level)
                        self._deliver_on_start(recorder, new)
                        recorder.start()
                        self.assertEqual(opened.call_count, reopens)
                    finally:
                        recorder.close()

    def test_muted_mic_reconnects_once_not_on_every_press(self):
        streams = [MagicMock(), MagicMock(), MagicMock()]
        with patch("recorder.sd.InputStream", side_effect=streams) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 0.0)
                streams[1].start.side_effect = lambda: recorder._callback(
                    np.zeros((1024, 1), np.float32), 1024, None, None)
                recorder.start()
                self.assertEqual(opened.call_count, 2)
                recorder.stop()
                for _ in range(3):  # still muted: the ring refills with zeros
                    _feed(recorder, 0.0)
                    recorder.start()
                    recorder.stop()
                self.assertEqual(opened.call_count, 2, "a muted mic must not reopen per press")
            finally:
                recorder.close()

    def test_signal_returning_re_arms_the_check(self):
        streams = [MagicMock(), MagicMock(), MagicMock()]
        with patch("recorder.sd.InputStream", side_effect=streams) as opened:
            recorder = AudioRecorder()
            try:
                _feed(recorder, 0.0)
                self._deliver_on_start(recorder, streams[1])
                recorder.start()
                recorder.stop()
                self.assertEqual(opened.call_count, 2)
                _feed(recorder, 0.05)  # real signal clears the one-shot guard
                _feed(recorder, 3e-5, seconds=5.5)  # then the stream goes dead again
                self._deliver_on_start(recorder, streams[2])
                recorder.start()
                self.assertEqual(opened.call_count, 3)
            finally:
                recorder.close()

    def test_reopen_without_callback_raises_and_does_not_record(self):
        with patch("recorder.sd.InputStream", side_effect=[MagicMock(), MagicMock()]):
            recorder = AudioRecorder()
            try:
                _feed(recorder, 3e-5)
                with patch.object(recorder._callback_ready, "wait", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "unavailable"):
                        recorder.start()
                self.assertFalse(recorder.recording)
            finally:
                recorder.close()

    def test_two_channel_stream_is_judged_across_channels(self):
        with patch("recorder.sd.InputStream", return_value=MagicMock()) as opened:
            recorder = AudioRecorder(channels=2)
            try:
                for _ in range(20):
                    chunk = np.zeros((1024, 2), np.float32)
                    chunk[:, 1] = 0.02  # live on one channel only
                    recorder._callback(chunk, 1024, None, None)
                recorder.start()
                self.assertEqual(opened.call_count, 1)
            finally:
                recorder.close()


if __name__ == "__main__":
    unittest.main()
