import re
import sys
import threading
from typing import Optional
from openai import OpenAI
from config import Config


# Voice commands that wipe multi-turn memory without hitting the LLM.
_RESET_COMMANDS = frozenset(
    {
        "clear conversation",
        "reset conversation",
        "clear history",
        "reset history",
        "forget everything",
        "clear chat",
        "reset chat",
    }
)

# Phrases / words that usually continue a prior turn (keep history).
_FOLLOW_UP_RE = re.compile(
    r"^(and|also|what about|how about|why|then|yes|yeah|yep|no|nope|ok|okay|"
    r"continue|go on|more|explain|elaborate|same|repeat|again)\b"
    r"|\b(it|that|this|those|these|them|earlier|previous|before|you said|my name)\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. Your reply is pasted at the user's text cursor, "
    "so be disciplined about length.\n"
    "\n"
    "LENGTH RULES (critical):\n"
    "- Match reply length to the complexity of the user's latest message.\n"
    "- Tiny / yes-no / status / greeting (e.g. 'are you working?', 'hello'): "
    "ONE short sentence. No extras.\n"
    "- Simple factual questions: 1–2 sentences max.\n"
    "- Medium questions: a short paragraph or a few tight bullets.\n"
    "- Only go long for clearly complex multi-part requests.\n"
    "\n"
    "STYLE RULES:\n"
    "- Answer the latest user message directly. Do not pad.\n"
    "- No capability lists, no 'happy to help', no 'let me know if…', no markdown fluff.\n"
    "- Do not drag in unrelated topics from earlier turns unless the user clearly refers to them.\n"
    "- Prefer plain text. Be accurate and useful, not chatty."
)


def estimate_max_tokens(text: str, hard_cap: int) -> int:
    """Pick a token budget from query complexity so small questions stay short."""
    words = len(text.split())
    lower = text.strip().lower()

    # Very short status / greeting / confirmation style
    if words <= 6:
        budget = 32
    elif words <= 14:
        budget = 64
    elif words <= 30:
        budget = 100
    elif words <= 60:
        budget = 140
    else:
        budget = hard_cap

    # Explicit "brief/short" requests
    if any(k in lower for k in ("briefly", "in one sentence", "short answer", "tl;dr", "tldr")):
        budget = min(budget, 40)

    # Explicit "detailed/explain" requests can use more of the cap
    if any(
        k in lower
        for k in (
            "in detail",
            "explain fully",
            "step by step",
            "thoroughly",
            "long answer",
            "comprehensive",
        )
    ):
        budget = hard_cap

    return max(16, min(hard_cap, budget))


def should_use_full_history(text: str) -> bool:
    """Avoid topic bleed: short standalone phrases should not inherit old context."""
    words = len(text.split())
    if words > 12:
        return True
    if _FOLLOW_UP_RE.search(text.strip()):
        return True
    return False


