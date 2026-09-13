import base64
import sys
import threading
import time
from typing import Optional, Union

from config import OPENROUTER_FALLBACK_MODEL, ENV_DEFAULTS, Config
from openrouter_catalog import (
    clamp_openrouter_effort,
    ensure_openrouter_catalog,
    lightest_openrouter_effort,
    peek_openrouter_reasoning,
    prefetch_openrouter_catalog,
    reset_openrouter_catalog,
)

# Lazy: openai / google-genai stay unloaded until an LLM provider needs them.
# Tests patch ``refiner.OpenAI`` / ``refiner.google_genai``; a non-None patch
# skips the real import.
OpenAI = None  # type: ignore
google_genai = None  # type: ignore
_openai_import_tried = False
_google_genai_import_tried = False


def _ensure_openai():
    global OpenAI, _openai_import_tried
    if OpenAI is not None or _openai_import_tried:
        return OpenAI
    _openai_import_tried = True
    try:
        from openai import OpenAI as _OpenAI

        OpenAI = _OpenAI
    except Exception:
        OpenAI = None  # type: ignore
    return OpenAI


def _ensure_google_genai():
    global google_genai, _google_genai_import_tried
    if google_genai is not None or _google_genai_import_tried:
        return google_genai
    _google_genai_import_tried = True
    try:
        from google import genai as _genai

        google_genai = _genai
    except Exception:
        google_genai = None  # type: ignore
    return google_genai


# Hard constraints — output is pasted verbatim into the user's document/chat box.
# The Meta Responses call sends no max_output_tokens: reasoning budget is
# uncapped and the effort knob (META_REASONING_EFFORT) is the only control.
# The prompt comes from Config.effective_system_prompt() (prompt.txt, else
# prompt.txt.example, else the built-in default).

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

# OpenRouter default is reasoning.effort=none. GLM-5.3 / GLM-5.3-Flash (and
# other always-on SKUs) reject that with HTTP 400. They accept low|high|max
# only — not none or minimal — so the lightest retry is low.
_OPENROUTER_MANDATORY_REASONING_FALLBACK = "low"
_OPENROUTER_FORCED_EFFORT: dict[str, str] = {}
_MANDATORY_REASONING_MARKERS = (
    "reasoning is mandatory",
    "cannot be disabled",
    "always engages in thinking",
    "always on and cannot be disabled",
)


def reset_openrouter_effort_cache() -> None:
    """Drop per-model reasoning overrides and the live catalog (tests)."""
    _OPENROUTER_FORCED_EFFORT.clear()
    reset_openrouter_catalog()


def _is_mandatory_reasoning_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _MANDATORY_REASONING_MARKERS)


def openrouter_effort_for_model(
    model: str, configured: str | None = None
) -> str:
    """Effort to send for this OpenRouter slug.

    Uses a process-local override after a model rejects disabled reasoning,
    then the live OpenRouter catalog (mandatory + supported_efforts). GLM-5.3
    remains a fallback when the catalog has not loaded yet.
    """
    slug = (model or "").strip()
    cached = _OPENROUTER_FORCED_EFFORT.get(slug.lower())
    if cached:
        return cached
    if configured is None:
        effort = Config.openrouter_reasoning_effort()
    else:
        effort = str(configured).strip().lower()
        if effort not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            effort = Config.openrouter_reasoning_effort()
    return clamp_openrouter_effort(slug, effort or "none")


def _remember_openrouter_effort(model: str, effort: str) -> None:
    slug = (model or "").strip().lower()
    if slug:
        _OPENROUTER_FORCED_EFFORT[slug] = effort


