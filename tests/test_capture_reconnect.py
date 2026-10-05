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

    def _deliver_on_start(self, recorder, stream):
        stream.start.side_effect = lambda: recorder._callback(
            np.ones((1024, 1), np.float32), 1024, None, None
        )

    def test_failed_abort_or_close_resets_stream_and_next_start_reconnects(self):
        for failing in ("abort", "close"):
            with self.subTest(failing=failing):
                old, new = MagicMock(), MagicMock()
                getattr(old, failing).side_effect = RuntimeError(f"{failing} failed")
                with patch("recorder.sd.InputStream", side_effect=[old, new]) as opened:
                    recorder = AudioRecorder()
                    try:
                        recorder._last_callback -= 4
                        with self.assertRaisesRegex(RuntimeError, f"{failing} failed"):
                            recorder.start()
                        # Close is still attempted when abort fails.
                        old.close.assert_called_once_with(ignore_errors=False)
                        self.assertIsNone(recorder._stream)
                        self.assertFalse(recorder.recording)
                        self.assertEqual(opened.call_count, 1)
                        self._deliver_on_start(recorder, new)
                        recorder.start()
                        self.assertTrue(recorder.recording)
                        self.assertIs(recorder._stream, new)
                        self.assertEqual(opened.call_count, 2)
                    finally:
                        recorder.close()

    def test_failed_cached_device_refreshes_portaudio_and_uses_default(self):
        old, new = MagicMock(), MagicMock()
        old.device, new.device = 7, 3
        calls = []
        with patch(
            "recorder.sd.InputStream", side_effect=[old, RuntimeError("bad index"), new]
        ) as opened, patch(
            "recorder.sd._terminate", side_effect=lambda: calls.append("terminate")
        ), patch(
            "recorder.sd._initialize", side_effect=lambda: calls.append("initialize")
        ), patch(
            "recorder.sd.query_devices", return_value={"name": "USB mic", "hostapi": 0}
        ), patch("recorder.sd.query_hostapis", return_value={"name": "WASAPI"}):
            recorder = AudioRecorder()
            try:
                self.assertEqual(recorder._device_index, 7)
                recorder._last_callback -= 4
                self._deliver_on_start(recorder, new)
                recorder.start()
                self.assertTrue(recorder.recording)
                devices = [c.kwargs["device"] for c in opened.call_args_list]
                # Startup default, cached index 7 on reconnect, then the default again.
                self.assertEqual(devices, [None, 7, None])
                self.assertEqual(calls, ["terminate", "initialize"])
                self.assertEqual(recorder._device_index, 3)
                self.assertEqual(recorder._device_info["name"], "USB mic")
            finally:
                recorder.close()

    def test_portaudio_refresh_errors_are_contained(self):
        old, new = MagicMock(), MagicMock()
        with patch(
            "recorder.sd.InputStream", side_effect=[old, RuntimeError("bad index"), new]
        ), patch("recorder.sd._terminate", side_effect=OSError("x")), patch(
            "recorder.sd._initialize", side_effect=OSError("y")
        ):
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4
                self._deliver_on_start(recorder, new)
                recorder.start()
                self.assertTrue(recorder.recording)
            finally:
                recorder.close()

    def test_default_device_failure_after_refresh_raises_without_recording(self):
        old = MagicMock()
        with patch(
            "recorder.sd.InputStream",
            side_effect=[old, RuntimeError("bad index"), RuntimeError("no default")],
        ) as opened, patch("recorder.sd._terminate"), patch("recorder.sd._initialize"):
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4
                with self.assertRaisesRegex(RuntimeError, "no default"):
                    recorder.start()
                self.assertFalse(recorder.recording)
                self.assertIsNone(recorder._stream)
                self.assertEqual(opened.call_count, 3)
            finally:
                recorder.close()

    def test_unclosable_half_open_endpoint_never_falls_back_to_default(self):
        old, broken = MagicMock(), MagicMock()
        broken.start.side_effect = RuntimeError("start failed")
        broken.close.side_effect = RuntimeError("driver busy")
        with patch("recorder.sd.InputStream", side_effect=[old, broken]) as opened, patch(
            "recorder.sd._terminate"
        ) as terminate:
            recorder = AudioRecorder()
            try:
                recorder._last_callback -= 4
                with self.assertRaisesRegex(RuntimeError, "driver busy"):
                    recorder.start()
                self.assertEqual(opened.call_count, 2)
                terminate.assert_not_called()
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