class TextRefiner:
    def __init__(self) -> None:
        """Initializes the LLM API client based on configuration.

        Supports local Ollama, OpenRouter, or 'none' (direct transcription bypass).
        """
        self.provider: str = Config.LLM_PROVIDER
        self.model: str = Config.effective_llm_model()
        self.client: Optional[OpenAI] = None
        self._history_lock = threading.Lock()
        self.conversation_history: list[dict[str, str]] = []

        if self.provider == "ollama":
            self.client = OpenAI(
                base_url=Config.effective_llm_api_base(),
                api_key="ollama",  # Ollama ignores API keys but the client requires a non-empty string
                max_retries=0,  # Fail-fast if the local server is offline
            )
        elif self.provider == "openrouter":
            self.client = OpenAI(
                base_url=Config.effective_llm_api_base(),
                api_key=Config.OPENROUTER_API_KEY,
                max_retries=0,
                default_headers={
                    # Recommended by OpenRouter for rankings / abuse attribution
                    "HTTP-Referer": "https://github.com/odicto",
                    "X-Title": "Odicto",
                },
            )
        else:  # "none"
            self.client = None

    def preload(self) -> None:
        """Pre-loads the model into memory in a background thread to avoid first-run latency."""
        if self.provider == "none" or not self.client:
            return

        def _load() -> None:
            try:
                print(f"Pre-loading LLM model '{self.model}' in the background...")
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "ok"},
                        {"role": "user", "content": "ping"},
                    ],
                    "max_tokens": 1,
                    "temperature": 0.0,
                }
                if self.provider == "ollama":
                    kwargs["extra_body"] = {
                        "keep_alive": -1,  # Keep model resident indefinitely
                        "options": {
                            "num_ctx": min(512, Config.LLM_NUM_CTX),
                            "num_predict": 1,
                        },
                    }
                self.client.chat.completions.create(**kwargs, timeout=(3.0, 20.0))
                print(f"LLM model '{self.model}' pre-loaded successfully!")
            except Exception as e:
                print(f"Notice: Background LLM pre-load did not complete: {e}")

        threading.Thread(target=_load, daemon=True, name="llm-preload").start()

    def refine(self, text: str, context: str = "") -> str:
        """Queries the LLM for a response to the spoken query, keeping multi-turn history.

        On provider='none' or API failure, returns the raw transcript so dictation never fails.

        Args:
            text: The raw transcribed voice query.
            context: Optional selected text from the active document to prepend
                as context for the LLM (e.g. content to refactor).

        Returns:
            str: The LLM's response (or raw text on bypass/failure).
        """
        if not text.strip():
            return ""

        # Ignore Whisper silence artifacts like ". . . ."
        if not any(c.isalnum() for c in text):
            return ""

        if self.provider == "none" or not self.client:
            return text

        clean_text = text.strip().lower().rstrip(".!?")
        if clean_text in _RESET_COMMANDS:
            with self._history_lock:
                self.conversation_history = []
            return "Conversation history cleared."

        try:
            max_tokens = estimate_max_tokens(text, Config.LLM_MAX_TOKENS)
            print(
                f"Sending query to {self.provider} ({self.model}) "
                f"max_tokens={max_tokens} for LLM response..."
            )

            # Build the user message, optionally prepending selected-text context.
            if context:
                user_message = f"Context:\n{context}\n\nQuery: {text}"
                print(f"Context: \"{context[:80]}{'...' if len(context) > 80 else ''}\"")
            else:
                user_message = text

            with self._history_lock:
                self.conversation_history.append({"role": "user", "content": user_message})
                # Cap history: last 16 messages ≈ 8 turns
                if len(self.conversation_history) > 16:
                    self.conversation_history = self.conversation_history[-16:]
                history_snapshot = list(self.conversation_history)

            # Short standalone phrases: don't inherit old topics (prevents rambling bleed).
            if should_use_full_history(text):
                turn_messages = history_snapshot
            else:
                turn_messages = [{"role": "user", "content": user_message}]

            messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
            messages.extend(turn_messages)

            # Slightly lower temperature for short budgets → less waffle
            temperature = 0.4 if max_tokens <= 48 else 0.6 if max_tokens <= 100 else 0.7

            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            if self.provider == "ollama":
                kwargs["extra_body"] = {
                    "options": {
                        "num_ctx": Config.LLM_NUM_CTX,
                        "num_predict": max_tokens,
                        # Mildly discourage fluff without hurting complex answers
                        "repeat_penalty": 1.1,
                    },
                    "keep_alive": -1,
                }

            response = self.client.chat.completions.create(
                **kwargs, timeout=(5.0, 30.0)
            )

            refined_text: Optional[str] = response.choices[0].message.content
            if refined_text:
                refined_text = refined_text.strip()
                with self._history_lock:
                    # Only keep history that matches what we stored as the user turn
                    self.conversation_history.append(
                        {"role": "assistant", "content": refined_text}
                    )
                return refined_text

            # Empty model reply — drop the unfinished user turn
            with self._history_lock:
                if (
                    self.conversation_history
                    and self.conversation_history[-1]["role"] == "user"
                ):
                    self.conversation_history.pop()
            return text

        except Exception as e:
            with self._history_lock:
                if (
                    self.conversation_history
                    and self.conversation_history[-1]["role"] == "user"
                ):
                    self.conversation_history.pop()
            print(
                f"Warning: LLM generation failed ({e}). Pasting raw transcription instead.",
                file=sys.stderr,
            )
            return text
