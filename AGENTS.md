# Agent install guide — Odicto

This file is for coding agents (and power users) automating setup on a **fresh machine**.

## Goal

Make the app runnable end-to-end: venv, Python deps, optional Ollama LLM,
Whisper weights, `.env`, then verify with unit tests / a dry launch.

## Constraints

- Cross-platform: **Windows**, **macOS**, and **Linux**.
- Never commit `.env` (may contain API keys).
- Prefer the project installer (`install.ps1` on Windows, `install.sh` on
  macOS/Linux) over ad-hoc steps.
- Installers are **uv-first** (fast, hash-verified, no system Python needed);
  they fall back to pip when uv cannot be obtained.

## One-shot install (preferred)

From the repository root.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Flags: `-Ollama` (opt-in: install Ollama + pull the default local model),
`-OllamaModel qwen2.5:1.5b-instruct`, `-WhisperModel tiny.en`.

### macOS / Linux

```bash
bash install.sh
# or: bash install.sh -Ollama
```

Ollama + its model are **opt-in** (`-Ollama`); without the flag nothing
local-AI related downloads. The setup page has a "Download local model"
button that pulls the model only when the user picks Ollama as provider.

## Manual checklist (if the script fails)

### Windows

1. Install **uv** (`winget install astral-sh.uv`), or install **Python 3.10+**
   (`winget install Python.Python.3.12`) for the pip path.
2. uv path: `uv venv .venv` then `uv pip install --python .venv -r requirements.txt`
   pip path: `py -3 -m venv .venv`, `.\.venv\Scripts\python.exe -m pip install -U pip wheel`, `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
3. `copy .env.example .env`
4. Optional AI: install [Ollama](https://ollama.com/download), then `ollama pull qwen2.5:1.5b-instruct`
5. Warm Whisper: `.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
6. Tests: `.\.venv\Scripts\python.exe -m unittest test_units -v`
7. Start: `.\start_dictation.bat` or `.\run_debug.bat`

### macOS

1. Install **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`), or install
   **Python 3.10+** (`brew install python`) for the pip path.
2. uv path: `uv venv .venv` then `uv pip install --python .venv -r requirements.txt`
   pip path: `python3 -m venv .venv`, `.venv/bin/python -m pip install -U pip wheel`, `.venv/bin/python -m pip install -r requirements.txt`
3. `cp .env.example .env`
4. Optional AI: `brew install ollama && ollama pull qwen2.5:1.5b-instruct`
5. Warm Whisper: `.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
6. Tests: `.venv/bin/python -m unittest test_units -v`
7. Start: `./run_debug.sh` (grant Accessibility + Input Monitoring when prompted)

### Linux

1. Install **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`), or install
   **Python 3.10+** with your distro package manager for the pip path.
2. uv path: `uv venv .venv` then `uv pip install --python .venv -r requirements.txt`
   pip path: `python3 -m venv .venv`, `.venv/bin/python -m pip install -U pip wheel`, `.venv/bin/python -m pip install -r requirements.txt`
3. `cp .env.example .env`
4. Optional AI: install Ollama from [ollama.com/download](https://ollama.com/download), then `ollama pull qwen2.5:1.5b-instruct`
5. Warm Whisper: `.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
6. Tests: `.venv/bin/python -m unittest test_units -v`
7. Start: `./run_debug.sh` (run as root or with `input` group; prefer X11)

## Verify success

| Check | Expected |
|-------|----------|
| Import check (`import PySide6, faster_whisper, keyboard`) | No import error |
| `python -m unittest test_units -v` | All tests OK |
| `run_debug.bat` / `./run_debug.sh` | Log shows `Application ready!` and `HUD enabled` |
| Hold hotkey | Bottom-center pill shows **Listening** |

## Setup page

After install, configure a provider and API key without hand-editing `.env`:

```bash
# Windows
.\setup.bat
# or: .\.venv\Scripts\python.exe odicto.py setup

# macOS / Linux
./setup.sh
# or: .venv/bin/python odicto.py setup
```

The page writes `.env` atomically, preserves unsubmitted keys, and can test the
selected provider before saving.

