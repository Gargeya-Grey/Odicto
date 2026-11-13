"""Host buffer depth of the input stream: deep for Windows MME, "low" elsewhere."""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from recorder import AudioRecorder, _MicrophoneOpenFailed

_APIS = {0: {"name": "MME"}, 1: {"name": "Windows WASAPI"}, 2: {"name": "Windows WDM-KS"}}


def _devices(hostapi):
    """query_devices fake: the default input and device 7 sit on the given host API."""
    return lambda *args, **kwargs: {"name": "USB mic", "hostapi": hostapi}


def _stream(device=None):
    stream = MagicMock()
    stream.device = device
    return stream


def _idle_recorder():
    """A recorder that has opened nothing; tests call its methods directly."""
    with patch.object(AudioRecorder, "_open_persistent_stream"):
        return AudioRecorder()


def _latencies(opened):
    return [call.kwargs["latency"] for call in opened.call_args_list]


class TestInputLatency(unittest.TestCase):
    def _latency(self, hostapi=0, platform="win32", device_index=None, **patches):
        with patch("recorder.sd.InputStream", return_value=_stream()), \
                patch("recorder.sys.platform", platform):
            recorder = AudioRecorder()
        try:
            recorder._device_index = device_index
            queries = dict(
                query_devices=MagicMock(side_effect=_devices(hostapi)),
                query_hostapis=MagicMock(side_effect=lambda i: _APIS[i]),
            )
            queries.update(patches)
            with patch("recorder.sd.query_devices", queries["query_devices"]), \
                    patch("recorder.sd.query_hostapis", queries["query_hostapis"]), \
                    patch("recorder.sd.InputStream") as opened, \
                    patch("recorder.sys.platform", platform):
                value = recorder._input_latency()
            opened.assert_not_called()
            return value, queries
        finally:
            recorder.close()

    def test_mme_on_windows_gets_the_deep_buffer(self):
        value, _ = self._latency(hostapi=0)
        self.assertEqual(value, AudioRecorder.MME_LATENCY_SECONDS)
        self.assertEqual(value, 1.0)

    def test_default_input_is_queried_when_no_device_is_cached(self):
        _, queries = self._latency(hostapi=0, device_index=None)
        queries["query_devices"].assert_called_once_with(kind="input")

    def test_cached_device_index_is_queried_when_known(self):
        value, queries = self._latency(hostapi=0, device_index=7)
        queries["query_devices"].assert_called_once_with(7)
        self.assertEqual(value, 1.0)

    def test_other_windows_host_apis_keep_low(self):
        for hostapi in (1, 2):
            with self.subTest(api=_APIS[hostapi]["name"]):
                self.assertEqual(self._latency(hostapi=hostapi)[0], "low")

    def test_other_systems_keep_low_even_for_a_host_api_named_mme(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                value, queries = self._latency(hostapi=0, platform=platform)
                self.assertEqual(value, "low")
                queries["query_devices"].assert_not_called()

    def test_failing_device_query_gives_low(self):
        value, _ = self._latency(query_devices=MagicMock(side_effect=OSError("no PortAudio")))
        self.assertEqual(value, "low")

    def test_failing_host_api_query_gives_low(self):
        value, _ = self._latency(query_hostapis=MagicMock(side_effect=IndexError("gone")))
        self.assertEqual(value, "low")

    def test_malformed_device_info_gives_low(self):
        value, _ = self._latency(query_devices=MagicMock(return_value={"name": "x"}))
        self.assertEqual(value, "low")


class TestOpenWithDeepBuffer(unittest.TestCase):
    def _recorder(self, *streams, hostapi=0, platform="win32"):
        patches = [
            patch("recorder.sd.InputStream", side_effect=list(streams)),
            patch("recorder.sd.query_devices", side_effect=_devices(hostapi)),
            patch("recorder.sd.query_hostapis", side_effect=lambda i: _APIS[i]),
            patch("recorder.sys.platform", platform),
        ]
        opened = patches[0].start()
        for p in patches[1:]:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return opened

    def test_mme_stream_opens_with_the_deep_buffer(self):
        opened = self._recorder(_stream())
        recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        self.assertEqual(_latencies(opened), [1.0])

    def test_other_host_api_opens_with_low(self):
        opened = self._recorder(_stream(), hostapi=1)
        recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        self.assertEqual(_latencies(opened), ["low"])

    def test_refused_deep_open_falls_back_to_low_in_the_same_attempt(self):
        good = _stream()
        opened = self._recorder(RuntimeError("Invalid latency"), good)
        with patch("recorder.time.sleep") as slept:
            recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        self.assertEqual(_latencies(opened), [1.0, "low"])
        # One attempt: the fallback did not wait for the retry delay.
        slept.assert_not_called()
        self.assertIs(recorder._stream, good)
        good.start.assert_called_once()

    def test_refused_deep_start_closes_the_stream_then_falls_back(self):
        refused, good = _stream(), _stream()
        refused.start.side_effect = RuntimeError("start refused")
        opened = self._recorder(refused, good)
        recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        refused.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual(_latencies(opened), [1.0, "low"])
        self.assertIs(recorder._stream, good)

    def test_failed_close_of_the_deep_stream_never_opens_the_low_one(self):
        broken = _stream()
        broken.start.side_effect = RuntimeError("start refused")
        broken.close.side_effect = RuntimeError("driver busy")
        opened = self._recorder(broken, _stream())
        with self.assertRaisesRegex(RuntimeError, "driver busy"):
            AudioRecorder()
        self.assertEqual(opened.call_count, 1)

    def test_both_opens_failing_counts_as_one_failed_attempt(self):
        opened = self._recorder(RuntimeError("deep"), RuntimeError("low"))
        recorder = _idle_recorder()
        with self.assertRaises(_MicrophoneOpenFailed) as caught:
            recorder._open_persistent_stream(delays=(0.0,))
        self.assertIn("after 1 attempts", str(caught.exception))
        self.assertIn("low", str(caught.exception))
        self.assertEqual(_latencies(opened), [1.0, "low"])
        recorder.close()

    def test_low_failure_is_not_tried_twice(self):
        opened = self._recorder(RuntimeError("busy"), hostapi=1)
        recorder = _idle_recorder()
        with self.assertRaises(_MicrophoneOpenFailed):
            recorder._open_persistent_stream(delays=(0.0,))
        self.assertEqual(_latencies(opened), ["low"])
        recorder.close()

    def test_reconnect_uses_the_same_decision(self):
        first, second = _stream(7), _stream(7)
        opened = self._recorder(first, RuntimeError("Invalid latency"), second)
        recorder = AudioRecorder()
        self.addCleanup(recorder.close)
        recorder._last_callback -= 4
        second.start.side_effect = lambda: recorder._callback(
            np.ones((1024, 1), np.float32), 1024, None, None
        )
        recorder.start()
        self.assertTrue(recorder.recording)
        self.assertEqual(_latencies(opened), [1.0, 1.0, "low"])
        self.assertEqual([c.kwargs["device"] for c in opened.call_args_list], [None, 7, 7])


if __name__ == "__main__":
    unittest.main()