def _openrouter_create(client, kwargs: dict, timeout):
    """chat.completions.create, retrying once if the model forbids effort=none."""
    model = kwargs.get("model") or ""
    extra = kwargs.get("extra_body") or {}
    raw = (extra.get("reasoning") or {}).get("effort")
    effort = openrouter_effort_for_model(model, configured=raw)
    call_kwargs = dict(kwargs)
    call_kwargs["extra_body"] = Config.openrouter_extra_body(effort=effort)
    try:
        return client.chat.completions.create(**call_kwargs, timeout=timeout)
    except Exception as e:
        fallback = _OPENROUTER_MANDATORY_REASONING_FALLBACK
        if effort != fallback and _is_mandatory_reasoning_error(e):
            _remember_openrouter_effort(model, fallback)
            print(
                f"Notice: {model} requires reasoning; retrying with effort={fallback}.",
                flush=True,
            )
            call_kwargs["extra_body"] = Config.openrouter_extra_body(effort=fallback)
            return client.chat.completions.create(**call_kwargs, timeout=timeout)
        raise


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
            # Muse Spark 1.3 echoes the request in `output` as input_text / user
            # messages. Taking those made AI mode paste the spoken instruction.
            if item.get("role") == "user":
                continue
            if item.get("type") in ("reasoning", "function_call", "file_search_call"):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    text = c.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    ctype = c.get("type")
                    if ctype == "input_text":
                        continue
                    if ctype in ("output_text", "text") or ctype is None:
                        texts.append(text)
            elif isinstance(content, str) and content.strip():
                if item.get("role") != "user":
                    texts.append(content)
            elif (
                item.get("role") == "assistant"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                texts.append(item["text"])
        if texts:
            joined = "\n".join(t.strip() for t in texts if t and t.strip())
            if joined.strip():
                return joined.strip()
        str_items = [str(x).strip() for x in output if isinstance(x, str) and str(x).strip()]
        if str_items:
            return "\n".join(str_items)
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


def _choice_content(response: object) -> str:
    """Visible assistant text from a Chat Completions response."""
    try:
        raw = response.choices[0].message.content
    except Exception:
        return ""
    return (raw or "").strip()


def _choice_finish_reason(response: object) -> str:
    try:
        return str(getattr(response.choices[0], "finish_reason", "") or "")
    except Exception:
        return ""


def _describe_meta_response(data: dict) -> str:
    """One-line summary of a Responses payload for dictation.log."""
    try:
        status = data.get("status", "?")
        output = data.get("output", [])
        kinds: list[str] = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    kinds.append("str")
                    continue
                t = str(item.get("type", "?"))
                r = str(item.get("role", ""))
                kinds.append(f"{t}/{r}" if r else t)
        usage = data.get("usage", {})
        tok = f" usage={usage}" if isinstance(usage, dict) and usage else ""
        return f"status={status} output=[{', '.join(kinds)}]{tok}"
    except Exception:
        return "status=? (unparseable)"


def _log_meta_response(data: dict, budget: object) -> None:
    """Print what the model actually returned so dictation.log shows why."""
    print(f"Meta response: {_describe_meta_response(data)} (budget={budget})", flush=True)
    if data.get("status") not in (None, "completed"):
        print(
            f"Meta incomplete details: {str(data.get('incomplete_details', ''))[:300]}",
            flush=True,
        )


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

    def _post_payload(
        self, url: str, payload: dict, timeout: tuple[float, float]
    ) -> dict:
        if self._has_requests and self._session is not None:
            import requests as _req  # type: ignore

            try:
                resp = self._session.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except _req.exceptions.RequestException as e:
                # Surface body for debugging if present
                body = ""
                try:
                    body = getattr(e.response, "text", "")[:500] if getattr(e, "response", None) is not None else ""
                except Exception:
                    pass
                raise RuntimeError(f"Meta API error: {e} {body}".strip()) from e
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
                return _json.loads(raw.decode("utf-8"))
        except _uerr.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")[:500]
            except Exception:
                err_body = str(e)
            raise RuntimeError(f"Meta API error: {e.code} {err_body}") from e

    def create_responses(
        self, input_payload: list[dict], timeout: tuple[float, float] = (5.0, 120.0)
    ) -> Optional[str]:
        url = self._url()
        # No max_output_tokens is sent: reasoning is uncapped and the effort
        # knob (META_REASONING_EFFORT → LLM_REASONING_EFFORT → "low") is the
        # only control over thinking depth. Timeout is generous because
        # medium/high effort can think for 60s+ before answering.
        effort = Config.meta_reasoning_effort()
        payload: dict = {
            "model": self.model,
            "input": input_payload,
            "stream": False,
        }
        if effort and effort != "none":
            payload["reasoning"] = {"effort": effort}

        def _extract_with_log(data: dict) -> Optional[str]:
            _log_meta_response(data, "uncapped")
            text = _extract_meta_text(data)
            if not text:
                reply_preview = str(data)[:400].replace("\n", " ")
                print(
                    f"Meta returned no extractable text; reply dump: {reply_preview}",
                    flush=True,
                )
            return text

        data = self._post_payload(url, payload, timeout)
        return _extract_with_log(data)

    def ping(self) -> None:
        try:
            self.create_responses(
                input_payload=[{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
                timeout=(5.0, 45.0),
            )
        except Exception as e:
            raise e


def build_system_prompt_with_context(base_prompt: str, context: Optional[str] = None) -> str:
    """Combines base system prompt with selected context block (if present)."""
    base = (base_prompt or "").strip()
    ctx = (context or "").strip()
    if not ctx:
        return base
    return (
        f"{base}\n\n"
        "SELECTED CONTEXT:\n"
        "<<<\n"
        f"{ctx}\n"
        ">>>\n\n"
        "The block above is the selected document/text context. The user's input is the instruction to execute on it. "
        "Produce only the final result to replace the selection or insert at the caret."
    )


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
        _ensure_google_genai()
        if google_genai is None:
            self.client = None
            return
        try:
            self.client = google_genai.Client(api_key=api_key)
        except Exception:
            self.client = None

    def _generation_config(self, max_tokens: int) -> dict:
        # The resolved cap arrives from the caller (Config.effective_max_output_tokens);
        # thinking level via Config.gemini_thinking_level().
        effective_max = max(1, int(max_tokens) if max_tokens else 1)
        cfg: dict = {"max_output_tokens": int(effective_max)}
        thinking = Config.gemini_thinking_level()
        if thinking:
            cfg["thinking_level"] = thinking
        return cfg

    def create_interaction(
        self,
        input_content: Union[str, list],
        max_tokens: int,
        keep_history: bool = False,
        system_instruction: Optional[str] = None,
    ) -> Optional[str]:
        if self.client is None:
            raise RuntimeError("google-genai package not installed — run: pip install google-genai")
        sys_inst = (
            system_instruction
            if system_instruction is not None
            else Config.effective_system_prompt()
        )
        kwargs: dict = {
            "model": self.model,
            "input": input_content,
            "system_instruction": sys_inst,
            "generation_config": self._generation_config(max_tokens),
        }
        if keep_history and self._last_interaction_id:
            kwargs["previous_interaction_id"] = self._last_interaction_id
        try:
            interaction = self.client.interactions.create(**kwargs)
        except Exception as e:
            # The SDK raises google.genai.errors.* and plain HTTP errors; surface
            # the message (usually includes the missing-model hint / 404 / 401).
            detail = ""
            try:
                detail = str(getattr(e, "message", "")) or str(e)
            except Exception:
                detail = str(e)
            raise RuntimeError(f"Gemini API error: {detail}".strip()) from e
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
        text = self.create_interaction("ping", max_tokens=1)
        # Treat empty output as "reachable but got no text" — still a successful ping.
        # The error path above is what makes a bad key / bad model surface.


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
            _ensure_openai()
            if OpenAI is None:
                raise RuntimeError("openai package not installed")
            self.client = OpenAI(
                base_url=Config.effective_llm_api_base(),
                api_key="ollama",
                max_retries=0,
            )
        elif self.provider == "openrouter":
            if not Config.effective_api_key():
                print(
                    "Warning: OPENROUTER_API_KEY is empty — "
                    "OpenRouter AI mode will fall back to raw transcript until set.",
                    file=sys.stderr,
                    flush=True,
                )
                self.client = None
            else:
                _ensure_openai()
                if OpenAI is None:
                    raise RuntimeError("openai package not installed")
                self.client = OpenAI(
                    base_url=Config.effective_llm_api_base(),
                    api_key=Config.effective_api_key(),
                    max_retries=0,
                    default_headers={
                        "HTTP-Referer": "https://github.com/odicto",
                        "X-Title": "Odicto",
                    },
                )
                prefetch_openrouter_catalog()
        elif self.provider == "meta":
            if not Config.effective_api_key():
                print(
                    "Warning: META_API_KEY is empty — "
                    "Meta AI mode will fall back to raw transcript until set.",
                    file=sys.stderr,
                    flush=True,
                )
                self.client = None
            else:
                self.client = _MetaClient(
                    api_key=Config.effective_api_key(),
                    base_url=Config.effective_llm_api_base(),
                    model=self.model,
                )
        elif self.provider == "gemini":
            if not Config.effective_api_key():
                print(
                    "Warning: GEMINI_API_KEY is empty — "
                    "Gemini AI mode will fall back to raw transcript until set.",
                    file=sys.stderr,
                    flush=True,
                )
                self.client = None
            else:
                self.client = _GeminiClient(
                    api_key=Config.effective_api_key(),
                    model=self.model,
                )
        else:  # "none"
            self.client = None

    def _meta_input_from_history(
        self, history_snapshot: list[dict[str, str]], system_prompt: str = ""
    ) -> list[dict]:
        sys_text = system_prompt or Config.effective_system_prompt()
        payload: list[dict] = [
            {"role": "system", "content": [{"type": "input_text", "text": sys_text}]}
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
                    # A real "ping" still thinks at full effort and can take
                    # 60s+ on medium/high, blocking nothing but logging noise
                    # on every boot. Skip the network round-trip: the client
                    # is stateless (requests Session) and needs no warm-up.
                    print(f"LLM model '{self.model}' ready (stateless client, no pre-load needed).")
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
                elif self.provider == "openrouter":
                    kwargs["extra_body"] = Config.openrouter_extra_body(
                        effort=openrouter_effort_for_model(self.model)
                    )
                    _openrouter_create(self.client, kwargs, timeout=(3.0, 20.0))
                else:
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

    def refine(
        self,
        text: str,
        context: str = "",
        image_bytes: Optional[bytes] = None,
        keep_history: bool = False,
    ) -> str:
        """Queries the LLM for a normal reply to the spoken query.

        Uses Config.LLM_MAX_TOKENS as the only output limit.
        Optional ``context`` is selected text from the focused app (if any), injected
        into the system prompt so the LLM treats it as document background context.
        Optional ``image_bytes`` is clipboard image/screenshot data for multimodal models.
        ``keep_history`` (F6 chord) sends and updates multi-turn memory.
        Default is a fresh one-shot that does not read or write conversation state.

        On provider='none' or API failure, returns the raw transcript so dictation never fails.
        """
        if not text.strip():
            return ""

        if not any(c.isalnum() for c in text):
            return ""

        if self.provider == "none" or not self.client:
            if self.provider == "meta" and not Config.effective_api_key():
                print("!!! Meta AI mode: no API key resolved — pasting raw transcript.", file=sys.stderr, flush=True)
            if self.provider == "gemini" and not Config.effective_api_key():
                print("!!! Gemini AI mode: no API key resolved — pasting raw transcript.", file=sys.stderr, flush=True)
            return text

        normalized = text.strip().lower().strip(".,!?")
        if normalized in _RESET_PHRASES:
            self.reset_context()
            return _RESET_REPLY

        try:
            max_tokens = Config.effective_max_output_tokens()
            # Meta ignores this (uncapped; effort knob controls thinking).
            # Every other provider uses it as the answer cap.
            budget_label = "uncapped" if self.provider == "meta" else max_tokens
            print(
                f"Sending query to {self.provider} ({self.model}) "
                f"max_tokens={budget_label} keep_history={keep_history} "
                f"for LLM response..."
            )

            if context:
                print(
                    f'Context: "{context[:80]}{"..." if len(context) > 80 else ""}"'
                )

            effective_sys_prompt = build_system_prompt_with_context(
                Config.effective_system_prompt(), context
            )
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
                if image_bytes:
                    print("Notice: Meta provider does not support image context; text only.", flush=True)
                input_payload = self._meta_input_from_history(
                    history_snapshot, system_prompt=effective_sys_prompt
                )
                llm_started = time.time()
                refined_text: Optional[str] = self.client.create_responses(
                    input_payload, timeout=(5.0, 120.0)
                )
                print(f"Meta responded in {time.time() - llm_started:.2f}s")
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
                llm_started = time.time()
                gemini_input: Union[str, list] = user_message
                if image_bytes:
                    try:
                        from google.genai import types as genai_types
                        part = genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png")
                        gemini_input = [part, user_message]
                    except Exception:
                        gemini_input = user_message
                refined_text = self.client.create_interaction(
                    gemini_input,
                    max_tokens=max_tokens,
                    keep_history=keep_history,
                    system_instruction=effective_sys_prompt,
                )
                print(f"Gemini responded in {time.time() - llm_started:.2f}s")
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

            messages = [{"role": "system", "content": effective_sys_prompt}]
            last_idx = len(history_snapshot) - 1
            for idx, msg in enumerate(history_snapshot):
                if idx == last_idx and msg.get("role") == "user" and image_bytes:
                    b64_img = base64.b64encode(image_bytes).decode("ascii")
                    user_content = [
                        {"type": "text", "text": user_message},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_img}"},
                        },
                    ]
                    messages.append({"role": "user", "content": user_content})
                else:
                    messages.append(msg)

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
            elif self.provider == "openrouter":
                kwargs["extra_body"] = Config.openrouter_extra_body(
                    effort=openrouter_effort_for_model(self.model)
                )

            read_timeout = 90.0 if self.provider == "openrouter" else 30.0
            llm_started = time.time()
            if self.provider == "openrouter":
                response = _openrouter_create(
                    self.client, kwargs, timeout=(5.0, read_timeout)
                )
            else:
                response = self.client.chat.completions.create(
                    **kwargs, timeout=(5.0, read_timeout)
                )
            print(f"{self.provider} responded in {time.time() - llm_started:.2f}s")

            refined_text = _choice_content(response)
            if not refined_text and self.provider == "openrouter":
                retry_tokens = max(int(max_tokens) * 4, 2048)
                print(
                    f"Notice: openrouter returned empty content "
                    f"(finish_reason={_choice_finish_reason(response)!r}, "
                    f"usage={getattr(response, 'usage', None)}). "
                    f"Retrying with max_tokens={retry_tokens}.",
                    flush=True,
                )
                kwargs["max_tokens"] = retry_tokens
                kwargs["extra_body"] = Config.openrouter_extra_body(
                    effort=openrouter_effort_for_model(self.model)
                )
                llm_started = time.time()
                response = _openrouter_create(
                    self.client, kwargs, timeout=(5.0, read_timeout)
                )
                print(
                    f"{self.provider} retry responded in "
                    f"{time.time() - llm_started:.2f}s"
                )
                refined_text = _choice_content(response)

            if refined_text:
                if keep_history:
                    with self._history_lock:
                        self.conversation_history.append(
                            {"role": "assistant", "content": refined_text}
                        )
                return refined_text

            print(
                f"Notice: {self.provider} returned no answer text "
                f"(finish_reason={_choice_finish_reason(response)!r}); "
                f"pasting the raw transcript.",
                flush=True,
            )
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


def test_provider(
    provider: str,
    api_key: str,
    model: str,
    api_base: str = "",
    reasoning_effort: str = "",
) -> str:
    """Ping a provider using explicit values, without touching the running Config.

    ``reasoning_effort`` is the setup-page OpenRouter dropdown (may be empty).
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
            _ensure_openai()
            if OpenAI is None:
                return "openai package not installed"
            base = api_base.strip() or ENV_DEFAULTS["LLM_API_BASE"]
            client = OpenAI(base_url=base, api_key="ollama", max_retries=0)
            client.chat.completions.create(
                model=model or ENV_DEFAULTS["LLM_MODEL"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=(3.0, 10.0),
            )
            return "ok"
        if provider == "openrouter":
            _ensure_openai()
            if OpenAI is None:
                return "openai package not installed"
            if not api_key.strip():
                return "OPENROUTER_API_KEY is required"
            base = api_base.strip() or ENV_DEFAULTS["OPENROUTER_API_BASE"]
            client = OpenAI(
                base_url=base,
                api_key=api_key.strip(),
                max_retries=0,
                default_headers={
                    "HTTP-Referer": "https://github.com/odicto",
                    "X-Title": "Odicto",
                },
            )
            ping_model = model or OPENROUTER_FALLBACK_MODEL
            ensure_openrouter_catalog()
            ping_effort = openrouter_effort_for_model(
                ping_model, configured=reasoning_effort.strip() or None
            )
            spec = peek_openrouter_reasoning(ping_model)
            if spec:
                ping_effort = lightest_openrouter_effort(spec)
            elif "glm-5.3" in ping_model.lower():
                ping_effort = _OPENROUTER_MANDATORY_REASONING_FALLBACK
            _openrouter_create(
                client,
                {
                    "model": ping_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "extra_body": Config.openrouter_extra_body(effort=ping_effort),
                },
                timeout=(3.0, 20.0),
            )
            return "ok"
        if provider == "meta":
            if not api_key.strip():
                return "META_API_KEY is required"
            base = api_base.strip() or ENV_DEFAULTS["META_API_BASE"]
            client = _MetaClient(
                api_key=api_key.strip(),
                base_url=base,
                model=model or ENV_DEFAULTS["META_MODEL"],
            )
            client.ping()
            return "ok"
        if provider == "gemini":
            _ensure_google_genai()
            if google_genai is None:
                return "google-genai package not installed"
            if not api_key.strip():
                return "GEMINI_API_KEY is required"
            client = _GeminiClient(
                api_key=api_key.strip(),
                model=model or ENV_DEFAULTS["GEMINI_MODEL"],
            )
            if client.client is None:
                return "Could not initialize the google-genai client"
            client.ping()
            return "ok"
        return f"Unknown provider: {provider}"
    except Exception as e:
        return str(e)
