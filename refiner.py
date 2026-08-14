import sys
import threading
from typing import Optional

from config import Config

try:
    from openai import OpenAI  # noqa: F401 — exposed as refiner.OpenAI for tests/mocking
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


# Hard constraints — output is pasted verbatim into the user's document/chat box.
# max_tokens (Config.LLM_MAX_TOKENS) is the hard ceiling.
_SYSTEM_PROMPT = (
    "You are a precise AI assistant for dictation. Your reply is pasted VERBATIM "
    "into the user's text cursor position (a document, editor, or chat box).\n"
    "\n"
    "ROLE AND STYLE:\n"
    "- Act as a diligent assistant. Follow the user's instructions exactly; when "
    "asked to transform text (rewrite, fix grammar, summarize, translate, make "
    "professional, make concise, etc.), do precisely that and nothing more.\n"
    "- If the user asks a question, answer directly and accurately. If the user "
    "dictates a sentence they want kept, keep it as close to their words as "
    "possible; do not silently rephrase unless asked.\n"
    "- If an instruction is ambiguous or would produce nonsense, make a sensible "
    "minimal interpretation and note the assumption in one short parenthetical. "
    "Never refuse a benign request, never invent facts, never add filler.\n"
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
        floor = max(64, int(getattr(Config, "META_MAX_OUTPUT_TOKENS", 4096)))
        effective_max = max(int(max_tokens), floor) if max_tokens else floor
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


class TextRefiner:
    def __init__(self) -> None:
        """Initializes the LLM API client based on configuration.

        Supports local Ollama, OpenRouter, Meta API, or 'none' (direct transcription bypass).
        """
        self.provider: str = Config.LLM_PROVIDER
        self.model: str = Config.effective_llm_model()
        self.client = None  # OpenAI for ollama/openrouter; _MetaClient for meta; None for none
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
        else:  # "none"
            self.client = None

    def _meta_input_from_history(self, history_snapshot: list[dict[str, str]]) -> list[dict]:
        payload: list[dict] = [
            {"role": "system", "content": [{"type": "input_text", "text": _SYSTEM_PROMPT}]}
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
        print(">>> AI context cleared (fresh conversation).", flush=True)

    def refine(self, text: str, context: str = "") -> str:
        """Queries the LLM for a normal reply to the spoken query.

        Uses Config.LLM_MAX_TOKENS as the only output limit.
        Optional ``context`` is selected text from the focused app (if any).
        Multi-turn history is always sent (capped by length for the API).

        On provider='none' or API failure, returns the raw transcript so dictation never fails.
        """
        if not text.strip():
            return ""

        if not any(c.isalnum() for c in text):
            return ""

        if self.provider == "none" or not self.client:
            if self.provider == "meta" and not Config.META_API_KEY:
                print("!!! Meta AI mode: META_API_KEY not set — pasting raw transcript.", file=sys.stderr, flush=True)
            return text

        normalized = text.strip().lower().strip(".,!?")
        if normalized in _RESET_PHRASES:
            self.reset_context()
            return _RESET_REPLY

        try:
            max_tokens = max(1, int(Config.LLM_MAX_TOKENS))
            print(
                f"Sending query to {self.provider} ({self.model}) "
                f"max_tokens={max_tokens} for LLM response..."
            )

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
                if len(self.conversation_history) > 16:
                    self.conversation_history = self.conversation_history[-16:]
                history_snapshot = list(self.conversation_history)

            if self.provider == "meta":
                assert isinstance(self.client, _MetaClient)
                input_payload = self._meta_input_from_history(history_snapshot)
                refined_text: Optional[str] = self.client.create_responses(
                    input_payload, max_tokens=max_tokens, timeout=(5.0, 30.0)
                )
                if refined_text:
                    refined_text = refined_text.strip()
                    with self._history_lock:
                        self.conversation_history.append(
                            {"role": "assistant", "content": refined_text}
                        )
                    return refined_text
                with self._history_lock:
                    if (
                        self.conversation_history
                        and self.conversation_history[-1]["role"] == "user"
                    ):
                        self.conversation_history.pop()
                return text

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

            refined_text = response.choices[0].message.content
            if refined_text:
                refined_text = refined_text.strip()
                with self._history_lock:
                    self.conversation_history.append(
                        {"role": "assistant", "content": refined_text}
                    )
                return refined_text

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
                f"Check META_API_KEY / OPENROUTER_API_KEY / LLM_MODEL in .env and restart.",
                file=sys.stderr,
                flush=True,
            )
            return text
