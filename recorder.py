import threading
import time
from typing import List, Optional
import numpy as np
import sounddevice as sd
import soundfile as sf


class AudioRecorder:
    """Always-on input stream with a ring buffer.

    The stream is opened once (startup) and left running, so pressing the hotkey
    costs zero device-open/prime latency — the mic is already delivering audio.
    ``start()`` only flips the capture flag; the callback copies the same chunks
    into the ring buffer and the active session buffer. This prevents the first
    ~50-100ms of speech from being lost while Windows opens the device.
    """

    # Ring buffer size in seconds. Long enough to always retain the pre-roll
    # window we need to bridge a key press, short enough to bound memory (~1MB).
    RING_SECONDS: int = 5
    # Pre-roll copied into a session when recording starts (seconds).
    PRE_ROLL_SECONDS: float = 0.4

    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        """Initializes the audio recorder and opens the persistent input stream.

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
        # Last time a stream status warning was logged — callback prints are throttled
        # because I/O in the real-time audio thread can cause dropouts/clicks.
        self._last_status_log: float = 0.0
        self._STATUS_LOG_MIN_INTERVAL = 5.0
        # Ring buffer (persistent, always capturing) and its per-session window.
        self._ring: List[np.ndarray] = []
        self._ring_samples = int(sample_rate * self.RING_SECONDS)
        self._session_samples = int(sample_rate * self.PRE_ROLL_SECONDS)
        self._ring_bytes = self._ring_samples * np.dtype(np.float32).itemsize

        # Open the device once; if it fails here, the app fails fast at startup
        # instead of discovering a broken mic on the first hotkey press.
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            callback=self._callback,
            dtype="float32",
            blocksize=1024,
            latency="low",
        )
        self._stream.start()

    def _callback(self, indata: np.ndarray, frames: int, time: object, status: object) -> None:
        """Internal callback for sounddevice input stream to capture audio chunks."""
        if status:
            # Minor buffer underflows are non-fatal; keep capturing.
            self._log_status_throttled(status)
        # Live meter (outside lock first for RMS compute, then short lock for store).
        try:
            peak = float(np.max(np.abs(indata))) if indata.size else 0.0
            # Soft-knee so quiet speech still moves the waveform.
            level = min(1.0, peak * 3.2)
        except Exception:
            level = 0.0
        with self._lock:
            chunk = indata.copy()
            # Always keep the ring fresh; drop the oldest data when it overflows.
            self._ring.append(chunk)
            ring_len = 0
            for part in self._ring:
                ring_len += part.size
            while self._ring and ring_len > self._ring_bytes:
                dropped = self._ring.pop(0)
                ring_len -= dropped.size
            if self.recording:
                # Session buffer is always 1D mono float32 (pre-roll is mixed in
                # start()); mix multi-channel frames down to mono and flatten so
                # concatenation in stop() never mixes dimensions.
                if chunk.ndim > 1:
                    if chunk.shape[1] > 1:
                        session_chunk = np.mean(chunk, axis=1).reshape(-1)
                    else:
                        session_chunk = chunk.reshape(-1)
                else:
                    session_chunk = chunk
                self.audio_data.append(session_chunk)
                # Exponential smooth toward current peak.
                self._level = (0.55 * self._level) + (0.45 * level)
            else:
                self._level = 0.0

    def get_level(self) -> float:
        """Returns a smoothed 0..1 mic level for the visualizer."""
        with self._lock:
            return self._level

    def start(self) -> None:
        """Starts a capture session from the persistent stream (no device re-open)."""
        with self._lock:
            if self.recording:
                return
            # Seed the session with the tail of the always-running ring buffer so
            # the first spoken syllable (which often starts before Windows would
            # have delivered the first callback) is not clipped.
            pre_roll: List[np.ndarray] = []
            pre_len = 0
            for part in reversed(self._ring):
                if pre_len + part.size > self._session_samples:
                    keep = max(0, self._session_samples - pre_len)
                    if keep > 0:
                        pre_roll.insert(0, part[-keep:])
                    break
                pre_roll.insert(0, part)
                pre_len += part.size
            # Chunks can be multi-channel (CHANNELS=2); mix the pre-roll to mono
            # now so the session stays 1D and concatenation in stop() is clean.
            if pre_roll:
                if self.channels > 1:
                    pre_mono = [np.mean(part, axis=1) for part in pre_roll]
                    pre_roll = pre_mono
                elif pre_roll[0].ndim > 1:
                    # channels=1 but chunks arrived 2D (tests / drivers that
                    # always emit (frames,1)) — flatten each chunk.
                    pre_roll = [part.reshape(-1) for part in pre_roll]
            self.audio_data = pre_roll
            self.last_audio_array = None
            self._level = 0.0
            self.recording = True

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

        # Stream stays open and running — only the session buffer is closed out.
        with self._lock:
            if not self.audio_data:
                self.last_audio_array = None
                return False
            data: np.ndarray = np.concatenate(self.audio_data, axis=0)
            self.audio_data = []

        # Flatten to 1D float32 for faster-whisper (skips disk write/read).
        # Session chunks are already mono (mixed in the callback); this is a
        # safety net for pre-roll or legacy paths that left 2D data.
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim > 1:
            if arr.shape[1] > 1:
                arr = np.mean(arr, axis=1)
            else:
                arr = arr.reshape(-1)
        self.last_audio_array = np.ascontiguousarray(arr, dtype=np.float32)

        if filepath:
            try:
                sf.write(filepath, data, self.sample_rate)
            except Exception as e:
                print(f"Warning: Failed to write debug WAV to {filepath}: {e}")
        return True

    def close(self) -> None:
        """Closes the persistent stream (app shutdown only)."""
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
            self.recording = False
            self.audio_data = []
            self._ring = []

    def clear(self) -> None:
        """Drops the last captured buffer to free memory."""
        self.last_audio_array = None


def play_beep(frequency: float, duration: float, volume: float = 0.12) -> None:
    """Generates and plays a clean sine wave tone using sounddevice.

    Includes 10ms linear fades to prevent audible click artifacts.

    Args:
        frequency: Audio frequency in Hz.
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
