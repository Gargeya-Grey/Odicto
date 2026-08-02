import sys
import threading
from typing import Optional
from openai import OpenAI
from config import Config


# Hard constraints — output is pasted verbatim into the user's document/chat box.
# max_tokens (Config.LLM_MAX_TOKENS) is the hard ceiling.
_SYSTEM_PROMPT = (
    "You are a precise AI assistant for dictation. Your reply is pasted VERBATIM "
    "into the user's text cursor position (a document, editor, or chat box).\n"
    "\n"
    "HARD FORMAT RULES (never break these):\n"
    "- Output PLAIN HUMAN-READABLE TEXT ONLY. Absolutely no Markdown of any kind.\n"
    "- Never use: # headings, **bold**, *italics*, `code`, ``` fences, [links](url), "
    "tables, or HTML.\n"
    "- Lists: use simple lines with a leading dash and a space (\"- item\"), or plain "
    "numbered lines (\"1. item\"). No other markup, no bullets that are not plain text.\n"
    "- Do not wrap the answer in quotes, backticks, or any decorative delimiters.\n"
    "- Write exactly as if typing into Notepad or a chat box that renders nothing "
    "but plain text.\n"
    "\n"
    "LENGTH:\n"
    "- Be concise by default. Prefer short scannable bullets over long paragraphs. "
    "Lead with the answer; add only needed detail. Expand only when the question "
    "clearly needs depth. Never pad, never ramble, never cut mid-thought.\n"
    "\n"
    "SELECTED-TEXT CONTEXT:\n"
    "- The user message may include a \"Context:\" section containing text the user "
    "selected in their active app. That selection is the PRIMARY subject.\n"
    "- The \"Query:\" is what the user wants done WITH that selection (summarize, "
    "rewrite, fix grammar, translate, explain, etc.).\n"
    "- Base your reply on the selection. Do NOT re-quote or echo the entire selection "
    "back unless explicitly asked. Directly produce the requested result.\n"
    "- If no Context section is present, answer the query directly and naturally."
)

# Spoken reset phrases — clear multi-turn memory without an LLM call.
_RESET_PHRASES = {
    "reset chat",
    "reset the chat",
    "clear chat",
    "clear the chat",
    "clear conversation",
    "clear the conversation",
    "clear memory",
    "clear the memory",
    "reset conversation",
    "reset the conversation",
}
_RESET_REPLY = "Chat memory cleared. Starting fresh."


class TextRefiner:
    def __init__(self) -> None:
        """Initializes the LLM API client based on configuration.

        Supports local Ollama, OpenRouter, or 'none' (direct transcription bypass).
        """
        self.provider: str = Config.LLM_PROVIDER
        self.model: str = Config.effective_llm_model()
        self.client: Optional[OpenAI] = None
        self._history_lock = threading.Lock()
        # Simple multi-turn memory (no keyword rules for when to include it).
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
        """Queries the LLM for a normal reply to the spoken query.

        Uses Config.LLM_MAX_TOKENS as the only output limit.
        Optional ``context`` is selected text from the focused app (if any).
        Multi-turn history is always sent (capped by length for the API).

        On provider='none' or API failure, returns the raw transcript so dictation never fails.
        """
        if not text.strip():
            return ""

        # Ignore Whisper silence artifacts like ". . . ."
        if not any(c.isalnum() for c in text):
            return ""

        if self.provider == "none" or not self.client:
            return text

        # Spoken reset: clear the multi-turn memory and confirm. Never calls the LLM.
        normalized = text.strip().lower().strip(".,!?")
        if normalized in _RESET_PHRASES:
            with self._history_lock:
                self.conversation_history.clear()
            print(">>> Chat memory cleared (reset chat).", flush=True)
            return _RESET_REPLY

        try:
            max_tokens = max(1, int(Config.LLM_MAX_TOKENS))
            print(
                f"Sending query to {self.provider} ({self.model}) "
                f"max_tokens={max_tokens} for LLM response..."
            )

            # Selected text from the active app, if the user had something highlighted.
            if context:
                user_message = f"Context:\n{context}\n\nQuery: {text}"
                print(
                    f'Context: "{context[:80]}{"..." if len(context) > 80 else ""}"'
                )
            else:
                user_message = text

            with self._history_lock:
                self.conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                # Cap history: last 16 messages ≈ 8 turns
                if len(self.conversation_history) > 16:
                    self.conversation_history = self.conversation_history[-16:]
                history_snapshot = list(self.conversation_history)

            messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
            messages.extend(history_snapshot)

            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": max_tokens,
            }

            if self.provider == "ollama":
                kwargs["extra_body"] = {
                    "options": {
                        "num_ctx": Config.LLM_NUM_CTX,
                        "num_predict": max_tokens,
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
                f"!!! AI mode FAILED for model '{self.model}' ({self.provider}): {e}\n"
                f"    Pasting raw Whisper transcript instead. "
                f"Check OPENROUTER_MODEL / LLM_MODEL in .env and restart.",
                file=sys.stderr,
                flush=True,
            )
            return text
