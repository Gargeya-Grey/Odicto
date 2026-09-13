import os
import re
from typing import Literal, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file (override system env vars so the
# project's .env takes precedence over Windows user/system environment).
load_dotenv(override=True)


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Single source of truth for built-in defaults.
#
# Every supported environment variable is registered here with its built-in
# default. Config attributes below MUST pull their fallback from this table
# (never inline the literal again), and .env.example is required to agree
# with it (enforced by tests). That means a default can never silently drift
# between code and documentation.
#
# Resolution order for every setting:
#   1. provider-specific override (when that var was explicitly set)
#   2. generic key (LLM_*) — applies to whichever LLM_PROVIDER is active
#   3. the default in this table
# ---------------------------------------------------------------------------
ENV_DEFAULTS: dict[str, str] = {
    # Hotkeys
    "HOTKEY": "ctrl+grave",
    "AI_HOTKEY": "ctrl+shift+grave",
    "HOTKEY_TOGGLE": "true",
    "AI_MODIFIER": "",
    "RESET_CONTEXT_HOTKEY": "f5",
    "CTRL_KEEP_CONTEXT_KEYS": "f6",
    # Audio
    "SAMPLE_RATE": "16000",
    "CHANNELS": "1",
    # Whisper
    "WHISPER_MODEL_SIZE": "tiny.en",
    "WHISPER_DEVICE": "auto",
    "WHISPER_VAD": "false",
    # Speech-to-text backend (independent of LLM_PROVIDER)
    "STT_PROVIDER": "whisper",
    "LIVE_STT_PROVIDER": "auto",
    "GEMINI_TRANSCRIBE_MODEL": "gemini-3.5-transcribe",
    "GEMINI_TRANSCRIBE_LIVE_MODEL": "gemini-3.5-transcribe-live",
    "GEMINI_TRANSCRIBE_MODE": "smart",
    "GEMINI_TRANSCRIBE_LANGUAGE": "",
    "GEMINI_TRANSCRIBE_VOCABULARY": "",
    "LIVE_HOTKEY": "f7",
    # LLM — generic knobs (apply to the active provider)
    "LLM_PROVIDER": "none",
    "LLM_MODEL": "qwen2.5:1.5b-instruct",
    "LLM_API_BASE": "http://localhost:11434/v1",
    "LLM_MAX_TOKENS": "1024",
    "LLM_NUM_CTX": "2048",
    "LLM_REASONING_EFFORT": "",
    # OpenRouter overrides
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_API_BASE": "https://openrouter.ai/api/v1",
    "OPENROUTER_MODEL": "",
    "OPENROUTER_MODEL_HISTORY": "",
    # latency = lowest time-to-first-token host; throughput/price are the other sorts.
    "OPENROUTER_PROVIDER_SORT": "latency",
    # none = skip thinking when the model allows it (fastest). Mandatory-
    # reasoning SKUs (GLM-5.3) reject none; the client retries at low.
    "OPENROUTER_REASONING_EFFORT": "none",
    # Ollama override
    "OLLAMA_MODEL": "qwen2.5:1.5b-instruct",
    "OLLAMA_MODEL_HISTORY": "",
    # Meta overrides
    "META_API_KEY": "",
    "META_API_BASE": "https://api.meta.ai/v1",
    "META_MODEL": "muse-spark-1.3-contributor",
    "META_MODEL_HISTORY": "",
    "META_REASONING_EFFORT": "low",
    # Gemini overrides
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-3.5-flash-lite",
    "GEMINI_MODEL_HISTORY": "",
    "GEMINI_THINKING_LEVEL": "minimal",
    "GEMINI_MAX_OUTPUT_TOKENS": "4096",
    # Timing & feedback
    "PASTE_DELAY_SECONDS": "0.05",
    "PLAY_AUDIO_CUES": "true",
    "SHOW_VISUAL_INDICATOR": "true",
    "MIN_HOLD_MS": "80",
    "RETRIGGER_COOLDOWN_MS": "120",
    # Terminal text injection
    "TYPE_IN_TERMINAL": "true",
    "EXTRA_TERMINAL_APPS": "",
    # AI prompt
    "SYSTEM_PROMPT": "",
    "SYSTEM_PROMPT_FILE": "",
    # Setup page
    "SETUP_PORT": "8765",
}

# Every variable Odicto knows about (defaults + pure aliases). Used to warn
# about typo'd keys in .env instead of silently ignoring them forever.
KNOWN_ENV_KEYS: frozenset = frozenset(ENV_DEFAULTS)

# Model sent to OpenRouter when NOTHING is configured anywhere. Not an env
# default (OPENROUTER_MODEL must stay blank for the cascade), so it lives here;
# refiner.test_provider and the setup page reuse this constant.
OPENROUTER_FALLBACK_MODEL = "openai/gpt-5.6-luna"


def _def(key: str) -> str:
    """Built-in default for a key (single source: ENV_DEFAULTS)."""
    return ENV_DEFAULTS[key]


def _present(key: str) -> bool:
    """True if the env var had a non-blank value at import time (.env included).

    Blank values count as unset so ``KEY=`` lines behave like commented-out
    lines: the next tier in the cascade applies.
    """
    return bool((os.getenv(key) or "").strip())


# Which keys were explicitly provided when the module was imported. Computed
# once here because load_dotenv(override=True) above has already merged .env
# into os.environ.
_PRESENT_AT_IMPORT: frozenset = frozenset(k for k in ENV_DEFAULTS if _present(k))

# Import-time snapshot of resolved class attributes, filled right after the
# Config class body (see below). Resolvers treat "attribute differs from its
# import-time value" as an explicit programmatic override — that keeps
# ``patch.object(Config, ...)``, which the unit tests rely on, working as an
# override even when the same key was absent from the environment.
_IMPORT_SNAPSHOT: dict = {}


def _strip_secret_quotes(value: str) -> str:
    """Strip one layer of matching surrounding quotes from a pasted secret.

    Users often paste ``"sk-..."`` including the quotes. ``str.strip()`` alone
    keeps the quotes and causes ``Bearer "sk-..."`` auth failures.
    """
    v = value.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1].strip()
    return v


def _clean_secret(name: str) -> str:
    """Read a secret env var with surrounding-quote tolerance."""
    return _strip_secret_quotes(os.getenv(name, _def(name)))


