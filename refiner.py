import sys
import threading
from typing import Optional

from config import Config

try:
    from openai import OpenAI  # noqa: F401 — exposed as refiner.OpenAI for tests/mocking
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

try:
    from google import genai as google_genai  # noqa: F401 — exposed as refiner.google_genai for tests/mocking
except Exception:  # pragma: no cover
    google_genai = None  # type: ignore


# Hard constraints — output is pasted verbatim into the user's document/chat box.
# max_tokens (Config.LLM_MAX_TOKENS) is the hard ceiling.
# The prompt itself lives in Config.SYSTEM_PROMPT (.env SYSTEM_PROMPT, else default).

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


def _extract_meta_text(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    output = data.get("output")
    if isinstance(output, list) and output:
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("text") and isinstance(c["text"], str):
                        if c.get("type") in ("output_text", "input_text", "text") or "type" not in c:
                            texts.append(c["text"])
            elif isinstance(content, str) and content.strip():
                texts.append(content)
            if isinstance(item.get("text"), str) and item["text"].strip():
                texts.append(item["text"])
        if texts:
            joined = "\n".join(t.strip() for t in texts if t and t.strip())
            if joined.strip():
                return joined.strip()
        # Some implementations return output as list of strings
        str_items = [str(x).strip() for x in output if isinstance(x, str) and str(x).strip()]
        if str_items:
            return "\n".join(str_items)
    # OpenAI-compatible fallback
    try:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            c = choices[0].get("message", {}).get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    except Exception:
        pass
    if isinstance(data.get("content"), str) and data["content"].strip():
        return data["content"].strip()
    return None


class _MetaClient:
    """Efficient Meta API client for https://api.meta.ai/v1/responses.

    Uses a single ``requests.Session`` for keep-alive / connection pooling so
    every AI hotkey press reuses the same TCP+TLS connection.
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or "https://api.meta.ai/v1"
        self.model = model
        self._session = None
        self._has_requests = False
        try:
            import requests as _req  # type: ignore

            s = _req.Session()
            s.headers.update(
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
            )
            adapter = _req.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._session = s
            self._has_requests = True
        except Exception:
            self._session = None
            self._has_requests = False

    def _url(self) -> str:
        return f"{self.base_url}/responses"

    def create_responses(
        self, input_payload: list[dict], max_tokens: int, timeout: tuple[float, float] = (5.0, 30.0)
    ) -> Optional[str]:
        url = self._url()
        # reasoning.effort=high burns output_tokens on reasoning (your hi-reasoning
        # model produced 61 reasoning tokens for a 64-budget → immediate truncation).
        # Keep a generous floor and default effort=low for fastest + robust dictation.
        effort = (getattr(Config, "META_REASONING_EFFORT", "low") or "low").strip().lower()
        # Ceiling, not a floor: a 4096 floor forced every reply to budget for a
        # long essay and slowed dictation. LLM_MAX_TOKENS is the request cap.
        ceiling = max(64, int(getattr(Config, "META_MAX_OUTPUT_TOKENS", 4096)))
        requested = max(1, int(max_tokens) if max_tokens else 512)
        effective_max = min(requested, ceiling)
        payload: dict = {
            "model": self.model,
            "input": input_payload,
            "stream": False,
        }
        if effort and effort != "none":
            payload["reasoning"] = {"effort": effort}
        if effective_max:
            payload["max_output_tokens"] = int(effective_max)

        if self._has_requests and self._session is not None:
            import requests as _req  # type: ignore

            try:
                resp = self._session.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                return _extract_meta_text(data)
            except _req.exceptions.RequestException as e:
                # Surface body for debugging if present
                body = ""
                try:
                    body = getattr(e.response, "text", "")[:500] if getattr(e, "response", None) is not None else ""
                except Exception:
                    pass
                raise RuntimeError(f"Meta API error: {e} {body}".strip()) from e
        else:
            import json as _json
            import urllib.request as _urllib
            import urllib.error as _uerr

            body_bytes = _json.dumps(payload).encode("utf-8")
            req = _urllib.Request(url, data=body_bytes, method="POST")
            req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Content-Type", "application/json")
            try:
                # urllib has no connect/read split; use read timeout as overall
                with _urllib.urlopen(req, timeout=timeout[1]) as r:  # type: ignore
                    raw = r.read()
                    data = _json.loads(raw.decode("utf-8"))
                    return _extract_meta_text(data)
            except _uerr.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8")[:500]
                except Exception:
                    err_body = str(e)
                raise RuntimeError(f"Meta API error: {e.code} {err_body}") from e

    def ping(self) -> None:
        try:
            self.create_responses(
                input_payload=[{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
                max_tokens=1,
                timeout=(3.0, 10.0),
            )
        except Exception as e:
            raise e


class _GeminiClient:
    """Efficient Gemini API client built on the official google-genai SDK.

    Uses the GA Interactions API (``client.interactions.create``): a single
    persistent SDK client with connection pooling, ``previous_interaction_id``
    server-side multi-turn state (cheaper implicit caching across turns), and
    the shared Odicto system prompt.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.client = None
        self._last_interaction_id: Optional[str] = None
        if google_genai is None:
            self.client = None
            return
        try:
            self.client = google_genai.Client(api_key=api_key)
        except Exception:
            self.client = None

    def _generation_config(self, max_tokens: int) -> dict:
        ceiling = max(64, int(getattr(Config, "GEMINI_MAX_OUTPUT_TOKENS", 4096)))
        requested = max(1, int(max_tokens) if max_tokens else 512)
        effective_max = min(requested, ceiling)
        thinking = (getattr(Config, "GEMINI_THINKING_LEVEL", "minimal") or "minimal").strip().lower()
        cfg: dict = {"max_output_tokens": int(effective_max)}
        if thinking:
            cfg["thinking_level"] = thinking
        return cfg

    def create_interaction(
        self, input_text: str, max_tokens: int, keep_history: bool = False
    ) -> Optional[str]:
        if self.client is None:
            raise RuntimeError("google-genai package not installed")
        kwargs: dict = {
            "model": self.model,
            "input": input_text,
            "system_instruction": Config.SYSTEM_PROMPT,
            "generation_config": self._generation_config(max_tokens),
        }
        # Server-side multi-turn only when the F6 keep-history chord is used.
        if keep_history and self._last_interaction_id:
            kwargs["previous_interaction_id"] = self._last_interaction_id
        interaction = self.client.interactions.create(**kwargs)
        text = getattr(interaction, "output_text", None)
        if isinstance(text, str) and text.strip():
            if keep_history:
                interaction_id = getattr(interaction, "id", None)
                if isinstance(interaction_id, str) and interaction_id:
                    self._last_interaction_id = interaction_id
            return text.strip()
        return None

    def reset_context(self) -> None:
        self._last_interaction_id = None

    def ping(self) -> None:
        self.create_interaction("ping", max_tokens=1)


class TextRefiner:
    def __init__(self) -> None:
        """Initializes the LLM API client based on configuration.

        Supports local Ollama, OpenRouter, Meta API, Gemini API, or 'none' (direct transcription bypass).
        """
        self.provider: str = Config.LLM_PROVIDER
        self.model: str = Config.effective_llm_model()
        self.client = None  # OpenAI for ollama/openrouter; _MetaClient for meta; _GeminiClient for gemini; None for none
        self._history_lock = threading.Lock()
        self.conversation_history: list[dict[str, str]] = []

        if self.provider == "ollama":
            if OpenAI is None:
                raise RuntimeError("openai package not installed")
            self.client = OpenAI(
                base_url=Config.effective_llm_api_base(),
                api_key="ollama",
                max_retries=0,
            )
        elif self.provider == "openrouter":
            if OpenAI is None:
                raise RuntimeError("openai package not installed")
            self.client = OpenAI(
                base_url=Config.effective_llm_api_base(),
                api_key=Config.OPENROUTER_API_KEY,
                max_retries=0,
                default_headers={
                    "HTTP-Referer": "https://github.com/odicto",
                    "X-Title": "Odicto",
                },
            )
        elif self.provider == "meta":
            if not Config.META_API_KEY:
                print(
                    "Warning: META_API_KEY is empty — Meta AI mode will fall back to raw transcript until set.",
                    file=sys.stderr,
                    flush=True,
                )
                self.client = None
            else:
                self.client = _MetaClient(
                    api_key=Config.META_API_KEY,
                    base_url=Config.effective_llm_api_base(),
                    model=self.model,
                )
        elif self.provider == "gemini":
            if not Config.GEMINI_API_KEY:
                print(
                    "Warning: GEMINI_API_KEY is empty — Gemini AI mode will fall back to raw transcript until set.",
                    file=sys.stderr,
                    flush=True,
                )
                self.client = None
            else:
                self.client = _GeminiClient(
                    api_key=Config.GEMINI_API_KEY,
                    model=self.model,
                )
        else:  # "none"
            self.client = None

    def _meta_input_from_history(self, history_snapshot: list[dict[str, str]]) -> list[dict]:
        payload: list[dict] = [
            {"role": "system", "content": [{"type": "input_text", "text": Config.SYSTEM_PROMPT}]}
        ]
        for msg in history_snapshot:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if role == "assistant":
                payload.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            elif role == "system":
                payload.append({"role": "system", "content": [{"type": "input_text", "text": text}]})
            else:
                payload.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
        return payload

    def preload(self) -> None:
        """Pre-loads the model into memory in a background thread to avoid first-run latency."""
        if self.provider == "none" or not self.client:
            return

        def _load() -> None:
            try:
                print(f"Pre-loading LLM model '{self.model}' in the background...")
                if self.provider == "meta":
                    assert isinstance(self.client, _MetaClient)
                    self.client.ping()
                    print(f"LLM model '{self.model}' pre-loaded successfully!")
                    return
                if self.provider == "gemini":
                    assert isinstance(self.client, _GeminiClient)
                    self.client.ping()
                    print(f"LLM model '{self.model}' pre-loaded successfully!")
                    return
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
                        "keep_alive": -1,
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

    def reset_context(self) -> None:
        """Clears the multi-turn conversation history (spoken 'reset chat' or hotkey)."""
        with self._history_lock:
            self.conversation_history.clear()
        if isinstance(self.client, _GeminiClient):
            self.client.reset_context()
        print(">>> AI context cleared (fresh conversation).", flush=True)

    def refine(self, text: str, context: str = "", keep_history: bool = False) -> str:
        """Queries the LLM for a normal reply to the spoken query.

        Uses Config.LLM_MAX_TOKENS as the only output limit.
        Optional ``context`` is selected text from the focused app (if any).
        ``keep_history`` (F6 chord) sends and updates multi-turn memory.
        Default is a fresh one-shot that does not read or write conversation state.

        On provider='none' or API failure, returns the raw transcript so dictation never fails.
        """
        if not text.strip():
            return ""

        if not any(c.isalnum() for c in text):
            return ""

        if self.provider == "none" or not self.client:
            if self.provider == "meta" and not Config.META_API_KEY:
                print("!!! Meta AI mode: META_API_KEY not set — pasting raw transcript.", file=sys.stderr, flush=True)
            if self.provider == "gemini" and not Config.GEMINI_API_KEY:
                print("!!! Gemini AI mode: GEMINI_API_KEY not set — pasting raw transcript.", file=sys.stderr, flush=True)
            return text

        normalized = text.strip().lower().strip(".,!?")
        if normalized in _RESET_PHRASES:
            self.reset_context()
            return _RESET_REPLY

        try:
            max_tokens = max(1, int(Config.LLM_MAX_TOKENS))
            print(
                f"Sending query to {self.provider} ({self.model}) "
                f"max_tokens={max_tokens} keep_history={keep_history} "
                f"for LLM response..."
            )

            if context:
                user_message = f"Context:\n{context}\n\nQuery: {text}"
                print(
                    f'Context: "{context[:80]}{"..." if len(context) > 80 else ""}"'
                )
            else:
                user_message = text

            with self._history_lock:
                if keep_history:
                    self.conversation_history.append(
                        {"role": "user", "content": user_message}
                    )
                    if len(self.conversation_history) > 16:
                        self.conversation_history = self.conversation_history[-16:]
                    history_snapshot = list(self.conversation_history)
                else:
                    history_snapshot = [
                        {"role": "user", "content": user_message}
                    ]

            if self.provider == "meta":
                assert isinstance(self.client, _MetaClient)
                input_payload = self._meta_input_from_history(history_snapshot)
                refined_text: Optional[str] = self.client.create_responses(
                    input_payload, max_tokens=max_tokens, timeout=(5.0, 30.0)
                )
                if refined_text:
                    refined_text = refined_text.strip()
                    if keep_history:
                        with self._history_lock:
                            self.conversation_history.append(
                                {"role": "assistant", "content": refined_text}
                            )
                    return refined_text
                if keep_history:
                    with self._history_lock:
                        if (
                            self.conversation_history
                            and self.conversation_history[-1]["role"] == "user"
                        ):
                            self.conversation_history.pop()
                return text

            if self.provider == "gemini":
                # Server-side multi-turn only when keep_history (F6). Fresh
                # captures send the current message with no previous_interaction_id.
                assert isinstance(self.client, _GeminiClient)
                refined_text = self.client.create_interaction(
                    user_message, max_tokens=max_tokens, keep_history=keep_history
                )
                if refined_text:
                    refined_text = refined_text.strip()
                    if keep_history:
                        with self._history_lock:
                            self.conversation_history.append(
                                {"role": "assistant", "content": refined_text}
                            )
                    return refined_text
                if keep_history:
                    with self._history_lock:
                        if (
                            self.conversation_history
                            and self.conversation_history[-1]["role"] == "user"
                        ):
                            self.conversation_history.pop()
                return text

            messages = [{"role": "system", "content": Config.SYSTEM_PROMPT}]
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

            refined_text = response.choices[0].message.content
            if refined_text:
                refined_text = refined_text.strip()
                if keep_history:
                    with self._history_lock:
                        self.conversation_history.append(
                            {"role": "assistant", "content": refined_text}
                        )
                return refined_text

            if keep_history:
                with self._history_lock:
                    if (
                        self.conversation_history
                        and self.conversation_history[-1]["role"] == "user"
                    ):
                        self.conversation_history.pop()
            return text

        except Exception as e:
            if keep_history:
                with self._history_lock:
                    if (
                        self.conversation_history
                        and self.conversation_history[-1]["role"] == "user"
                    ):
                        self.conversation_history.pop()
            print(
                f"!!! AI mode FAILED for model '{self.model}' ({self.provider}): {e}\n"
                f"    Pasting raw Whisper transcript instead. "
                f"Check META_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY / LLM_MODEL in .env and restart.",
                file=sys.stderr,
                flush=True,
            )
            return text


def test_provider(provider: str, api_key: str, model: str, api_base: str = "") -> str:
    """Ping a provider using explicit values, without touching the running Config.

    Returns ``"ok"`` on success or a human-readable error string.
    """
    provider = provider.strip().lower().replace("-", "_")
    if provider in ("meta", "meta_api"):
        provider = "meta"
    if provider in ("gemini", "gemini_api", "google", "google_api"):
        provider = "gemini"

    try:
        if provider == "none":
            return "ok"
        if provider == "ollama":
            if OpenAI is None:
                return "openai package not installed"
            base = api_base.strip() or "http://localhost:11434/v1"
            client = OpenAI(base_url=base, api_key="ollama", max_retries=0)
            client.chat.completions.create(
                model=model or "qwen2.5:1.5b-instruct",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=(3.0, 10.0),
            )
            return "ok"
        if provider == "openrouter":
            if OpenAI is None:
                return "openai package not installed"
            if not api_key.strip():
                return "OPENROUTER_API_KEY is required"
            base = api_base.strip() or "https://openrouter.ai/api/v1"
            client = OpenAI(
                base_url=base,
                api_key=api_key.strip(),
                max_retries=0,
                default_headers={
                    "HTTP-Referer": "https://github.com/odicto",
                    "X-Title": "Odicto",
                },
            )
            client.chat.completions.create(
                model=model or "google/gemini-2.0-flash-001",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=(3.0, 10.0),
            )
            return "ok"
        if provider == "meta":
            if not api_key.strip():
                return "META_API_KEY (or MODEL_API_KEY) is required"
            base = api_base.strip() or "https://api.meta.ai/v1"
            client = _MetaClient(
                api_key=api_key.strip(),
                base_url=base,
                model=model or "muse-spark-1.2-contributor",
            )
            client.ping()
            return "ok"
        if provider == "gemini":
            if google_genai is None:
                return "google-genai package not installed"
            if not api_key.strip():
                return "GEMINI_API_KEY (or GOOGLE_API_KEY) is required"
            client = _GeminiClient(
                api_key=api_key.strip(),
                model=model or "gemini-3.7-flash",
            )
            if client.client is None:
                return "Could not initialize the google-genai client"
            client.ping()
            return "ok"
        return f"Unknown provider: {provider}"
    except Exception as e:
        return str(e)
