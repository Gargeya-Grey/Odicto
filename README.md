<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows" />
  <img src="https://img.shields.io/badge/macOS-12%2B-000000?style=for-the-badge&logo=apple&logoColor=white" alt="macOS" />
  <img src="https://img.shields.io/badge/Linux-X11%2FWayland-FCC624?style=for-the-badge&logo=linux&logoColor=black" alt="Linux" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/STT-Whisper%20%7C%20Gemini%203.5%20Transcribe-00C853?style=for-the-badge" alt="Whisper" />
  <img src="https://img.shields.io/badge/LLM-Meta%20%7C%20Ollama%20%7C%20OpenRouter%20%7C%20Gemini-FF6F00?style=for-the-badge" alt="LLM" />
  <img src="https://img.shields.io/badge/UI-PySide6-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="Qt" />
</p>

<h1 align="center">Odicto</h1>

<p align="center">
  <b>Hold a hotkey. Speak. Text appears where your cursor is.</b><br/>
  Local Whisper or Gemini 3.5 Transcribe — optional AI replies — quiet glass HUD — private by default.
</p>

<p align="center">
  <i>Push-to-talk dictation for any app. Private by default. Fast on GPU.</i>
</p>

---

## Why this exists

Most dictation tools are either cloud-bound, locked to one app, or slow.  
**Odicto** is a small background service that:

1. Records when you **tap** a global hotkey (tap again to stop), **hold** it if `HOTKEY_TOGGLE=false`, or **tap F7** for live captions
2. Transcribes with **local Whisper** or **Gemini 3.5 Transcribe** (`STT_PROVIDER`)  
3. Pastes into the focused field via clipboard  
4. Optionally answers with an LLM — local Ollama, Meta API, OpenRouter, or **Google Gemini** (one-line switch)  
5. Shows a slim **bottom-center HUD** while it works (live captions on F7)  

```text
  Tap (or hold) hotkey ──► mic ──► Whisper or Gemini STT ──► (optional LLM) ──► Ctrl+V paste
  Tap F7               ──► live stream ──► same STT mode ─────────────────────► Ctrl+V paste
                              │
                         glass HUD
```

