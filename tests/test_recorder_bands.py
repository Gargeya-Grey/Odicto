"""Spectral band levels that drive the HUD voice rows. No microphone: each test sets the newest block."""

import unittest
from unittest.mock import patch

import numpy as np

from recorder import AudioRecorder

RATE = 16000
BLOCK = 1024


def _tone(hz: float, amp: float = 0.1) -> np.ndarray:
    t = np.arange(BLOCK) / RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


class TestGetBands(unittest.TestCase):
    def setUp(self) -> None:
        with patch("recorder.sd.InputStream"):
            self.rec = AudioRecorder(sample_rate=RATE, channels=1)
        self.addCleanup(self.rec.close)

    def _bands(self, block: np.ndarray, n: int = 5) -> list:
        self.rec.recording = True
        self.rec.audio_data = [block]
        return self.rec.get_bands(n)

    def test_an_idle_recorder_reports_silence(self) -> None:
        self.assertEqual(self.rec.get_bands(5), [0.0] * 5)

    def test_digital_silence_is_zero_in_every_band(self) -> None:
        self.assertEqual(self._bands(np.zeros(BLOCK, np.float32)), [0.0] * 5)

    def test_a_tone_lights_its_own_band_and_no_other(self) -> None:
        for hz, band in ((150, 0), (330, 1), (700, 2), (1600, 3), (4000, 4)):
            with self.subTest(hz=hz):
                levels = self._bands(_tone(hz))
                self.assertGreater(levels[band], 0.5)
                self.assertEqual([round(v, 2) for i, v in enumerate(levels) if i != band], [0.0] * 4)

    def test_louder_input_reads_higher(self) -> None:
        quiet = self._bands(_tone(330, amp=0.005))[1]
        loud = self._bands(_tone(330, amp=0.05))[1]
        self.assertGreater(quiet, 0.0)
        self.assertGreater(loud, quiet + 0.3)

    def test_full_scale_noise_stays_inside_zero_to_one(self) -> None:
        block = np.random.default_rng(7).uniform(-1.0, 1.0, BLOCK).astype(np.float32)
        levels = self._bands(block)
        self.assertEqual(len(levels), 5)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in levels), levels)

    def test_the_band_count_follows_the_request(self) -> None:
        self.assertEqual(len(self._bands(_tone(700), n=3)), 3)
        self.assertEqual(len(self.rec.get_bands(0)), 1)


if __name__ == "__main__":
    unittest.main()