def _default_env(name: str) -> str:
    """os.getenv with the ENV_DEFAULTS table as the only fallback source."""
    return os.getenv(name, _def(name))


# Default AI-mode instructions. Used when SYSTEM_PROMPT is blank in .env.
# Keep this paste-friendly: replies are inserted at the cursor.
# Speech-to-text (Whisper / Gemini Transcribe) already produced the query;
# this prompt is the assistant, not a second transcriber.
DEFAULT_SYSTEM_PROMPT = (
    "You are the user's personal assistant. Your reply is pasted at their cursor "
    "in whatever they are typing into. Speech-to-text already wrote the query. "
    "Do not transcribe, clean, or polish dictation. Do the work they asked for.\n"
    "\n"
    "VOICE\n"
    "Match how they sound. Casual stays casual. Precise stays precise. If they "
    "selected text, match that writing: formality, length, vocabulary, punctuation, "
    "unless they ask for a different register. Follow the instruction. Answer, "
    "draft, transform, or decide. Never echo the request back as tidied dictation. "
    "If something is ambiguous, make a small sensible call. Never refuse a benign "
    "request, never invent facts, never pad.\n"
    "\n"
    "HARD FORMAT\n"
    "Output PLAIN HUMAN-READABLE TEXT ONLY. No Markdown, no HTML.\n"
    "Never use # headings, **bold**, *italics*, `code`, fences, [links](url), or "
    "tables.\n"
    "Never wrap the answer in quotes or backticks.\n"
    "Never use an em dash, en dash, or a hyphen standing in for a dash. Use a "
    "period or a comma. Do not swap in parentheses to do the same job.\n"
    "Straight quotes only. No curly quotes. No decorative emoji.\n"
    "\n"
    "LISTS\n"
    "Do not start with a list. Open with a short intro that actually says something, "
    "then list only if the content needs one.\n"
    "Do not use a leading hyphen-minus for bullets (\"- item\"). That is the default "
    "AI look.\n"
    "When a list helps, mix markers: • for facts or items, → for steps, results, or "
    "cause to effect. Do not stamp the same marker on every line unless it is a "
    "tight sequence of one kind.\n"
    "Numbered lines (\"1. item\") are fine for ordered steps.\n"
    "A list of exactly three is a tell. Use the natural count.\n"
    "\n"
    "LENGTH AND TELL\n"
    "Be concise. Lead with the answer. Expand only when the question needs depth. "
    "Never ramble. Never cut mid-thought.\n"
    "Vary sentence length. Have a point of view when it fits. Be specific.\n"
    "Skip chatbot closers: happy to help, let me know if, certainly, of course, "
    "great question.\n"
    "Skip stock AI words: delve, pivotal, landscape, tapestry, testament, leverage, "
    "utilize, robust, seamless, groundbreaking.\n"
    "\n"
    "CONTEXT\n"
    "The system prompt or context block may include Selected text between <<< and "
    ">>>. That block is the document/context to act upon. The user's input is the "
    "instruction to execute.\n"
    "Do the instruction to the selected text. Produce only the final result that "
    "belongs in the user's field.\n"
    "Never repeat the instruction back. Never ignore the selected text when it is "
    "present.\n"
    "If there is no Selected text block, the spoken instruction is the whole "
    "request. Answer it as an assistant."
)

# Live private copy vs shipped default. Presence of prompt.txt is the source of
# truth for AI-mode instructions (setup page, runtime, agents).
PROMPT_LIVE_NAME = "prompt.txt"
PROMPT_EXAMPLE_NAME = "prompt.txt.example"


