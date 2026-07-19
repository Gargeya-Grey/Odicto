<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/STT-faster--whisper-00C853?style=for-the-badge" alt="Whisper" />
  <img src="https://img.shields.io/badge/LLM-Ollama%20%7C%20OpenRouter-FF6F00?style=for-the-badge" alt="LLM" />
  <img src="https://img.shields.io/badge/UI-PySide6-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="Qt" />
</p>

<h1 align="center">Odicto</h1>

<p align="center">
  <b>Hold a hotkey. Speak. Text appears where your cursor is.</b><br/>
  Local speech-to-text on Windows — optional AI replies — quiet glass HUD — no cloud required.
</p>

<p align="center">
  <i>Push-to-talk dictation for any app. Private by default. Fast on GPU.</i>
</p>

---

## Why this exists

Most dictation tools are either cloud-bound, locked to one app, or slow.  
**Odicto** is a small background service that:

1. Records while you **hold** a global hotkey  
2. Transcribes with **local Whisper** (`faster-whisper`)  
3. Pastes into the focused field via clipboard  
4. Optionally answers with a **local Ollama** (or OpenRouter) model  
5. Shows a slim **bottom-center HUD** while it works  

```text
  Hold hotkey ──► mic ──► Whisper ──► (optional LLM) ──► Ctrl+V paste
                              │
                         glass HUD
```

| Mode | How | Result |
|------|-----|--------|
| **Dictation** | Hold hotkey, speak, release | Raw transcript pasted |
| **AI reply** | Hold **F12 + hotkey**, speak, release | Model answer pasted |
| **Reset chat** | Say *“reset chat”* in AI mode | Clears multi-turn memory |

---

## Supported platform

| OS | Status |
|----|--------|
| **Windows 10 / 11** | Supported |
| macOS / Linux | Not supported (hotkey + paste stack is Windows-oriented) |

---

## For coding agents (install from zero)

> **If you are an AI agent:** clone this repo on Windows, run the installer, verify tests, then start the app.  
> Full automation notes: [`AGENTS.md`](./AGENTS.md).

```powershell
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\.venv\Scripts\python.exe -m unittest test_units -v
.\start_dictation.bat
```

The installer will (when possible):

| Step | What it does |
|------|----------------|
| 1 | Locate or install **Python 3.10+** (via `winget` if missing) |
| 2 | Create **`.venv`** |
| 3 | `pip install -r requirements.txt` |
| 4 | Copy **`.env.example` → `.env`** |
| 5 | Install **Ollama** (optional path) + `ollama pull` default model |
| 6 | Pre-download **Whisper** weights (`tiny.en` by default) |

Optional flags:

```powershell
# Dictation only (no local LLM)
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipOllama

# Choose models
powershell -ExecutionPolicy Bypass -File .\install.ps1 -OllamaModel "phi4-mini:latest" -WhisperModel "base.en"
```

---

## Manual install (human, step-by-step)

Assume a **clean Windows PC** with nothing installed.

### 0. Prerequisites you may need first

