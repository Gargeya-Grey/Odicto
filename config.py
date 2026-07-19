import os
import re
from typing import Literal, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def parse_hold_hotkey(hotkey: str) -> Tuple[Tuple[str, ...], str]:
    """Split a hold-to-talk chord into (modifiers, primary_key).

    Examples:
        "alt+x"        -> (("alt",), "x")
        "ctrl+shift+space" -> (("ctrl", "shift"), "space")
        "scroll lock"  -> ((), "scroll lock")
    """
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", hotkey.strip()) if p.strip()]
    if not parts:
        raise ValueError(f"HOTKEY is empty or invalid: {hotkey!r}")
    if len(parts) == 1:
        return (), parts[0]
    return tuple(parts[:-1]), parts[-1]


class Config:
    # Hotkey config
    # Hold-to-talk chord (modifiers + primary). Primary is press/release; modifiers must be held.
    HOTKEY: str = os.getenv("HOTKEY", "alt+x")
    # Extra key held with HOTKEY to enable AI refinement (e.g. z → Alt+X+Z).
    AI_MODIFIER: str = os.getenv("AI_MODIFIER", "z").strip().lower()

    # Audio config
    SAMPLE_RATE: int = int(os.getenv("SAMPLE_RATE", "16000"))
    CHANNELS: int = int(os.getenv("CHANNELS", "1"))

    # Whisper config
    WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "tiny.en")
    WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "auto")

    # LLM config
    # Flip LLM_PROVIDER between ollama / openrouter / none to switch backends.
    LLM_PROVIDER: Literal["ollama", "openrouter", "none"] = os.getenv(
        "LLM_PROVIDER", "ollama"
    ).lower()  # type: ignore
    # Ollama model tag (also used as fallback model id for openrouter if OPENROUTER_MODEL is blank)
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen2.5:1.5b-instruct")
    # OpenRouter-only model slug (e.g. google/gemini-2.0-flash-001). Preferred when provider=openrouter.
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "").strip()
    # Ollama OpenAI-compatible base. For openrouter, localhost is auto-rewritten in TextRefiner.
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
    # Canonical OpenRouter OpenAI-compatible endpoint (used when provider=openrouter)
    OPENROUTER_API_BASE: str = os.getenv(
        "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
    ).strip()
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "150"))
    LLM_NUM_CTX: int = int(os.getenv("LLM_NUM_CTX", "2048"))
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")

    @classmethod
    def effective_llm_model(cls) -> str:
        """Model id for the active provider (OPENROUTER_MODEL wins when set)."""
        if cls.LLM_PROVIDER == "openrouter" and cls.OPENROUTER_MODEL:
            return cls.OPENROUTER_MODEL
        return cls.LLM_MODEL

    @classmethod
    def effective_llm_api_base(cls) -> str:
        """API base for the active provider."""
        if cls.LLM_PROVIDER == "openrouter":
            base = cls.LLM_API_BASE
            # Keep a custom base if the user pointed LLM_API_BASE at a non-local proxy.
            if "localhost" in base or "127.0.0.1" in base or not base:
                return cls.OPENROUTER_API_BASE or "https://openrouter.ai/api/v1"
            return base
        return cls.LLM_API_BASE

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

        # Validate hotkey parses (and that AI modifier is not the primary key).
        mods, primary = parse_hold_hotkey(cls.HOTKEY)
        if cls.AI_MODIFIER:
            if cls.AI_MODIFIER == primary or cls.AI_MODIFIER in mods:
                raise ValueError(
                    f"AI_MODIFIER '{cls.AI_MODIFIER}' must be distinct from HOTKEY parts "
                    f"({cls.HOTKEY})"
                )


# Validate config at module load time to catch misconfigurations early
Config.validate()