def _prompt_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read_utf8_prompt(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def prompt_live_path() -> str:
    return os.path.join(_prompt_dir(), PROMPT_LIVE_NAME)


def prompt_example_path() -> str:
    return os.path.join(_prompt_dir(), PROMPT_EXAMPLE_NAME)


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
    HOTKEY: str = _default_env("HOTKEY")
    AI_HOTKEY: str = _default_env("AI_HOTKEY").strip()
    # true = tap the chord to start, tap again to stop (like F7).
    # false = classic hold-to-talk: keep the chord down while speaking.
    HOTKEY_TOGGLE: bool = _env_bool("HOTKEY_TOGGLE", _def("HOTKEY_TOGGLE"))
    # Legacy optional third key (unused when AI_HOTKEY is set). Prefer AI_HOTKEY.
    AI_MODIFIER: str = _default_env("AI_MODIFIER").strip().lower()
    # Plain hotkey (no modifiers required) that clears the AI multi-turn memory
    # immediately, without a recording. Empty = disabled. 'f5' is the default.
    RESET_CONTEXT_HOTKEY: str = _default_env("RESET_CONTEXT_HOTKEY").strip().lower()
    # Extra keys held during a capture keep AI multi-turn memory (opt-in).
    # Default AI chord is always a fresh one-shot. Hold F6 with Ctrl+` (or the
    # AI chord) to continue the previous F6 conversation. Empty = disabled.
    CTRL_KEEP_CONTEXT_KEYS: tuple = tuple(
        k.strip().lower()
        for k in _default_env("CTRL_KEEP_CONTEXT_KEYS").split(",")
        if k.strip()
    )

    # Audio config
    SAMPLE_RATE: int = int(_default_env("SAMPLE_RATE"))
    CHANNELS: int = int(_default_env("CHANNELS"))

    # Whisper config
    WHISPER_MODEL_SIZE: str = _default_env("WHISPER_MODEL_SIZE")
    WHISPER_DEVICE: str = _default_env("WHISPER_DEVICE")
    # Silero VAD before decode. Off by default: hold-to-talk clips are already
    # bounded, and VAD adds latency plus a risk of clipping the first syllable.
    # Forced on for recordings >= 8s in transcriber.py, or set WHISPER_VAD=true.
    WHISPER_VAD: bool = _env_bool("WHISPER_VAD", _def("WHISPER_VAD"))

    # Speech-to-text backend. Independent of LLM_PROVIDER: Gemini STT can run
    # while AI replies still go through Meta/OpenRouter/Ollama/none.
    # whisper | gemini | auto  (auto = Gemini when GEMINI_API_KEY is set)
    _raw_stt = os.getenv("STT_PROVIDER", _def("STT_PROVIDER")).strip().lower().replace("-", "_")
    STT_PROVIDER: Literal["whisper", "gemini", "auto"] = (  # type: ignore
        "gemini"
        if _raw_stt in ("gemini", "gemini_api", "google", "google_api")
        else "auto"
        if _raw_stt == "auto"
        else "whisper"
    )
    _raw_live_stt = os.getenv("LIVE_STT_PROVIDER", _def("LIVE_STT_PROVIDER")).strip().lower().replace("-", "_")
    LIVE_STT_PROVIDER: Literal["whisper", "gemini", "auto"] = (  # type: ignore
        "gemini"
        if _raw_live_stt in ("gemini", "gemini_api", "google", "google_api")
        else "whisper"
        if _raw_live_stt == "whisper"
        else "auto"
    )
    GEMINI_TRANSCRIBE_MODEL: str = _sanitize_model_id(
        _default_env("GEMINI_TRANSCRIBE_MODEL")
    )
    GEMINI_TRANSCRIBE_LIVE_MODEL: str = _sanitize_model_id(
        _default_env("GEMINI_TRANSCRIBE_LIVE_MODEL")
    )
    _raw_stt_mode = _default_env("GEMINI_TRANSCRIBE_MODE").strip().lower()
    GEMINI_TRANSCRIBE_MODE: Literal["smart", "verbatim"] = (  # type: ignore
        "verbatim" if _raw_stt_mode in ("verbatim", "off", "false", "0") else "smart"
    )
    GEMINI_TRANSCRIBE_LANGUAGE: str = _default_env("GEMINI_TRANSCRIBE_LANGUAGE").strip()
    GEMINI_TRANSCRIBE_VOCABULARY: str = _default_env("GEMINI_TRANSCRIBE_VOCABULARY").strip()
    # Tap-to-talk (press once to start, press again to stop and paste). Empty disables.
    LIVE_HOTKEY: str = _default_env("LIVE_HOTKEY").strip().lower()

    # LLM config
    # Flip LLM_PROVIDER between ollama / openrouter / meta / gemini / none to switch backends.
    # Aliases: meta-api, meta_api -> meta ; google, gemini-api -> gemini
    _raw_provider = os.getenv("LLM_PROVIDER", _def("LLM_PROVIDER")).strip().lower().replace("-", "_")
    LLM_PROVIDER: Literal["ollama", "openrouter", "meta", "gemini", "none"] = (  # type: ignore
        "meta"
        if _raw_provider in ("meta", "meta_api")
        else "gemini"
        if _raw_provider in ("gemini", "gemini_api", "google", "google_api")
        else _raw_provider
    )
    # Generic model id — applies to whichever provider is active. Provider
    # specific *_MODEL vars are optional overrides that win when explicitly set.
    LLM_MODEL: str = _sanitize_model_id(_default_env("LLM_MODEL"))
    OLLAMA_MODEL: str = _sanitize_model_id(_default_env("OLLAMA_MODEL"))
    # Ollama OpenAI-compatible base. For openrouter, localhost is auto-rewritten in TextRefiner.
    LLM_API_BASE: str = _default_env("LLM_API_BASE")
    # Canonical OpenRouter OpenAI-compatible endpoint (used when provider=openrouter)
    OPENROUTER_API_BASE: str = _default_env("OPENROUTER_API_BASE").strip()
    # OpenRouter-only model slug override (e.g. openai/gpt-5.6-luna).
    OPENROUTER_MODEL: str = _sanitize_model_id(_default_env("OPENROUTER_MODEL"))
    # Generic output cap for EVERY provider; GEMINI_MAX_OUTPUT_TOKENS remains
    # as a per-provider ceiling override. Meta reasoning is uncapped (the
    # effort knob is the only control), so Meta has no output-cap override.
    LLM_MAX_TOKENS: int = int(_default_env("LLM_MAX_TOKENS"))
    # Ollama context window (higher = smarter multi-turn, slightly slower)
    LLM_NUM_CTX: int = int(_default_env("LLM_NUM_CTX"))
    OPENROUTER_API_KEY: str = _clean_secret("OPENROUTER_API_KEY")
    OPENROUTER_PROVIDER_SORT: str = _default_env("OPENROUTER_PROVIDER_SORT").strip().lower()
    OPENROUTER_REASONING_EFFORT: str = _default_env(
        "OPENROUTER_REASONING_EFFORT"
    ).strip().lower()
    META_API_KEY: str = _clean_secret("META_API_KEY")
    META_API_BASE: str = _default_env("META_API_BASE").strip().rstrip("/")
    META_MODEL: str = _sanitize_model_id(_default_env("META_MODEL"))
    META_REASONING_EFFORT: str = _default_env("META_REASONING_EFFORT").strip().lower()
    # Google Gemini API (https://ai.google.dev/gemini-api) — Interactions API via google-genai SDK.
    GEMINI_API_KEY: str = _clean_secret("GEMINI_API_KEY")
    GEMINI_MODEL: str = _sanitize_model_id(_default_env("GEMINI_MODEL"))
    GEMINI_THINKING_LEVEL: str = _default_env("GEMINI_THINKING_LEVEL").strip().lower()
    GEMINI_MAX_OUTPUT_TOKENS: int = int(_default_env("GEMINI_MAX_OUTPUT_TOKENS"))
    # Unified reasoning knob. Mapped onto Meta's reasoning.effort, Gemini's
    # thinking_level, and OpenRouter's reasoning.effort. Ignored by ollama.
    # Provider-specific knobs (META_REASONING_EFFORT / GEMINI_THINKING_LEVEL /
    # OPENROUTER_REASONING_EFFORT) override when set.
    LLM_REASONING_EFFORT: str = _default_env("LLM_REASONING_EFFORT").strip().lower()
    # AI-mode system prompt. Runtime prefers prompt.txt, then prompt.txt.example.
    # These .env keys are kept for setup Save (pointer vs empty) and legacy installs.
    SYSTEM_PROMPT: str = _default_env("SYSTEM_PROMPT").strip() or DEFAULT_SYSTEM_PROMPT
    SYSTEM_PROMPT_FILE: str = _default_env("SYSTEM_PROMPT_FILE").strip()

    # Timing & Feedback
    PASTE_DELAY_SECONDS: float = float(_default_env("PASTE_DELAY_SECONDS"))
    PLAY_AUDIO_CUES: bool = _env_bool("PLAY_AUDIO_CUES", _def("PLAY_AUDIO_CUES"))
    SHOW_VISUAL_INDICATOR: bool = _env_bool(
        "SHOW_VISUAL_INDICATOR", _def("SHOW_VISUAL_INDICATOR")
    )
    # Minimum hold time (ms) before a recording is accepted — filters accidental taps
    MIN_HOLD_MS: int = int(_default_env("MIN_HOLD_MS"))
    # Debounce between consecutive capture cycles (ms)
    RETRIGGER_COOLDOWN_MS: int = int(_default_env("RETRIGGER_COOLDOWN_MS"))

    # Terminals have no shared paste chord and treat Ctrl+C as SIGINT, so when
    # the focused window is one Odicto types the text instead of pasting it.
    TYPE_IN_TERMINAL: bool = _env_bool("TYPE_IN_TERMINAL", _def("TYPE_IN_TERMINAL"))
    # Comma-separated window classes / process names that also count as
    # terminals — the escape hatch for one this build does not know.
    EXTRA_TERMINAL_APPS: tuple = tuple(
        part.strip().lower()
        for part in _default_env("EXTRA_TERMINAL_APPS").split(",")
        if part.strip()
    )

    # provider -> the attribute that overrides the generic tier. Adding a provider
    # is a one-row change in these two tables.
    _REASONING_ATTR = {
        "meta": "META_REASONING_EFFORT",
        "gemini": "GEMINI_THINKING_LEVEL",
        "openrouter": "OPENROUTER_REASONING_EFFORT",
    }
    _MODEL_ATTR = {
        "ollama": "OLLAMA_MODEL",
        "openrouter": "OPENROUTER_MODEL",
        "meta": "META_MODEL",
        "gemini": "GEMINI_MODEL",
    }

    @classmethod
    def _provider_override(cls, attr_map: dict, require_nonblank: bool = False) -> str:
        """The active provider's override attribute, or "" when it was not set explicitly.

        Mirrors the historical per-provider ladder: an override only wins when
        ``_explicit()`` reports it as provided, so ``patch.object(Config, ...)`` in the
        unit tests keeps counting as an override.
        """
        attr = attr_map.get(cls.LLM_PROVIDER)
        if not attr or not cls._explicit(attr, (attr,)):
            return ""
        value = getattr(cls, attr)
        if require_nonblank and not value.strip():
            return ""
        return value if value else ""

    @classmethod
    def _explicit(cls, attr: str, env_keys: tuple = ()) -> bool:
        """True when a setting was explicitly provided (not just a default).

        Two ways to be explicit:
          1. any of ``env_keys`` had a non-blank value at import time;
          2. the class attribute was changed after import — this is how
             ``patch.object(Config, ...)`` in tests marks an override.
        """
        if any(k in _PRESENT_AT_IMPORT for k in env_keys):
            return True
        if attr in _IMPORT_SNAPSHOT:
            return getattr(cls, attr, None) != _IMPORT_SNAPSHOT[attr]
        return False

    @classmethod
    def effective_llm_model(cls) -> str:
        """Model id for the active provider.

        Cascade: provider-specific *_MODEL override → generic LLM_MODEL →
        built-in provider default.
        """
        provider = cls.LLM_PROVIDER
        override = cls._provider_override(cls._MODEL_ATTR, require_nonblank=True)
        if override:
            return override
        if cls.LLM_MODEL.strip():
            return cls.LLM_MODEL
        return {
            "ollama": _def("LLM_MODEL"),
            "openrouter": OPENROUTER_FALLBACK_MODEL,
            "meta": _def("META_MODEL"),
            "gemini": _def("GEMINI_MODEL"),
        }.get(provider, "")

    @classmethod
    def effective_llm_api_base(cls) -> str:
        """API base for the active provider."""
        if cls.LLM_PROVIDER == "openrouter":
            base = cls.LLM_API_BASE
            # Keep a custom base if the user pointed LLM_API_BASE at a non-local proxy.
            if "localhost" in base or "127.0.0.1" in base or not base:
                return cls.OPENROUTER_API_BASE or _def("OPENROUTER_API_BASE")
            return base
        if cls.LLM_PROVIDER == "meta":
            return cls.META_API_BASE or _def("META_API_BASE")
        if cls.LLM_PROVIDER == "gemini":
            # Gemini uses the google-genai SDK with the official endpoint;
            # no OpenAI-compatible base is involved.
            return "https://generativelanguage.googleapis.com"
        return cls.LLM_API_BASE

    @classmethod
    def effective_api_key(cls) -> str:
        """Credential for the active provider (its own key, or its alias).

        Each provider owns its key — saved once, remembered across provider
        switches. Empty string means not configured yet.
        """
        provider = cls.LLM_PROVIDER
        if provider == "openrouter":
            if cls.OPENROUTER_API_KEY.strip():
                return cls.OPENROUTER_API_KEY.strip()
        elif provider == "meta":
            if cls.META_API_KEY.strip():
                return cls.META_API_KEY.strip()
        elif provider == "gemini":
            if cls.GEMINI_API_KEY.strip():
                return cls.GEMINI_API_KEY.strip()
        return ""

    @classmethod
    def effective_max_output_tokens(cls) -> int:
        """Output token cap for the active provider.

        Cascade: provider-specific ceiling override → generic LLM_MAX_TOKENS,
        floored at 64 either way (matches the historical hard floor).
        """
        provider = cls.LLM_PROVIDER
        try:
            if provider == "gemini" and cls._explicit(
                "GEMINI_MAX_OUTPUT_TOKENS", ("GEMINI_MAX_OUTPUT_TOKENS",)
            ):
                return max(64, int(cls.GEMINI_MAX_OUTPUT_TOKENS))
            return max(64, int(cls.LLM_MAX_TOKENS))
        except (TypeError, ValueError):
            return max(64, int(_def("LLM_MAX_TOKENS")))

    @classmethod
    def effective_reasoning_effort(cls) -> str:
        """Raw unified reasoning effort for the active provider.

        Cascade: META_REASONING_EFFORT / GEMINI_THINKING_LEVEL /
        OPENROUTER_REASONING_EFFORT override → LLM_REASONING_EFFORT →
        provider-appropriate default. Use meta_reasoning_effort() /
        gemini_thinking_level() / openrouter_reasoning_effort() for the
        mapped, provider-safe value.
        """
        override = cls._provider_override(cls._REASONING_ATTR)
        if override:
            return override
        if cls.LLM_REASONING_EFFORT:
            return cls.LLM_REASONING_EFFORT
        attr = cls._REASONING_ATTR.get(cls.LLM_PROVIDER)
        return _def(attr) if attr else ""

    @classmethod
    def meta_reasoning_effort(cls) -> str:
        """Meta-API-safe reasoning effort (minimal→low clamp; invalid→low)."""
        effort = (cls.effective_reasoning_effort() or "low").strip().lower()
        if effort == "minimal":
            return "low"
        return effort if effort in ("low", "medium", "high", "none") else "low"

    @classmethod
    def gemini_thinking_level(cls) -> str:
        """Gemini-safe thinking level (none→minimal clamp; invalid→minimal)."""
        level = (cls.effective_reasoning_effort() or "minimal").strip().lower()
        if level in ("minimal", "low", "medium", "high"):
            return level
        return "minimal"

    @classmethod
    def openrouter_reasoning_effort(cls) -> str:
        """OpenRouter-safe reasoning effort (invalid→none, the fastest default).

        ``none`` disables thinking when the model allows it. Models that
        require reasoning reject ``none`` (GLM-5.3 accepts only
        ``low``/``high``/``max``). The OpenRouter client retries at ``low``.
        """
        if cls.LLM_PROVIDER == "openrouter":
            effort = (cls.effective_reasoning_effort() or "none").strip().lower()
        elif (
            cls._explicit(
                "OPENROUTER_REASONING_EFFORT", ("OPENROUTER_REASONING_EFFORT",)
            )
            and cls.OPENROUTER_REASONING_EFFORT
        ):
            effort = cls.OPENROUTER_REASONING_EFFORT.strip().lower()
        else:
            effort = (_def("OPENROUTER_REASONING_EFFORT") or "none").strip().lower()
        if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            return effort
        return "none"

    @classmethod
    def openrouter_provider_sort(cls) -> str:
        """OpenRouter provider.sort (latency|throughput|price; invalid→latency)."""
        sort = (cls.OPENROUTER_PROVIDER_SORT or "latency").strip().lower()
        if sort in ("latency", "throughput", "price"):
            return sort
        return "latency"

    @classmethod
    def openrouter_extra_body(cls, effort: str | None = None) -> dict:
        """Chat Completions extras: lowest-latency host + lowest thinking.

        OpenRouter's default routing is cheapest, not fastest. ``sort=latency``
        picks the host with the lowest time-to-first-token for the chosen
        model. ``reasoning.effort`` defaults to ``none``. Pass ``effort`` to
        override for a single call (mandatory-reasoning retry uses ``low``).
        """
        chosen = (effort or cls.openrouter_reasoning_effort()).strip().lower()
        if chosen not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            chosen = cls.openrouter_reasoning_effort()
        return {
            "provider": {"sort": cls.openrouter_provider_sort()},
            "reasoning": {"effort": chosen},
        }

    @classmethod
    def effective_stt_provider(cls) -> Literal["whisper", "gemini"]:
        """Resolved STT backend: gemini only when a Gemini key is present."""
        if cls.STT_PROVIDER == "whisper":
            return "whisper"
        if cls.GEMINI_API_KEY.strip():
            return "gemini"
        return "whisper"

    @classmethod
    def effective_live_stt_provider(cls) -> Literal["whisper", "gemini"]:
        """Resolved STT backend for F7 Live Tap-to-Talk."""
        if cls.LIVE_STT_PROVIDER == "whisper":
            return "whisper"
        if cls.LIVE_STT_PROVIDER == "gemini":
            return "gemini" if cls.GEMINI_API_KEY.strip() else "whisper"
        # auto:
        if cls.GEMINI_API_KEY.strip():
            return "gemini"
        return "whisper"

    @classmethod
    def gemini_transcribe_mode(cls) -> Literal["smart", "verbatim"]:
        """Smart (cleaned dictation) or verbatim (literal).

        Applies to the dictation chord and F7 live tap. The AI chord uses local
        Whisper instead (the LLM is the cleanup step).
        """
        mode = (cls.GEMINI_TRANSCRIBE_MODE or "smart").strip().lower()
        return "verbatim" if mode == "verbatim" else "smart"

    @classmethod
    def gemini_transcribe_language_codes(cls) -> list:
        """BCP-47 hints for Gemini STT. Empty list → automatic detection."""
        raw = (cls.GEMINI_TRANSCRIBE_LANGUAGE or "").strip()
        if not raw:
            return []
        codes = []
        seen = set()
        for tok in raw.replace(";", ",").split(","):
            code = tok.strip()
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes

    @classmethod
    def gemini_transcribe_vocabulary(cls) -> list:
        """Custom vocabulary terms (best ≤100). Empty when unset."""
        raw = (cls.GEMINI_TRANSCRIBE_VOCABULARY or "").strip()
        if not raw:
            return []
        terms = []
        seen = set()
        for tok in raw.split(","):
            term = tok.strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
            if len(terms) >= 100:
                break
        return terms

    @classmethod
    def prompt_source_label(cls) -> str:
        """Where the live AI prompt is coming from (for setup + `odicto config`)."""
        if _read_utf8_prompt(prompt_live_path()):
            return "prompt.txt"
        file_ref = (cls.SYSTEM_PROMPT_FILE or "").strip()
        if file_ref and file_ref.replace("\\", "/") not in (
            PROMPT_LIVE_NAME,
            f"./{PROMPT_LIVE_NAME}",
        ):
            if _read_utf8_prompt(
                file_ref
                if os.path.isabs(file_ref)
                else os.path.join(_prompt_dir(), file_ref)
            ):
                return f".env (SYSTEM_PROMPT_FILE={file_ref})"
        if cls._explicit("SYSTEM_PROMPT", ("SYSTEM_PROMPT",)):
            raw = (cls.SYSTEM_PROMPT or "").strip()
            if raw and raw != DEFAULT_SYSTEM_PROMPT.strip():
                return ".env (SYSTEM_PROMPT)"
        if _read_utf8_prompt(prompt_example_path()):
            return "prompt.txt.example"
        return "built-in default"

    @classmethod
    def effective_system_prompt(cls) -> str:
        """AI instructions: prompt.txt → example → built-in default.

        ``prompt.txt`` is the private live copy (gitignored). If it is missing,
        ``prompt.txt.example`` (shipped, matches DEFAULT_SYSTEM_PROMPT) is used.
        Legacy ``SYSTEM_PROMPT_FILE`` / inline ``SYSTEM_PROMPT`` are read only
        when ``prompt.txt`` is absent, until the next setup Save migrates them.
        """
        live = _read_utf8_prompt(prompt_live_path())
        if live:
            return live

        file_ref = (cls.SYSTEM_PROMPT_FILE or "").strip()
        normalized = file_ref.replace("\\", "/").lstrip("./")
        if file_ref and normalized != PROMPT_LIVE_NAME:
            path = (
                file_ref
                if os.path.isabs(file_ref)
                else os.path.join(_prompt_dir(), file_ref)
            )
            legacy = _read_utf8_prompt(path)
            if legacy:
                print(
                    f"Notice: using SYSTEM_PROMPT_FILE '{file_ref}'. "
                    f"Save setup to move this into {PROMPT_LIVE_NAME}.",
                    flush=True,
                )
                return legacy

        if cls._explicit("SYSTEM_PROMPT", ("SYSTEM_PROMPT",)):
            raw = (cls.SYSTEM_PROMPT or "").strip()
            if raw and raw != DEFAULT_SYSTEM_PROMPT.strip():
                print(
                    f"Notice: using inline SYSTEM_PROMPT from .env. "
                    f"Save setup to move this into {PROMPT_LIVE_NAME}.",
                    flush=True,
                )
                return raw

        example = _read_utf8_prompt(prompt_example_path())
        if example:
            return example
        return DEFAULT_SYSTEM_PROMPT.strip()

    @classmethod
    def explain(cls) -> list:
        """Resolved settings for display: one row dict per meaningful knob.

        Each row: {group, label, value, source}. ``source`` names the cascade
        tier that won ('.env (KEY)' or 'default'); secrets are masked to a
        set/unset marker. Presentation lives in the CLI, data lives here.
        """
        rows: list = []

        def add(group: str, label: str, value, source: str, secret: bool = False) -> None:
            rows.append(
                {
                    "group": group,
                    "label": label,
                    "value": "<set>" if secret and value else ("(empty)" if secret else value),
                    "source": source,
                    "secret": secret,
                }
            )

        provider = cls.LLM_PROVIDER
        add("Provider", "LLM_PROVIDER", provider, _source_of("LLM_PROVIDER"))

        key_env_keys = {
            "openrouter": ("OPENROUTER_API_KEY",),
            "meta": ("META_API_KEY",),
            "gemini": ("GEMINI_API_KEY",),
        }.get(provider, ())
        if key_env_keys and cls.effective_api_key():
            key_src = _source_of(*key_env_keys)
        elif provider in ("none", "ollama"):
            key_src = "n/a (no key needed)" if provider == "ollama" else "n/a (raw dictation)"
        else:
            key_src = "default"
        add(
            "Provider",
            "API key",
            cls.effective_api_key(),
            key_src,
            secret=True,
        )
        add(
            "Provider",
            "API base",
            cls.effective_llm_api_base(),
            {
                "openrouter": _source_of("OPENROUTER_API_BASE", "LLM_API_BASE"),
                "meta": _source_of("META_API_BASE"),
                # Gemini always uses the official endpoint baked into the SDK.
                "gemini": "built-in (Google endpoint)",
                "ollama": _source_of("LLM_API_BASE"),
            }.get(cls.LLM_PROVIDER, _source_of("LLM_API_BASE")),
        )

        model = cls.effective_llm_model()
        override_attr = {
            "ollama": "OLLAMA_MODEL",
            "openrouter": "OPENROUTER_MODEL",
            "meta": "META_MODEL",
            "gemini": "GEMINI_MODEL",
        }.get(provider)
        model_src = (
            f".env ({override_attr})"
            if override_attr
            and cls._explicit(override_attr, (override_attr,))
            and getattr(cls, override_attr).strip()
            else (_source_of("LLM_MODEL") if cls.LLM_MODEL.strip() else "default")
        )
        add("Model & generation", "Model", model, model_src)
        cap = cls.effective_max_output_tokens()
        cap_override = {"gemini": "GEMINI_MAX_OUTPUT_TOKENS"}.get(provider)
        cap_src = (
            f".env ({cap_override})"
            if cap_override and cls._explicit(cap_override, (cap_override,))
            else _source_of("LLM_MAX_TOKENS")
        )
        add("Model & generation", "Max output tokens", cap, cap_src)
        effort_display = {
            "meta": cls.meta_reasoning_effort,
            "gemini": cls.gemini_thinking_level,
            "openrouter": cls.openrouter_reasoning_effort,
        }.get(provider)
        if effort_display is not None:
            eff_override = {
                "meta": "META_REASONING_EFFORT",
                "gemini": "GEMINI_THINKING_LEVEL",
                "openrouter": "OPENROUTER_REASONING_EFFORT",
            }.get(provider)
            eff_src = (
                f".env ({eff_override})"
                if cls._explicit(eff_override, (eff_override,))
                else (
                    _source_of("LLM_REASONING_EFFORT")
                    if cls.LLM_REASONING_EFFORT
                    else "default"
                )
            )
            add("Model & generation", "Reasoning effort", effort_display(), eff_src)
        if provider == "openrouter":
            add(
                "Model & generation",
                "OpenRouter provider sort",
                cls.openrouter_provider_sort(),
                _source_of("OPENROUTER_PROVIDER_SORT"),
            )
        if provider == "ollama":
            add("Model & generation", "Ollama context window", cls.LLM_NUM_CTX, _source_of("LLM_NUM_CTX"))

        add("Hotkeys", "Dictation chord", cls.HOTKEY, _source_of("HOTKEY"))
        if cls.AI_HOTKEY.strip():
            ai_hotkey_display = cls.AI_HOTKEY
        elif cls.AI_MODIFIER:
            ai_hotkey_display = f"{cls.HOTKEY}+{cls.AI_MODIFIER}"
        else:
            ai_hotkey_display = "(none - AI mode off)"
        add(
            "Hotkeys",
            "AI reply chord",
            ai_hotkey_display,
            _source_of("AI_HOTKEY"),
        )
        add(
            "Hotkeys",
            "Chord tap-to-toggle",
            cls.HOTKEY_TOGGLE,
            _source_of("HOTKEY_TOGGLE"),
        )
        add(
            "Hotkeys",
            "Reset-context key",
            cls.RESET_CONTEXT_HOTKEY or "(disabled)",
            _source_of("RESET_CONTEXT_HOTKEY"),
        )
        keep = ", ".join(k for k in cls.CTRL_KEEP_CONTEXT_KEYS if k) or "(disabled)"
        add(
            "Hotkeys",
            "Keep-memory keys",
            keep,
            _source_of("CTRL_KEEP_CONTEXT_KEYS"),
        )
        add(
            "Hotkeys",
            "Live tap-to-talk key",
            cls.LIVE_HOTKEY or "(disabled)",
            _source_of("LIVE_HOTKEY"),
        )

        add("Audio & Whisper", "Sample rate", cls.SAMPLE_RATE, _source_of("SAMPLE_RATE"))
        add("Audio & Whisper", "Channels", cls.CHANNELS, _source_of("CHANNELS"))
        add("Audio & Whisper", "Whisper model", cls.WHISPER_MODEL_SIZE, _source_of("WHISPER_MODEL_SIZE"))
        add("Audio & Whisper", "Whisper device", cls.WHISPER_DEVICE, _source_of("WHISPER_DEVICE"))
        add("Audio & Whisper", "Whisper VAD", cls.WHISPER_VAD, _source_of("WHISPER_VAD"))
        add("Speech to text", "STT provider", cls.STT_PROVIDER, _source_of("STT_PROVIDER"))
        add(
            "Speech to text",
            "Resolved STT",
            cls.effective_stt_provider(),
            _source_of("STT_PROVIDER"),
        )
        add("Speech to text", "Live STT provider", cls.LIVE_STT_PROVIDER, _source_of("LIVE_STT_PROVIDER"))
        add(
            "Speech to text",
            "Resolved live STT",
            cls.effective_live_stt_provider(),
            _source_of("LIVE_STT_PROVIDER"),
        )
        add(
            "Speech to text",
            "Transcribe mode",
            cls.gemini_transcribe_mode(),
            _source_of("GEMINI_TRANSCRIBE_MODE"),
        )
        add(
            "Speech to text",
            "Transcribe model",
            cls.GEMINI_TRANSCRIBE_MODEL,
            _source_of("GEMINI_TRANSCRIBE_MODEL"),
        )
        add(
            "Speech to text",
            "Live transcribe model",
            cls.GEMINI_TRANSCRIBE_LIVE_MODEL,
            _source_of("GEMINI_TRANSCRIBE_LIVE_MODEL"),
        )
        lang = ", ".join(cls.gemini_transcribe_language_codes()) or "(auto)"
        add(
            "Speech to text",
            "Language",
            lang,
            _source_of("GEMINI_TRANSCRIBE_LANGUAGE"),
        )
        vocab_n = len(cls.gemini_transcribe_vocabulary())
        add(
            "Speech to text",
            "Custom vocabulary",
            f"{vocab_n} term(s)" if vocab_n else "(none)",
            _source_of("GEMINI_TRANSCRIBE_VOCABULARY"),
        )

        add("Timing", "Paste delay (s)", cls.PASTE_DELAY_SECONDS, _source_of("PASTE_DELAY_SECONDS"))
        add("Timing", "Audio cues", cls.PLAY_AUDIO_CUES, _source_of("PLAY_AUDIO_CUES"))
        add("Timing", "Visual HUD", cls.SHOW_VISUAL_INDICATOR, _source_of("SHOW_VISUAL_INDICATOR"))
        add("Timing", "Min hold (ms)", cls.MIN_HOLD_MS, _source_of("MIN_HOLD_MS"))
        add("Timing", "Retrigger cooldown (ms)", cls.RETRIGGER_COOLDOWN_MS, _source_of("RETRIGGER_COOLDOWN_MS"))
        add("Timing", "Type in terminal", cls.TYPE_IN_TERMINAL, _source_of("TYPE_IN_TERMINAL"))
        add(
            "Timing",
            "Extra terminal apps",
            ", ".join(cls.EXTRA_TERMINAL_APPS) or "(none)",
            _source_of("EXTRA_TERMINAL_APPS"),
        )

        prompt = cls.effective_system_prompt()
        prompt_src = cls.prompt_source_label()
        ascii_preview = prompt[:64].encode("ascii", "replace").decode("ascii")
        preview = ascii_preview + ("..." if len(prompt) > 64 else "")
        add("Prompt", "System prompt", preview, prompt_src)

        return rows

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

        for warning in config_warnings():
            print(f"Warning: {warning}", flush=True)

        if cls.LLM_PROVIDER == "openrouter" and not cls.effective_api_key():
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER is 'openrouter'"
            )
        if cls.LLM_PROVIDER == "meta" and not cls.effective_api_key():
            print(
                "Warning: META_API_KEY is empty while "
                "LLM_PROVIDER='meta'. AI mode will fall back to raw transcript "
                "until a key is set in .env.",
                flush=True,
            )
        if cls.LLM_PROVIDER == "gemini" and not cls.effective_api_key():
            print(
                "Warning: GEMINI_API_KEY is empty while "
                "LLM_PROVIDER='gemini'. AI mode will fall back to raw transcript "
                "until a key is set in .env.",
                flush=True,
            )
        if cls.LLM_REASONING_EFFORT and cls.LLM_REASONING_EFFORT not in (
            "minimal",
            "low",
            "medium",
            "high",
            "none",
        ):
            raise ValueError(
                "LLM_REASONING_EFFORT must be minimal|low|medium|high|none, got "
                f"{cls.LLM_REASONING_EFFORT!r}"
            )
        if cls.META_REASONING_EFFORT not in ("low", "medium", "high", "none", ""):
            raise ValueError(f"META_REASONING_EFFORT must be low|medium|high|none, got {cls.META_REASONING_EFFORT!r}")
        if cls.GEMINI_THINKING_LEVEL not in ("minimal", "low", "medium", "high", ""):
            raise ValueError(
                f"GEMINI_THINKING_LEVEL must be minimal|low|medium|high, got {cls.GEMINI_THINKING_LEVEL!r}"
            )
        if cls.OPENROUTER_REASONING_EFFORT not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "",
        ):
            raise ValueError(
                "OPENROUTER_REASONING_EFFORT must be "
                "none|minimal|low|medium|high|xhigh|max, got "
                f"{cls.OPENROUTER_REASONING_EFFORT!r}"
            )
        if cls.OPENROUTER_PROVIDER_SORT not in (
            "latency",
            "throughput",
            "price",
            "",
        ):
            raise ValueError(
                "OPENROUTER_PROVIDER_SORT must be latency|throughput|price, got "
                f"{cls.OPENROUTER_PROVIDER_SORT!r}"
            )
        if cls.GEMINI_MAX_OUTPUT_TOKENS < 64:
            raise ValueError(f"GEMINI_MAX_OUTPUT_TOKENS must be >= 64, got {cls.GEMINI_MAX_OUTPUT_TOKENS}")
        if cls.STT_PROVIDER not in ("whisper", "gemini", "auto"):
            raise ValueError(
                f"STT_PROVIDER must be whisper|gemini|auto, got {cls.STT_PROVIDER!r}"
            )
        if cls.LIVE_STT_PROVIDER not in ("whisper", "gemini", "auto"):
            raise ValueError(
                f"LIVE_STT_PROVIDER must be whisper|gemini|auto, got {cls.LIVE_STT_PROVIDER!r}"
            )
        if cls.GEMINI_TRANSCRIBE_MODE not in ("smart", "verbatim"):
            raise ValueError(
                f"GEMINI_TRANSCRIBE_MODE must be smart|verbatim, got {cls.GEMINI_TRANSCRIBE_MODE!r}"
            )
        if cls.STT_PROVIDER == "gemini" and not cls.GEMINI_API_KEY.strip():
            print(
                "Warning: GEMINI_API_KEY is empty while STT_PROVIDER='gemini'. "
                "Speech-to-text will fall back to local Whisper until a key is set.",
                flush=True,
            )

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
        live_key = (cls.LIVE_HOTKEY or "").split("+")[-1].strip()
        if live_key:
            if live_key == dict_primary:
                raise ValueError(
                    f"LIVE_HOTKEY '{cls.LIVE_HOTKEY}' must not use the dictation "
                    f"primary key '{dict_primary}'"
                )
            reset_key = (
                cls.RESET_CONTEXT_HOTKEY.split("+")[-1].strip()
                if cls.RESET_CONTEXT_HOTKEY
                else ""
            )
            if reset_key and live_key == reset_key:
                raise ValueError(
                    f"LIVE_HOTKEY '{cls.LIVE_HOTKEY}' must be distinct from "
                    f"RESET_CONTEXT_HOTKEY '{cls.RESET_CONTEXT_HOTKEY}'"
                )


