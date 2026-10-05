"""On-demand reconnect without native devices or global hooks."""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from recorder import AudioRecorder


class TestCaptureReconnect(unittest.TestCase):
    def test_stale_stream_closes_before_same_device_reopens(self):
        old, new = MagicMock(), MagicMock()
        old.device = new.device = 7
        with patch("recorder.sd.InputStream", side_effect=[old, new]) as opened:
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4
                def deliver():
                    old.close.assert_called_once_with(ignore_errors=False)
                    recorder._callback(np.ones((1024, 1), np.float32), 1024, None, None)
                new.start.side_effect = deliver
                recorder.start()
                self.assertTrue(recorder.recording)
                self.assertEqual(opened.call_args.kwargs["device"], 7)
                self.assertTrue(recorder.stop())
                self.assertEqual(len(recorder.last_audio_array), 1024)
            finally:
                recorder.close()

    def test_failed_close_never_stacks_another_endpoint(self):
        with patch("recorder.sd.InputStream") as opened:
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4
                opened.return_value.close.side_effect = RuntimeError("close failed")
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    recorder.start()
                self.assertFalse(recorder.recording)
                self.assertEqual(opened.call_count, 1)
            finally:
                recorder.close()

    def test_shutdown_during_reconnect_never_restarts_recording(self):
        old, new = MagicMock(), MagicMock()
        with patch("recorder.sd.InputStream", side_effect=[old, new]) as opened:
            recorder = AudioRecorder()
            recorder._last_callback -= 4
            old.close.side_effect = lambda **kw: recorder._closed.set()
            with patch.object(recorder._callback_ready, "wait", return_value=False):
                with self.assertRaises(RuntimeError):
                    recorder.start()
            self.assertFalse(recorder.recording)
            self.assertEqual(opened.call_count, 1)
            recorder.close()


if __name__ == "__main__":
    unittest.main()