| Mode | How | Result |
|------|-----|--------|
| **Dictation** | Tap **Ctrl+\`**, speak, tap again (hold if `HOTKEY_TOGGLE=false`) | Transcript pasted (smart or verbatim) |
| **AI reply** | Tap **Ctrl+Shift+\`**, speak, tap again (hold if `HOTKEY_TOGGLE=false`) | Local Whisper, then a fresh model answer |
| **Live tap-to-talk** | Tap **F7**, speak, tap **F7** again | Live text at the caret; tap again when done |
| **AI with memory** | Tap **F6** + **Ctrl+\`**, speak, tap again | Continues the F6 conversation |
| **Reset chat** | **F5**, or say *“reset chat”* | Clears multi-turn memory |

---

## Supported platforms

| OS | Status |
|----|--------|
| **Windows 10 / 11** | Supported |
| **macOS 12+** | Supported (Accessibility + Input Monitoring permissions required) |
| **Linux (X11 recommended)** | Supported (root or `input` group for global hooks) |

Notes:
- **macOS** runs Whisper on CPU. `faster-whisper` does not currently
  expose an Apple Silicon Metal device, so `WHISPER_DEVICE=auto` uses
  `cpu` (int8).
- **Windows / Linux:** `WHISPER_DEVICE=auto` keeps **tiny/base on CPU** to
  avoid a ~1GB CUDA context at login. Larger models still try CUDA first.
  Set `WHISPER_DEVICE=cuda` to force GPU.
- **Linux Wayland** needs `wl-clipboard` and may have compositor-specific
  synthetic-keyboard limits; an X11 session is the most reliable target.

---

## Quick start

### Windows

```powershell
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\setup.bat   # pick provider + paste key (or: python odicto.py setup)
.\start_dictation.bat
```

### macOS

```bash
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
bash install.sh
./setup.sh
./run_debug.sh
# Grant Accessibility + Input Monitoring when macOS prompts, then restart the app.
```

### Linux (X11 recommended)

```bash
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
sudo bash install.sh   # or run as a user with access to /dev/input
./setup.sh
./run_debug.sh
```

---

## For coding agents (install from zero)

> **If you are an AI agent:** clone this repo, run the installer for the OS,
> verify tests, then start the app. Full automation notes: [`AGENTS.md`](./AGENTS.md).

The installer will (when possible):

| Step | What it does |
|------|----------------|
| 1 | Locate or install **uv** (fallback: locate/install **Python 3.10+**) |
| 2 | Create **`.venv`** |
| 3 | `uv pip install -r requirements.txt` (fallback: `pip install`) |
| 4 | Copy **`.env.example` → `.env`** |
| 5 | Pre-download **Whisper** weights (`tiny.en` by default) |
| 6 | *(opt-in `-Ollama`)* Install Ollama + `ollama pull` default model |

Optional flags:

```powershell
# Windows: also install Ollama + pull the default local model
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Ollama
```

```bash
# macOS / Linux: also install Ollama + pull the default local model
bash install.sh -Ollama
```

Ollama and its model are **opt-in**: without the flag, nothing local-AI
related is downloaded. If you pick Ollama on the setup page later, the page
offers a **Download local model** button that pulls exactly the model you
entered — and only then.

---

## Manual install (human, step-by-step)

Pick your OS below, or use the one-command installers above.

### 0. Prerequisites you may need first

| Tool | Why | How to get it |
|------|-----|----------------|
| **Git** | Clone the repo | Windows: `winget install Git.Git` · macOS: `xcode-select --install` · Linux: your package manager |
| **uv** (recommended) | Fast, hash-verified dependency install | [astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) — the installers fetch it automatically when missing |
| **Python 3.10+** | Runtime (needed only for the pip fallback; uv can fetch its own) | Windows: `winget install Python.Python.3.12` · macOS: `brew install python` · Linux: distro package |
| **Microphone** | Capture speech | Working default input device in system sound settings |
| **(Optional) NVIDIA GPU + CUDA** | Faster Whisper on Windows/Linux | Drivers from NVIDIA; `faster-whisper` uses CUDA when available |
| **(Optional) One API key per cloud backend** | AI replies — Meta, OpenRouter, or Gemini (Ollama needs none) | Use `odicto.py setup`, or paste each into its `*_API_KEY` in `.env` |
| **(Optional) Ollama** | Local AI replies | [ollama.com/download](https://ollama.com/download) or `brew install ollama` |
| **(Optional) OpenRouter key** | Cloud LLM instead of Meta/Ollama | [openrouter.ai](https://openrouter.ai/) |
| **(Optional) Gemini API key** | Cloud LLM via Google Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

Permissions:
- **Windows**: admin usually **not** required. If global hotkeys fail on a
  locked-down PC, try running the terminal as Administrator once.
- **macOS**: grant Odicto **Accessibility** and **Input Monitoring** in
  System Settings → Privacy & Security when prompted.
- **Linux**: run as root, or add your user to the `input` group. X11 is
  recommended; on Wayland install `wl-clipboard`.

### 1. Clone

```bash
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
```

### 2. One command (recommended)

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

```bash
# macOS / Linux
bash install.sh
```

**Or** do it by hand (Windows shows `.venv\Scripts`, macOS/Linux use `.venv/bin`):

```bash
# uv path (recommended)
uv venv .venv
uv pip install --python .venv -r requirements.txt

# pip path (fallback)
python3 -m venv .venv
.venv/bin/python -m pip install -U pip wheel
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### 3. Python packages installed (from `requirements.txt`)

| Package | Role |
|---------|------|
| `faster-whisper` | Local speech-to-text (downloads model weights on first use) |
| `google-genai` | Gemini LLM **and** Gemini 3.5 Transcribe (unary + Live API) |
| `sounddevice` / `soundfile` / `numpy` | Microphone capture + audio buffers |
| `keyboard` | Global hotkey hold-to-talk (Windows/Linux) |
| `pynput` | Global hotkey hold-to-talk (macOS) |
| `pyperclip` | Clipboard paste injection |
| `openai` | OpenAI-compatible client for Ollama / OpenRouter |
| `requests` | HTTP + keep-alive for Meta API (`/v1/responses`) |
| `python-dotenv` | Load `.env` |
| `PySide6` | Always-on-top HUD overlay |
| `psutil` | Cross-platform process enumeration |