| Tool | Why | How to get it |
|------|-----|----------------|
| **Git** | Clone the repo | [git-scm.com](https://git-scm.com/download/win) or `winget install Git.Git` |
| **Python 3.10+** | Runtime | [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12` — enable **“Add python.exe to PATH”** |
| **Microphone** | Capture speech | Working default input device in Windows Sound settings |
| **(Optional) NVIDIA GPU + CUDA** | Faster Whisper | Drivers from NVIDIA; `faster-whisper` will use CUDA when available |
| **(Optional) Ollama** | Local AI replies | [ollama.com/download](https://ollama.com/download) or `winget install Ollama.Ollama` |
| **(Optional) OpenRouter key** | Cloud LLM instead of Ollama | [openrouter.ai](https://openrouter.ai/) |

> Admin rights: usually **not** required. If the global hotkey fails on a locked-down PC, try running the terminal as Administrator once.

### 1. Clone

```powershell
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
```

### 2. One command (recommended)

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**Or** do it by hand:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

### 3. Python packages installed (from `requirements.txt`)

| Package | Role |
|---------|------|
| `faster-whisper` | Local speech-to-text (downloads model weights on first use) |
| `sounddevice` / `soundfile` / `numpy` | Microphone capture + audio buffers |
| `keyboard` | Global hotkey hold-to-talk |
| `pyperclip` | Clipboard paste injection |
| `openai` | OpenAI-compatible client for Ollama / OpenRouter |
| `python-dotenv` | Load `.env` |
| `PySide6` | Always-on-top HUD overlay |

### 4. Models that get downloaded

| Model | When | Approx. size | Purpose |
|-------|------|--------------|---------|
| **Whisper `tiny.en`** (default) | First transcribe / install warm-up | ~75 MB | English STT (fast) |
| **Whisper `base.en` / `small.en`** | If you change `.env` | larger | Better accuracy, slower |
| **Ollama `qwen2.5:1.5b-instruct`** (default example) | `ollama pull` / install script | ~1 GB class | Local AI replies |
| Your chosen Ollama/OpenRouter model | When configured | varies | AI mode |

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

### 6. Optional: OpenRouter instead of Ollama

```env
LLM_PROVIDER=openrouter
LLM_MODEL=your/model-id
OPENROUTER_API_KEY=sk-or-...
```

### 7. Optional: raw dictation only (no LLM)

```env
LLM_PROVIDER=none
```

### 8. Verify

```powershell
.\.venv\Scripts\python.exe -m unittest test_units -v
```

### 9. Run

| Action | File |
|--------|------|
| Start (background, no console) | `start_dictation.bat` |
| Start (console logs) | `run_debug.bat` |
| Stop | `stop_dictation.bat` |

---

## Daily use (how to operate it)

### Start your day

1. Double-click **`start_dictation.bat`**  
2. Wait a few seconds for models to load (first run is slower)  
3. You’ll briefly see a **Starting** pill at the bottom center, then it fades  

### Dictate into any app

1. Click into a text field (browser, Notion, VS Code, Discord, …)  
2. **Press and hold** the hotkey (default: **`Ctrl+Shift+Space`**)  
3. Speak  
4. **Release** the key  
5. Watch the HUD: **Listening → Transcribing → Done**  
6. Text is pasted at the cursor  

### Ask the local AI

1. Hold **`F12` + hotkey** together  
2. Speak your question  
3. Release  
4. HUD: **Listening → Thinking → Done**  
5. The model’s short reply is pasted  

### Tips that matter

| Tip | Detail |
|-----|--------|
| **Hold, don’t tap** | Very short holds are ignored (anti-accidental) |
| **Wait for Ready** | Hotkeys do nothing until models finish loading |
| **One utterance at a time** | System is busy while processing; wait for **Done** |
| **Clear AI memory** | In AI mode, say *“reset chat”* / *“clear conversation”* |
| **Change hotkey** | Edit `HOTKEY=` in `.env` (e.g. `scroll lock`) then restart |
| **Logs** | Use `run_debug.bat`, or check `dictation.log` when using `pythonw` |

### Stop

Double-click **`stop_dictation.bat`** (or close the debug console with Ctrl+C).

---

## Configuration (`.env`)

Copy from `.env.example`. Important knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOTKEY` | `ctrl+shift+space` | Hold-to-talk key (see `keyboard` lib names) |
| `WHISPER_MODEL_SIZE` | `tiny.en` | `tiny.en` / `base.en` / `small.en` … |
| `WHISPER_DEVICE` | `auto` | `auto` · `cuda` · `cpu` |
| `LLM_PROVIDER` | `ollama` | `ollama` · `openrouter` · `none` |
| `LLM_MODEL` | see example | Model id for the provider |
| `LLM_MAX_TOKENS` | `150` | Hard cap; short questions use less automatically |
| `LLM_NUM_CTX` | `2048` | Ollama context window |
| `SHOW_VISUAL_INDICATOR` | `true` | Bottom HUD on/off |
| `PLAY_AUDIO_CUES` | `true` | Soft start/stop beeps |
| `MIN_HOLD_MS` | `80` | Ignore shorter presses |
| `PASTE_DELAY_SECONDS` | `0.05` | Clipboard settle before restore |

---

## Architecture (quick map)

| File | Role |
|------|------|
| `main.py` | App lifecycle, hotkeys, pipeline orchestration |
| `recorder.py` | Low-latency mic capture + level meter |
| `transcriber.py` | `faster-whisper` STT |
| `refiner.py` | Adaptive-length LLM replies + history |
| `typer.py` | Clipboard paste |
| `indicator.py` | PySide6 glass HUD |
| `config.py` | Env-backed settings |
| `app_state.py` | Shared state enum (import-safe) |
| `install.ps1` | Zero-to-one Windows installer |
| `AGENTS.md` | Agent-oriented install contract |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No HUD on hotkey | Restart with `run_debug.bat`; look for `HUD enabled` and `[HUD] → RECORDING` |
| Hotkey does nothing | Wait until “Application ready”; check `HOTKEY` in `.env`; try `run_debug.bat` as Admin |
| Empty paste / “No speech” | Check mic privacy settings (Windows → Privacy → Microphone) |
| AI mode pastes raw text | Ollama not running / wrong model — `ollama list`, `ollama pull …` |
| Slow first run | Whisper/Ollama downloading; later runs are faster |
| CUDA errors | Set `WHISPER_DEVICE=cpu` in `.env` |
| Import errors | Recreate venv and reinstall `requirements.txt` |

---

## Development

```powershell
.\.venv\Scripts\python.exe -m unittest test_units -v
.\run_debug.bat
```

---

## Privacy

- **Dictation path** can stay fully local (Whisper + paste).  
- **AI path** stays local if you use Ollama; OpenRouter sends text to a third party.  
- No telemetry in this project.

---

## License

Use and modify freely for personal or commercial projects. Attribution appreciated but not required.

---

<p align="center">
  Built for people who think faster than they type.<br/>
  <b>Hold. Speak. Continue.</b>
</p>
