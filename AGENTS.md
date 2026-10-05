# Agent install guide — Odicto

This file is for coding agents (and power users) automating setup on a **fresh machine**.

> **Before changing code, read [`docs/architecture.md`](docs/architecture.md)** — the canonical
> module map, diagram set, "where do I change X?" cookbook, and the invariants not to break.
> [`docs/engineering-notes.md`](docs/engineering-notes.md) records why the code is shaped this way,
> including refactorings that were deliberately rejected and the coupling that makes them unsafe.
> Run `.\tools\verify.ps1` after any change; it is the whole safety net in one command.

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
6. Tests: `.\.venv\Scripts\python.exe -m unittest tests.test_units -v`
7. Start: `.\start_dictation.bat` or `.\scripts\windows\run_debug.bat`

### macOS

1. Install **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`), or install
   **Python 3.10+** (`brew install python`) for the pip path.
2. uv path: `uv venv .venv` then `uv pip install --python .venv -r requirements.txt`
   pip path: `python3 -m venv .venv`, `.venv/bin/python -m pip install -U pip wheel`, `.venv/bin/python -m pip install -r requirements.txt`
3. `cp .env.example .env`
4. Optional AI: `brew install ollama && ollama pull qwen2.5:1.5b-instruct`
5. Warm Whisper: `.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
6. Tests: `.venv/bin/python -m unittest tests.test_units -v`
7. Start: `bash scripts/posix/run_debug.sh` (grant Accessibility + Input Monitoring when prompted)

### Linux

1. Install **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`), or install
   **Python 3.10+** with your distro package manager for the pip path.
2. uv path: `uv venv .venv` then `uv pip install --python .venv -r requirements.txt`
   pip path: `python3 -m venv .venv`, `.venv/bin/python -m pip install -U pip wheel`, `.venv/bin/python -m pip install -r requirements.txt`
3. `cp .env.example .env`
4. Optional AI: install Ollama from [ollama.com/download](https://ollama.com/download), then `ollama pull qwen2.5:1.5b-instruct`
5. Warm Whisper: `.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
6. Tests: `.venv/bin/python -m unittest tests.test_units -v`
7. Start: `bash scripts/posix/run_debug.sh` (run as root or with `input` group; prefer X11)

## Verify success

| Check | Expected |
|-------|----------|
| Import check (`import PySide6, faster_whisper, keyboard`) | No import error |
| `python -m unittest tests.test_units -v` | All tests OK |
| `scripts/windows/run_debug.bat` / `bash scripts/posix/run_debug.sh` | Log shows `Application ready!` and `HUD enabled` |
| Hold hotkey | Bottom-center pill shows **Listening** |

## Setup page

After install, configure a provider and API key without hand-editing `.env`:

```bash
# Windows
.\scripts\windows\setup.bat
# or: .\.venv\Scripts\python.exe odicto.py setup

# macOS / Linux
bash scripts/posix/setup.sh
# or: .venv/bin/python odicto.py setup
```

The page writes `.env` atomically, preserves unsubmitted keys, and can test the
selected provider before saving. AI-mode instructions live in `prompt.txt` when
that file exists (private, gitignored); otherwise `prompt.txt.example` (the
shipped default). The setup textarea shows that same text. Save writes
`prompt.txt` and restarts Odicto; Restore default then Save deletes `prompt.txt`.

Inspect the fully-resolved configuration (every value plus the `.env` key that
supplied it) with:

```bash
.venv/bin/python odicto.py config    # Windows: .\.venv\Scripts\python.exe odicto.py config
```

## Runtime notes for agents

- Default hotkeys: tap **Ctrl+`** (`HOTKEY=ctrl+grave`) for dictation;
  tap **Ctrl+Shift+`** (`AI_HOTKEY=ctrl+shift+grave`) for a **fresh** AI reply
  (no previous conversation). Tap the same chord again to stop and paste
  (`HOTKEY_TOGGLE=true`). Set `HOTKEY_TOGGLE=false` to hold the chord while
  speaking instead. Hold **F6** + **Ctrl+`** (`CTRL_KEEP_CONTEXT_KEYS`)
  to keep / continue AI memory. Tap **F7** (`LIVE_HOTKEY`) to start live
  dictation; tap again to finalize and insert once. Interim Gemini captions appear
  in the HUD. Stop remains PROCESSING through finalization and optional polish, and
  waits up to ~2.5s for the single Live call's authoritative final. A final or draft
  skips batch STT. F7 leaves its final text on the clipboard outside terminals; no
  caret worker, tail replacement or delayed clipboard restoration remains.
  Keyboard lib name for `` ` `` is `grave`.
  Avoid Alt chords (browser focus loss on Alt release).
