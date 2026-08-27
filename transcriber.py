import asyncio
import base64
import io
import os
import queue
import threading
import time
import wave
from typing import Callable, List, Optional, Tuple, Union

import numpy as np

from config import Config

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
except Exception:  # pragma: no cover
    google_genai = None  # type: ignore
    google_genai_types = None  # type: ignore

# Filled on first Whisper load so Gemini-only boots skip ctranslate2 import.
WhisperModel = None  # type: ignore

_genai_client_lock = threading.Lock()
_genai_client = None
_genai_client_key: Optional[str] = None
_genai_client_factory = None


def get_genai_client(api_key: str):
    """One SDK client per process for the same API key.

    The cache is keyed by both the key and the ``Client`` factory object so
    tests that patch ``google_genai.Client`` still get a fresh mock.
    """
    global _genai_client, _genai_client_key, _genai_client_factory
    key = (api_key or "").strip()
    if google_genai is None or not key:
        return None
    factory = getattr(google_genai, "Client", None)
    if factory is None:
        return None
    with _genai_client_lock:
        if (
            _genai_client is not None
            and _genai_client_key == key
            and _genai_client_factory is factory
        ):
            return _genai_client
        try:
            client = factory(api_key=key)
        except Exception:
            return None
        _genai_client = client
        _genai_client_key = key
        _genai_client_factory = factory
        return client


def float32_to_pcm16_bytes(audio: np.ndarray) -> bytes:
    """Convert mono float32 PCM in [-1, 1] to little-endian 16-bit PCM bytes."""
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.mean(arr, axis=1) if arr.shape[-1] > 1 else arr.reshape(-1)
    if arr.size == 0:
        return b""
    pcm = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
    return np.ascontiguousarray(pcm).tobytes()


