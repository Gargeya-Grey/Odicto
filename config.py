import os
from typing import Literal
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


class Config:
    # Hotkey config
    HOTKEY: str = os.getenv("HOTKEY", "ctrl+shift+space")

    # Audio config
    SAMPLE_RATE: int = int(os.getenv("SAMPLE_RATE", "16000"))
    CHANNELS: int = int(os.getenv("CHANNELS", "1"))

    # Whisper config
    WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "tiny.en")
    WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "auto")

    # LLM config
    LLM_PROVIDER: Literal["ollama", "openrouter", "none"] = os.getenv(
        "LLM_PROVIDER", "ollama"
    ).lower()  # type: ignore
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen2.5:1.5b-instruct")
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "150"))
    LLM_NUM_CTX: int = int(os.getenv("LLM_NUM_CTX", "2048"))
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")

    # Timing & Feedback
    PASTE_DELAY_SECONDS: float = float(os.getenv("PASTE_DELAY_SECONDS", "0.05"))
    PLAY_AUDIO_CUES: bool = _env_bool("PLAY_AUDIO_CUES", "true")
    SHOW_VISUAL_INDICATOR: bool = _env_bool("SHOW_VISUAL_INDICATOR", "true")
    # Minimum hold time (ms) before a recording is accepted — filters accidental taps
    MIN_HOLD_MS: int = int(os.getenv("MIN_HOLD_MS", "80"))
    # Debounce between consecutive capture cycles (ms)
    RETRIGGER_COOLDOWN_MS: int = int(os.getenv("RETRIGGER_COOLDOWN_MS", "120"))

    @classmethod
    def validate(cls) -> None:
        """Validates configuration parameters, checking for invalid inputs or missing API keys.

        Raises:
            ValueError: If a configuration value is invalid.
        """
        valid_providers = {"ollama", "openrouter", "none"}
        if cls.LLM_PROVIDER not in valid_providers:
            raise ValueError(
                f"LLM_PROVIDER must be one of {valid_providers}, got '{cls.LLM_PROVIDER}'"
            )

        if cls.LLM_PROVIDER == "openrouter" and not cls.OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER is 'openrouter'"
            )

        if cls.SAMPLE_RATE <= 0:
            raise ValueError(f"SAMPLE_RATE must be positive, got {cls.SAMPLE_RATE}")
        if cls.CHANNELS not in (1, 2):
            raise ValueError(f"CHANNELS must be 1 or 2, got {cls.CHANNELS}")
        if cls.LLM_MAX_TOKENS < 1:
            raise ValueError(f"LLM_MAX_TOKENS must be >= 1, got {cls.LLM_MAX_TOKENS}")
        if cls.LLM_NUM_CTX < 256:
            raise ValueError(f"LLM_NUM_CTX must be >= 256, got {cls.LLM_NUM_CTX}")


# Validate config at module load time to catch misconfigurations early
Config.validate()