- **Terminals are typed into, not pasted into.** No paste chord is universal
  (`Ctrl+V` on Windows Terminal/conhost, `Shift+Insert` on mintty/Git Bash,
  `Ctrl+Shift+V` on most Linux terminals, `Cmd+V` on macOS, and a TUI claims
  the chord outright), and `Ctrl+C` is SIGINT there. So when the focused window
  is a terminal, `paste_text` calls `send_text_bulk` and never touches the
  clipboard, and the AI selection probe sends `Ctrl+Shift+C`
  (`send_copy_terminal`) instead of Ctrl+C. Detection is best-effort:
  `platforms.base.is_terminal_identifier` matches window class / process name /
  macOS bundle id against the tables in `app/platforms/base.py`; Windows reads them
  in `app/platforms/_keyboard.py`, Linux via `xdotool`/`xprop` in
  `app/platforms/linux.py` (X11 only — Wayland returns False), macOS via
  `NSWorkspace` in `app/platforms/macos.py`. An unresolved window falls back to the
  chord, so it must never raise. `TYPE_IN_TERMINAL=false` disables the whole
  path; `EXTRA_TERMINAL_APPS` adds a terminal the tables miss. Newlines are
  typed as-is, so a multi-line dictation into a shell runs each line.
- **STT is independent of `LLM_PROVIDER`:** `STT_PROVIDER=whisper|gemini|groq|openrouter|auto`
  (default `whisper`). The resolved provider **and model** are used for raw
  dictation and for AI mode. AI mode only adds the LLM on top of that transcript.
  `LIVE_STT_PROVIDER=auto` follows the same provider. Gemini STT reuses
  `GEMINI_API_KEY` and does **not** require `LLM_PROVIDER=gemini`. Groq uses
  `GROQ_API_KEY` and `GROQ_STT_MODEL` (default `whisper-large-v3-turbo`).
  OpenRouter speech uses `OPENROUTER_API_KEY` and `OPENROUTER_STT_MODEL`
  (default `openai/whisper-large-v3`), separate from the chat model. Grok
  speech models are selected there by slug (for example `x-ai/grok-stt-1.0`).
  `GEMINI_TRANSCRIBE_MODE=smart|verbatim` applies only when the
  speech backend is Gemini. `auto` uses Gemini when a Gemini key is saved,
  otherwise Whisper. A cloud choice with no key, and any speech error, falls
  back to Whisper. First Whisper load downloads model weights (~75MB for
  `tiny.en`) only when Whisper is the active or fallback backend.
- First Ollama pull downloads the LLM (size depends on model). Odicto only
  starts/calls Ollama when `LLM_PROVIDER=ollama`.
- **Config cascade:** generic `LLM_*` keys (`LLM_MODEL`, `LLM_MAX_TOKENS`,
  `LLM_REASONING_EFFORT`) apply to whichever provider `LLM_PROVIDER` selects;
  per-provider keys (`META_*`, `GEMINI_*`, `OPENROUTER_*`) are optional
  overrides that win only when set. The built-in default provider is `none`
  (raw dictation until a key is picked). Blank values behave like
  commented-out lines: the next tier applies.
- **API keys are per provider and remembered:** `META_API_KEY`,
  `OPENROUTER_API_KEY`, and `GEMINI_API_KEY` are independent; the setup page
  saves whichever provider you configured, masks stored values, and never
  requires re-entry of a saved key. A legacy `LLM_API_KEY` in an old `.env`
  is ignored with a one-line deprecation warning.
- **Defaults live in one place:** `ENV_DEFAULTS` at the top of `config.py` is
  the single source of built-in defaults; `.env.example` must agree with it.
  `test_units.TestEnvExampleParity` fails when an uncommented `.env.example`
  value drifts from a default, when a known key is undocumented, or when the
  example documents a key the app does not read. Update both together.
- **Prompt files:** `prompt.txt` is the private live prompt (gitignored). If it
  is missing, `prompt.txt.example` is used. That example must match
  `DEFAULT_SYSTEM_PROMPT` in `config.py` byte-for-byte. Setup Save writes
  `prompt.txt` and sets `SYSTEM_PROMPT_FILE=prompt.txt` with `SYSTEM_PROMPT`
  empty. Do not store a second copy of the body in `.env`.