## Runtime notes for agents

- Default hotkeys: hold **Ctrl+`** (`HOTKEY=ctrl+grave`) for raw dictation;
  hold **Ctrl+Shift+`** (`AI_HOTKEY=ctrl+shift+grave`) for a **fresh** AI reply
  (no previous conversation). Hold **F6** + **Ctrl+`** (`CTRL_KEEP_CONTEXT_KEYS`)
  to keep / continue AI memory. Keyboard lib name for `` ` `` is `grave`.
  Avoid Alt chords (browser focus loss on Alt release).
- First Whisper load downloads model weights (~75MB for `tiny.en`). Whisper
  always loads for STT, independent of LLM provider.
- First Ollama pull downloads the LLM (size depends on model). Odicto only
  starts/calls Ollama when `LLM_PROVIDER=ollama`.
- **OpenRouter:** set `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, and
  `OPENROUTER_MODEL`. Localhost `LLM_API_BASE` is auto-rewritten to
  `OPENROUTER_API_BASE`. Odicto will **not** spawn Ollama in this mode.
- **Meta:** set `META_API_KEY` (or `MODEL_API_KEY`) and `META_MODEL`. Default
  model is `muse-spark-1.2-contributor` — do not silently fall back to the
  base `muse-spark-1.2` SKU.
- **Gemini:** set `LLM_PROVIDER=gemini`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`),
  and `GEMINI_MODEL` (default `gemini-3.5-flash-lite`). Uses the GA Interactions API
  via the `google-genai` SDK (`client.interactions.create`). Optional
  `GEMINI_THINKING_LEVEL` (`minimal|low|medium|high`, default `minimal`).
  Odicto will **not** spawn Ollama in this mode.
- **Provider `none`:** raw dictation only; no LLM client; Ollama not started.
- macOS requires **Accessibility** and **Input Monitoring** permissions for
  `pynput` global hooks and synthetic copy/paste.
- Linux global suppression usually requires root or `input` group; prefer X11.
- GPU: if CUDA is available, Whisper uses it automatically
  (`WHISPER_DEVICE=auto`). macOS uses CPU (int8) — faster-whisper has no Metal
  backend.
- Stop with `stop_dictation.bat` / `./stop_dictation.sh`, or
  `.venv/bin/python odicto.py stop`.

## STRICT: single instance only (never stack keyboard hooks)

**Why normal typing is related to Odicto:** the hold-to-talk hotkey installs a
**system-wide** keyboard hook with suppression. While Odicto is running,
**every keypress in every app** goes through that hook — not only the chord and
not only while recording. A second Odicto process installing a second hook can
deliver each character twice (`tthhiiss`). That is not the mic, Whisper, or paste
path; it is the global hook layer.

The Windows mutex + lockfile layering is preserved. On macOS/Linux the
equivalent single-instance gate is an exclusive `fcntl.flock` on
`dictation.lock` plus process enumeration.

**Hard rules for agents and operators:**

1. **Never run two Odicto `main.py` processes** for the same install.
2. Before a new start, prefer the stop/start scripts (they stop first).
3. Startup **must** acquire the install-scoped lock and kill orphan `main.py`
   processes; if the lock cannot be taken, **exit without** installing hooks.
4. **Never** install global hotkey hooks unless the single-instance lock is held.
5. On shutdown, always release hooks and the lock so typing returns to normal.
6. If the user reports **double letters while typing**, assume stacked instances
   first: stop all, confirm no `main.py` left, start **once**.
7. Do **not** “fix” orphan kill by restoring the old WMIC
   `for /f tokens=2 delims==` batch loop.

## Do not

- Do not publish `.env` or API keys (`OPENROUTER_API_KEY` especially).
- Do not hardcode OS-specific paths in docs/scripts; use `platforms.base`.
- Do not replace the hotkey/paste behavior without user request.
- Do not assume Ollama is stopped system-wide just because `LLM_PROVIDER` is not
  `ollama` — only Odicto’s own spawn path is skipped.
- Do not allow multiple Odicto instances / stacked keyboard hooks.