### 4. Models that get downloaded

| Model | When | Approx. size | Purpose |
|-------|------|--------------|---------|
| **Whisper `tiny.en`** (default) | First transcribe / install warm-up | ~75 MB | English STT (fast) |
| **Whisper `base.en` / `small.en`** | If you change `.env` | larger | Better accuracy, slower |
| **Ollama model** (default example `qwen2.5:1.5b-instruct`) | Only when you pick Ollama (setup page **Download local model** button, or `-Ollama` installer flag) | ~1 GB class | Local AI replies |
| Your chosen OpenRouter/Meta/Gemini model | Cloud — nothing downloads | — | AI mode |

Whisper cache is managed by `faster-whisper` / Hugging Face cache on the machine.  
Ollama stores models in its own library (`ollama list` to inspect).

### 5. Optional: pull an Ollama model yourself

```powershell
ollama serve
ollama pull qwen2.5:1.5b-instruct
```

Edit `.env`:

```env
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:1.5b-instruct
LLM_API_BASE=http://localhost:11434/v1
```

### 6. Optional: pick an AI backend — one line, its own key

Each cloud provider owns **its own API key**, saved independently:

```env
LLM_PROVIDER=gemini          # or: meta / ollama / openrouter / none
GEMINI_API_KEY=AIza...      # each provider keeps its own key variable
# META_API_KEY=...           # used when LLM_PROVIDER=meta
# OPENROUTER_API_KEY=...     # used when LLM_PROVIDER=openrouter
```

Keys start empty. Save a key once under its provider and it is **remembered
forever** — switching providers later never asks you to re-enter a key you
already saved (the setup page shows stored keys masked; retype only to replace).
The generic knobs (`LLM_MODEL`, `LLM_MAX_TOKENS`, …) follow whichever provider
is selected.

```env
# Per-backend models (selected via the setup page’s categorized dropdowns):
# META_MODEL=muse-spark-1.3-contributor
# GEMINI_MODEL=gemini-3.5-flash-lite
# OPENROUTER_MODEL=openai/gpt-5.6-luna
# OLLAMA_MODEL= (blank → qwen2.5:1.5b-instruct)
```

See exactly what is in effect after any change:

```bash
python odicto.py config   # resolved values + which .env key supplied each
```

### 7. Optional: OpenRouter instead of Meta/Ollama

You can keep all three backends configured, then flip one line:

```env
LLM_PROVIDER=openrouter
OPENROUTER_MODEL=openai/gpt-5.6-luna
OPENROUTER_API_KEY=sk-or-...
```

