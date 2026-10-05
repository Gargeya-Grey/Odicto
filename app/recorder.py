from collections import deque
import threading
import time
from time import monotonic
from typing import Callable, List, Optional
import numpy as np
import sounddevice as sd
import soundfile as sf


class _MicrophoneOpenFailed(RuntimeError):
    """Every open attempt failed and no stream was left half-open."""


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
    # Callback silence (seconds) that counts as a gap in, or death of, the stream.
    GAP_SECONDS: float = 3.0

    def __init__(self, sample_rate: int = 16000, channels: int = 1, max_seconds: float = 0) -> None:
        """Initializes the audio recorder and opens the persistent input stream.

        Args:
            sample_rate: The sample rate for recording, default 16000 (Whisper optimized).
            channels: The number of audio channels, default 1 (mono).
            max_seconds: Longest session kept, in seconds; 0 means no limit.
        """
        self.sample_rate: int = sample_rate
        self.channels: int = channels
        self.recording: bool = False
        self.audio_data: List[np.ndarray] = []
        self.last_audio_array: Optional[np.ndarray] = None
        self._stream: Optional[sd.InputStream] = None
        self._lock: threading.Lock = threading.Lock()
        self._stream_lock = threading.RLock()
        self._callback_ready = threading.Event()
        # Session quality flags. A gap or overflow keeps the audio and flags it;
        # the limit stops appending. stop() publishes them as last_capture_*.
        self._session_gap = False
        self._session_limited = False
        self._session_frames = 0
        try:
            max_seconds = float(max_seconds or 0)
        except (TypeError, ValueError):
            max_seconds = 0.0
        self.max_seconds: float = max(0.0, max_seconds)
        self._max_samples = int(sample_rate * self.max_seconds)
        self._limit_callback: Optional[Callable[[], None]] = None
        self.last_capture_gap: bool = False
        self.last_capture_limited: bool = False
        self._device_index = None
        # Smoothed peak level 0..1 for the live UI waveform (updated from audio callback).
        self._level: float = 0.0
        # Warnings are deferred to the runtime heartbeat, away from audio input.
        self._last_status_log: float = 0.0
        self._STATUS_LOG_MIN_INTERVAL = 5.0
        # Ring buffer (persistent, always capturing) and its per-session window.
        self._ring = deque()
        self._ring_frames = 0
        self._ring_samples = int(sample_rate * self.RING_SECONDS)
        self._session_samples = int(sample_rate * self.PRE_ROLL_SECONDS)
        # Optional live-STT listeners. Invoked on the audio thread with a copy
        # of each captured mono chunk; listeners must never block.
        self._chunk_listeners: List[Callable[[np.ndarray], None]] = []
        self._closed = threading.Event()
        self._last_callback = time.monotonic()
        self._callback_count = 0
        self._pending_status = None
        self._input_peak = 0.0
        self._device_info = {}
        self._last_capture_signal = None
        # Each opened stream gets a generation; only the current one may write.
        # A stream that survived a failed close can never feed the ring/session.
        self._generation = 0
        # Streams whose abort/close failed; retried before reopen and at close().
        self._stale_streams: List[object] = []

        # Open the device once. At login the WASAPI endpoint may not exist yet,
        # so retry with backoff instead of failing the whole app on first try.
        self._open_persistent_stream()

    def _open_persistent_stream(self, delays=(0.0, 0.5, 1.0, 2.0, 4.0)) -> None:
        """Open and start the always-on input stream, retrying a cold audio stack."""
        last_error: Optional[BaseException] = None
        for attempt, delay in enumerate(delays):
            if self._closed.is_set():
                return
            if delay:
                time.sleep(delay)
            stream = None
            with self._lock:
                self._generation += 1
                generation = self._generation

            def bound_callback(indata, frames, time_info, status, _gen=generation):
                self._callback(indata, frames, time_info, status, _generation=_gen)

            try:
                stream = sd.InputStream(
                    device=self._device_index,
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    callback=bound_callback,
                    dtype="float32",
                    blocksize=1024,
                    latency="low",
                )
                if self._closed.is_set():
                    stream.close()
                    return
                stream.start()
                self._stream = stream
                # Metadata only, off the audio callback; no second capture stream.
                try:
                    index = stream.device
                    if isinstance(index, (int, np.integer)):
                        self._device_index = int(index)
                        info = sd.query_devices(int(index))
                        self._device_info = {"index": int(index), "name": str(info["name"]),
                                             "host_api": str(sd.query_hostapis(info["hostapi"])["name"]),
                                             "sample_rate": self.sample_rate, "channels": self.channels}
                except Exception:
                    self._device_info = {}
                if attempt:
                    print(
                        f"Microphone ready after {attempt + 1} attempts.",
                        flush=True,
                    )
                return
            except Exception as e:
                last_error = e
                print(f"Microphone not ready ({e}); retrying...", flush=True)
                if stream is not None:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                    # A failed close propagates unchanged: it must never
                    # authorize another open (see _MicrophoneOpenFailed).
                    try:
                        stream.close(ignore_errors=False)
                    except Exception:
                        self._stale_streams.append(stream)
                        raise
        raise _MicrophoneOpenFailed(
            f"Could not open microphone after {len(delays)} attempts: {last_error}"
        ) from last_error

    def _refresh_device_list(self) -> None:
        """Re-enumerate PortAudio devices so a re-plugged endpoint gets a fresh index."""
        try:
            sd._terminate()
        except Exception:
            pass
        try:
            sd._initialize()
        except Exception:
            pass

    def _retry_stale_streams(self) -> bool:
        """Retry closing streams whose earlier close failed; True when none remain."""
        remaining = []
        for stream in self._stale_streams:
            try:
                try:
                    stream.abort()
                except Exception:
                    pass
                stream.close(ignore_errors=False)
            except Exception:
                remaining.append(stream)
        self._stale_streams = remaining
        return not remaining

    def _reopen_stream(self) -> None:
        """Reopen the cached device; on failure refresh PortAudio and use the default."""
        try:
            self._open_persistent_stream(delays=(0.0,))
            return
        except _MicrophoneOpenFailed as e:
            if self._closed.is_set():
                raise
            print(f"Cached microphone failed ({e}); refreshing device list...", flush=True)
        # The cached index can point at a removed or renumbered endpoint.
        self._refresh_device_list()
        self._device_index = None
        self._device_info = {}
        self._open_persistent_stream(delays=(0.0,))

    def set_limit_callback(self, fn: Optional[Callable[[], None]]) -> None:
        """Register fn, called once per session when max_seconds is reached.

        fn runs on a new daemon thread, never on the audio callback thread and
        never while recorder locks are held.
        """
        with self._lock:
            self._limit_callback = fn

    def _fire_limit_callback(self, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception as e:
                print(f"Recording limit handler failed: {e}", flush=True)
        try:
            threading.Thread(target=run, name="odicto-rec-limit", daemon=True).start()
        except Exception:
            pass

    def health_snapshot(self) -> dict:
        """Observe the existing stream; never reopen or alter the microphone."""
        with self._lock:
            latest = self._ring[-1] if self._ring else None
            peak = self._input_peak
            callback_count = self._callback_count
            status, self._pending_status = self._pending_status, None
        if status is not None:
            self._log_status_throttled(status)
        # Snapshot calculation uses an immutable chunk outside the callback lock.
        # Silence is legitimate input, never a reason to reset a microphone.
        rms = float(np.sqrt(np.mean(latest * latest))) if latest is not None and latest.size else 0.0
        return {"callback_age_s": round(time.monotonic() - self._last_callback, 2),
                "callback_count": callback_count,
                "closed": self._closed.is_set(), "device": dict(self._device_info),
                "input_peak": round(peak, 6), "input_rms": round(rms, 6),
                "last_capture": dict(self._last_capture_signal) if self._last_capture_signal else None}

    def _log_status_throttled(self, status: object) -> None:
        """Log deferred PortAudio warnings from the runtime heartbeat."""
        now = time.monotonic()
        if (now - self._last_status_log) < self._STATUS_LOG_MIN_INTERVAL:
            return
        self._last_status_log = now
        try:
            print(f"Audio stream status: {status}", flush=True)
        except Exception:
            pass

    def _callback(self, indata: np.ndarray, frames: int, time: object, status: object,
                  _generation: Optional[int] = None) -> None:
        """Internal callback for sounddevice input stream to capture audio chunks.

        Streams pass their generation; a stale one returns at once. Direct calls
        (no generation) are treated as the current stream.
        """
        if _generation is not None and _generation != self._generation:
            return
        # Live meter (outside lock first for RMS compute, then short lock for store).
        try:
            peak = float(np.max(np.abs(indata))) if indata.size else 0.0
            # Soft-knee so quiet speech still moves the waveform.
            level = min(1.0, peak * 3.2)
        except Exception:
            peak = level = 0.0
        limit_fn = None
        with self._lock:
            if self._closed.is_set():
                return
            if _generation is not None and _generation != self._generation:
                return
            now = monotonic()
            if now - self._last_callback > self.GAP_SECONDS:
                # Stale pre-roll must not bridge a gap; the session keeps its audio.
                self._ring.clear()
                self._ring_frames = 0
                if self.recording:
                    self._session_gap = True
            if self.recording and getattr(status, "input_overflow", False):
                self._session_gap = True
            self._last_callback = now
            self._callback_count += 1
            self._input_peak = peak
            if status:
                # No disk/console I/O in the real-time callback.
                self._pending_status = status
            chunk = indata.copy()
            # Always keep the ring fresh; drop the oldest data when it overflows.
            self._ring.append(chunk)
            self._ring_frames += len(chunk)
            while self._ring and self._ring_frames > self._ring_samples:
                excess = self._ring_frames - self._ring_samples
                oldest = self._ring[0]
                if len(oldest) <= excess:
                    self._ring_frames -= len(self._ring.popleft())
                else:
                    self._ring[0] = oldest[excess:].copy()
                    self._ring_frames -= excess
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
                listeners = []
                if self._max_samples:
                    room = self._max_samples - self._session_frames
                    if room <= 0:
                        session_chunk = None
                        if not self._session_limited:
                            # Pre-roll alone filled a very small limit.
                            self._session_limited = True
                            limit_fn = self._limit_callback
                    elif len(session_chunk) >= room:
                        session_chunk = session_chunk[:room]
                        self._session_limited = True
                        limit_fn = self._limit_callback
                if session_chunk is not None:
                    self.audio_data.append(session_chunk)
                    self._session_frames += len(session_chunk)
                    listeners = list(self._chunk_listeners)
                # Exponential smooth toward current peak.
                self._level = (0.55 * self._level) + (0.45 * level)
            else:
                listeners = []
                self._level = 0.0
            self._callback_ready.set()
        if limit_fn is not None:
            # Exactly once per session: only the chunk that fills the last room
            # reaches here. The handler runs off the audio thread, lock-free.
            self._fire_limit_callback(limit_fn)
        for fn in listeners:
            try:
                fn(session_chunk)
            except Exception:
                pass

    def get_level(self) -> float:
        """Returns a smoothed 0..1 mic level for the visualizer."""
        with self._lock:
            return self._level

    def get_waveform(self, n: int) -> list:
        """Cheap per-bar envelope from the latest captured samples.

        Splits the most recent callback chunk into ``n`` RMS buckets so
        the HUD tracks the actual voice instead of a synthetic sine. O(chunk).
        """
        n = max(1, int(n))
        with self._lock:
            if not self.recording or not self.audio_data:
                return [0.0] * n
            arr = np.asarray(self.audio_data[-1], dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return [0.0] * n
        if arr.size < n:
            # Repeat-pad so quiet/short callbacks still fill the bars.
            reps = int(np.ceil(n / max(1, arr.size)))
            arr = np.tile(arr, reps)[: n]
        # Vectorized bucket RMS — one reduce, no Python loop over samples.
        usable = (arr.size // n) * n
        if usable <= 0:
            return [0.0] * n
        shaped = arr[:usable].reshape(n, -1)
        rms = np.sqrt(np.mean(shaped * shaped, axis=1))
        # Speech is often quiet in float32; a modest gain keeps peaks readable.
        levels = np.clip(rms * 5.5, 0.0, 1.0)
        return levels.astype(np.float32).tolist()

    def add_chunk_listener(self, fn: Callable[[np.ndarray], None]) -> None:
        """Register a live-audio consumer (called from the input callback)."""
        with self._lock:
            if fn not in self._chunk_listeners:
                self._chunk_listeners.append(fn)

    def remove_chunk_listener(self, fn: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._chunk_listeners = [x for x in self._chunk_listeners if x != fn]

    def start(self) -> None:
        """Reuse healthy input; reconnect a stale endpoint before capture."""
        with self._stream_lock:
            if self._closed.is_set():
                raise RuntimeError("Microphone closed; restart Odicto")
            if not self.recording and time.monotonic() - self._last_callback > self.GAP_SECONDS:
                # A USB endpoint can return while its old stream stays dead.
                # Reconnect only on demand, after closing the previous stream.
                if self._stream is not None:
                    stream = self._stream
                    # Retire the old stream's generation first: if the driver
                    # keeps it alive, its callbacks are ignored from now on.
                    with self._lock:
                        self._generation += 1
                    try:
                        stream.abort()
                    except Exception:
                        pass  # close() below decides whether the stream is gone
                    try:
                        stream.close(ignore_errors=False)
                    except Exception:
                        # A dead stream that refuses to close must not wedge
                        # every later start(); this attempt still fails, so no
                        # second endpoint opens on top of it now. Keep the
                        # handle so later reopen/close() can retry closing it.
                        self._stale_streams.append(stream)
                        raise
                    finally:
                        self._stream = None
                if not self._retry_stale_streams():
                    # Never open a second endpoint while a native stream that
                    # refuses to close may still be alive.
                    raise RuntimeError(
                        "Microphone is still held by a previous stream; reconnect it or restart Odicto"
                    )
                with self._lock:
                    self._ring.clear()
                    self._ring_frames = 0
                self._callback_ready.clear()
                self._reopen_stream()
                if not self._callback_ready.wait(1.0):
                    raise RuntimeError("Microphone unavailable; reconnect it and try again")
            self._start_capture()

    def _start_capture(self) -> None:
        with self._lock:
            if self.recording:
                return
            if (self._closed.is_set() or self._stream is None
                    or time.monotonic() - self._last_callback > self.GAP_SECONDS):
                raise RuntimeError("Microphone stopped; restart Odicto")
            # Seed the session with the tail of the always-running ring buffer so
            # the first spoken syllable (which often starts before Windows would
            # have delivered the first callback) is not clipped.
            pre_roll: List[np.ndarray] = []
            pre_len = 0
            for part in reversed(self._ring):
                if pre_len + len(part) > self._session_samples:
                    keep = max(0, self._session_samples - pre_len)
                    if keep > 0:
                        pre_roll.insert(0, part[-keep:])
                    break
                pre_roll.insert(0, part)
                pre_len += len(part)
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
            if self._max_samples and sum(len(part) for part in pre_roll) > self._max_samples:
                # Keep the newest pre-roll when the limit is shorter than it.
                kept: List[np.ndarray] = []
                kept_len = 0
                for part in reversed(pre_roll):
                    take = min(len(part), self._max_samples - kept_len)
                    if take <= 0:
                        break
                    kept.insert(0, part[-take:])
                    kept_len += take
                pre_roll = kept
            self.audio_data = pre_roll
            self._session_frames = sum(len(part) for part in pre_roll)
            self._session_gap = False
            self._session_limited = False
            self.last_capture_gap = False
            self.last_capture_limited = False
            self.last_audio_array = None
            self._level = 0.0
            self.recording = True
            # Listener delivery stays ordered with the input callback. Consumers
            # only enqueue chunks and must not block or re-enter the recorder.
            for part in pre_roll:
                for listener in self._chunk_listeners:
                    try:
                        listener(part)
                    except Exception:
                        pass

    def stop(self, filepath: Optional[str] = None) -> bool:
        """Stops recording and keeps the captured buffer in memory.

        Optionally saves to a WAV file when a filepath is provided (debug / fallback).
        The hot path should leave filepath=None to avoid disk IO latency.

        Args:
            filepath: Optional path to save the audio file.

        Captured audio is never discarded because of an overflow or a callback
        gap: it is kept and flagged in ``last_capture_gap``. ``last_capture_limited``
        is True when the session reached ``max_seconds``.

        Returns:
            bool: True if nonzero audio was captured, False otherwise.

        Raises:
            RuntimeError: The stream died and the session holds no audio at all.
        """
        with self._lock:
            if not self.recording:
                return False
            self.recording = False
            self._level = 0.0
            stream_dead = time.monotonic() - self._last_callback > self.GAP_SECONDS
            self.last_capture_gap = self._session_gap or stream_dead
            self.last_capture_limited = self._session_limited
            chunks = self.audio_data
            self.audio_data = []
            self._session_frames = 0
            if not any(len(part) for part in chunks):
                self.last_audio_array = None
                if stream_dead:
                    raise RuntimeError("Microphone interrupted; please record again")
                return False

        # A long capture must not hold the callback lock during allocation/copy.
        data: np.ndarray = np.concatenate(chunks, axis=0)

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
        # Retain only magnitude statistics after pipeline cleanup, so status can
        # assess completed speech rather than whatever room noise happens now.
        self._last_capture_signal = {"seconds": round(arr.size / self.sample_rate, 2),
                                     "rms": round(float(np.sqrt(np.dot(arr, arr) / arr.size)), 6) if arr.size else 0.0}

        if filepath:
            try:
                sf.write(filepath, data, self.sample_rate)
            except Exception as e:
                print(f"Warning: Failed to write debug WAV to {filepath}: {e}")
        # Exact digital silence must not produce recognizer hallucinations.
        # Preserve quiet speech and the unmodified buffer for diagnostics.
        return bool(np.any(arr))

    def close(self) -> None:
        """Closes the persistent stream (app shutdown only)."""
        self._closed.set()
        with self._stream_lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            # Shutdown never forgets a stream that refused to close earlier.
            self._retry_stale_streams()
            self._stale_streams = []
        with self._lock:
            self.recording = False
            self.audio_data = []
            self._ring.clear()
            self._ring_frames = 0

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