- **OpenRouter:** set `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, and
  `OPENROUTER_MODEL`. Localhost `LLM_API_BASE` is auto-rewritten to
  `OPENROUTER_API_BASE`. Every chat call sends `provider.sort=latency` (lowest
  time-to-first-token host, not cheapest) and `reasoning.effort=none` (skip
  thinking when the model allows it). Override with `OPENROUTER_PROVIDER_SORT`
  (`latency|throughput|price`) and `OPENROUTER_REASONING_EFFORT`
  (`none|minimal|low|medium|high|xhigh|max`). Models that require reasoning
  reject `none`. Odicto fetches OpenRouter `GET /api/v1/models` (cached) and
  clamps effort to that model's `supported_efforts` / `mandatory` flag; GLM-5.3
  is only a fallback when the catalog has not loaded. A 400 "reasoning is
  mandatory" still retries at `low`. Odicto will **not** spawn Ollama in this
  mode.
- **Meta:** set `META_API_KEY` and `META_MODEL`. Default
  model is `muse-spark-1.3-contributor` — do not silently fall back to the
  base `muse-spark-1.3` SKU.
- **Gemini:** set `LLM_PROVIDER=gemini`, `GEMINI_API_KEY`,
  and `GEMINI_MODEL` (default `gemini-3.5-flash-lite`). Uses the GA Interactions API
  via the `google-genai` SDK (`client.interactions.create`). Optional
  `GEMINI_THINKING_LEVEL` (`minimal|low|medium|high`, default `minimal`).
  Odicto will **not** spawn Ollama in this mode.
- **Gemini 3.5 Transcribe (STT):** unary `GEMINI_TRANSCRIBE_MODEL=gemini-3.5-transcribe`
  via `interactions.create`; live tap-to-talk uses
  `GEMINI_TRANSCRIBE_LIVE_MODEL=gemini-3.5-transcribe-live` (Live API, Manual VAD).
  Requires `google-genai>=2.20.0` for `AudioTranscriptionConfig.mode`.
  Public preview as of Aug 2026; free-tier audio may be used to improve Google products.
- **Ollama:** set `LLM_PROVIDER=ollama`, `OLLAMA_MODEL` (default `qwen2.5:1.5b-instruct`);
  per-provider keys — use `OLLAMA_MODEL` for Ollama only (hand-edit `LLM_MODEL` is
  the legacy generic tier that the setup page no longer writes).
- **OpenRouter default model** is `openai/gpt-5.6-luna` (override with `OPENROUTER_MODEL`).
  Default routing is lowest-latency + no thinking (`OPENROUTER_PROVIDER_SORT=latency`,
  `OPENROUTER_REASONING_EFFORT=none`).
- **Provider `none`:** raw dictation only; no LLM client; Ollama not started.
- macOS requires **Accessibility** and **Input Monitoring** permissions for
  `pynput` global hooks and synthetic copy/paste.
- Linux global suppression usually requires root or `input` group; prefer X11.
- GPU: `WHISPER_DEVICE=auto` keeps **tiny/base on CPU** so login does not pay a
  ~1GB CUDA context for a 75MB model. Larger models (`small` and up) still try
  CUDA first. Set `WHISPER_DEVICE=cuda` to keep the chosen model resident in
  GPU memory from startup. That load runs one silent warmup so the first
  hotkey does not wait on CUDA kernel setup. macOS uses CPU (int8).
  faster-whisper has no Metal backend.
- Stop with `scripts/windows/stop_dictation.bat` / `bash scripts/posix/stop_dictation.sh`, or
  `.venv/bin/python odicto.py stop`.

## STRICT: single instance only (never stack keyboard hooks)

The full lock → hook-bind gate diagram and the invariant list live in
[`docs/architecture.md`](docs/architecture.md) (§7 and §13). The operational rules below are the
short version.

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

- Do not read, view, open, or publish `.env` or API keys (`OPENROUTER_API_KEY`, etc.); inspect `.env.example` or `config.py` instead.
- Do not hardcode OS-specific paths in docs/scripts; use `platforms.base`.
- Do not replace the hotkey/paste behavior without user request.
- Do not assume Ollama is stopped system-wide just because `LLM_PROVIDER` is not
  `ollama` — only Odicto’s own spawn path is skipped.
- Do not allow multiple Odicto instances / stacked keyboard hooks.

## Transcript polish and Groq chat

`LLM_PROVIDER=groq` uses `GROQ_API_KEY`, `GROQ_API_BASE` and `GROQ_MODEL`
(default `openai/gpt-oss-20b`), separately from `GROQ_STT_MODEL`. The setup page
offers an editable chat model and the shared saved key on Speech.
`POLISH_DICTATION=false` is opt-in grammar/punctuation/capitalization cleanup;
`POLISH_MODEL` blank uses the selected AI model. It never touches AI memory or
selection/image context, waits at most two seconds, and keeps raw text on failure.
Gemini Smart skips the extra call. `AI_CLIPBOARD_IMAGE=false` makes screenshots
explicit; when enabled the HUD reads them on Qt's GUI thread. Auto Whisper uses
CPU when its bounded CUDA probe fails, and always on macOS. `SAMPLE_RATE` must be
16000. Run `.\tools\verify.ps1` (or `python tools/run_tests.py`); it is the full gate.

## Reliability behaviour

- **Timing keys** (invalid numbers log a warning naming the key and fall back to the default):
  `PASTE_DELAY_SECONDS` (default 1.0; wait after the paste chord before the clipboard restore, 0.15-10),
  `MAX_RECORDING_SECONDS` (600; auto-stop and process, 0 = no limit),
  `STT_DEADLINE_SECONDS` (20) and `LLM_DEADLINE_SECONDS` (30) are wall-clock limits per stage,
  `CANCEL_HOTKEY` (`esc`; cancels PROCESSING, never suppressed, blank disables),
  `LOG_TRANSCRIPTS` (`false`; `dictation.log` holds lengths and timings, never text),
  `POLISH_MAX_CHARS` (1200; polish is skipped above it, 0 = no limit). Polish wait scales with length.
- **Clipboard:** `platforms/clipboard.py` snapshots every format (Windows: all HGLOBAL formats;
  macOS: NSPasteboard items; Linux: text, and a non-text clipboard falls back to typing).
  Restore is synchronous: `paste_text` holds the clipboard lock, waits `PASTE_DELAY_SECONDS`,
  then restores only if the clipboard still holds Odicto's payload (Windows checks the change
  token inside the same OpenClipboard session that writes). A restore that still fails after
  ~2 s of retries (same guard) is dropped with a HUD notice (`typer.last_paste_restore_failed()`);
  no state carries to the next paste (the carry-over was removed after three review rounds).
  No background restore thread exists. The AI
  selection probe sends Ctrl+Insert in IDE hosts (VS Code, JetBrains) instead of Ctrl+C, and
  returns `""` when the clipboard cannot be saved.
- **Recorder:** an overflow or callback gap keeps the audio, processes it, and flags the HUD
  (`Mic gap · check text`). Only a dead stream with no audio errors. Reconnect refreshes
  PortAudio and falls back to the default device.
- **Pipeline:** cloud STT that errors or times out falls back to local Whisper. An AI timeout
  keeps the raw text. Cancel and late results never paste. Init retries with backoff and leaves
  no zombie. Shutdown removes hooks before it takes the lifecycle lock (3 s timeout) and ends
  an in-progress paste's restore wait early. In toggle mode the AI selection probe starts at capture start. Whisper
  prewarms at capture start. Preflight problems print and show in the HUD.
- **Providers:** `http_clients.py` holds shared keep-alive clients with connect, read, write
  and pool timeouts. One shared Gemini client runs with SDK retries off. Groq and Gemini
  receive FLAC uploads; OpenRouter stays WAV.
- **Setup page:** each launch makes a CSRF token (`X-Odicto-Token` header or hidden field).
  The Host header is checked strictly. Bodies are capped at 1 MiB (411 no length, 400 bad
  length, 413 too large).
- **Status:** `odicto.py status` lists environment problems. POSIX start waits with
  `odicto.py wait-ready`.
- **Test gate:** `tools/run_tests.py` discovers `tests/test_*.py`, enforces a test-count floor
  that may only rise, and a per-platform skip allow-list. CI and `verify.ps1` both use it.
  `tests/test_units.py` is hash-frozen; a re-base needs a numbered reason line in
  `tools/verify.ps1`. The mic diagnostic is `tools/manual_pipeline_check.py` (manual, not in the gate).
