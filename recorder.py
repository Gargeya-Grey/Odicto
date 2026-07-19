import threading
from typing import List, Optional
import numpy as np
import sounddevice as sd
import soundfile as sf


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        """Initializes the audio recorder.

        Args:
            sample_rate: The sample rate for recording, default 16000 (Whisper optimized).
            channels: The number of audio channels, default 1 (mono).
        """
        self.sample_rate: int = sample_rate
        self.channels: int = channels
        self.recording: bool = False
        self.audio_data: List[np.ndarray] = []
        self.last_audio_array: Optional[np.ndarray] = None
        self._stream: Optional[sd.InputStream] = None
        self._lock: threading.Lock = threading.Lock()
        # Smoothed peak level 0..1 for the live UI waveform (updated from audio callback).
        self._level: float = 0.0

    def _callback(self, indata: np.ndarray, frames: int, time: object, status: object) -> None:
        """Internal callback for sounddevice input stream to capture audio chunks."""
        if status:
            # Minor buffer underflows are non-fatal; keep capturing.
            print(f"SoundDevice status warning: {status}")
        # Live meter (outside lock first for RMS compute, then short lock for store).
        try:
            peak = float(np.max(np.abs(indata))) if indata.size else 0.0
            # Soft-knee so quiet speech still moves the waveform.
            level = min(1.0, peak * 3.2)
        except Exception:
            level = 0.0
        with self._lock:
            if self.recording:
                self.audio_data.append(indata.copy())
                # Exponential smooth toward current peak.
                self._level = (0.55 * self._level) + (0.45 * level)
            else:
                self._level = 0.0

    def get_level(self) -> float:
        """Returns a smoothed 0..1 mic level for the visualizer."""
        with self._lock:
            return self._level

    def start(self) -> None:
        """Starts recording audio from the default input device in a non-blocking stream."""
        with self._lock:
            if self.recording:
                return
            self.audio_data = []
            self.last_audio_array = None
            self._level = 0.0
            self.recording = True

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                callback=self._callback,
                dtype="float32",
                # Smaller blocksize reduces first-chunk latency after press.
                blocksize=1024,
                latency="low",
            )
            self._stream.start()
        except Exception:
            # Roll back recording flag if the device fails to open.
            with self._lock:
                self.recording = False
                self.audio_data = []
            raise

    def stop(self, filepath: Optional[str] = None) -> bool:
        """Stops recording and keeps the captured buffer in memory.

        Optionally saves to a WAV file when a filepath is provided (debug / fallback).
        The hot path should leave filepath=None to avoid disk IO latency.

        Args:
            filepath: Optional path to save the audio file.

        Returns:
            bool: True if audio was captured, False otherwise.
        """
        with self._lock:
            if not self.recording:
                return False
            self.recording = False
            self._level = 0.0

        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        with self._lock:
            if not self.audio_data:
                self.last_audio_array = None
                return False
            data: np.ndarray = np.concatenate(self.audio_data, axis=0)
            self.audio_data = []

        # Flatten to 1D float32 for faster-whisper (skips disk write/read).
        self.last_audio_array = np.ascontiguousarray(np.squeeze(data), dtype=np.float32)

        if filepath:
            try:
                sf.write(filepath, data, self.sample_rate)
            except Exception as e:
                print(f"Warning: Failed to write debug WAV to {filepath}: {e}")
        return True

    def clear(self) -> None:
        """Drops the last captured buffer to free memory."""
        self.last_audio_array = None


def play_beep(frequency: float, duration: float, volume: float = 0.12) -> None:
    """Generates and plays a clean sine wave tone using sounddevice.

    Includes 10ms linear fades to prevent audible click artifacts.

    Args:
        frequency: Audio tone frequency in Hz.
        duration: Duration of the beep in seconds.
        volume: Volume level between 0.0 and 1.0.
    """
    sample_rate = 16000
    n_samples = max(1, int(sample_rate * duration))
    t: np.ndarray = np.linspace(0, duration, n_samples, endpoint=False, dtype=np.float32)
    wave: np.ndarray = (volume * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)

    # Fade in/out by 10ms to smooth out the start/stop click
    fade_len = min(int(sample_rate * 0.01), len(wave) // 2)
    if fade_len > 0:
        fade_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        fade_out = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
        wave[:fade_len] *= fade_in
        wave[-fade_len:] *= fade_out

    sd.play(wave, sample_rate)
    sd.wait()
