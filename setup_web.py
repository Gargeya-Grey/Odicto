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

from config import Config, DEFAULT_SYSTEM_PROMPT

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover
    dotenv_values = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example")

# Keys the page is allowed to write, in provider groups. Unsubmitted keys are
# preserved when the .env file is merged.
EDITABLE_KEYS = {
    "LLM_PROVIDER",
    "META_API_KEY",
    "MODEL_API_KEY",
    "META_MODEL",
    "META_API_BASE",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_API_BASE",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_THINKING_LEVEL",
    "GEMINI_MAX_OUTPUT_TOKENS",
    "LLM_MODEL",
    "LLM_API_BASE",
    "WHISPER_MODEL_SIZE",
    "HOTKEY",
    "AI_HOTKEY",
    "SYSTEM_PROMPT",
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
        "MODEL_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    )


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


def merge_env(updates: dict) -> None:
    """Merge submitted values into .env, preserving every unsubmitted key.

    Existing keys are updated in place (line order preserved); new editable
    keys are appended at the end. Comments and unrelated keys are untouched.
    """
    original_lines: list[str] = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            original_lines = f.read().splitlines()

    cleaned: dict[str, str] = {}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            continue
        if value == _MASKED:
            continue
        value = value.strip()
        cleaned[key] = value

    updated: set = set()
    out_lines: list[str] = []
    for line in original_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in cleaned:
                if cleaned[key]:
                    out_lines.append(_format_env_assignment(key, cleaned[key]))
                # Empty submitted value: drop the line.
                updated.add(key)
                continue
        out_lines.append(line)

    for key in sorted(cleaned):
        if key not in updated and cleaned[key]:
            out_lines.append(_format_env_assignment(key, cleaned[key]))

    text = "\n".join(out_lines).rstrip() + "\n"
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
    provider = current.get("LLM_PROVIDER", "meta")
    meta_key = current.get("META_API_KEY", "") or current.get("MODEL_API_KEY", "")
    or_key = current.get("OPENROUTER_API_KEY", "")
    gemini_key = current.get("GEMINI_API_KEY", "") or current.get("GOOGLE_API_KEY", "")
    ollama_base = current.get("LLM_API_BASE", "http://localhost:11434/v1")
    meta_model = current.get("META_MODEL", "muse-spark-1.2-contributor")
    or_model = current.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
    gemini_model = current.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_thinking = current.get("GEMINI_THINKING_LEVEL", "minimal")
    llm_model = current.get("LLM_MODEL", "qwen2.5:1.5b-instruct")
    whisper = current.get("WHISPER_MODEL_SIZE", "tiny.en")
    hotkey = current.get("HOTKEY", "ctrl+grave")
    ai_hotkey = current.get("AI_HOTKEY", "ctrl+shift+grave")
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
  max-width: 520px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: 0 24px 60px -32px rgba(0,0,0,0.35);
  padding: 2rem 2rem 2.2rem;
}}
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
  resize: vertical;
  line-height: 1.45;
  font-size: 0.86rem;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
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
</style>
</head>
<body>
<main class="card">
  <div class="brand">
    <span class="logo">O</span>
    <h1>Odicto Setup</h1>
  </div>
  <p class="sub">Pick a backend, paste its key, and optionally edit the AI system prompt. Everything saves to your local <code>.env</code> file.</p>
  <p style="margin:0;font-size:0.78rem;color:var(--muted);">build v2 · inline test feedback</p>

  <form id="setupForm" method="post" action="/save">
    <label for="LLM_PROVIDER">AI backend</label>
    <div class="custom-select" id="provider_select">
      <button type="button" class="select-trigger" id="provider_trigger" aria-haspopup="listbox" aria-expanded="false">
        <span id="provider_label">Meta API (default)</span>
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="select-menu" role="listbox" id="provider_menu">
        <button type="button" class="select-option" data-value="meta" role="option"><span>Meta API</span><span class="hint">default</span></button>
        <button type="button" class="select-option" data-value="openrouter" role="option"><span>OpenRouter</span><span class="hint">cloud</span></button>
        <button type="button" class="select-option" data-value="gemini" role="option"><span>Google Gemini</span><span class="hint">cloud</span></button>
        <button type="button" class="select-option" data-value="ollama" role="option"><span>Ollama</span><span class="hint">local</span></button>
        <button type="button" class="select-option" data-value="none" role="option"><span>None</span><span class="hint">raw dictation</span></button>
      </div>
      <input type="hidden" name="LLM_PROVIDER" id="LLM_PROVIDER" value="{html.escape(provider)}">
    </div>

    <div class="field" id="field-meta">
      <label>Meta API key</label>
      <input type="password" name="META_API_KEY" value="{html.escape(meta_key)}" placeholder="sk-meta-...">
      <label>Meta model</label>
      <input type="text" name="META_MODEL" value="{html.escape(meta_model)}">
    </div>

    <div class="field" id="field-openrouter">
      <label>OpenRouter API key</label>
      <input type="password" name="OPENROUTER_API_KEY" value="{html.escape(or_key)}" placeholder="sk-or-...">
      <label>OpenRouter model</label>
      <input type="text" name="OPENROUTER_MODEL" value="{html.escape(or_model)}">
    </div>

    <div class="field" id="field-gemini">
      <label>Gemini API key</label>
      <input type="password" name="GEMINI_API_KEY" value="{html.escape(gemini_key)}" placeholder="AIza...">
      <label>Gemini model</label>
      <input type="text" name="GEMINI_MODEL" value="{html.escape(gemini_model)}">
      <label>Thinking level</label>
      <input type="text" name="GEMINI_THINKING_LEVEL" value="{html.escape(gemini_thinking)}" placeholder="minimal">
    </div>

    <div class="field" id="field-ollama">
      <label>Ollama model</label>
      <input type="text" name="LLM_MODEL" value="{html.escape(llm_model)}">
      <label>Ollama API base</label>
      <input type="text" name="LLM_API_BASE" value="{html.escape(ollama_base)}">
      <div class="pull-row">
        <button type="button" id="pull_button" class="secondary" onclick="startPull()">Download local model</button>
        <span class="hint">only downloads when you choose local AI</span>
      </div>
      <div id="pull_status" class="pull-status" hidden></div>
    </div>

    <details open>
      <summary>AI system prompt</summary>
      <p style="margin:0.45rem 0 0.5rem;font-size:0.8rem;color:var(--muted);">Used for AI-mode replies (Ctrl+Shift+`). Saved as <code>SYSTEM_PROMPT</code> in <code>.env</code>. Leave blank and save to restore the built-in default. Restart Odicto after saving.</p>
      <textarea name="SYSTEM_PROMPT" id="SYSTEM_PROMPT" spellcheck="false">__SYSTEM_PROMPT__</textarea>
      <button type="button" class="link" style="margin-top:0.35rem;" onclick="restoreDefaultPrompt()">Restore default prompt</button>
    </details>

    <details>
      <summary>Advanced (Whisper + hotkeys)</summary>
      <label>Whisper model</label>
      <input type="text" name="WHISPER_MODEL_SIZE" value="{html.escape(whisper)}">

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
      <p style="margin:0.4rem 0 0;font-size:0.8rem;color:var(--muted);">Press and hold the modifier(s), then the main key. Release all keys to save the chord.</p>
    </details>

    <div class="actions">
      <button type="submit" class="primary">Save settings</button>
      <button type="button" id="test_button" class="secondary" onclick="testConnection()">Test connection</button>
    </div>

    <div id="status" class="status" role="status"></div>
    {server_status}
    <button type="button" class="link" id="reset_button" onclick="resetSettings()">Reset settings to defaults</button>
  </form>
</main>

<script>
var DEFAULT_SYSTEM_PROMPT = __DEFAULT_SYSTEM_PROMPT_JSON__;
function restoreDefaultPrompt() {{
  document.getElementById('SYSTEM_PROMPT').value = DEFAULT_SYSTEM_PROMPT;
}}
function showProvider(v) {{
  ['meta','openrouter','gemini','ollama'].forEach(function(id) {{
    document.getElementById('field-' + id).classList.toggle('active', id === v);
  }});
  document.getElementById('test_button').disabled = (v === 'none');
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
      body: new URLSearchParams({{ LLM_MODEL: document.querySelector('input[name="LLM_MODEL"]').value || 'qwen2.5:1.5b-instruct' }}),
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
    var v = hidden.value || 'meta';
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
  setStatus('neutral', '<span class="spinner"></span> Testing connection...');
  var controller = new AbortController();
  var timer = setTimeout(function() {{ controller.abort(); }}, 30000);
  try {{
    var body = new URLSearchParams(new FormData(document.getElementById('setupForm')));
    var resp = await fetch('/test', {{
      method: 'POST',
      body: body,
      signal: controller.signal,
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }}
    }});
    var data = await resp.json();
    if (data.ok) {{
      setStatus('ok', '&#10003; ' + data.message);
    }} else {{
      setStatus('err', '&#9888; ' + data.message);
    }}
  }} catch (e) {{
    if (e && e.name === 'AbortError') {{
      setStatus('err', '&#9888; Test timed out after 30s. Check your network and key, then try again.');
    }} else {{
      setStatus('err', '&#9888; Could not reach the local server.');
    }}
  }} finally {{
    clearTimeout(timer);
    btn.disabled = (document.getElementById('LLM_PROVIDER').value === 'none');
  }}
}}

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
            model = (form.get("LLM_MODEL") or ["qwen2.5:1.5b-instruct"])[0].strip()
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

        provider = (form.get("LLM_PROVIDER") or ["meta"])[0].strip().lower()
        if provider in ("meta", "meta_api", "meta-api"):
            provider = "meta"
        if provider in ("gemini", "gemini_api", "google", "google_api", "google-api"):
            provider = "gemini"

        def pick(key: str, default: str = "") -> str:
            value = (form.get(key) or [""])[0].strip()
            # A masked field means the user left an existing secret unchanged;
            # fall back to the real stored value for testing only.
            if value == _MASKED:
                return read_env_raw().get(key, default)
            # Old cached pages may omit the field entirely; use the stored value.
            if not value and key not in form:
                return read_env_raw().get(key, default)
            return value or default

        if provider == "meta":
            api_key = pick("META_API_KEY") or pick("MODEL_API_KEY")
            model = pick("META_MODEL", "muse-spark-1.2-contributor")
            api_base = pick("META_API_BASE", "https://api.meta.ai/v1")
        elif provider == "openrouter":
            api_key = pick("OPENROUTER_API_KEY")
            model = pick("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
            api_base = pick("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
        elif provider == "gemini":
            api_key = pick("GEMINI_API_KEY") or pick("GOOGLE_API_KEY")
            model = pick("GEMINI_MODEL", "gemini-3.5-flash-lite")
            api_base = ""
        elif provider == "ollama":
            api_key = ""
            model = pick("LLM_MODEL", "qwen2.5:1.5b-instruct")
            api_base = pick("LLM_API_BASE", "http://localhost:11434/v1")
        else:
            api_key = ""
            model = ""
            api_base = ""

        result = test_provider(provider, api_key, model, api_base)
        ok = result == "ok"
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

        hotkey = updates.get("HOTKEY") or Config.HOTKEY
        ai_hotkey = updates.get("AI_HOTKEY") or Config.AI_HOTKEY
        try:
            validate_hotkey_pair(hotkey, ai_hotkey)
        except Exception as e:
            body = _page(f"Hotkey invalid: {e}", "err").encode("utf-8")
            self._send(body)
            return

        merge_env(updates)
        # Validate against the freshly merged .env, not the import-time Config
        # (Config class attributes are loaded once at server start and would
        # print a stale "META_API_KEY is empty" warning after a successful save).
        merged = read_env_raw()
        provider = (
            updates.get("LLM_PROVIDER") or merged.get("LLM_PROVIDER") or "meta"
        ).strip().lower()
        if provider not in ("meta", "ollama", "openrouter", "gemini", "none"):
            body = _page(f"Saved, but LLM_PROVIDER '{provider}' is invalid.", "err").encode("utf-8")
            self._send(body)
            return
        if provider == "openrouter":
            key = updates.get("OPENROUTER_API_KEY") or merged.get("OPENROUTER_API_KEY", "")
            if not key:
                body = _page(
                    "Saved, but OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter.",
                    "err",
                ).encode("utf-8")
                self._send(body)
                return
        if provider == "gemini":
            key = (
                updates.get("GEMINI_API_KEY")
                or updates.get("GOOGLE_API_KEY")
                or merged.get("GEMINI_API_KEY", "")
                or merged.get("GOOGLE_API_KEY", "")
            )
            if not key:
                body = _page(
                    "Saved, but GEMINI_API_KEY is required when LLM_PROVIDER=gemini.",
                    "err",
                ).encode("utf-8")
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
