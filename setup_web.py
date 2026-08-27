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
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from config import OPENROUTER_FALLBACK_MODEL, DEFAULT_SYSTEM_PROMPT, ENV_DEFAULTS, Config

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover
    dotenv_values = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example")

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
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_MODEL_HISTORY",
    "GEMINI_THINKING_LEVEL",
    "WHISPER_MODEL_SIZE",
    "WHISPER_DEVICE",
    "STT_PROVIDER",
    "GEMINI_TRANSCRIBE_MODE",
    "GEMINI_TRANSCRIBE_MODEL",
    "GEMINI_TRANSCRIBE_LIVE_MODEL",
    "GEMINI_TRANSCRIBE_LANGUAGE",
    "GEMINI_TRANSCRIBE_VOCABULARY",
    "LIVE_HOTKEY",
    "HOTKEY",
    "AI_HOTKEY",
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


def validate_provider_requirements(provider: str, updates: dict, merged: dict) -> str:
    """Return '' when satisfied, otherwise the error message to show.

    Extracted so unit tests exercise it without a live HTTP handler.
    """
    p = (provider or "none").strip().lower()
    if p == "meta":
        src = updates.get("META_API_KEY")
        if src == _MASKED:
            return ""
        key = src if src is not None else merged.get("META_API_KEY", "")
        if not (key or "").strip():
            return "META_API_KEY is required when LLM_PROVIDER=meta. Paste your Meta API key and save."
    if p == "openrouter":
        src = updates.get("OPENROUTER_API_KEY")
        if src == _MASKED:
            return ""
        key = src if src is not None else merged.get("OPENROUTER_API_KEY", "")
        if not (key or "").strip():
            return "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter."
    if p == "gemini":
        src = updates.get("GEMINI_API_KEY")
        if src == _MASKED:
            return ""
        key = src if src is not None else merged.get("GEMINI_API_KEY", "")
        if not (key or "").strip():
            return "GEMINI_API_KEY is required when LLM_PROVIDER=gemini."
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
    os.replace(tmp, ENV_PATH)