| Variable | Role |
|----------|------|
| `LLM_PROVIDER=openrouter` | Selects the cloud backend |
| `OPENROUTER_MODEL` | OpenRouter model slug ([model list](https://openrouter.ai/models)) |
| `OPENROUTER_API_KEY` | Required for openrouter (app refuses to start if missing) |
| `OPENROUTER_API_BASE` | Defaults to `https://openrouter.ai/api/v1` |
| `OPENROUTER_PROVIDER_SORT` | Always `latency` by default — pick the lowest time-to-first-token host for the chosen model (`throughput` / `price` also valid) |
| `OPENROUTER_REASONING_EFFORT` | Always `none` by default — skip thinking when the model allows it. Odicto fetches OpenRouter's live model catalog and clamps illegal values (GLM-5.3 requires `low`/`high`/`max`) |
| `LLM_MODEL` / `LLM_API_BASE` | Stay as your Ollama settings for easy switch-back |
| `META_API_KEY` | Placeholder for Meta — paste real key in `.env` (never commit it) |
| `META_MODEL` | Meta model id (default `muse-spark-1.3-contributor`) |

Notes:
- If `OPENROUTER_MODEL` is blank, the app falls back to `LLM_MODEL` (must be a valid OpenRouter id).
- Every OpenRouter chat call sends `provider.sort=latency` and `reasoning.effort=none` so answers come from the fastest host with thinking turned off. The setup page loads OpenRouter's model list and per-model reasoning levels; mandatory-reasoning models are clamped to the lightest allowed effort. OpenRouter's own default is cheapest, not fastest.
- For `meta`, `META_MODEL` is used; `LLM_MODEL` stays as the Ollama fallback when meta keys are not set.
- Localhost `LLM_API_BASE` is ignored for openrouter so you do **not** need to edit the API path by hand.

### 7b. Optional: Google Gemini instead of Meta/Ollama

Gemini uses the official `google-genai` SDK and the GA **Interactions API**
(`client.interactions.create`), the same call Google recommends for new code:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...     # from https://aistudio.google.com/apikey
GEMINI_MODEL=gemini-3.5-flash-lite
# Optional: GEMINI_THINKING_LEVEL=minimal   (minimal|low|medium|high)
```

| Variable | Role |
|----------|------|
| `LLM_PROVIDER=gemini` | Selects the Gemini backend (`google` / `google-api` also work) |
| `GEMINI_API_KEY` | Required (app refuses to start if missing) |
| `GEMINI_MODEL` | Gemini model id (default `gemini-3.5-flash-lite` — the fast, non-reasoning pick for dictation; `gemini-3.7-flash` is available for heavier questions) |
| `GEMINI_THINKING_LEVEL` | Thinking budget: `minimal` / `low` / `medium` / `high` (default `minimal`) |
| `GEMINI_MAX_OUTPUT_TOKENS` | Output budget floor (default `4096`) |

Notes:
- Multi-turn chat (F6 chord only) uses Gemini **server-side state**
  (`previous_interaction_id`), which reuses cached context across those turns.
- Ctrl+Shift+\` AI is always a fresh one-shot (no previous_interaction_id).
- Speaking “reset chat” (or pressing `RESET_CONTEXT_HOTKEY`) starts a fresh conversation.

### 7c. Optional: Gemini 3.5 Transcribe as STT

Speech-to-text is **independent** of `LLM_PROVIDER`. You can keep Meta/OpenRouter/Ollama
for AI replies and still use Gemini for dictation. The setup page toggle
**Verbatim ↔ Smart** is stored as `GEMINI_TRANSCRIBE_MODE` and applies to the
**dictation chord and F7**. The AI chord uses **local Whisper** (the LLM is the
cleanup step) and only falls back to Gemini **verbatim** if Whisper cannot load.

```env
STT_PROVIDER=auto            # whisper | gemini | auto
GEMINI_API_KEY=AIza...       # same key as Gemini LLM; reused, not a second secret
GEMINI_TRANSCRIBE_MODE=smart # smart (cleaned) or verbatim (literal)
LIVE_HOTKEY=f7               # tap to start, tap again to stop and paste
```

| Variable | Role |
|----------|------|
| `STT_PROVIDER` | `whisper` (default, local), `gemini` (cloud STT), or `auto` (Gemini when a key is saved) |
| `GEMINI_TRANSCRIBE_MODE` | `smart` strips ums/self-corrections and punctuates; `verbatim` is word-for-word |
| `GEMINI_TRANSCRIBE_MODEL` | Unary model (`gemini-3.5-transcribe`) used after hold-to-talk release |
| `GEMINI_TRANSCRIBE_LIVE_MODEL` | Live API model (`gemini-3.5-transcribe-live`) used while F7 is active |
| `GEMINI_TRANSCRIBE_LANGUAGE` | Optional BCP-47 hint (`en-US`); blank = auto-detect 85+ languages |
| `GEMINI_TRANSCRIBE_VOCABULARY` | Optional comma-separated bias terms |
| `LIVE_HOTKEY` | Tap-to-talk key (default `f7`). Empty disables. Captured while Odicto runs |

On Gemini STT failure (no key, 429, network), Odicto falls back to local Whisper.
Default `STT_PROVIDER=whisper` so existing local-only installs do not change.

### Resource use: Ollama vs OpenRouter vs Meta vs Gemini vs Whisper

| Component | When Odicto starts / uses it | RAM / GPU |
|-----------|------------------------------|-----------|
| **Whisper (STT)** | When `STT_PROVIDER=whisper`, as fallback, or for the AI chord | Local — skipped at boot if Gemini STT is selected; tiny/base may warm after ready when an LLM is configured. `auto` device keeps tiny/base on CPU (int8) so idle RAM is not a CUDA context |
| **Meta API** | Only if `LLM_PROVIDER=meta` | Cloud — no local LLM VRAM from Odicto |
| **Ollama** | Only if `LLM_PROVIDER=ollama` | Odicto **does not** start or call Ollama for `meta` / `openrouter` / `gemini` / `none` |
| **OpenRouter** | Only if `LLM_PROVIDER=openrouter` | Cloud — no local LLM VRAM from Odicto |
| **Google Gemini (LLM)** | Only if `LLM_PROVIDER=gemini` | Cloud — no local LLM VRAM from Odicto |
| **Gemini 3.5 Transcribe (STT)** | Only if `STT_PROVIDER` resolves to `gemini` | Cloud — falls back to Whisper on error |

**Important:** Switching to Meta, OpenRouter, or Gemini stops Odicto from launching or talking to Ollama.  
It does **not** force-quit an Ollama tray app / service that Windows (or a previous session) already started. If Ollama is still in the system tray with a model loaded, that process can still use RAM/VRAM until you quit it yourself.

```powershell
# Optional: check whether Ollama is listening locally
netstat -ano | findstr 11434
# Optional: see loaded models (if the CLI is available)
ollama ps
```

To free local LLM memory while using Meta/OpenRouter/Gemini: quit **Ollama** from the tray, or stop the service. Whisper will still use some local RAM for dictation.

### 8. Optional: raw dictation only (no LLM)

```env
LLM_PROVIDER=none
```

Same as above: Odicto will not start Ollama. Whisper still loads for speech-to-text.

### 9. Verify

```powershell
# Windows
.\.venv\Scripts\python.exe -m unittest test_units -v
```

```bash
# macOS / Linux
.venv/bin/python -m unittest test_units -v
```

### 10. Run

| Action | Windows | macOS / Linux |
|--------|---------|---------------|
| Configure provider | `setup.bat` or `.venv\Scripts\python.exe odicto.py setup` | `./setup.sh` or `.venv/bin/python odicto.py setup` |
| Start (background) | `start_dictation.bat` | `./start_dictation.sh` |
| Start (console logs) | `run_debug.bat` | `./run_debug.sh` |
| Stop | `stop_dictation.bat` | `./stop_dictation.sh` |

---

## Daily use (how to operate it)

### Start your day

1. Start Odicto with your OS's start script (see table above)  
2. Wait a few seconds for models to load (first run is slower)  
3. You’ll briefly see a **Starting** pill at the bottom center, then it fades  

### Dictate into any app

1. Click into a text field (browser, Notion, VS Code, Discord, ...)  
2. **Press and hold** the hotkey (default: **Ctrl+\`**)  
3. Speak  
4. **Release** the key  
5. Watch the HUD: **Listening -> Transcribing -> Done**  
6. Text is pasted at the cursor  

### Terminals

There is no paste chord that works in every terminal — Windows Terminal and
`cmd`/PowerShell take **Ctrl+V**, Git Bash/mintty defaults to **Shift+Insert**,
most Linux terminals use **Ctrl+Shift+V**, macOS takes **Cmd+V**, and inside a
TUI (vim, an agent CLI, an SSH session) the foreground app claims **Ctrl+V**
outright. Plain **Ctrl+C** is worse: in a terminal it is **SIGINT** and kills
whatever is running.

So when the focused window is a terminal, Odicto **types the text** instead of
pasting it — the same path your keyboard uses, which every terminal accepts —
and leaves your clipboard untouched. For the same reason AI mode's
"refine the selected text" probe sends **Ctrl+Shift+C**, never Ctrl+C.

Detection is best-effort: a window it cannot identify falls back to the normal
paste chord. Add your terminal with `EXTRA_TERMINAL_APPS` if it is not
recognized. Notes:

| Situation | Behavior |
|-----------|----------|
| Windows Terminal, `cmd`, PowerShell, Git Bash, WSL | Detected |
| macOS Terminal, iTerm2, Warp, kitty | Detected |
| Linux X11 (`xdotool` or `xprop` installed) | Detected |
| Linux Wayland | Not detectable — paste chord is used; set `TYPE_IN_TERMINAL=false` if it misbehaves |
| VS Code / JetBrains built-in terminal | Not distinguishable from the editor — paste chord is used |
| Multi-line dictation in a shell | Typed as-is, so each newline is **Enter** and the line runs |

### Ask the AI

1. Hold **Ctrl+Shift+\`** (the backtick key under Esc, plus **Shift**)  
2. Speak your question  
3. Release  
4. HUD: **Listening -> Thinking -> Done**  
5. The model's short reply is pasted — this is always a **fresh** run (no previous task)

To keep talking about the same task, hold **F6** together with **Ctrl+\`** (or with the AI chord). Only those captures share conversation memory. Press **F5** to wipe that memory.  

### Tips that matter

| Tip | Detail |
|-----|--------|
| **Too-short clips are ignored** | Start+stop faster than `MIN_HOLD_MS` is dropped (anti-accidental) |
| **Wait for Ready** | Hotkeys do nothing until models finish loading |
| **One utterance at a time** | System is busy while processing; wait for **Done** |
| **Clear AI memory** | Press **F5** (instant) or say *reset chat* / *clear conversation* in F6/AI mode — both wipe the multi-turn history |
| **Continue a conversation** | Hold **F6** + **Ctrl+\`** (or F6 + the AI chord): that capture keeps / continues AI memory. Ctrl+Shift+\` alone is always a fresh one-shot |
| **Keep focus in the field** | Prefer non-**Alt** chords; Alt often steals browser focus on release |
| **VS Code note** | Ctrl+\` toggles the terminal there -- while Odicto runs it steals that chord |
| **Change hotkey** | Edit `HOTKEY=` / `AI_HOTKEY=` in `.env` then restart |
| **Logs** | Use `run_debug.bat` / `./run_debug.sh`, or check `dictation.log` when using the no-console launcher |

### Stop

Run your OS's stop script (see table above), or close the debug console with Ctrl+C.

### Keeping it fast

- Raw dictation never waits on the clipboard or the LLM.
- AI mode runs Whisper and selection-copy **at the same time**, so clipboard wait does not sit in front of STT.
- Short hold-to-talk clips skip Silero VAD (set `WHISPER_VAD=true` if you want it always on).
- AI replies are capped by `LLM_MAX_TOKENS` (default 1024, enough for a mid-length social post). Raise it only if you need long essays.
- Gemini thinking stays at `minimal` unless you change it; higher thinking levels are slower.

---

## Configuration (`.env`)

Copy from `.env.example`. The file follows one rule:

> **Generic keys apply to whichever provider `LLM_PROVIDER` selects;
> provider-specific keys are optional overrides that win when set.**
> A commented-out or blank line means "use the built-in default".

So switching AI backends is a one-line change plus one key. After editing,
`python odicto.py config` prints every resolved value and which `.env` key
supplied it (and warns about typo'd or legacy-shadowed keys).

Core knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LLM_PROVIDER` | `none` | `none` · `ollama` · `openrouter` · `meta` (also `meta-api`) · `gemini` (also `google`) |
| `LLM_MODEL` | provider default | Model id for any provider (`qwen2.5:1.5b-instruct` / `muse-spark-1.3-contributor` / `gemini-3.5-flash-lite` / an OpenRouter slug) |
| `LLM_MAX_TOKENS` | `1024` | Output cap for every provider (~750 words) |
| `LLM_REASONING_EFFORT` | provider default | Unified thinking knob; maps to Meta `reasoning.effort` / Gemini `thinking_level` / OpenRouter `reasoning.effort` (`minimal\|low\|medium\|high\|none`) |

Per-provider credentials — each starts empty, is saved independently, and is
**remembered across provider switches** (the setup page masks stored keys):

| Variable | Default | Meaning |
|----------|---------|---------|
| `OPENROUTER_API_KEY` | *(empty)* | OpenRouter key ([openrouter.ai/keys](https://openrouter.ai/keys)) |
| `META_API_KEY` | *(empty)* | Meta API key |
| `GEMINI_API_KEY` | *(empty)* | Gemini key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) |

Optional overrides:

| Variable | Default | Meaning |
|----------|---------|---------|
| `OPENROUTER_MODEL` / `META_MODEL` / `GEMINI_MODEL` / `OLLAMA_MODEL` | `openai/gpt-5.6-luna` (fallback) / `muse-spark-1.3-contributor` / `gemini-3.5-flash-lite` / `qwen2.5:1.5b-instruct` | Pin a model per backend; OLLAMA is blank-by-default → falls back to `LLM_MODEL` |
| `GEMINI_MAX_OUTPUT_TOKENS` | `4096` | Per-provider ceiling over `LLM_MAX_TOKENS` (Meta reasoning is uncapped; effort knob only) |
| `META_REASONING_EFFORT` / `GEMINI_THINKING_LEVEL` / `OPENROUTER_REASONING_EFFORT` | `low` / `minimal` / `none` | Per-provider effort over `LLM_REASONING_EFFORT` |
| `OPENROUTER_PROVIDER_SORT` | `latency` | OpenRouter host ranking: `latency` (fastest TTFT), `throughput`, or `price` |
| `LLM_API_BASE` | `http://localhost:11434/v1` | Ollama endpoint (OpenRouter auto-switches to its own root) |

Behavior & UI:

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOTKEY` | `ctrl+grave` | Dictation chord (`grave` = the `` ` `` key) |
| `AI_HOTKEY` | `ctrl+shift+grave` | AI chord (same primary key + Shift) |
| `HOTKEY_TOGGLE` | `true` | `true` = tap chord to start, tap again to stop. `false` = hold while speaking |
| `RESET_CONTEXT_HOTKEY` | `f5` | Instant clear of AI multi-turn memory (no recording) |
| `CTRL_KEEP_CONTEXT_KEYS` | `f6` | Key held during a capture keeps AI conversation memory (default AI is always fresh) |
| `WHISPER_MODEL_SIZE` | `tiny.en` | `tiny.en` / `base.en` / `small.en` … |
| `WHISPER_DEVICE` | `auto` | `auto` (tiny/base → CPU; larger → CUDA then CPU) · `cuda` · `cpu` |
| `WHISPER_VAD` | `false` | Silero VAD before decode; auto-on for clips ≥ 8s |
| `LLM_NUM_CTX` | `2048` | Ollama context window |
| `SYSTEM_PROMPT_FILE` | *(empty)* | Set to `prompt.txt` when you have a private live prompt. Do not point this at other files. |
| `SYSTEM_PROMPT` | *(empty)* | Leave blank. The prompt body lives in `prompt.txt` or `prompt.txt.example`, not in `.env`. |
| `SHOW_VISUAL_INDICATOR` | `true` | Bottom HUD on/off |
| `PLAY_AUDIO_CUES` | `true` | Soft start/stop beeps |
| `MIN_HOLD_MS` | `80` | Ignore shorter presses |
| `PASTE_DELAY_SECONDS` | `0.05` | Clipboard settle before restore |
| `TYPE_IN_TERMINAL` | `true` | Type the text (clipboard untouched) when the focused window is a terminal |
| `EXTRA_TERMINAL_APPS` | *(empty)* | Comma-separated window classes / process names to also treat as terminals |

---

## Architecture (quick map)

**Canonical reference: [`docs/architecture.md`](docs/architecture.md)** — module map, Mermaid
diagrams (system context, dependency graph, the dictation and live-F7 sequences, state machine,
lock gate, threads and locks, failure paths), the config cascade, "where do I change X?", and the
invariants not to break.

**Why the code is shaped this way, including refactorings that were deliberately rejected:
[`docs/engineering-notes.md`](docs/engineering-notes.md).**

| File | Role |
|------|------|
| `main.py` | App lifecycle, hotkeys, pipeline orchestration |
| `setup_web.py` + `setup_template.html` | Local setup page (server + markup) |
| `platforms/` | OS backends for hotkeys, clipboard, process, and window styling |
| `tools/verify.ps1` | Runs every gate used to prove behaviour is unchanged |
| `tools/module_graph.py` | Regenerates the dependency diagram in `docs/architecture.md` |
| `install.ps1` / `install.sh` | Zero-to-one installers (uv-first, pip fallback) |
| `setup.bat` / `setup.sh` | Launchers for the setup page |
| `AGENTS.md` | Agent-oriented install contract |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No HUD on hotkey | Restart with `run_debug.bat` / `./run_debug.sh`; look for `HUD enabled` and `[HUD] → RECORDING` |
| Hotkey does nothing | Wait until “Application ready”; check `HOTKEY` / `AI_HOTKEY` in `.env`; on macOS grant Accessibility/Input Monitoring; on Linux try root |
| Old hotkeys still work / both modes feel wrong | Multiple instances — run the stop script (kills all), then start once. Check log for `Hotkeys bound: …` |
| **Every letter types twice** while typing in any app (`tthhiiss`) | **Two Odicto processes** each installed a system-wide keyboard hook. Run the stop script, confirm no second start, then launch **once**. Log should show `Single-instance lock acquired`. |
| Always raw, never AI | Hold **Shift** too (`Ctrl+Shift+\``). Log should say `Recording (AI refined)` |
| Empty paste / “No speech” | Check mic privacy settings (Windows → Privacy → Microphone; macOS → Privacy → Microphone) |
| AI mode pastes raw text (Ollama) | Server/model issue — `ollama list`, `ollama pull …`, ensure `LLM_PROVIDER=ollama` |
| AI mode pastes raw text (OpenRouter) | Check `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, and network; restart after `.env` edits |
| AI mode pastes raw text (Meta) | Check `META_API_KEY`, `META_MODEL`, and network; restart after `.env` edits |
| AI mode pastes raw text (Gemini) | Check `GEMINI_API_KEY`, `GEMINI_MODEL`, and network; restart after `.env` edits |
| App refuses to start on openrouter | `OPENROUTER_API_KEY` is required when `LLM_PROVIDER=openrouter` |
| App refuses to start on gemini | `GEMINI_API_KEY` is required when `LLM_PROVIDER=gemini` |
| Ollama still using RAM on Meta/OpenRouter/Gemini | Odicto is not calling it; quit the Ollama app / service separately (see resource section above) |
| Slow first run | Whisper/Ollama downloading; later runs are faster |
| CUDA errors | Set `WHISPER_DEVICE=cpu` in `.env` |
| Import errors | Recreate venv and reinstall `requirements.txt` (`uv venv .venv && uv pip install --python .venv -r requirements.txt`) |
| macOS hotkey/paste doesn't work | Grant **Accessibility** and **Input Monitoring**, then fully quit and restart Odicto |
| Linux hotkey/paste doesn't work | Run as root or add your user to the `input` group; on Wayland prefer X11 |
| Nothing appears in the terminal | Odicto types there instead of pasting; if your terminal isn't detected, add its window class or process name to `EXTRA_TERMINAL_APPS` |
| Terminal pastes but never types | `TYPE_IN_TERMINAL=false` is set, or the window wasn't detected. On Linux X11 install `xdotool` or `xprop`; Wayland can't be detected |
| Multi-line text ran as commands in the shell | Newlines are typed as-is, so each is **Enter**. Dictate a single line, or set `TYPE_IN_TERMINAL=false` to paste instead |

---

## Development

```powershell
# Windows
.\.venv\Scripts\python.exe -m unittest test_units -v
.\run_debug.bat
```

```bash
# macOS / Linux
.venv/bin/python -m unittest test_units -v
./run_debug.sh
```

---

## Privacy

- **Dictation path** can stay fully local (Whisper + paste).  
- **AI path** stays local if you use Ollama; **Meta / OpenRouter / Gemini send the transcribed text to a third party**.  
- With `LLM_PROVIDER=meta` / `openrouter` / `gemini` / `none`, Odicto does not start Ollama — but a separately running Ollama install may still be active on the machine.  
- Never commit `.env` (may contain `META_API_KEY` / `MODEL_API_KEY` / `OPENROUTER_API_KEY` / `GEMINI_API_KEY`).
- No telemetry in this project.

---

## License

Use and modify freely for personal or commercial projects. Attribution appreciated but not required.

---

<p align="center">
  Built for people who think faster than they type.<br/>
  <b>Hold. Speak. Continue.</b>
</p>
