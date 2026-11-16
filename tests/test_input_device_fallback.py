"""No default input device: use the ALSA "pulse" device (WSLg), then "pipewire", "default".

PortAudio is faked; no stream opens and no microphone is touched.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import recorder as recorder_module
from recorder import AudioRecorder

_NO_DEFAULT = "Error querying device -1"


def device(name, inputs=1):
    return {"name": name, "max_input_channels": inputs, "max_output_channels": 2 - min(inputs, 1),
            "hostapi": 0}


def fake_portaudio(devices, default=None):
    """query_devices for a host with these devices; ``default`` is the default input index."""
    def query(device=None, kind=None):
        if device is None and kind is None:
            return list(devices)
        if kind == "input":
            if default is None:
                raise recorder_module.sd.PortAudioError(_NO_DEFAULT)
            return devices[default]
        return devices[device]
    return query


class Fixture(unittest.TestCase):
    def setUp(self):
        self.stream = MagicMock()
        self.stream.device = 4
        self.opened = patch("recorder.sd.InputStream", return_value=self.stream).start()
        patch("recorder.sd.query_hostapis", return_value={"name": "ALSA"}).start()
        patch("recorder.sys.platform", "linux").start()
        self.addCleanup(patch.stopall)

    def host(self, devices, default=None):
        patch("recorder.sd.query_devices", side_effect=fake_portaudio(devices, default)).start()

    def idle(self, cached=None):
        with patch.object(AudioRecorder, "_open_persistent_stream"):
            rec = AudioRecorder()
        rec._device_index = cached
        self.addCleanup(rec.close)
        return rec

    def open(self, rec):
        out = io.StringIO()
        with redirect_stdout(out):
            rec._open_persistent_stream(delays=(0.0,))
        return out.getvalue()

    def chosen(self):
        return self.opened.call_args.kwargs["device"]


class FallbackOrderTests(Fixture):
    def test_pulse_wins_over_pipewire_default_and_the_first_input(self):
        self.host([device("HDA Intel"), device("default"), device("pipewire"), device("pulse")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 3)

    def test_pipewire_is_next(self):
        self.host([device("HDA Intel"), device("default"), device("pipewire")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 2)

    def test_default_is_next(self):
        self.host([device("HDA Intel"), device("default")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 1)

    def test_otherwise_the_first_input_device(self):
        self.host([device("HDMI out", inputs=0), device("USB mic"), device("Webcam mic")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 1)

    def test_names_match_without_regard_to_case_or_spaces(self):
        self.host([device("USB mic"), device(" Pulse ")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 1)

    def test_a_device_without_inputs_is_never_chosen_even_when_named_pulse(self):
        self.host([device("pulse", inputs=0), device("pipewire"), device("USB mic")])
        self.open(self.idle())
        self.assertEqual(self.chosen(), 1)

    def test_the_log_line_names_the_device(self):
        self.host([device("HDA Intel"), device("pulse")])
        text = self.open(self.idle())
        self.assertIn("No default microphone; using 'pulse' (device 1).", text)

    def test_the_opened_device_is_cached_for_the_next_reopen(self):
        self.host([device("a"), device("b"), device("c"), device("d"), device("pulse")])
        rec = self.idle()
        self.open(rec)
        self.assertEqual(rec._device_index, 4)
        self.stream.device = 4
        self.opened.reset_mock()
        self.assertEqual(self.open(rec), "")  # cached: no second choice, no second log line
        self.assertEqual(self.chosen(), 4)


class UnchangedBehaviourTests(Fixture):
    def test_a_default_input_keeps_the_old_open_with_no_device(self):
        self.host([device("HDA Intel"), device("pulse")], default=0)
        text = self.open(self.idle())
        self.assertIsNone(self.chosen())
        self.assertNotIn("No default microphone", text)

    def test_a_configured_device_is_kept_even_without_a_default(self):
        self.host([device("HDA Intel"), device("pulse"), device("USB mic")])
        text = self.open(self.idle(cached=2))
        self.assertEqual(self.chosen(), 2)
        self.assertNotIn("No default microphone", text)

    def test_no_input_device_at_all_keeps_the_old_open_and_its_failure(self):
        self.host([device("HDMI out", inputs=0)])
        self.opened.side_effect = RuntimeError("Error querying device -1")
        with self.assertRaises(recorder_module._MicrophoneOpenFailed):
            self.open(self.idle())
        self.assertIsNone(self.chosen())

    def test_a_failing_device_query_keeps_the_old_open(self):
        with patch("recorder.sd.query_devices", side_effect=OSError("no PortAudio")):
            self.open(self.idle())
        self.assertIsNone(self.chosen())

    def test_the_fallback_query_never_opens_a_stream(self):
        self.host([device("pulse")])
        rec = self.idle()
        self.assertEqual(rec._fallback_input_device(), (0, "pulse"))
        self.opened.assert_not_called()


class ReopenTests(Fixture):
    def test_a_reopen_after_a_lost_device_falls_back_again(self):
        self.host([device("a"), device("pulse")])
        rec = self.idle(cached=7)
        self.opened.side_effect = [RuntimeError("bad index"), self.stream]
        with patch.object(rec, "_refresh_device_list"), redirect_stdout(io.StringIO()):
            rec._reopen_stream()
        self.assertEqual([c.kwargs["device"] for c in self.opened.call_args_list], [7, 1])


if __name__ == "__main__":
    unittest.main()