def float32_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Wrap mono float32 PCM as an in-memory 16-bit WAV (no disk)."""
    pcm = float32_to_pcm16_bytes(audio)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate) or 16000)
        wf.writeframes(pcm)
    return buf.getvalue()


def _audio_to_wav_bytes(audio: Union[str, np.ndarray], sample_rate: int) -> bytes:
    if isinstance(audio, str):
        if not os.path.exists(audio):
            raise FileNotFoundError(f"Audio file '{audio}' does not exist.")
        with open(audio, "rb") as f:
            return f.read()
    if isinstance(audio, np.ndarray):
        return float32_to_wav_bytes(audio, sample_rate)
    raise TypeError(f"Unsupported audio type: {type(audio)}")


class WhisperTranscriber:
    def __init__(self) -> None:
        """Initializes the Whisper model with hardware acceleration detection and safety fallbacks."""
        self.model: Optional[WhisperModel] = None
        self.device: str = ""
        self.compute_type: str = ""
        self._load_model()

    def _load_model(self) -> None:
        """Internal helper to load the Whisper model.

        Attempts to load on CUDA (GPU) first if auto-detected or configured, and falls
        back to optimized CPU (int8) if CUDA initialization fails or is unavailable.
        """
        model_size: str = Config.WHISPER_MODEL_SIZE
        configured_device: str = Config.WHISPER_DEVICE.lower()

        # Device/quantization fallback chain
        devices_to_try: List[Tuple[str, str]] = []
        if configured_device == "cuda":
            devices_to_try = [("cuda", "float16")]
        elif configured_device == "cpu":
            devices_to_try = [("cpu", "int8")]
        else:  # "auto"
            devices_to_try = [("cuda", "float16"), ("cpu", "int8")]

        last_error: Optional[Exception] = None
        global WhisperModel
        if WhisperModel is None:
            from faster_whisper import WhisperModel as _WhisperModel

            WhisperModel = _WhisperModel
        for device, compute_type in devices_to_try:
            try:
                print(
                    f"Attempting to load Whisper model '{model_size}' on {device} ({compute_type})..."
                )
                start_time = time.time()
                self.model = WhisperModel(
                    model_size,
                    device=device,
                    compute_type=compute_type,
                    # Reduce CPU oversubscription when using GPU; still fine on CPU.
                    cpu_threads=max(1, (os.cpu_count() or 4) // 2),
                    num_workers=1,
                )
                self.device = device
                self.compute_type = compute_type
                elapsed = time.time() - start_time
                print(
                    f"Whisper model loaded successfully on {device} in {elapsed:.2f} seconds."
                )
                return
            except Exception as e:
                last_error = e
                print(f"Warning: Failed to load Whisper on {device} ({compute_type}): {e}")
                if device == "cpu" or configured_device == device:
                    raise e

        if not self.model:
            raise RuntimeError(
                f"Could not initialize Whisper model on any device: {last_error}"
            )

    def transcribe(self, audio: Union[str, np.ndarray]) -> str:
        """Transcribes audio to text (accepts filepath string or in-memory numpy array).

        Speed-oriented decode settings preserve accuracy for short push-to-talk clips
        (greedy beam, VAD, no timestamps).

        Args:
            audio: Path to the mono WAV file, or in-memory 1D float32 numpy array.

        Returns:
            str: The transcribed text.
        """
        if not self.model:
            raise RuntimeError("Whisper model is not loaded.")

        if isinstance(audio, str):
            if not os.path.exists(audio):
                raise FileNotFoundError(f"Audio file '{audio}' does not exist.")
        elif isinstance(audio, np.ndarray):
            if audio.size == 0:
                return ""
            # faster-whisper expects 1D float32 mono PCM in [-1, 1]
            if audio.ndim > 1:
                audio = np.squeeze(audio)
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32, copy=False)
        else:
            raise TypeError(f"Unsupported audio type: {type(audio)}")

        is_english_model = Config.WHISPER_MODEL_SIZE.endswith(".en")

        # Hold-to-talk already bounds the utterance. Silero VAD on short clips
        # costs extra CPU and can clip the first syllable; skip it unless the
        # clip is long enough that silence-trimming pays off, or the user
        # forced WHISPER_VAD=true.
        use_vad = bool(Config.WHISPER_VAD)
        if not use_vad and isinstance(audio, np.ndarray) and audio.size:
            duration_s = float(audio.size) / float(Config.SAMPLE_RATE or 16000)
            use_vad = duration_s >= 8.0

        transcribe_kwargs = {
            "beam_size": 1,
            "best_of": 1,
            "temperature": 0.0,
            "vad_filter": use_vad,
            "condition_on_previous_text": False,
            "without_timestamps": True,
            "language": "en" if is_english_model else None,
            "word_timestamps": False,
        }
        if use_vad:
            transcribe_kwargs["vad_parameters"] = {
                "threshold": 0.4,
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 300,
            }

        segments, _info = self.model.transcribe(audio, **transcribe_kwargs)

        # Consume generator promptly; join without intermediate list growth for tiny clips.
        parts: List[str] = []
        for segment in segments:
            text = segment.text
            if text:
                parts.append(text)
        return "".join(parts).strip()


def _transcription_config_payload(mode: str) -> dict:
    """Unary Interactions API transcription_config (official Gemini 3.5 Transcribe shape)."""
    cfg: dict = {"mode": {"type": "verbatim" if mode == "verbatim" else "smart"}}
    langs = Config.gemini_transcribe_language_codes()
    cfg["language_codes"] = langs
    vocab = Config.gemini_transcribe_vocabulary()
    if vocab:
        cfg["custom_vocabulary"] = vocab
    return cfg


class GeminiTranscriber:
    """Cloud STT via Gemini 3.5 Transcribe (unary Interactions API).

    Falls back to local Whisper on missing key, SDK errors, or empty output.
    """

    def __init__(self) -> None:
        self._client = None
        self._whisper: Optional[WhisperTranscriber] = None
        api_key = Config.GEMINI_API_KEY.strip()
        if google_genai is None:
            print(
                "Warning: google-genai is not installed — Gemini STT unavailable.",
                flush=True,
            )
            return
        if not api_key:
            print(
                "Warning: GEMINI_API_KEY empty — Gemini STT will fall back to Whisper.",
                flush=True,
            )
            return
        self._client = get_genai_client(api_key)
        if self._client is None:
            print("Warning: Could not create Gemini STT client.", flush=True)

    def _whisper_fallback(self, audio: Union[str, np.ndarray], reason: str) -> str:
        print(f"Gemini STT fallback to Whisper ({reason})", flush=True)
        if self._whisper is None:
            self._whisper = WhisperTranscriber()
        return self._whisper.transcribe(audio)

    def transcribe(
        self, audio: Union[str, np.ndarray], mode: Optional[str] = None
    ) -> str:
        if isinstance(audio, np.ndarray) and audio.size == 0:
            return ""
        if self._client is None:
            return self._whisper_fallback(audio, "no Gemini client")

        try:
            wav_bytes = _audio_to_wav_bytes(audio, Config.SAMPLE_RATE or 16000)
        except Exception as e:
            return self._whisper_fallback(audio, f"audio encode failed: {e}")
        if not wav_bytes:
            return ""

        resolved = (mode or Config.gemini_transcribe_mode() or "smart").strip().lower()
        if resolved != "verbatim":
            resolved = "smart"
        model = Config.GEMINI_TRANSCRIBE_MODEL or "gemini-3.5-transcribe"
        try:
            interaction = self._client.interactions.create(
                model=model,
                input=[
                    {
                        "type": "audio",
                        "data": base64.b64encode(wav_bytes).decode("ascii"),
                        "mime_type": "audio/wav",
                    }
                ],
                generation_config={
                    "transcription_config": _transcription_config_payload(resolved)
                },
            )
        except Exception as e:
            detail = str(getattr(e, "message", "")) or str(e)
            return self._whisper_fallback(audio, detail.strip() or type(e).__name__)

        text = getattr(interaction, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        return self._whisper_fallback(audio, "empty Gemini transcript")


class GeminiLiveSession:
    """One tap-to-talk Live API session (Manual VAD).

    Audio chunks are pushed from the recorder callback; interim text is
    delivered on the caller thread via ``on_interim``. ``stop()`` returns the
    concatenated finalized transcript (may be empty — caller should fall back).
    """

    def __init__(
        self,
        on_interim: Optional[Callable[[str], None]] = None,
        on_final: Optional[Callable[[str], None]] = None,
        client=None,
    ) -> None:
        self._on_interim = on_interim
        self._on_final = on_final
        self._sdk_client = client
        self._chunks: queue.Queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._final_parts: List[str] = []
        self._error: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="odicto-live-stt"
        )
        self._thread.start()

    def push_audio(self, chunk: np.ndarray) -> None:
        if self._stop.is_set():
            return
        try:
            self._chunks.put_nowait(np.ascontiguousarray(chunk, dtype=np.float32))
        except queue.Full:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunks.put_nowait(np.ascontiguousarray(chunk, dtype=np.float32))
            except queue.Full:
                pass

    def stop(self, timeout: float = 8.0) -> str:
        self._stop.set()
        try:
            self._chunks.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._error:
            print(f"Gemini Live STT error: {self._error}", flush=True)
        return " ".join(p for p in self._final_parts if p).strip()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as e:
            self._error = str(e)
            print(f"Gemini Live STT thread failed: {e}", flush=True)

    async def _run(self) -> None:
        if google_genai is None or google_genai_types is None:
            self._error = "google-genai not installed"
            return
        api_key = Config.GEMINI_API_KEY.strip()
        if not api_key:
            self._error = "GEMINI_API_KEY empty"
            return
        client = self._sdk_client or get_genai_client(api_key)
        if client is None:
            self._error = "no Gemini client"
            return
        mode = Config.gemini_transcribe_mode()
        mode_enum = (
            google_genai_types.AudioTranscriptionConfigMode.VERBATIM
            if mode == "verbatim"
            else google_genai_types.AudioTranscriptionConfigMode.SMART
        )
        vocab = Config.gemini_transcribe_vocabulary() or None
        langs = Config.gemini_transcribe_language_codes()
        transcribe_cfg = google_genai_types.AudioTranscriptionConfig(
            language_codes=langs or None,
            custom_vocabulary=vocab,
            mode=mode_enum,
        )
        live_cfg = google_genai_types.LiveConnectConfig(
            response_modalities=["TEXT"],
            realtime_input_config=google_genai_types.RealtimeInputConfig(
                automatic_activity_detection=google_genai_types.AutomaticActivityDetection(
                    disabled=True
                )
            ),
            input_audio_transcription=transcribe_cfg,
        )
        model = Config.GEMINI_TRANSCRIBE_LIVE_MODEL or "gemini-3.5-transcribe-live"
        sample_rate = int(Config.SAMPLE_RATE or 16000)
        mime = f"audio/pcm;rate={sample_rate}"
        try:
            async with client.aio.live.connect(model=model, config=live_cfg) as session:
                self._session = session
                await session.send_realtime_input(
                    activity_start=google_genai_types.ActivityStart()
                )
                self._ready.set()
                receiver = asyncio.create_task(self._recv_loop(session))
                await self._send_loop(session, mime)
                try:
                    await session.send_realtime_input(
                        activity_end=google_genai_types.ActivityEnd()
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(receiver), timeout=0.45)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    receiver.cancel()
        except Exception as e:
            self._error = str(getattr(e, "message", "")) or str(e)
        finally:
            self._ready.set()
            self._session = None

    async def _send_loop(self, session, mime: str) -> None:
        while not self._stop.is_set():
            try:
                chunk = await asyncio.to_thread(self._chunks.get, True, 0.15)
            except queue.Empty:
                continue
            if chunk is None:
                break
            pcm = float32_to_pcm16_bytes(chunk)
            if not pcm:
                continue
            try:
                await session.send_realtime_input(
                    audio=google_genai_types.Blob(data=pcm, mime_type=mime)
                )
            except Exception as e:
                self._error = str(e)
                break
        try:
            await session.send_realtime_input(audio_stream_end=True)
        except Exception:
            pass

    async def _recv_loop(self, session) -> None:
        async for response in session.receive():
            server = getattr(response, "server_content", None)
            if server is None:
                continue
            interim = getattr(server, "interim_input_transcription", None)
            if interim is not None:
                text = getattr(interim, "text", None)
                if isinstance(text, str) and text.strip() and self._on_interim:
                    try:
                        self._on_interim(text.strip())
                    except Exception:
                        pass
            final = getattr(server, "input_transcription", None)
            if final is not None:
                text = getattr(final, "text", None)
                if isinstance(text, str) and text.strip():
                    self._final_parts.append(text.strip())
                    if self._on_final:
                        try:
                            self._on_final(text.strip())
                        except Exception:
                            pass
            if self._stop.is_set() and getattr(server, "turn_complete", False):
                break


def build_transcriber():
    """Factory used by main: Gemini unary (with Whisper fallback) or local Whisper."""
    if Config.effective_stt_provider() == "gemini":
        print(
            f"STT: Gemini 3.5 Transcribe "
            f"({Config.gemini_transcribe_mode()}, model={Config.GEMINI_TRANSCRIBE_MODEL})",
            flush=True,
        )
        return GeminiTranscriber()
    print(
        f"STT: local Whisper ({Config.WHISPER_MODEL_SIZE} on {Config.WHISPER_DEVICE})",
        flush=True,
    )
    return WhisperTranscriber()