# Snapshot resolved attribute values once, right after the class body. Used by
# Config._explicit() so post-import mutations (unit-test patch.object calls)
# count as explicit overrides of a cascade tier. Only the attributes that
# _explicit() actually consults are listed here.
_IMPORT_SNAPSHOT.update(
    {
        name: getattr(Config, name)
        for name in (
            "OLLAMA_MODEL",
            "OPENROUTER_MODEL",
            "META_MODEL",
            "META_REASONING_EFFORT",
            "GEMINI_MODEL",
            "GEMINI_THINKING_LEVEL",
            "GEMINI_MAX_OUTPUT_TOKENS",
        )
    }
)


def load_env_file_keys() -> dict:
    """Parse the install's .env into {key: value} without touching os.environ.

    Returns an empty dict when python-dotenv is missing or .env doesn't exist,
    so callers (warnings, CLI) degrade gracefully.
    """
    try:
        from dotenv import dotenv_values
    except Exception:  # pragma: no cover
        return {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return {}
    try:
        return {k: v for k, v in (dotenv_values(path) or {}).items()}
    except Exception:
        return {}


def config_warnings() -> list:
    """Human-readable configuration problems that are not fatal.

    - unknown keys in .env (typos would otherwise be silently ignored forever)
    - removed keys still present in old .env files
    """
    warnings: list = []
    env_vals = load_env_file_keys()
    unknown = sorted(k for k in env_vals if k and k not in KNOWN_ENV_KEYS)
    for key in unknown:
        if key == "LLM_API_KEY":
            warnings.append(
                "Legacy .env key 'LLM_API_KEY' is no longer used — set "
                "META_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY individually "
                "(each is saved per provider and remembered)."
            )
            continue
        if key == "MODEL_API_KEY":
            warnings.append(
                "Legacy alias 'MODEL_API_KEY' → use 'META_API_KEY' instead."
            )
            continue
        if key == "GOOGLE_API_KEY":
            warnings.append(
                "Legacy alias 'GOOGLE_API_KEY' → use 'GEMINI_API_KEY' instead."
            )
            continue
        if key == "META_API_MODEL":
            warnings.append("Legacy alias 'META_API_MODEL' → use 'META_MODEL' instead.")
            continue
        if key == "CTRL_FORCE_FRESH_KEYS":
            warnings.append(
                "Legacy 'CTRL_FORCE_FRESH_KEYS' → use 'CTRL_KEEP_CONTEXT_KEYS' (note: F6 now KEEPS memory; the old name meant the opposite)."
            )
            continue
        warnings.append(
            f"Unknown .env key '{key}' — ignored (typo?). Run 'odicto.py config' "
            "for every valid key."
        )
    return warnings


def _source_of(*env_keys: str) -> str:
    """Source tag for display: which tier supplied this setting."""
    for key in env_keys:
        if key in _PRESENT_AT_IMPORT:
            return f".env ({key})"
    return "default"


# NOTE: no import-time Config.validate() here. Validating on import would fire
# when the setup web page or CLI imports this module (before the user has even
# saved a key), printing a confusing "META_API_KEY is empty" warning. The app
# entry point (main.py) validates explicitly at startup.
