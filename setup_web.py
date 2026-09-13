"""Local setup web page for Odicto.

Serves a single self-contained HTML form on 127.0.0.1 so a user can pick an
LLM provider, enter API keys/model ids, test the connection, and write the
result back into ``.env``. Keys are masked on re-render and never logged.

The server is stdlib-only and deliberately binds to loopback.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from config import (
    OPENROUTER_FALLBACK_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    ENV_DEFAULTS,
    Config,
    PROMPT_LIVE_NAME,
    prompt_live_path,
)

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover
    dotenv_values = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example")

_TOKEN_RE = re.compile(r"__[A-Z0-9_]+__")

# The page markup lives in setup_template.html beside this module so it can be edited with
# HTML/CSS/JS tooling rather than as an f-string. That f-string's {{ }} doubling once broke
# the whole script (see test_setup_web_page_js_parses); moving the markup out removes the
# hazard class. Read once, then cached.
_TEMPLATE_CACHE = None


def _load_template() -> str:
    """The setup page markup, read from setup_template.html beside this module."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_template.html")
        with open(path, "r", encoding="utf-8") as handle:
            _TEMPLATE_CACHE = handle.read()
    return _TEMPLATE_CACHE


# Keys the page is allowed to write. The merge writer expects every editable key
# to have a positioned line in .env.example; missing ones would be appended.
EDITABLE_KEYS = {
    "LLM_PROVIDER",
    "OLLAMA_MODEL",
    "OLLAMA_MODEL_HISTORY",
    "LLM_MAX_TOKENS",
    "LLM_NUM_CTX",
    "LLM_API_BASE",
    "META_API_KEY",
    "META_MODEL",
    "META_MODEL_HISTORY",
    "META_REASONING_EFFORT",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_MODEL_HISTORY",
    "OPENROUTER_API_BASE",
    "OPENROUTER_PROVIDER_SORT",
    "OPENROUTER_REASONING_EFFORT",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_MODEL_HISTORY",
    "GEMINI_THINKING_LEVEL",
    "WHISPER_MODEL_SIZE",
    "WHISPER_DEVICE",
    "STT_PROVIDER",
    "LIVE_STT_PROVIDER",
    "GEMINI_TRANSCRIBE_MODE",
    "GEMINI_TRANSCRIBE_MODEL",
    "GEMINI_TRANSCRIBE_LIVE_MODEL",
    "GEMINI_TRANSCRIBE_LANGUAGE",
    "GEMINI_TRANSCRIBE_VOCABULARY",
    "LIVE_HOTKEY",
    "HOTKEY",
    "AI_HOTKEY",
    "HOTKEY_TOGGLE",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_FILE",
}

_MASKED = "••••••••••••••••"

# Background "ollama pull" state (started on demand from the setup page).
_PULL_LOCK = threading.Lock()
_PULL_PROC = None
_PULL_LOG = ""
_PULL_DONE = False


def _mask_key(key: str) -> bool:
    return key in (
        "META_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
    )


def _strip_secret_quotes(value: str) -> str:
    """One layer of matching surrounding quotes, with whitespace."""
    v = value.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1].strip()
    return v


_SECRET_KEYS = frozenset({"META_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"})


def _clean_submitted_value(key: str, value: str) -> str:
    """Trim secrets' pasted quotes; leave everything else as plain strip."""
    v = value.strip()
    if key in _SECRET_KEYS:
        return _strip_secret_quotes(v)
    return v


_PROVIDER_KEY_REQUIREMENTS = {
    "meta": ("META_API_KEY", "META_API_KEY is required when LLM_PROVIDER=meta. Paste your Meta API key and save."),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter."),
    "gemini": ("GEMINI_API_KEY", "GEMINI_API_KEY is required when LLM_PROVIDER=gemini."),
}