def write_prompt_file(file_ref: str, text: str) -> str:
    """Write the AI prompt to a UTF-8 text file next to the app (atomic).

    ``file_ref`` may be a bare filename or a relative subpath inside the
    install directory; absolute paths and ``..`` escapes are rejected.

    Returns "" on success or an error message.
    """
    ref = (file_ref or "").strip()
    if not ref:
        return "Prompt filename is empty."
    if os.path.isabs(ref) or ".." in ref.replace("\\", "/").split("/"):
        return "Prompt file must be a relative path inside the Odicto folder."
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), ref)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, target)
    except OSError as e:
        return f"Could not write prompt file '{ref}': {e}"
    return ""


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
    transcribe_mode = env_or("GEMINI_TRANSCRIBE_MODE")
    transcribe_lang = env_or("GEMINI_TRANSCRIBE_LANGUAGE")
    transcribe_vocab = env_or("GEMINI_TRANSCRIBE_VOCABULARY")
    live_hotkey = env_or("LIVE_HOTKEY")
    hotkey = env_or("HOTKEY")
    ai_hotkey = env_or("AI_HOTKEY")
    system_prompt_file = current.get("SYSTEM_PROMPT_FILE", "")
    system_prompt = (current.get("SYSTEM_PROMPT") or "").strip() or DEFAULT_SYSTEM_PROMPT

    # Server-rendered status (after Save / Reset). The Test button uses inline
    # JS instead, so its status is not rendered here.
    server_status = ""
    if message:
        server_status = (
            f'<div class="status show {html.escape(message_kind)}" role="status">'
            f"{html.escape(message)}</div>"
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Odicto Setup</title>
<style>
@font-face {{
  font-family: 'Stack Sans';
  font-style: normal;
  font-display: swap;
  font-weight: 200 700;
  src: url(https://cdn.jsdelivr.net/fontsource/fonts/stack-sans-text:vf@5.3.0/latin-wght-normal.woff2) format('woff2-variations');
}}
:root {{
  color-scheme: light dark;
  --bg: #f5f4f2;
  --card: #ffffff;
  --ink: #1b1b1f;
  --muted: #6b6a70;
  --line: #e3e1dd;
  --accent: #0f766e;
  --accent-soft: #e6f4f2;
  --ok: #15803d;
  --ok-bg: #e7f6ec;
  --err: #b42318;
  --err-bg: #fdeceb;
  --radius: 16px;
  --radius-sm: 10px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #17171a;
    --card: #222226;
    --ink: #ececf0;
    --muted: #a3a3ab;
    --line: #34343a;
    --accent: #2dd4bf;
    --accent-soft: #123a36;
    --ok: #4ade80;
    --ok-bg: #12281a;
    --err: #fca5a5;
    --err-bg: #331a1a;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: 'Stack Sans', ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--ink);
  margin: 0;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem 1rem;
  line-height: 1.5;
}}
.card {{
  width: 100%;
  max-width: min(1100px, calc(100vw - 2rem));
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: 0 24px 60px -32px rgba(0,0,0,0.35);
  padding: 2.2rem 2.4rem 2.5rem;
}}
.layout {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 2.4rem;
}}
.layout > .col {{ min-width: 0; }}
@media (max-width: 900px) {{
  .layout {{ grid-template-columns: 1fr; }}
  .card {{ padding: 1.6rem 1.25rem 1.7rem; }}
}}
#none-note {{
  display: none;
  margin-top: 1.1rem;
  padding: 0.85rem 1rem;
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--muted);
  font-size: 0.92rem;
}}
#none-note.show {{ display: block; }}
#sec-ai {{ margin-top: 1.15rem; }}
#sec-prompt {{ margin-top: 1.5rem; }}
.panel-note {{ font-size: 0.8rem; color: var(--muted); margin: 0.35rem 0 0; }}
.ai-hidden {{ display: none !important; }}
.brand {{
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 1.25rem;
}}
.logo {{
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: linear-gradient(145deg, #14b8a6, #0f766e);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: 560;
  font-size: 15px;
}}
h1 {{ font-size: 1.35rem; margin: 0; letter-spacing: -0.01em; }}
.sub {{ color: var(--muted); margin: 0.25rem 0 0; font-size: 0.95rem; }}
label {{ display: block; margin: 1.15rem 0 0.4rem; font-weight: 480; font-size: 0.92rem; }}
select, input[type=text], input[type=password], textarea {{
  width: 100%;
  padding: 0.62rem 0.8rem;
  font-size: 0.98rem;
  font-family: inherit;
  color: var(--ink);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}}
select {{
  appearance: none;
  -webkit-appearance: none;
  padding-right: 2.5rem;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%236b6a70' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 0.85rem center;
  cursor: pointer;
}}
select:hover {{
  border-color: var(--accent);
}}
textarea {{
  min-height: 10.5rem;
  max-height: 60vh;
  overflow-y: auto;
  resize: vertical;
  line-height: 1.45;
  font-size: 0.86rem;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}}
/* Full-screen prompt editing (Expand editor button; Esc closes). */
textarea.prompt-fullscreen {{
  position: fixed;
  inset: 1.5rem;
  z-index: 100;
  width: calc(100% - 3rem);
  height: calc(100vh - 3rem);
  max-height: none;
  min-height: 0;
  font-size: 0.95rem;
  resize: none;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
}}
select:focus, input:focus, textarea:focus {{
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}}
.field {{ display: none; }}
.field.active {{ display: block; }}
.custom-select {{ position: relative; width: 100%; }}
.select-trigger {{
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.62rem 0.8rem;
  font-size: 0.98rem;
  font-family: inherit;
  color: var(--ink);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}}
.select-trigger:hover {{ border-color: var(--accent); }}
.select-trigger:focus-visible {{
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}}
.select-trigger .chevron {{
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  transition: transform 0.15s ease;
  color: var(--muted);
}}
.custom-select.open .chevron {{ transform: rotate(180deg); }}
.select-menu {{
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 20;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  box-shadow: 0 16px 40px -20px rgba(0,0,0,0.4);
  padding: 0.35rem;
  opacity: 0;
  transform: translateY(-4px);
  pointer-events: none;
  transition: opacity 0.14s ease, transform 0.14s ease;
}}
.custom-select.open .select-menu {{
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}}
.select-option {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  width: 100%;
  padding: 0.55rem 0.65rem;
  font-size: 0.95rem;
  font-family: inherit;
  color: var(--ink);
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
}}
.select-option:hover {{ background: var(--accent-soft); }}
.select-option.selected {{ background: var(--accent-soft); color: var(--accent); font-weight: 480; }}
.select-option .hint {{ font-size: 0.78rem; color: var(--muted); font-weight: 400; }}
.hotkey-row {{ display: flex; gap: 0.5rem; align-items: center; }}
.hotkey-row input {{ flex: 1; }}
.hotkey-row button {{ flex: 0 0 auto; }}
.mode-switch {{
  display: flex;
  align-items: center;
  gap: 0.85rem;
  margin-top: 0.35rem;
  user-select: none;
}}
.mode-switch .mode-label {{
  font-size: 0.88rem;
  color: var(--muted);
  font-weight: 480;
  min-width: 4.6rem;
}}
.mode-switch .mode-label.on {{ color: var(--ink); }}
.switch {{
  position: relative;
  width: 46px;
  height: 26px;
  flex: 0 0 auto;
}}
.switch input {{
  opacity: 0;
  width: 0;
  height: 0;
  position: absolute;
}}
.switch .slider {{
  position: absolute;
  inset: 0;
  background: var(--line);
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s ease;
}}
.switch .slider::before {{
  content: "";
  position: absolute;
  width: 20px;
  height: 20px;
  left: 3px;
  top: 3px;
  background: #ececf0;
  border-radius: 50%;
  transition: transform 0.15s ease;
}}
.switch input:checked + .slider {{ background: var(--accent); }}
.switch input:checked + .slider::before {{ transform: translateX(20px); }}
.switch input:focus-visible + .slider {{ box-shadow: 0 0 0 3px var(--accent-soft); }}
button.recording {{ background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }}
.actions {{ display: flex; gap: 0.7rem; margin-top: 1.6rem; }}
button {{
  flex: 1;
  padding: 0.68rem 1rem;
  font-size: 0.98rem;
  font-family: inherit;
  font-weight: 480;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  transition: transform 0.06s ease, box-shadow 0.15s ease, background 0.15s ease;
}}
button:active {{ transform: translateY(1px); }}
.primary {{ background: var(--accent); color: #fff; }}
.primary:hover {{ box-shadow: 0 8px 20px -10px rgba(15,118,110,0.7); }}
.secondary {{ background: transparent; color: var(--ink); border-color: var(--line); }}
.secondary:hover {{ background: var(--accent-soft); }}
button:disabled {{ opacity: 0.55; cursor: default; }}
.link {{
  display: block;
  width: 100%;
  margin-top: 1rem;
  padding: 0.5rem;
  background: none;
  border: none;
  color: var(--muted);
  font-size: 0.86rem;
  text-decoration: underline;
  text-underline-offset: 3px;
  font-weight: 400;
}}
.link:hover {{ color: var(--err); }}
.link:disabled {{ opacity: 0.55; cursor: default; }}
.status {{
  margin-top: 1.1rem;
  padding: 0.7rem 0.9rem;
  border-radius: var(--radius-sm);
  font-size: 0.92rem;
  display: none;
}}
.status.show {{ display: flex; align-items: center; gap: 0.5rem; }}
.status.ok {{ background: var(--ok-bg); color: var(--ok); }}
.status.err {{ background: var(--err-bg); color: var(--err); }}
.status.neutral {{ background: var(--accent-soft); color: var(--muted); }}
.spinner {{
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  display: inline-block;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.pull-row {{
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-top: 0.9rem;
}}
.pull-row .secondary {{ flex: 0 0 auto; padding: 0.45rem 0.8rem; }}
.pull-row .hint {{ font-size: 0.78rem; color: var(--muted); }}
.pull-status {{
  margin-top: 0.7rem;
  max-height: 12rem;
  overflow-y: auto;
  white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.78rem;
  line-height: 1.5;
  color: var(--muted);
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
  padding: 0.6rem 0.8rem;
}}
details {{ margin-top: 1.5rem; }}
summary {{ cursor: pointer; font-weight: 480; color: var(--muted); font-size: 0.92rem; }}
code {{ background: var(--accent-soft); padding: 0.1rem 0.35rem; border-radius: 6px; }}
.model-chips {{ display:flex; flex-wrap:wrap; gap:0.35rem; margin-top:0.45rem; }}
.model-chip {{ display:inline-flex; align-items:center; gap:0.3rem; padding:0.18rem 0.45rem; border:1px solid var(--line); border-radius:999px; font-size:0.78rem; background: var(--accent-soft); color: var(--ink); }}
.model-chip button {{ border:none; background:transparent; cursor:pointer; font-size:0.85rem; line-height:1; padding:0 0.1rem; color: var(--muted); }}
.model-chip button:hover {{ color: var(--err); }}
.model-chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.test-modal-backdrop {{ position: fixed; inset: 0; display:flex; align-items:center; justify-content:center; background: rgba(0,0,0,0.35); z-index: 120; padding: 1rem; }}
.test-modal-backdrop[hidden] {{ display: none !important; }}
.test-modal {{ width: min(520px, 100%); background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 1.1rem 1.2rem; box-shadow: 0 24px 60px -20px rgba(0,0,0,0.4); }}
.test-modal h3 {{ margin: 0 0 0.35rem; font-size: 1rem; }}
.test-modal p {{ margin: 0; font-size: 0.92rem; color: var(--muted); white-space: pre-wrap; word-break: break-word; }}
.test-modal .actions {{ margin-top: 0.9rem; display:flex; justify-content:flex-end; }}
</style>
</head>
<body>
<main class="card">
  <div class="brand">
    <span class="logo">O</span>
    <h1>Odicto Setup</h1>
  </div>
  <p class="sub">Pick a backend and paste its key — each backend remembers its own key, so you only ever enter it once. Everything saves to your local <code>.env</code> file.</p>
  <p style="margin:0;font-size:0.78rem;color:var(--muted);">build v3 · per-provider keys</p>

  <form id="setupForm" method="post" action="/save">
    <label for="LLM_PROVIDER">AI backend</label>
    <div class="custom-select" id="provider_select">
      <button type="button" class="select-trigger" id="provider_trigger" aria-haspopup="listbox" aria-expanded="false">
        <span id="provider_label">AI backend</span>
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="select-menu" role="listbox" id="provider_menu">
        <button type="button" class="select-option" data-value="meta" role="option"><span>Meta API</span><span class="hint">cloud</span></button>
        <button type="button" class="select-option" data-value="openrouter" role="option"><span>OpenRouter</span><span class="hint">cloud</span></button>
        <button type="button" class="select-option" data-value="gemini" role="option"><span>Google Gemini</span><span class="hint">cloud</span></button>
        <button type="button" class="select-option" data-value="ollama" role="option"><span>Ollama</span><span class="hint">local</span></button>
        <button type="button" class="select-option" data-value="none" role="option"><span>None</span><span class="hint">raw dictation</span></button>
      </div>
      <input type="hidden" name="LLM_PROVIDER" id="LLM_PROVIDER" value="{html.escape(provider)}">
    </div>
    <p style="margin:0.4rem 0 0;font-size:0.78rem;color:var(--muted);">Only the selected backend's settings are shown below. Keys are saved per backend and kept when you switch.</p>

        <div id="none-note"><strong>Raw dictation only.</strong> Pick a backend above to enable AI replies.</div>
    <div id="sec-ai">
    <div class="layout">
    <div class="col">
      <div class="field" id="field-meta">
        <label>Meta API key{ ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if meta_key else ''}</label>
        <input type="password" name="META_API_KEY" value="{html.escape(meta_key)}" placeholder="paste your Meta API key — quotes are fine">
        <label>Model · Meta</label>
        <select id="meta_model_select" onchange="syncModelSelect('meta')" style="margin-top:0.35rem;"></select>
        <input type="text" id="meta_model_custom" style="display:none;margin-top:0.4rem;" placeholder="custom model id, e.g. muse-spark-1.2">
        <input type="hidden" name="META_MODEL" id="META_MODEL" value="{html.escape(meta_model_seed)}">
        <input type="hidden" name="META_MODEL_HISTORY" id="META_MODEL_HISTORY" value="{html.escape(",".join(meta_hist))}">
        <div id="meta_model_chips" class="model-chips" aria-label="Saved models"></div>
        <p class="panel-note">Backend default: <code>muse-spark-1.2-contributor</code>{' · saved value shown' if already_meta else ''}</p>
        <label>Reasoning effort</label>
        <select id="meta_reasoning_select" onchange="syncEffort('meta')">
          <option value="">Backend default (low)</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="none">none (no reasoning)</option>
        </select>
        <input type="hidden" name="META_REASONING_EFFORT" id="META_REASONING_EFFORT" value="{html.escape(meta_reasoning_seed)}">
      </div>
      <div class="field" id="field-openrouter">
        <label>OpenRouter API key{ ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if or_key else ''}</label>
        <input type="password" name="OPENROUTER_API_KEY" value="{html.escape(or_key)}" placeholder="paste your OpenRouter key — quotes are fine">
        <label>Model · OpenRouter</label>
        <select id="openrouter_model_select" onchange="syncModelSelect('openrouter')" style="margin-top:0.35rem;"></select>
        <input type="text" id="openrouter_model_custom" style="display:none;margin-top:0.4rem;" placeholder="custom slug, e.g. openai/gpt-4o-mini">
        <input type="hidden" name="OPENROUTER_MODEL" id="OPENROUTER_MODEL" value="{html.escape(or_model_seed)}">
        <input type="hidden" name="OPENROUTER_MODEL_HISTORY" id="OPENROUTER_MODEL_HISTORY" value="{html.escape(",".join(or_hist))}">
        <div id="openrouter_model_chips" class="model-chips" aria-label="Saved models"></div>
        <p class="panel-note">Backend default: <code>openai/gpt-5.6-luna</code>{' · saved value shown' if already_or else ''}</p>
      </div>
      <div class="field" id="field-gemini">
        <label>Gemini API key{ ' <span style="font-weight:400;color:var(--ok);">saved — type to replace</span>' if gemini_key else ''}</label>
        <input type="password" name="GEMINI_API_KEY" value="{html.escape(gemini_key)}" placeholder="paste your Gemini API key — quotes are fine">
        <label>Model · Gemini</label>
        <select id="gemini_model_select" onchange="syncModelSelect('gemini')" style="margin-top:0.35rem;"></select>
        <input type="text" id="gemini_model_custom" style="display:none;margin-top:0.4rem;" placeholder="custom model id">
        <input type="hidden" name="GEMINI_MODEL" id="GEMINI_MODEL" value="{html.escape(gemini_model_seed)}">
        <input type="hidden" name="GEMINI_MODEL_HISTORY" id="GEMINI_MODEL_HISTORY" value="{html.escape(",".join(gemini_hist))}">
        <div id="gemini_model_chips" class="model-chips" aria-label="Saved models"></div>
        <p class="panel-note">Backend default: <code>gemini-3.5-flash-lite</code>{' · saved value shown' if already_gemini else ''}</p>
        <label>Thinking level</label>
        <select id="gemini_thinking_select" onchange="syncEffort('gemini')">
          <option value="">Backend default (minimal)</option><option value="minimal">minimal</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option>
        </select>
        <input type="hidden" name="GEMINI_THINKING_LEVEL" id="GEMINI_THINKING_LEVEL" value="{html.escape(gemini_thinking_seed)}">
      </div>
      <div class="field" id="field-ollama">
        <label>Model · Ollama</label>
        <select id="ollama_model_select" onchange="syncModelSelect('ollama')" style="margin-top:0.35rem;"></select>
        <input type="text" id="ollama_model_custom" style="display:none;margin-top:0.4rem;" placeholder="custom tag, e.g. llama3.2:3b">
        <input type="hidden" name="OLLAMA_MODEL" id="OLLAMA_MODEL" value="{html.escape(ollama_model_seed)}">
        <input type="hidden" name="OLLAMA_MODEL_HISTORY" id="OLLAMA_MODEL_HISTORY" value="{html.escape(",".join(ollama_hist))}">
        <div id="ollama_model_chips" class="model-chips" aria-label="Saved models"></div>
        <p class="panel-note">Backend default: <code>qwen2.5:1.5b-instruct</code>{' · saved value shown' if already_ollama else ''} · no key needed</p>
        <label>Ollama API base</label>
        <input type="text" name="LLM_API_BASE" value="{html.escape(ollama_base)}">
        <label>Context window <span style="font-weight:400;color:var(--muted);">(higher = better memory, slower)</span></label>
        <input type="text" name="LLM_NUM_CTX" value="{html.escape(llm_num_ctx)}" placeholder="2048">
        <div class="pull-row">
          <button type="button" id="pull_button" class="secondary" onclick="startPull()">Download local model</button>
          <span class="hint">pulls the model selected for Ollama above</span>
        </div>
        <div id="pull_status" class="pull-status" hidden></div>
      </div>
      <label>Max output tokens <span style="font-weight:400;color:var(--muted);">(shared cap for every backend)</span></label>
      <input type="text" name="LLM_MAX_TOKENS" value="{html.escape(llm_max_tokens)}" placeholder="1024">
    </div><!-- /.col AI -->
    <div class="col" id="sec-prompt">
    <details open>
      <summary>AI system prompt</summary>
      <p style="margin:0.45rem 0 0.5rem;font-size:0.8rem;color:var(--muted);">Used for AI-mode replies (Ctrl+Shift+`). Saved as <code>SYSTEM_PROMPT</code> in <code>.env</code>. Leave blank and save to restore the built-in default. Restart Odicto after saving.</p>
      <textarea name="SYSTEM_PROMPT" id="SYSTEM_PROMPT" spellcheck="false">__SYSTEM_PROMPT__</textarea>
      <div class="pull-row">
        <button type="button" class="secondary" id="prompt_expand" onclick="togglePromptExpand()">Expand editor</button>
        <span class="hint">grows as you type; Expand gives a full-screen view (Esc to close)</span>
      </div>
      <button type="button" class="link" style="margin-top:0.35rem;" onclick="restoreDefaultPrompt()">Restore default prompt</button>
      <label style="margin-top:0.8rem;">Prompt file (optional — wins over the text above)</label>
      <input type="text" name="SYSTEM_PROMPT_FILE" value="{html.escape(system_prompt_file)}" placeholder="prompt.txt">
      <p style="margin:0.3rem 0 0;font-size:0.78rem;color:var(--muted);">When saving with a filename here, the textarea content is written to that plain-text file (UTF-8) next to Odicto — no \n escaping needed.</p>
    </details>
    </div><!-- /.col prompt -->
    </div><!-- /.layout -->
    </div><!-- /#sec-ai -->

    <details open>
      <summary>Speech to text</summary>
      <p style="margin:0.45rem 0 0.5rem;font-size:0.8rem;color:var(--muted);">Independent of the AI backend. Gemini STT uses <code>GEMINI_API_KEY</code> even when replies go through Meta / OpenRouter / Ollama. Smart/verbatim applies to both hold-to-talk chords and the live tap key.</p>
      <label>STT provider</label>
      <select name="STT_PROVIDER" id="STT_PROVIDER">
        <option value="whisper"{" selected" if stt_provider == "whisper" else ""}>whisper — local, offline</option>
        <option value="gemini"{" selected" if stt_provider == "gemini" else ""}>gemini — Gemini 3.5 Transcribe (cloud)</option>
        <option value="auto"{" selected" if stt_provider == "auto" else ""}>auto — Gemini when a key is saved, else Whisper</option>
      </select>
      <label>Transcription mode</label>
      <div class="mode-switch" role="group" aria-label="Transcription mode">
        <span class="mode-label" id="mode_label_verbatim">Verbatim</span>
        <label class="switch">
          <input type="checkbox" id="stt_mode_toggle" {"checked" if transcribe_mode != "verbatim" else ""} onchange="syncTranscribeMode()">
          <span class="slider"></span>
        </label>
        <span class="mode-label" id="mode_label_smart">Smart</span>
      </div>
      <input type="hidden" name="GEMINI_TRANSCRIBE_MODE" id="GEMINI_TRANSCRIBE_MODE" value="{html.escape(transcribe_mode)}">
      <p class="panel-note">Off = verbatim (word-for-word). On = smart (strip ums, apply self-corrections, punctuate). Used on Ctrl+`, Ctrl+Shift+`, and the live tap key.</p>
      <label>Language hint <span style="font-weight:400;color:var(--muted);">(blank = auto-detect)</span></label>
      <input type="text" name="GEMINI_TRANSCRIBE_LANGUAGE" value="{html.escape(transcribe_lang)}" placeholder="en-US">
      <label>Custom vocabulary <span style="font-weight:400;color:var(--muted);">(comma-separated, optional)</span></label>
      <input type="text" name="GEMINI_TRANSCRIBE_VOCABULARY" value="{html.escape(transcribe_vocab)}" placeholder="Odicto, Kubernetes">
      <label>Whisper model</label>
      <input type="text" name="WHISPER_MODEL_SIZE" value="{html.escape(whisper)}">
      <label>Whisper device</label>
      <input type="text" name="WHISPER_DEVICE" value="{html.escape(whisper_device)}" placeholder="auto">
    </details>

    <details>
      <summary>Advanced (hotkeys)</summary>
      <label>Dictation hotkey</label>
      <div class="hotkey-row">
        <input type="text" name="HOTKEY" id="HOTKEY" value="{html.escape(hotkey)}" readonly>
        <button type="button" class="secondary" data-record-for="HOTKEY" onclick="recordHotkey(this)">Press key</button>
      </div>

      <label>AI hotkey</label>
      <div class="hotkey-row">
        <input type="text" name="AI_HOTKEY" id="AI_HOTKEY" value="{html.escape(ai_hotkey)}" readonly>
        <button type="button" class="secondary" data-record-for="AI_HOTKEY" onclick="recordHotkey(this)">Press key</button>
      </div>
      <label>Live tap-to-talk hotkey</label>
      <div class="hotkey-row">
        <input type="text" name="LIVE_HOTKEY" id="LIVE_HOTKEY" value="{html.escape(live_hotkey)}" readonly>
        <button type="button" class="secondary" data-record-for="LIVE_HOTKEY" onclick="recordHotkey(this)">Press key</button>
      </div>
      <p style="margin:0.4rem 0 0;font-size:0.8rem;color:var(--muted);">Hold-to-talk: press and hold the modifier(s), then the main key. Live tap: press once to start, press again to stop and paste (default F7). While Odicto is running, F7 is captured so it does not fire in other apps.</p>
    </details>

    <div class="actions">
      <button type="submit" class="primary">Save settings</button>
      <button type="button" id="test_button" class="secondary" onclick="testConnection()">Test connection</button>
    </div>

    <div id="status" class="status" role="status"></div>
    {server_status}
    <button type="button" class="link" id="reset_button" onclick="resetSettings()">Reset settings to defaults</button>
  
  </form>

  <div id="test_modal" class="test-modal-backdrop" hidden role="dialog" aria-modal="true" aria-labelledby="test_modal_title">
    <div class="test-modal">
      <h3 id="test_modal_title">Testing connection…</h3>
      <p id="test_modal_msg">Contacting the selected backend.</p>
      <div class="actions"><button type="button" class="secondary" onclick="closeTestModal(); return false;">Close</button></div>
    </div>
  </div>
</main>

<script>
var DEFAULT_SYSTEM_PROMPT = __DEFAULT_SYSTEM_PROMPT_JSON__;
// Per-provider built-in default for the shared Model field. The input only
// ever shows what is explicitly configured; an unset model renders empty and
// the provider's default appears as the placeholder + hint text below it.
var MODEL_DEFAULTS = __MODEL_DEFAULTS_JSON__;
var MODEL_CATALOGS = __MODEL_CATALOGS_JSON__;
function restoreDefaultPrompt() {{
  var el = document.getElementById('SYSTEM_PROMPT');
  el.value = DEFAULT_SYSTEM_PROMPT;
  autoGrow(el);
}}
function initModelSelects() {{
  var defaultLabels = {{ meta: 'Backend default (muse-spark-1.2-contributor)', openrouter: 'Backend default (openai/gpt-5.6-luna)', gemini: 'Backend default (gemini-3.5-flash-lite)', ollama: 'Backend default (qwen2.5:1.5b-instruct)' }};
  ['meta','openrouter','gemini','ollama'].forEach(function(p) {{
    var sel = document.getElementById(p + '_model_select');
    if (!sel) return;
    sel.innerHTML = '';
    var o0 = document.createElement('option'); o0.value = ''; o0.textContent = defaultLabels[p] || 'Backend default'; sel.appendChild(o0);
    (MODEL_CATALOGS[p] || []).forEach(function(m) {{
      var o = document.createElement('option'); o.value = m; o.textContent = m; sel.appendChild(o);
    }});
    var oc = document.createElement('option'); oc.value = '__custom__'; oc.textContent = 'Custom\\u2026'; sel.appendChild(oc);
    var hid = document.getElementById(p.toUpperCase() + '_MODEL');
    var cv = hid ? hid.value : '';
    var has = [].slice.call(sel.options).some(function(o){{ return o.value === cv; }});
    if (cv && has) sel.value = cv;
    else if (cv) {{ sel.value = '__custom__'; var ci = document.getElementById(p + '_model_custom'); if (ci) {{ ci.value = cv; ci.style.display = ''; }} }}
  }});
  ['meta','openrouter','gemini','ollama'].forEach(function(p){{ _renderChips(p); }});
}}
function syncModelSelect(p) {{
  var sel = document.getElementById(p + '_model_select');
  var cust = document.getElementById(p + '_model_custom');
  var hid = document.getElementById(p.toUpperCase() + '_MODEL');
  var val = sel ? sel.value : '';
  if (val === '__custom__') {{ if (cust) {{ cust.style.display = ''; cust.focus(); }} if (hid && cust) hid.value = cust.value; }}
  else {{ if (cust) cust.style.display = 'none'; if (hid) hid.value = val; }}
}}
function syncCustomModel(p) {{
  var cust = document.getElementById(p + '_model_custom');
  var hid = document.getElementById(p.toUpperCase() + '_MODEL');
  if (cust && hid) hid.value = cust.value;
}}
function initEffortSelects() {{
  var map = {{ meta: ['meta_reasoning_select','META_REASONING_EFFORT'], gemini: ['gemini_thinking_select','GEMINI_THINKING_LEVEL'] }};
  Object.keys(map).forEach(function(p) {{
    var sel = document.getElementById(map[p][0]), hid = document.getElementById(map[p][1]);
    if (sel && hid && hid.value) {{
      var ok = [].slice.call(sel.options).some(function(o){{ return o.value === hid.value; }});
      if (ok) sel.value = hid.value;
    }}
  }});
}}
function syncEffort(p) {{
  var map = {{ meta: ['meta_reasoning_select','META_REASONING_EFFORT'], gemini: ['gemini_thinking_select','GEMINI_THINKING_LEVEL'] }};
  var pair = map[p]; if (!pair) return;
  var sel = document.getElementById(pair[0]), hid = document.getElementById(pair[1]);
  if (sel && hid) hid.value = sel.value;
}}
function syncTranscribeMode() {{
  var tog = document.getElementById('stt_mode_toggle');
  var hid = document.getElementById('GEMINI_TRANSCRIBE_MODE');
  var on = tog && tog.checked;
  if (hid) hid.value = on ? 'smart' : 'verbatim';
  var v = document.getElementById('mode_label_verbatim');
  var s = document.getElementById('mode_label_smart');
  if (v) v.className = 'mode-label' + (on ? '' : ' on');
  if (s) s.className = 'mode-label' + (on ? ' on' : '');
}}
function initTranscribeMode() {{
  var hid = document.getElementById('GEMINI_TRANSCRIBE_MODE');
  var tog = document.getElementById('stt_mode_toggle');
  if (tog && hid) tog.checked = (hid.value || 'smart') !== 'verbatim';
  syncTranscribeMode();
}}

function _historyTokens(raw){{ return (raw||'').split(',').map(function(s){{return s.trim();}}).filter(Boolean).filter(function(v,i,a){{return a.indexOf(v)===i;}}); }}
function _providerHistory(provider){{
  var id = provider.toUpperCase() + '_MODEL_HISTORY';
  var hid = document.getElementById(id);
  return _historyTokens(hid ? hid.value : '');
}}
function _writeHistory(provider, list){{
  var hid = document.getElementById(provider.toUpperCase() + '_MODEL_HISTORY');
  if(hid) hid.value = list.join(', ');
}}
function _renderChips(provider){{
  var wrap = document.getElementById(provider + '_model_chips');
  var curId = provider.toUpperCase() + '_MODEL';
  var cur = document.getElementById(curId);
  var curVal = cur ? cur.value : '';
  var hist = _providerHistory(provider);
  if(!wrap) return;
  wrap.innerHTML = '';
  hist.forEach(function(m){{
    var chip = document.createElement('span');
    chip.className = 'model-chip' + (m === curVal ? ' active' : '');
    chip.title = m;
    var label = document.createElement('span'); label.textContent = m; chip.appendChild(label);
    var x = document.createElement('button'); x.type='button'; x.setAttribute('aria-label','Remove '+m); x.textContent='\u00d7';
    x.addEventListener('click', function(){{ removeModel(provider, m); }});
    chip.appendChild(x);
    chip.addEventListener('click', function(e){{ if(e.target===x) return; var sel=document.getElementById(provider+'_model_select'); var hid=document.getElementById(curId); if(hid) hid.value=m; if(sel) {{ var has=[].slice.call(sel.options).some(function(o){{return o.value===m;}}); if(!has){{ var o=document.createElement('option'); o.value=m; o.textContent=m; sel.insertBefore(o, sel.querySelector('option[value="__custom__"]')); }} sel.value=m; syncModelSelect(provider); _renderChips(provider);}} }});
    wrap.appendChild(chip);
  }});
}}
function removeModel(provider, model){{
  var list = _providerHistory(provider);
  var idx = list.indexOf(model);
  if(idx === -1) return;
  list.splice(idx,1);
  _writeHistory(provider, list);
  var sel=document.getElementById(provider+'_model_select');
  if(sel){{ var opt=[].slice.call(sel.options).find(function(o){{return o.value===model;}}); if(opt) opt.remove(); if(sel.value===model) {{ sel.value=''; syncModelSelect(provider); }} }}
  var cur=document.getElementById(provider.toUpperCase()+'_MODEL');
  if(cur && cur.value===model) {{ cur.value=''; var sel2=document.getElementById(provider+'_model_select'); if(sel2) sel2.value=''; }}
  _renderChips(provider);
}}
function rememberModel(provider, model){{
  if(!model) return;
  var list=_providerHistory(provider);
  if(list.indexOf(model)===-1){{ list.push(model); _writeHistory(provider, list); var sel=document.getElementById(provider+'_model_select'); if(sel && ![].slice.call(sel.options).some(function(o){{return o.value===model;}})){{ var o=document.createElement('option'); o.value=model; o.textContent=model; sel.insertBefore(o, sel.querySelector('option[value="__custom__"]')); }} _renderChips(provider); }}
}}
function openTestModal(title, msg){{ var m=document.getElementById('test_modal'); if(!m) return; var t=document.getElementById('test_modal_title'); var p=document.getElementById('test_modal_msg'); if(t) t.textContent=title||'Testing connection\u2026'; if(p) p.textContent=msg||'Contacting the selected backend.'; m.hidden=false; m.removeAttribute('hidden'); m.classList.remove('hidden'); m.style.display='flex'; }}
function closeTestModal(){{ var m=document.getElementById('test_modal'); if(!m) return; m.hidden=true; m.setAttribute('hidden',''); m.style.display='none'; }}

function showProvider(v) {{
  ['meta','openrouter','gemini','ollama'].forEach(function(id) {{
    var el = document.getElementById('field-' + id);
    if (el) el.classList.toggle('active', id === v);
  }});
  var noneNote = document.getElementById('none-note');
  if (noneNote) noneNote.classList.toggle('show', v === 'none');
  var secAi = document.getElementById('sec-ai');
  if (secAi) secAi.style.display = (v === 'none') ? 'none' : '';
  var secPrompt = document.getElementById('sec-prompt');
  if (secPrompt) secPrompt.style.display = (v === 'none') ? 'none' : '';
  var tb = document.getElementById('test_button');
  if (tb) tb.disabled = (v === 'none');
}}

var PROVIDER_LABELS = {{
  meta: 'Meta API',
  openrouter: 'OpenRouter',
  gemini: 'Google Gemini',
  ollama: 'Ollama',
  none: 'None'
}};

var _pullTimer = null;

function pollPullStatus() {{
  fetch('/pull-status').then(function(r) {{ return r.json(); }}).then(function(s) {{
    var el = document.getElementById('pull_status');
    if (s.running || s.done) {{
      el.hidden = false;
      el.textContent = s.log || 'Preparing...';
    }}
    if (s.running) {{
      _pullTimer = setTimeout(pollPullStatus, 800);
    }} else if (s.done) {{
      clearTimeout(_pullTimer);
      var btn = document.getElementById('pull_button');
      btn.disabled = false;
      btn.textContent = s.exit_code === 0 ? 'Download complete' : 'Download failed - retry';
      el.textContent = (s.log ? s.log + '\\n' : '') + (s.exit_code === 0 ? 'Done. The model is ready for local AI.' : 'The download failed. Check that Ollama is running and retry.');
    }}
  }}).catch(function() {{ /* server may be busy; keep polling */ _pullTimer = setTimeout(pollPullStatus, 1500); }});
}}

async function startPull() {{
  var btn = document.getElementById('pull_button');
  var el = document.getElementById('pull_status');
  btn.disabled = true;
  el.hidden = false;
  el.textContent = 'Starting download...';
  try {{
    var resp = await fetch('/pull-ollama', {{
      method: 'POST',
      body: new URLSearchParams({{ OLLAMA_MODEL: (document.getElementById('OLLAMA_MODEL') ? document.getElementById('OLLAMA_MODEL').value : '') || 'qwen2.5:1.5b-instruct' }}),
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }}
    }});
    var data = await resp.json();
    if (!data.ok) {{
      btn.disabled = false;
      el.textContent = data.message;
      return;
    }}
    btn.textContent = 'Downloading...';
    clearTimeout(_pullTimer);
    _pullTimer = setTimeout(pollPullStatus, 600);
  }} catch (e) {{
    btn.disabled = false;
    el.textContent = 'Could not reach the local server.';
  }}
}}

function initCustomSelect() {{
  var trigger = document.getElementById('provider_trigger');
  var menu = document.getElementById('provider_menu');
  var select = document.getElementById('provider_select');
  var hidden = document.getElementById('LLM_PROVIDER');
  var label = document.getElementById('provider_label');

  function render() {{
    var v = hidden.value || 'none';
    label.textContent = PROVIDER_LABELS[v] || v;
    menu.querySelectorAll('.select-option').forEach(function(opt) {{
      opt.classList.toggle('selected', opt.getAttribute('data-value') === v);
    }});
    showProvider(v);
  }}

  function close() {{
    select.classList.remove('open');
    trigger.setAttribute('aria-expanded', 'false');
  }}

  trigger.addEventListener('click', function(e) {{
    e.stopPropagation();
    var isOpen = select.classList.contains('open');
    close();
    if (!isOpen) {{
      select.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
    }}
  }});

  menu.addEventListener('click', function(e) {{
    var opt = e.target.closest('.select-option');
    if (!opt) return;
    hidden.value = opt.getAttribute('data-value');
    render();
    close();
  }});

  document.addEventListener('click', function(e) {{
    if (!select.contains(e.target)) close();
  }});

  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') close();
  }});

  render();
}}

initCustomSelect();
initModelSelects();
initEffortSelects();
initTranscribeMode();
['meta','openrouter','gemini','ollama'].forEach(function(p){{
  var ci=document.getElementById(p+'_model_custom');
  if(ci) ci.addEventListener('input', function(){{ syncCustomModel(p); }});
}});

// --- System prompt textarea: auto-grow + full-screen editor -----------------
var promptEl = document.getElementById('SYSTEM_PROMPT');
var expandBtn = document.getElementById('prompt_expand');

function autoGrow(el) {{
  if (el.classList.contains('prompt-fullscreen')) return; // fixed height there
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight + 2, window.innerHeight * 0.6) + 'px';
}}
promptEl.addEventListener('input', function() {{ autoGrow(promptEl); }});

function togglePromptExpand() {{
  var open = promptEl.classList.toggle('prompt-fullscreen');
  expandBtn.textContent = open ? 'Close editor (Esc)' : 'Expand editor';
  document.body.style.overflow = open ? 'hidden' : '';
  if (open) {{
    promptEl.style.height = '';
    promptEl.focus();
    promptEl.setSelectionRange(0, 0);
  }} else {{
    autoGrow(promptEl);
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape' && promptEl.classList.contains('prompt-fullscreen')) {{
    togglePromptExpand();
  }}
}});
autoGrow(promptEl);

var hotkeyRecorder = null;
var MODIFIER_NAMES = {{ Control: 'ctrl', Shift: 'shift', Alt: 'alt', Meta: 'cmd' }};

function browserKeyToAppKey(key, code) {{
  var k = (key || '').toLowerCase();
  if (k === 'control') return 'ctrl';
  if (k === 'shift') return 'shift';
  if (k === 'alt') return 'alt';
  if (k === 'meta') return 'cmd';
  if (k === ' ') return 'space';
  if (k === '`' || code === 'Backquote') return 'grave';
  if (k === 'escape') return 'esc';
  if (k === 'arrowup') return 'up';
  if (k === 'arrowdown') return 'down';
  if (k === 'arrowleft') return 'left';
  if (k === 'arrowright') return 'right';
  if (k === 'pageup') return 'page up';
  if (k === 'pagedown') return 'page down';
  if (k.length === 1) return k;
  return k;
}}

function recordHotkey(btn) {{
  if (hotkeyRecorder) {{
    stopHotkeyRecording(true);
  }}
  hotkeyRecorder = {{
    btn: btn,
    targetId: btn.getAttribute('data-record-for'),
    modifiers: [],
    primary: null,
    pressed: new Set(),
    keydown: null,
    keyup: null
  }};
  btn.classList.add('recording');
  btn.textContent = 'Press now...';
  document.getElementById(hotkeyRecorder.targetId).value = '';

  hotkeyRecorder.keydown = function(e) {{
    e.preventDefault();
    e.stopPropagation();
    var appKey = browserKeyToAppKey(e.key, e.code);
    if (MODIFIER_NAMES[e.key]) {{
      appKey = MODIFIER_NAMES[e.key];
      if (!hotkeyRecorder.pressed.has(appKey)) {{
        hotkeyRecorder.pressed.add(appKey);
        hotkeyRecorder.modifiers.push(appKey);
      }}
    }} else if (!hotkeyRecorder.pressed.has(appKey) && !hotkeyRecorder.primary) {{
      hotkeyRecorder.pressed.add(appKey);
      hotkeyRecorder.primary = appKey;
    }}
    renderHotkey();
  }};

  hotkeyRecorder.keyup = function(e) {{
    var appKey = MODIFIER_NAMES[e.key] || browserKeyToAppKey(e.key, e.code);
    hotkeyRecorder.pressed.delete(appKey);
    if (hotkeyRecorder.pressed.size === 0 && hotkeyRecorder.primary) {{
      stopHotkeyRecording(false);
    }}
  }};

  document.addEventListener('keydown', hotkeyRecorder.keydown, true);
  document.addEventListener('keyup', hotkeyRecorder.keyup, true);
  setTimeout(function() {{
    if (hotkeyRecorder && hotkeyRecorder.btn === btn) {{
      stopHotkeyRecording(false);
    }}
  }}, 6000);
}}

function renderHotkey() {{
  var rec = hotkeyRecorder;
  if (!rec) return;
  var parts = rec.modifiers.slice();
  if (rec.primary) parts.push(rec.primary);
  document.getElementById(rec.targetId).value = parts.join('+');
}}

function stopHotkeyRecording(cancel) {{
  var rec = hotkeyRecorder;
  if (!rec) return;
  document.removeEventListener('keydown', rec.keydown, true);
  document.removeEventListener('keyup', rec.keyup, true);
  rec.btn.classList.remove('recording');
  rec.btn.textContent = 'Press key';
  if (cancel) {{
    document.getElementById(rec.targetId).value = '';
  }}
  hotkeyRecorder = null;
}}

function setStatus(kind, html) {{
  var el = document.getElementById('status');
  el.className = 'status show ' + kind;
  el.innerHTML = html;
}}

async function testConnection() {{
  var btn = document.getElementById('test_button');
  btn.disabled = true;
  openTestModal('Testing connection\u2026', 'Contacting the selected backend. This may take a few seconds.');
  // Keep the old inline status in sync too (useful if the modal is dismissed).
  setStatus('neutral', '<span class="spinner"></span> Testing connection...');
  var controller = new AbortController();
  var timer = setTimeout(function() {{ controller.abort(); }}, 30000);
  try {{
    // Snapshot the hidden model value's provider so we can grow history on success.
    var _lpEl = document.getElementById('LLM_PROVIDER'); var provider = (_lpEl && _lpEl.value) ? _lpEl.value : 'none';
    var modelForHistory = '';
    if(provider !== 'none'){{ var hid=document.getElementById(provider.toUpperCase()+'_MODEL'); modelForHistory = hid ? hid.value : ''; }}
    var body = new URLSearchParams(new FormData(document.getElementById('setupForm')));
    var resp = await fetch('/test', {{
      method: 'POST',
      body: body,
      signal: controller.signal,
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }}
    }});
    var ct = resp.headers.get('content-type') || '';
    if(!ct.includes('application/json')){{ var txt = await resp.text(); var snippet = (txt||'').slice(0,800); throw new Error('The test endpoint returned a non-JSON response. Is the setup server still running? ' + snippet); }}
    var data = await resp.json();
    if (data.ok) {{
      setStatus('ok', '&#10003; ' + data.message);
      document.getElementById('test_modal_title').textContent = 'Connection OK';
      document.getElementById('test_modal_msg').textContent = data.message || 'Connected successfully.';
      if(provider!=='none' && modelForHistory) rememberModel(provider, modelForHistory);
    }} else {{
      setStatus('err', '&#9888; ' + data.message);
      document.getElementById('test_modal_title').textContent = 'Test failed';
      document.getElementById('test_modal_msg').textContent = data.message || 'The backend rejected the request.';
    }}
  }} catch (e) {{
    var msg = (e && e.message) ? e.message : String(e);
    if (e && e.name === 'AbortError') {{
      msg = 'Test timed out after 30s. Check your network and key, then try again.';
    }} else if (/Failed to fetch|Could not reach/i.test(msg)) {{
      msg = 'Could not reach the local setup server at 127.0.0.1. Is it still running? Try reopening the setup page via .\\setup.bat (or odicto.py setup) and retry.';
    }}
    setStatus('err', '&#9888; ' + msg);
    var mt2=document.getElementById('test_modal_title'); if(mt2) mt2.textContent='Test failed';
    var mm2=document.getElementById('test_modal_msg'); if(mm2) mm2.textContent=msg;
  }} finally {{
    clearTimeout(timer);
    btn.disabled = (document.getElementById('LLM_PROVIDER').value === 'none');
  }}
}}
document.addEventListener('keydown', function(e){{ if(e.key==='Escape') closeTestModal(); }});
(function(){{ var m=document.getElementById('test_modal'); if(!m) return; m.addEventListener('click', function(e){{ if(e.target===m) closeTestModal(); }}); if(!m.hasAttribute('hidden')){{ m.setAttribute('hidden',''); m.style.display='none'; }} }})();

async function resetSettings() {{
  if (!confirm('Reset settings to defaults? This clears any saved API keys and model choices.')) {{
    return;
  }}
  var btn = document.getElementById('reset_button');
  btn.disabled = true;
  setStatus('neutral', '<span class="spinner"></span> Resetting to defaults...');
  try {{
    var resp = await fetch('/reset', {{ method: 'POST' }});
    var data = await resp.json();
    if (data.ok) {{
      setStatus('ok', '&#10003; ' + data.message);
      location.reload();
    }} else {{
      setStatus('err', '&#9888; ' + data.message);
      btn.disabled = false;
    }}
  }} catch (e) {{
    setStatus('err', '&#9888; Could not reach the local server.');
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>
"""
    return (
        page.replace("__SYSTEM_PROMPT__", html.escape(system_prompt))
        .replace("__DEFAULT_SYSTEM_PROMPT_JSON__", json.dumps(DEFAULT_SYSTEM_PROMPT))
        .replace("__MODEL_DEFAULTS_JSON__", model_defaults_json)
        .replace("__MODEL_CATALOGS_JSON__", model_catalogs_json)
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "OdictoSetup/1.0"

    def do_GET(self) -> None:
        if self.path == "/pull-status":
            self._send_json(pull_status())
            return
        if self.path != "/":
            self.send_error(404)
            return
        body = _page().encode("utf-8")
        self._send(body)

    def do_POST(self) -> None:
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

        # Prompt file mode: the textarea content becomes the file's plain-text
        # content (no \n escaping), and SYSTEM_PROMPT_FILE points at it.
        prompt_file = (updates.get("SYSTEM_PROMPT_FILE") or "").strip()
        if prompt_file:
            err = write_prompt_file(prompt_file, updates.get("SYSTEM_PROMPT", ""))
            if err:
                body = _page(err, "err").encode("utf-8")
                self._send(body)
                return

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
        body = _page("Settings saved. Restart Odicto to apply.", "ok").encode("utf-8")
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
