import os
import re
from typing import Literal, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file (override system env vars so the
# project's .env takes precedence over Windows user/system environment).
load_dotenv(override=True)


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _sanitize_model_id(raw: str) -> str:
    """Clean a model slug from .env.

    python-dotenv only treats ``#`` as a comment when there is whitespace before it.
    A common mistake is::

        OPENROUTER_MODEL=new-model#old-model:free

    which becomes one invalid OpenRouter id. If ``#`` appears mid-value and the
    right side looks like another model, keep only the left side and warn.
    """
    value = raw.strip().strip('"').strip("'")
    if not value or "#" not in value:
        return value
    left, right = value.split("#", 1)
    left = left.strip()
    right = right.strip()
    # Accidental dual-model / inline "comment" without a leading space
    if left and right and ("/" in right or ":" in right or " " not in right):
        print(
            f"Warning: model id contained '#...' ({value!r}). "
            f"Using {left!r} only. Put old models on a separate commented line.",
            flush=True,
        )
        return left
    return value


# keyboard lib name for the US `~ key (top-left, under Esc).
_KEY_ALIASES = {
    "`": "grave",
    "backtick": "grave",
    "back-tick": "grave",
    "back quote": "grave",
    "backquote": "grave",
}


def normalize_key_name(key: str) -> str:
    """Canonicalize a single key token for the keyboard library."""
    k = key.strip().lower()
    return _KEY_ALIASES.get(k, k)


def parse_hold_hotkey(hotkey: str) -> Tuple[Tuple[str, ...], str]:
    """Split a hold-to-talk chord into (modifiers, primary_key).

    Examples:
        "ctrl+grave"       -> (("ctrl",), "grave")
        "ctrl+shift+grave" -> (("ctrl", "shift"), "grave")
        "ctrl+`"           -> (("ctrl",), "grave")
        "scroll lock"      -> ((), "scroll lock")
    """
    parts = [
        normalize_key_name(p)
        for p in re.split(r"\s*\+\s*", hotkey.strip())
        if p.strip()
    ]
    if not parts:
        raise ValueError(f"HOTKEY is empty or invalid: {hotkey!r}")
    if len(parts) == 1:
        return (), parts[0]
    return tuple(parts[:-1]), parts[-1]


def validate_hotkey_pair(hotkey: str, ai_hotkey: str = "") -> None:
    """Validate a proposed HOTKEY / AI_HOTKEY pair without touching live config.

    Raises ValueError when the chord would be unsafe (bare primary key) or the
    AI chord would be indistinguishable from the dictation chord.
    """
    dict_mods, dict_primary = parse_hold_hotkey(hotkey)
    if not dict_mods:
        raise ValueError(
            f"HOTKEY '{hotkey}' needs at least one modifier. A bare primary "
            "would be globally suppressed (the key could never be typed in any app)."
        )
    if ai_hotkey:
        ai_mods, ai_primary = parse_hold_hotkey(ai_hotkey)
        if ai_primary != dict_primary:
            raise ValueError(
                f"AI_HOTKEY primary key '{ai_primary}' must match HOTKEY primary "
                f"'{dict_primary}' (both chords share one hold key)"
            )
        if set(ai_mods) == set(dict_mods):
            raise ValueError(
                "AI_HOTKEY must differ from HOTKEY (add Shift or another modifier "
                "so dictation and AI are distinguishable)"
            )


class Config:
    # Hotkey config — two full chords sharing one primary key is preferred:
    #   HOTKEY=ctrl+grave          → raw dictation
    #   AI_HOTKEY=ctrl+shift+grave → AI reply
    # (keyboard lib name for ` is "grave")
    HOTKEY: str = os.getenv("HOTKEY", "ctrl+grave")
    AI_HOTKEY: str = os.getenv("AI_HOTKEY", "ctrl+shift+grave").strip()
    # Legacy optional third key (unused when AI_HOTKEY is set). Prefer AI_HOTKEY.
    AI_MODIFIER: str = os.getenv("AI_MODIFIER", "").strip().lower()
    # Plain hotkey (no modifiers required) that clears the AI multi-turn memory
    # immediately, without a recording. Empty = disabled. 'f5' is the default.
    RESET_CONTEXT_HOTKEY: str = os.getenv("RESET_CONTEXT_HOTKEY", "f5").strip().lower()
    # Extra keys held during a capture keep AI multi-turn memory (opt-in).
    # Default AI chord is always a fresh one-shot. Hold F6 with Ctrl+` (or the
    # AI chord) to continue the previous F6 conversation. Empty = disabled.
    # CTRL_FORCE_FRESH_KEYS is a leftover name from the inverted behavior; if
    # CTRL_KEEP_CONTEXT_KEYS is unset we still read it so old .env files work.
    CTRL_KEEP_CONTEXT_KEYS: tuple = tuple(
        k.strip().lower()
        for k in os.getenv(
            "CTRL_KEEP_CONTEXT_KEYS",
            os.getenv("CTRL_FORCE_FRESH_KEYS", "f6"),
        ).split(",")
        if k.strip()
    )

    # Audio config
    SAMPLE_RATE: int = int(os.getenv("SAMPLE_RATE", "16000"))
    CHANNELS: int = int(os.getenv("CHANNELS", "1"))

    # Whisper config
    WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "tiny.en")
    WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "auto")
    # Silero VAD before decode. Off by default: hold-to-talk clips are already
    # bounded, and VAD adds latency plus a risk of clipping the first syllable.
    # Forced on for recordings >= 8s in transcriber.py, or set WHISPER_VAD=true.
    WHISPER_VAD: bool = _env_bool("WHISPER_VAD", "false")

    # LLM config
    # Flip LLM_PROVIDER between ollama / openrouter / meta / gemini / none to switch backends.
    # Aliases: meta-api, meta_api -> meta ; google, gemini-api -> gemini
    _raw_provider = os.getenv("LLM_PROVIDER", "meta").strip().lower().replace("-", "_")
    LLM_PROVIDER: Literal["ollama", "openrouter", "meta", "gemini", "none"] = (  # type: ignore
        "meta"
        if _raw_provider in ("meta", "meta_api")
        else "gemini"
        if _raw_provider in ("gemini", "gemini_api", "google", "google_api")
        else _raw_provider
    )
    # Ollama model tag (also used as fallback model id for openrouter if OPENROUTER_MODEL is blank)
    LLM_MODEL: str = _sanitize_model_id(
        os.getenv("LLM_MODEL", "qwen2.5:1.5b-instruct")
    )
    # OpenRouter-only model slug (e.g. google/gemini-2.0-flash-001). Preferred when provider=openrouter.
    OPENROUTER_MODEL: str = _sanitize_model_id(os.getenv("OPENROUTER_MODEL", ""))
    # Ollama OpenAI-compatible base. For openrouter, localhost is auto-rewritten in TextRefiner.
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
    # Canonical OpenRouter OpenAI-compatible endpoint (used when provider=openrouter)
    OPENROUTER_API_BASE: str = os.getenv(
        "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
    ).strip()
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "1024"))
    LLM_NUM_CTX: int = int(os.getenv("LLM_NUM_CTX", "2048"))
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    # Meta API (https://api.meta.ai/v1) — default provider
    META_API_KEY: str = os.getenv(
        "META_API_KEY", os.getenv("MODEL_API_KEY", "")
    ).strip()
    META_API_BASE: str = os.getenv(
        "META_API_BASE", "https://api.meta.ai/v1"
    ).strip().rstrip("/")
    META_MODEL: str = _sanitize_model_id(
        os.getenv("META_MODEL", os.getenv("META_API_MODEL", "muse-spark-1.2-contributor"))
    )
    META_REASONING_EFFORT: str = os.getenv("META_REASONING_EFFORT", "low").strip().lower()
    META_MAX_OUTPUT_TOKENS: int = int(os.getenv("META_MAX_OUTPUT_TOKENS", "4096"))
    # Google Gemini API (https://ai.google.dev/gemini-api) — Interactions API via google-genai SDK.
    # GEMINI_API_KEY is also accepted as alias for GOOGLE_API_KEY.
    GEMINI_API_KEY: str = os.getenv(
        "GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")
    ).strip()
    GEMINI_MODEL: str = _sanitize_model_id(
        os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    )
    GEMINI_THINKING_LEVEL: str = os.getenv("GEMINI_THINKING_LEVEL", "minimal").strip().lower()
    GEMINI_MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))

    @classmethod
    def effective_llm_model(cls) -> str:
        """Model id for the active provider (provider-specific model wins when set)."""
        if cls.LLM_PROVIDER == "openrouter" and cls.OPENROUTER_MODEL:
            return cls.OPENROUTER_MODEL
        if cls.LLM_PROVIDER == "meta" and cls.META_MODEL:
            return cls.META_MODEL
        if cls.LLM_PROVIDER == "gemini" and cls.GEMINI_MODEL:
            return cls.GEMINI_MODEL
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
        if cls.LLM_PROVIDER == "meta":
            return cls.META_API_BASE or "https://api.meta.ai/v1"
        if cls.LLM_PROVIDER == "gemini":
            # Gemini uses the google-genai SDK with the official endpoint;
            # no OpenAI-compatible base is involved.
            return "https://generativelanguage.googleapis.com"
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
        valid_providers = {"ollama", "openrouter", "meta", "gemini", "none"}
        if cls.LLM_PROVIDER not in valid_providers:
            raise ValueError(
                f"LLM_PROVIDER must be one of {valid_providers}, got '{cls.LLM_PROVIDER}'"
            )

        if cls.LLM_PROVIDER == "openrouter" and not cls.OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER is 'openrouter'"
            )
        if cls.LLM_PROVIDER == "meta" and not cls.META_API_KEY:
            print(
                "Warning: META_API_KEY (or MODEL_API_KEY) is empty while LLM_PROVIDER='meta'. "
                "AI mode will fall back to raw transcript until a key is set in .env.",
                flush=True,
            )
        if cls.LLM_PROVIDER == "gemini" and not cls.GEMINI_API_KEY:
            print(
                "Warning: GEMINI_API_KEY (or GOOGLE_API_KEY) is empty while LLM_PROVIDER='gemini'. "
                "AI mode will fall back to raw transcript until a key is set in .env.",
                flush=True,
            )
        if cls.META_REASONING_EFFORT not in ("low", "medium", "high", "none", ""):
            raise ValueError(f"META_REASONING_EFFORT must be low|medium|high|none, got {cls.META_REASONING_EFFORT!r}")
        if cls.META_MAX_OUTPUT_TOKENS < 64:
            raise ValueError(f"META_MAX_OUTPUT_TOKENS must be >= 64, got {cls.META_MAX_OUTPUT_TOKENS}")
        if cls.GEMINI_THINKING_LEVEL not in ("minimal", "low", "medium", "high", ""):
            raise ValueError(
                f"GEMINI_THINKING_LEVEL must be minimal|low|medium|high, got {cls.GEMINI_THINKING_LEVEL!r}"
            )
        if cls.GEMINI_MAX_OUTPUT_TOKENS < 64:
            raise ValueError(f"GEMINI_MAX_OUTPUT_TOKENS must be >= 64, got {cls.GEMINI_MAX_OUTPUT_TOKENS}")

        if cls.SAMPLE_RATE <= 0:
            raise ValueError(f"SAMPLE_RATE must be positive, got {cls.SAMPLE_RATE}")
        if cls.CHANNELS not in (1, 2):
            raise ValueError(f"CHANNELS must be 1 or 2, got {cls.CHANNELS}")
        if cls.LLM_MAX_TOKENS < 1:
            raise ValueError(f"LLM_MAX_TOKENS must be >= 1, got {cls.LLM_MAX_TOKENS}")
        if cls.LLM_NUM_CTX < 256:
            raise ValueError(f"LLM_NUM_CTX must be >= 256, got {cls.LLM_NUM_CTX}")

        dict_mods, dict_primary = parse_hold_hotkey(cls.HOTKEY)
        if not dict_mods:
            raise ValueError(
                f"HOTKEY '{cls.HOTKEY}' needs at least one modifier. A bare primary "
                "would be globally suppressed (the key could never be typed in any app)."
            )
        if cls.RESET_CONTEXT_HOTKEY:
            reset_key = cls.RESET_CONTEXT_HOTKEY.split("+")[-1].strip()
            if reset_key == dict_primary:
                raise ValueError(
                    f"RESET_CONTEXT_HOTKEY '{cls.RESET_CONTEXT_HOTKEY}' must not use "
                    f"the dictation primary key '{dict_primary}'"
                )
        if cls.AI_HOTKEY:
            validate_hotkey_pair(cls.HOTKEY, cls.AI_HOTKEY)
        if cls.AI_MODIFIER:
            if cls.AI_MODIFIER == dict_primary or cls.AI_MODIFIER in dict_mods:
                raise ValueError(
                    f"AI_MODIFIER '{cls.AI_MODIFIER}' must be distinct from HOTKEY parts "
                    f"({cls.HOTKEY})"
                )


# NOTE: no import-time Config.validate() here. Validating on import would fire
# when the setup web page or CLI imports this module (before the user has even
# saved a key), printing a confusing "META_API_KEY is empty" warning. The app
# entry point (main.py) validates explicitly at startup.
