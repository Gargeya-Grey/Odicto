import os
import time
from typing import List, Optional, Tuple, Union
import numpy as np
from faster_whisper import WhisperModel
from config import Config


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

        segments, _info = self.model.transcribe(
            audio,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={
                # Aggressive enough to drop silence quickly without clipping short words.
                "min_silence_duration_ms": 250,
                "speech_pad_ms": 120,
            },
            condition_on_previous_text=False,
            without_timestamps=True,
            language="en" if is_english_model else None,
            # Skip expensive alignment work we never use.
            word_timestamps=False,
        )

        # Consume generator promptly; join without intermediate list growth for tiny clips.
        parts: List[str] = []
        for segment in segments:
            text = segment.text
            if text:
                parts.append(text)
        return "".join(parts).strip()