def validate_provider_requirements(provider: str, updates: dict, merged: dict) -> str:
    """Return '' when satisfied, otherwise the error message to show.

    Extracted so unit tests exercise it without a live HTTP handler.
    """
    row = _PROVIDER_KEY_REQUIREMENTS.get((provider or "none").strip().lower())
    if not row:
        return ""
    env_key, message = row
    src = updates.get(env_key)
    if src == _MASKED:
        return ""
    key = src if src is not None else merged.get(env_key, "")
    if not (key or "").strip():
        return message
    return ""


def read_env() -> dict:
    """Read current .env values; masks secret values for display."""
    if dotenv_values is None:
        return {}
    values = dotenv_values(ENV_PATH) or {}
    out = {}
    for k, v in values.items():
        if _mask_key(k) and v:
            out[k] = _MASKED
        else:
            out[k] = v
    return out


def read_env_raw() -> dict:
    """Read current .env values unmasked (used only for provider testing)."""
    if dotenv_values is None:
        return {}
    return dotenv_values(ENV_PATH) or {}


def _format_env_assignment(key: str, value: str) -> str:
    """Write a .env assignment. Multiline SYSTEM_PROMPT is double-quoted with \\n."""
    if key == "SYSTEM_PROMPT":
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r\n", "\n")
            .replace("\n", "\\n")
        )
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def _parse_env_text(text: str) -> dict:
    """Parse raw .env text into a key→value dict (preserves unedited keys)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _template_lines() -> list:
    """Canonical .env ordering, from .env.example; fallback when missing."""
    if os.path.exists(ENV_EXAMPLE_PATH):
        with open(ENV_EXAMPLE_PATH, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    # Fallback: at least the editable keys, in deterministic order.
    return [f"{k}=" for k in sorted(EDITABLE_KEYS)]


def merge_env(updates: dict) -> None:
    """Rewrite .env anchored on .env.example so slots stay categorized.

    Every EDITABLE_KEYS entry has a positioned line in .env.example (now mostly
    empty ``KEY=`` slots). A save fills those slots in place instead of
    appending at the end — an old .env migrates into the canonical layout on
    the next save, with unknown keys appended under a marker section.
    """
    # Stored values before this save (raw, unmasked — but never written).
    stored: dict[str, str] = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            stored = _parse_env_text(f.read())

    cleaned: dict[str, str] = {}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            continue
        if value == _MASKED:
            continue
        cleaned[key] = _clean_submitted_value(key, value)

    template = _template_lines()
    # Keys the template already positions.
    template_keys: set = set()
    for line in template:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            template_keys.add(stripped.split("=", 1)[0].strip())

    out: list[str] = []
    seen: set = set()
    for line in template:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in cleaned:
                val = cleaned[key]
                if val:
                    out.append(_format_env_assignment(key, val))
                else:
                    # Intentional clear (user blanked the field): restore the pristine slot.
                    out.append(line)
                seen.add(key)
            elif key in stored and (stored[key] or "").strip():
                # Unsubmitted this round (sentinel or hidden field): preserve.
                out.append(_format_env_assignment(key, stored[key].strip()))
                seen.add(key)
            else:
                out.append(line)
                seen.add(key)
        else:
            out.append(line)

    # Unknown/legacy keys from the old file, after the template.
    unknown = [(k, v) for k, v in stored.items() if k not in template_keys]
    if unknown:
        # Avoid duplicating the marker if it already exists in the template.
        if not any("kept from your previous" in l for l in out):
            out.append("")
            out.append("# --- kept from your previous .env ---")
        for k, v in sorted(unknown):
            out.append(_format_env_assignment(k, v))

    # Keys submitted this round whose line never existed in the template (defensive).
    for key in sorted(cleaned):
        if key not in seen and cleaned[key]:
            out.append(_format_env_assignment(key, cleaned[key]))

    text = "\n".join(out).rstrip() + "\n"
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if hasattr(os, "chmod") and sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, ENV_PATH)


def reset_env() -> None:
    """Reset ``.env`` to the shipped ``.env.example`` (blank placeholders)."""
    if not os.path.exists(ENV_EXAMPLE_PATH):
        raise FileNotFoundError("Missing .env.example; cannot reset settings")
    with open(ENV_EXAMPLE_PATH, "r", encoding="utf-8") as f:
        example = f.read()
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(example)
    if hasattr(os, "chmod") and sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, ENV_PATH)
    delete_live_prompt()


def write_prompt_file(file_ref: str, text: str) -> str:
    """Write the AI prompt to a UTF-8 text file next to the app (atomic).

    ``file_ref`` may be a bare filename or a relative subpath inside the
    install directory; absolute paths and ``..`` escapes are rejected.

    Browser form posts arrive with CRLF; normalize to LF and write with
    newline="" so Windows text-mode translation cannot stack CR on CR on
    every save (the prompt.txt blank-line growth bug).

    Returns "" on success or an error message.
    """
    ref = (file_ref or "").strip()
    if not ref:
        return "Prompt filename is empty."
    if os.path.isabs(ref) or ".." in ref.replace("\\", "/").split("/"):
        return "Prompt file must be a relative path inside the Odicto folder."
    target = os.path.join(os.path.dirname(prompt_live_path()), ref)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body + "\n")
        os.replace(tmp, target)
    except OSError as e:
        return f"Could not write prompt file '{ref}': {e}"
    return ""


def delete_live_prompt() -> str:
    """Remove private ``prompt.txt`` so the shipped example is used again."""
    target = prompt_live_path()
    try:
        if os.path.exists(target):
            os.remove(target)
    except OSError as e:
        return f"Could not remove {PROMPT_LIVE_NAME}: {e}"
    return ""


def restart_odicto() -> str:
    """Stop any running Odicto for this install, then start a fresh instance.

    Setup itself is ``odicto.py setup`` and is not matched by the main.py killer,
    so this page stays up. Returns a short status line for the save banner.
    """
    import platforms

    root = os.path.dirname(os.path.abspath(__file__))
    pid_file = os.path.join(root, "dictation.pid")
    try:
        platforms.kill_other_odicto_processes(pid_file)
    except Exception as e:
        return f"Settings saved, but could not stop Odicto ({e}). Start it yourself."

    if sys.platform == "win32":
        pyw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
        py = os.path.join(root, ".venv", "Scripts", "python.exe")
        exe = pyw if os.path.isfile(pyw) else py
    else:
        exe = os.path.join(root, ".venv", "bin", "python")
    main_py = os.path.join(root, "main.py")
    if not os.path.isfile(exe) or not os.path.isfile(main_py):
        return "Settings saved. Start Odicto yourself to apply them."
    try:
        platforms.spawn_detached([exe, main_py])
    except Exception as e:
        return f"Settings saved, but Odicto did not start ({e})."
    return "Settings saved. Odicto is restarting with the new settings."


def apply_prompt_save(text: str) -> str:
    """Persist the setup textarea to prompt.txt, or restore the shipped default.

    Matching the built-in default deletes ``prompt.txt``. Anything else writes
    the private file. Returns "" on success or an error message.
    """
    body = (text or "").strip()
    if not body or body == DEFAULT_SYSTEM_PROMPT.strip():
        return delete_live_prompt()
    return write_prompt_file(PROMPT_LIVE_NAME, body)


def start_ollama_pull(model: str) -> str:
    """Kick off `ollama pull <model>` in the background (idempotent).

    Returns an error string if the pull cannot start; otherwise "".
    """
    global _PULL_PROC, _PULL_LOG, _PULL_DONE
    with _PULL_LOCK:
        if _PULL_PROC is not None and _PULL_PROC.poll() is None:
            return ""  # already pulling
        _PULL_LOG = ""
        _PULL_DONE = False
        try:
            _PULL_PROC = subprocess.Popen(
                ["ollama", "pull", model],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return "Ollama is not installed. Install it from https://ollama.com/download, then retry."
        except Exception as e:  # pragma: no cover
            return f"Could not start ollama pull: {e}"

    threading.Thread(target=_drain_pull_output, daemon=True).start()
    return ""


def _drain_pull_output() -> None:
    global _PULL_LOG, _PULL_DONE
    proc = _PULL_PROC
    if proc is None or proc.stdout is None:
        return
    for line in proc.stdout:
        _PULL_LOG += line
        # keep the log bounded; progress bars spam carriage returns
        if len(_PULL_LOG) > 6000:
            _PULL_LOG = _PULL_LOG[-4000:]
    proc.wait()
    with _PULL_LOCK:
        _PULL_DONE = True


def pull_status() -> dict:
    """Snapshot of the background pull for the /pull-status endpoint."""
    with _PULL_LOCK:
        running = _PULL_PROC is not None and _PULL_PROC.poll() is None
        return {
            "running": running,
            "done": _PULL_DONE,
            "exit_code": _PULL_PROC.poll() if _PULL_PROC is not None else None,
            "log": _PULL_LOG[-1500:],
        }


def _page(message: str = "", message_kind: str = "neutral") -> str:
    current = read_env()

    # Form initial values come from .env when set, otherwise from the single
    # built-in defaults table — never a second copy of a default literal.
    def env_or(key: str) -> str:
        value = (current.get(key) or "").strip()
        return value if value else ENV_DEFAULTS[key]

    def env_raw(key: str) -> str:
        """Explicitly configured value only — no default substitution.

        Used for the Model field: an unset generic model must render EMPTY
        (with the provider's default as the placeholder), never as if the
        user had chosen it.
        """
        return (current.get(key) or "").strip()

    provider = env_or("LLM_PROVIDER")
    meta_key = current.get("META_API_KEY", "")
    or_key = current.get("OPENROUTER_API_KEY", "")
    gemini_key = current.get("GEMINI_API_KEY", "")
    ollama_base = env_or("LLM_API_BASE")
    # Per-backend slots (raw: blank = unset → app's built-in default).
    ollama_model = env_raw("OLLAMA_MODEL")
    meta_model = env_raw("META_MODEL")
    or_model_raw = env_raw("OPENROUTER_MODEL")
    gemini_model_raw = env_raw("GEMINI_MODEL")
    meta_reasoning = env_raw("META_REASONING_EFFORT")
    gemini_thinking_raw = env_raw("GEMINI_THINKING_LEVEL")
    or_reasoning = env_raw("OPENROUTER_REASONING_EFFORT")
    or_sort = env_raw("OPENROUTER_PROVIDER_SORT")
    # Legacy seed: a hand-set LLM_MODEL/LLM_REASONING_EFFORT is shown once so
    # the next save migrates it visibly into the per-backend slot.
    legacy_model = env_raw("LLM_MODEL")
    legacy_reasoning = env_raw("LLM_REASONING_EFFORT")
    # Shorthands for the env-derived model slots + history helpers.
    def _history_tokens(raw: str) -> list:
        seen: set = set()
        out: list = []
        for tok in (raw or "").split(","):
            tok = tok.strip()
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
        return out

    # One default per provider — everything else is Custom + user-grown history.
    GEMINI_KNOWN_ALL: list = []
    META_CATALOG_BUILTIN: list = []
    OPENROUTER_CATALOG_BUILTIN: list = []
    OLLAMA_CATALOG_BUILTIN: list = []
    # Stored histories: only API keys are blank on a fresh install; model rows
    # ship filled, so histories start empty and grow as the user verifies new models.
    meta_hist = _history_tokens(env_raw("META_MODEL_HISTORY"))
    or_hist = _history_tokens(env_raw("OPENROUTER_MODEL_HISTORY"))
    gemini_hist = _history_tokens(env_raw("GEMINI_MODEL_HISTORY"))
    ollama_hist = _history_tokens(env_raw("OLLAMA_MODEL_HISTORY"))
    # A model that came from the legacy LLM_MODEL seed is also remembered once the
    # user next saves — don't inject it here; the save path does the book-keeping.
    def _catalog(provider: str, builtin: list, hist: list) -> list:
        # Deduplicated, stable-ordered: builtins first, then verified extras.
        seen: set = set()
        out: list = []
        for x in builtin + hist:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out
    META_CATALOG = _catalog("meta", META_CATALOG_BUILTIN, meta_hist)
    GEMINI_CATALOG = _catalog("gemini", GEMINI_KNOWN_ALL, gemini_hist)
    OPENROUTER_CATALOG = _catalog("openrouter", OPENROUTER_CATALOG_BUILTIN, or_hist)
    OLLAMA_CATALOG = _catalog("ollama", OLLAMA_CATALOG_BUILTIN, ollama_hist)
    # Seed: explicit slot wins; fallback to legacy hand-edit; else blank (default placeholder in the card).
    meta_model_seed = meta_model or legacy_model
    or_model_seed = or_model_raw or legacy_model
    gemini_model_seed = gemini_model_raw or legacy_model
    ollama_model_seed = ollama_model or legacy_model
    meta_reasoning_seed = meta_reasoning or legacy_reasoning
    gemini_thinking_seed = gemini_thinking_raw or legacy_reasoning
    or_reasoning_seed = or_reasoning or legacy_reasoning
    or_sort_seed = or_sort
    # For the saved-hint underline.
    already_meta = bool(meta_model.strip())
    already_or = bool(or_model_raw.strip())
    already_gemini = bool(gemini_model_raw.strip())
    already_ollama = bool(ollama_model.strip())
    model_defaults_json = json.dumps(
        {
            "meta": ENV_DEFAULTS["META_MODEL"],
            "openrouter": OPENROUTER_FALLBACK_MODEL,
            "gemini": ENV_DEFAULTS["GEMINI_MODEL"],
            "ollama": ENV_DEFAULTS["LLM_MODEL"],
            "none": "",
        }
    )
    model_catalogs_json = json.dumps(
        {
            "meta": META_CATALOG,
            "openrouter": OPENROUTER_CATALOG,
            "gemini": GEMINI_CATALOG,
            "ollama": OLLAMA_CATALOG,
        }
    )
    llm_max_tokens = env_or("LLM_MAX_TOKENS")
    llm_num_ctx = env_or("LLM_NUM_CTX")
    whisper = env_or("WHISPER_MODEL_SIZE")
    whisper_device = env_or("WHISPER_DEVICE")
    stt_provider = env_or("STT_PROVIDER")
    live_stt_provider = env_or("LIVE_STT_PROVIDER")
    transcribe_mode = env_or("GEMINI_TRANSCRIBE_MODE")
    transcribe_lang = env_or("GEMINI_TRANSCRIBE_LANGUAGE")
    transcribe_vocab = env_or("GEMINI_TRANSCRIBE_VOCABULARY")
    live_hotkey = env_or("LIVE_HOTKEY")
    hotkey = env_or("HOTKEY")
    ai_hotkey = env_or("AI_HOTKEY")
    hotkey_toggle_raw = env_or("HOTKEY_TOGGLE").strip().lower()
    hotkey_toggle_on = hotkey_toggle_raw not in ("false", "0", "no", "off")
    system_prompt = Config.effective_system_prompt()
    prompt_source = Config.prompt_source_label()

    # Server-rendered status (after Save / Reset). The Test button uses inline
    # JS instead, so its status is not rendered here.
    server_status = ""
    if message:
        server_status = (
            f'<div class="status show {html.escape(message_kind)}" role="status">'
            f"{html.escape(message)}</div>"
        )

    replacements = {
        "__HOTKEY_GEAR_CLASS__": 'long' if hotkey_toggle_on else 'short',
        "__HOTKEY_TOGGLE_TRUE__": 'true' if hotkey_toggle_on else 'false',
        "__HOTKEY_LONG_SELECTED__": ' selected' if hotkey_toggle_on else '',
        "__HOTKEY_LONG_ARIA__": 'true' if hotkey_toggle_on else 'false',
        "__HOTKEY_SHORT_SELECTED__": '' if hotkey_toggle_on else ' selected',
        "__HOTKEY_SHORT_ARIA__": 'false' if hotkey_toggle_on else 'true',
        "__PROVIDER__": html.escape(provider),
        "__META_KEY_SAVED_HINT__": ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if meta_key else '',
        "__META_KEY__": html.escape(meta_key),
        "__META_MODEL_DEFAULT__": html.escape(ENV_DEFAULTS['META_MODEL']),
        "__META_MODEL_SEED__": html.escape(meta_model_seed),
        "__META_HISTORY_CSV__": html.escape(','.join(meta_hist)),
        "__META_MODEL_DEFAULT_NOTE__": html.escape(ENV_DEFAULTS['META_MODEL']),
        "__META_SAVED_HINT__": ' · saved value shown' if already_meta else '',
        "__META_REASONING_SEED__": html.escape(meta_reasoning_seed),
        "__OPENROUTER_KEY_SAVED_HINT__": ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if or_key else '',
        "__OR_KEY__": html.escape(or_key),
        "__OR_MODEL_SEED__": html.escape(or_model_seed),
        "__OPENROUTER_HISTORY_CSV__": html.escape(','.join(or_hist)),
        "__OPENROUTER_SAVED_HINT__": ' · saved value shown' if already_or else '',
        "__OR_REASONING_SEED__": html.escape(or_reasoning_seed),
        "__OR_SORT_SEED__": html.escape(or_sort_seed),
        "__GEMINI_KEY_SAVED_HINT__": ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if gemini_key else '',
        "__GEMINI_KEY__": html.escape(gemini_key),
        "__GEMINI_MODEL_SEED__": html.escape(gemini_model_seed),
        "__GEMINI_HISTORY_CSV__": html.escape(','.join(gemini_hist)),
        "__GEMINI_SAVED_HINT__": ' · saved value shown' if already_gemini else '',
        "__GEMINI_THINKING_SEED__": html.escape(gemini_thinking_seed),
        "__OLLAMA_MODEL_SEED__": html.escape(ollama_model_seed),
        "__OLLAMA_HISTORY_CSV__": html.escape(','.join(ollama_hist)),
        "__OLLAMA_SAVED_HINT__": ' · saved value shown' if already_ollama else '',
        "__OLLAMA_BASE__": html.escape(ollama_base),
        "__LLM_NUM_CTX__": html.escape(llm_num_ctx),
        "__LLM_MAX_TOKENS__": html.escape(llm_max_tokens),
        "__PROMPT_SOURCE__": html.escape(prompt_source),
        "__STT_WHISPER_SELECTED__": ' selected' if stt_provider == 'whisper' else '',
        "__STT_GEMINI_SELECTED__": ' selected' if stt_provider == 'gemini' else '',
        "__STT_AUTO_SELECTED__": ' selected' if stt_provider == 'auto' else '',
        "__STT_WHISPER_HIDDEN__": ' hidden' if stt_provider == 'whisper' else '',
        "__TRANSCRIBE_SMART_CHECKED__": 'checked' if transcribe_mode != 'verbatim' else '',
        "__TRANSCRIBE_MODE__": html.escape(transcribe_mode),
        "__TRANSCRIBE_LANG__": html.escape(transcribe_lang),
        "__TRANSCRIBE_VOCAB__": html.escape(transcribe_vocab),
        "__WHISPER__": html.escape(whisper),
        "__WHISPER_DEVICE__": html.escape(whisper_device),
        "__STT_WHISPER_HIDDEN_SECOND__": ' hidden' if stt_provider == 'whisper' else '',
        "__HOTKEY__": html.escape(hotkey),
        "__AI_HOTKEY__": html.escape(ai_hotkey),
        "__LIVE_HOTKEY__": html.escape(live_hotkey),
        "__LIVE_STT_AUTO_SELECTED__": ' selected' if live_stt_provider == 'auto' else '',
        "__LIVE_STT_GEMINI_SELECTED__": ' selected' if live_stt_provider == 'gemini' else '',
        "__LIVE_STT_WHISPER_SELECTED__": ' selected' if live_stt_provider == 'whisper' else '',
        "__SERVER_STATUS__": server_status,
        "__SYSTEM_PROMPT__": html.escape(system_prompt),
        "__DEFAULT_SYSTEM_PROMPT_JSON__": json.dumps(DEFAULT_SYSTEM_PROMPT),
        "__MODEL_DEFAULTS_JSON__": model_defaults_json,
        "__MODEL_CATALOGS_JSON__": model_catalogs_json,
    }
    # One pass over the template only. The previous chained .replace() calls re-scanned
    # values they had already inserted, so a prompt or status message containing a
    # literal __TOKEN__ could corrupt the page.
    return _TOKEN_RE.sub(
        lambda match: replacements.get(match.group(0), match.group(0)), _load_template()
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "OdictoSetup/1.0"

    def do_GET(self) -> None:
        if self.path == "/pull-status":
            self._send_json(pull_status())
            return
        if self.path == "/openrouter-models":
            from openrouter_catalog import ensure_openrouter_catalog

            self._send_json(ensure_openrouter_catalog())
            return
        if self.path != "/":
            self.send_error(404)
            return
        body = _page().encode("utf-8")
        self._send(body)

    def _validate_origin(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        allowed_hosts = {"127.0.0.1", "localhost"}
        host_name = host.split(":")[0].lower()
        if host_name and host_name not in allowed_hosts:
            return False
        for header_val in (origin, referer):
            if header_val:
                from urllib.parse import urlparse
                p = urlparse(header_val)
                if p.hostname and p.hostname.lower() not in allowed_hosts:
                    return False
        return True

    def do_POST(self) -> None:
        if not self._validate_origin():
            self.send_error(403, "Forbidden: Cross-origin requests not permitted")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        form = parse_qs(raw)

        if self.path == "/pull-ollama":
            model = (form.get("OLLAMA_MODEL") or form.get("LLM_MODEL") or [ENV_DEFAULTS["LLM_MODEL"]])[0].strip()
            if not model:
                self._send_json({"ok": False, "message": "Enter an Ollama model first."})
                return
            err = start_ollama_pull(model)
            if err:
                self._send_json({"ok": False, "message": err})
            else:
                self._send_json({"ok": True, "message": f"Downloading {model} in the background..."})
            return

        if self.path == "/test":
            self._handle_test(form)
            return

        if self.path == "/save":
            self._handle_save(form)
            return

        if self.path == "/reset":
            self._handle_reset()
            return

        self.send_error(404)

    def _handle_test(self, form: dict) -> None:
        from refiner import test_provider

        provider = (form.get("LLM_PROVIDER") or ["none"])[0].strip().lower()
        if provider in ("meta", "meta_api", "meta-api"):
            provider = "meta"
        if provider in ("gemini", "gemini_api", "google", "google_api", "google-api"):
            provider = "gemini"

        reasoning_effort = ""

        def pick(key: str, default: str = "") -> str:
            raw = (form.get(key) or [""])[0]
            value = _clean_submitted_value(key, raw)
            if value == _MASKED:
                stored = read_env_raw().get(key, default)
                return _strip_secret_quotes((stored or default))
            if not value and key not in form:
                stored = read_env_raw().get(key, default)
                return _strip_secret_quotes((stored or default))
            if not value:
                return default
            return _strip_secret_quotes(value) or default

        if provider == "meta":
            api_key = pick("META_API_KEY")
            model = pick("META_MODEL", ENV_DEFAULTS["META_MODEL"])
            api_base = pick("META_API_BASE", ENV_DEFAULTS["META_API_BASE"])
        elif provider == "openrouter":
            api_key = pick("OPENROUTER_API_KEY")
            model = pick("OPENROUTER_MODEL", OPENROUTER_FALLBACK_MODEL)
            api_base = pick("OPENROUTER_API_BASE", ENV_DEFAULTS["OPENROUTER_API_BASE"])
            reasoning_effort = pick("OPENROUTER_REASONING_EFFORT")
        elif provider == "gemini":
            api_key = pick("GEMINI_API_KEY")
            model = pick("GEMINI_MODEL", ENV_DEFAULTS["GEMINI_MODEL"])
            api_base = ""
        elif provider == "ollama":
            api_key = ""
            model = pick("OLLAMA_MODEL", ENV_DEFAULTS["LLM_MODEL"]) or pick("LLM_MODEL", ENV_DEFAULTS["LLM_MODEL"])
            api_base = pick("LLM_API_BASE", ENV_DEFAULTS["LLM_API_BASE"])
        else:
            api_key = ""
            model = ""
            api_base = ""
            reasoning_effort = ""

        if provider == "openrouter":
            result = test_provider(
                provider, api_key, model, api_base, reasoning_effort=reasoning_effort
            )
        else:
            result = test_provider(provider, api_key, model, api_base)
        ok = result == "ok"
        if not ok and any(s in result.lower() for s in ("401", "unauthorized", "403", "forbidden")):
            result = result + " — check the key is valid and was pasted without extra quotes around it."
        message = "Connected successfully." if ok else f"Test failed: {result}"
        self._send_json({"ok": ok, "message": message})

    def _handle_save(self, form: dict) -> None:
        from config import validate_hotkey_pair

        updates = {}
        for key in EDITABLE_KEYS:
            if key in form:
                updates[key] = form[key][0]
        if updates.get("LLM_PROVIDER") == "none":
            updates.pop("META_API_KEY", None)
            updates.pop("OPENROUTER_API_KEY", None)
            updates.pop("GEMINI_API_KEY", None)

        # prompt.txt is the only live prompt. Matching the shipped default
        # deletes it; any other textarea content writes the private file.
        prompt_body = updates.get("SYSTEM_PROMPT", "")
        err = apply_prompt_save(prompt_body)
        if err:
            body = _page(err, "err").encode("utf-8")
            self._send(body)
            return
        if (prompt_body or "").strip() and (
            prompt_body.strip() != DEFAULT_SYSTEM_PROMPT.strip()
        ):
            updates["SYSTEM_PROMPT_FILE"] = PROMPT_LIVE_NAME
        else:
            updates["SYSTEM_PROMPT_FILE"] = ""
        updates["SYSTEM_PROMPT"] = ""

        hotkey = updates.get("HOTKEY") or Config.HOTKEY
        ai_hotkey = updates.get("AI_HOTKEY") or Config.AI_HOTKEY
        try:
            validate_hotkey_pair(hotkey, ai_hotkey)
        except Exception as e:
            body = _page(f"Hotkey invalid: {e}", "err").encode("utf-8")
            self._send(body)
            return

        merge_env(updates)
        merged = read_env_raw()
        provider = (
            updates.get("LLM_PROVIDER") or merged.get("LLM_PROVIDER") or "none"
        ).strip().lower()
        if provider not in ("meta", "ollama", "openrouter", "gemini", "none"):
            body = _page(f"Saved, but LLM_PROVIDER '{provider}' is invalid.", "err").encode("utf-8")
            self._send(body)
            return
        err_msg = validate_provider_requirements(provider, updates, merged)
        if err_msg:
            body = _page(f"Saved, but {err_msg}", "err").encode("utf-8")
            self._send(body)
            return
        body = _page(restart_odicto(), "ok").encode("utf-8")
        self._send(body)

    def _handle_reset(self) -> None:
        try:
            reset_env()
        except Exception as e:
            self._send_json({"ok": False, "message": str(e)})
            return
        self._send_json({"ok": True, "message": "Settings reset to defaults."})

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Do not log query bodies or any key material.
        return


def run_server(port: int = 8765, open_browser: bool = True) -> None:
    port = int(os.getenv("SETUP_PORT", str(port)))
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Odicto setup page: {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
