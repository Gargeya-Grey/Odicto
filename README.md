<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows" />
  <img src="https://img.shields.io/badge/macOS-12%2B-000000?style=for-the-badge&logo=apple&logoColor=white" alt="macOS" />
  <img src="https://img.shields.io/badge/Linux-X11%2FWayland-FCC624?style=for-the-badge&logo=linux&logoColor=black" alt="Linux" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/STT-faster--whisper-00C853?style=for-the-badge" alt="Whisper" />
  <img src="https://img.shields.io/badge/LLM-Meta%20%7C%20Ollama%20%7C%20OpenRouter-FF6F00?style=for-the-badge" alt="LLM" />
  <img src="https://img.shields.io/badge/UI-PySide6-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="Qt" />
</p>

<h1 align="center">Odicto</h1>

<p align="center">
  <b>Hold a hotkey. Speak. Text appears where your cursor is.</b><br/>
  Local speech-to-text on Windows, macOS, and Linux — optional AI replies — quiet glass HUD — no cloud required.
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
4. Optionally answers with **Meta API** (default), local Ollama, or OpenRouter  
5. Shows a slim **bottom-center HUD** while it works  

```text
  Hold hotkey ──► mic ──► Whisper ──► (optional LLM) ──► Ctrl+V paste
                              │
                         glass HUD
```

| Mode | How | Result |
|------|-----|--------|
| **Dictation** | Hold **Ctrl+`**, speak, release | Raw transcript pasted |
| **AI reply** | Hold **Ctrl+Shift+`**, speak, release | Model answer pasted |
| **Reset chat** | Say *“reset chat”* in AI mode | Clears multi-turn memory |

---

## Supported platforms

| OS | Status |
|----|--------|
| **Windows 10 / 11** | Supported |
| **macOS 12+** | Supported (Accessibility + Input Monitoring permissions required) |
| **Linux (X11 recommended)** | Supported (root or `input` group for global hooks) |

Notes:
- **macOS** runs Whisper on CPU by default. `faster-whisper` does not currently
  expose an Apple Silicon Metal device, so `WHISPER_DEVICE=auto` falls back to
  `cpu` (int8).
- **Linux Wayland** needs `wl-clipboard` and may have compositor-specific
  synthetic-keyboard limits; an X11 session is the most reliable target.

---

## Quick start

### Windows

```powershell
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\.venv\Scripts\python.exe odicto.py setup   # pick provider + paste key
.\start_dictation.bat
```

### macOS

```bash
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
bash install.sh
.venv/bin/python odicto.py setup
./run_debug.sh
# Grant Accessibility + Input Monitoring when macOS prompts, then restart the app.
```

### Linux (X11 recommended)

```bash
git clone https://github.com/Gargeya-Grey/Odicto.git
cd Odicto
sudo bash install.sh   # or run as a user with access to /dev/input
.venv/bin/python odicto.py setup
./run_debug.sh
```

---

## For coding agents (install from zero)

> **If you are an AI agent:** clone this repo, run the installer for the OS,
> verify tests, then start the app. Full automation notes: [`AGENTS.md`](./AGENTS.md).

The installer will (when possible):

| Step | What it does |
|------|----------------|
| 1 | Locate or install **Python 3.10+** |
| 2 | Create **`.venv`** |
| 3 | `pip install -r requirements.txt` |
| 4 | Copy **`.env.example` → `.env`** |
| 5 | Install **Ollama** (optional path) + `ollama pull` default model |
| 6 | Pre-download **Whisper** weights (`tiny.en` by default) |

Optional flags:

```powershell
# Windows: dictation only (no local LLM)
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipOllama
```

```bash
# macOS / Linux: dictation only (no local LLM)
bash install.sh -SkipOllama
```

---

## Manual install (human, step-by-step)

Pick your OS below, or use the one-command installers above.

### 0. Prerequisites you may need first

| Tool | Why | How to get it |
|------|-----|----------------|
| **Git** | Clone the repo | Windows: `winget install Git.Git` · macOS: `xcode-select --install` · Linux: your package manager |
| **Python 3.10+** | Runtime | Windows: `winget install Python.Python.3.12` · macOS: `brew install python` · Linux: distro package |
| **Microphone** | Capture speech | Working default input device in system sound settings |
| **(Optional) NVIDIA GPU + CUDA** | Faster Whisper on Windows/Linux | Drivers from NVIDIA; `faster-whisper` uses CUDA when available |
| **(Optional) Meta API key** | Cloud AI replies (default backend) | Use `odicto.py setup` or paste `META_API_KEY` into `.env` |
| **(Optional) Ollama** | Local AI replies | [ollama.com/download](https://ollama.com/download) or `brew install ollama` |
| **(Optional) OpenRouter key** | Cloud LLM instead of Meta/Ollama | [openrouter.ai](https://openrouter.ai/) |

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
python3 -m venv .venv
.venv/bin/python -m pip install -U pip wheel
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### 3. Python packages installed (from `requirements.txt`)

| Package | Role |
|---------|------|
| `faster-whisper` | Local speech-to-text (downloads model weights on first use) |
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

### 6. Optional: Meta API (default) — flip with one line

Meta is the default. Your example payload:

```
POST https://api.meta.ai/v1/responses
Headers: Authorization: Bearer $META_API_KEY
Body: { "model": "muse-spark-1.2-contributor", "input": [{"role":"user","content":[{"type":"input_text","text":"..."}]}], "stream": false }
```

```env
# Default — no extra step if you already set META_API_KEY
LLM_PROVIDER=meta
META_API_KEY=sk-meta-...
META_MODEL=muse-spark-1.2-contributor
META_API_BASE=https://api.meta.ai/v1
# Alias that also works: MODEL_API_KEY, META_API_MODEL
```

Single-switch `.env` between the three:

```env
LLM_PROVIDER=meta        # or: meta-api / meta_api (aliases)
# LLM_PROVIDER=ollama
# LLM_PROVIDER=openrouter
# LLM_PROVIDER=none      # raw dictation only
```

Leave unused keys blank — only the active provider's key is required. Switching does not require editing API bases.

### 7. Optional: OpenRouter instead of Meta/Ollama

You can keep all three backends configured, then flip one line:

```env
LLM_PROVIDER=openrouter
OPENROUTER_MODEL=google/gemini-2.0-flash-001
OPENROUTER_API_KEY=sk-or-...
```

| Variable | Role |
|----------|------|
| `LLM_PROVIDER=openrouter` | Selects the cloud backend |
| `OPENROUTER_MODEL` | OpenRouter model slug ([model list](https://openrouter.ai/models)) |
| `OPENROUTER_API_KEY` | Required for openrouter (app refuses to start if missing) |
| `OPENROUTER_API_BASE` | Defaults to `https://openrouter.ai/api/v1` |
| `LLM_MODEL` / `LLM_API_BASE` | Stay as your Ollama settings for easy switch-back |
| `META_API_KEY` / `MODEL_API_KEY` | Placeholder for Meta — paste real key in `.env` (never commit it) |
| `META_MODEL` | Meta model id (default `muse-spark-1.2-contributor`) |

Notes:
- If `OPENROUTER_MODEL` is blank, the app falls back to `LLM_MODEL` (must be a valid OpenRouter id).
- For `meta`, `META_MODEL` is used; `LLM_MODEL` stays as the Ollama fallback when meta keys are not set.
- Localhost `LLM_API_BASE` is ignored for openrouter so you do **not** need to edit the API path by hand.

### Resource use: Ollama vs OpenRouter vs Meta vs Whisper

| Component | When Odicto starts / uses it | RAM / GPU |
|-----------|------------------------------|-----------|
| **Whisper (STT)** | Always (dictation needs it) | Local — loads regardless of LLM provider |
| **Meta API** | Only if `LLM_PROVIDER=meta` | Cloud — no local LLM VRAM from Odicto |
| **Ollama** | Only if `LLM_PROVIDER=ollama` | Odicto **does not** start or call Ollama for `meta` / `openrouter` / `none` |
| **OpenRouter** | Only if `LLM_PROVIDER=openrouter` | Cloud — no local LLM VRAM from Odicto |

**Important:** Switching to Meta or OpenRouter stops Odicto from launching or talking to Ollama.  
It does **not** force-quit an Ollama tray app / service that Windows (or a previous session) already started. If Ollama is still in the system tray with a model loaded, that process can still use RAM/VRAM until you quit it yourself.

```powershell
# Optional: check whether Ollama is listening locally
netstat -ano | findstr 11434
# Optional: see loaded models (if the CLI is available)
ollama ps
```

To free local LLM memory while using Meta/OpenRouter: quit **Ollama** from the tray, or stop the service. Whisper will still use some local RAM for dictation.

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
| Configure provider | `.venv\Scripts\python.exe odicto.py setup` | `.venv/bin/python odicto.py setup` |
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
2. **Press and hold** the hotkey (default: **Ctrl+`**)  
3. Speak  
4. **Release** the key  
5. Watch the HUD: **Listening -> Transcribing -> Done**  
6. Text is pasted at the cursor  

### Ask the local AI

1. Hold **Ctrl+Shift+`** (same ` key under Esc, plus **Shift**)  
2. Speak your question  
3. Release  
4. HUD: **Listening -> Thinking -> Done**  
5. The model's short reply is pasted  

### Tips that matter

| Tip | Detail |
|-----|--------|
| **Hold, don't tap** | Very short holds are ignored (anti-accidental) |
| **Wait for Ready** | Hotkeys do nothing until models finish loading |
| **One utterance at a time** | System is busy while processing; wait for **Done** |
| **Clear AI memory** | Press **F5** (instant) or say *reset chat* / *clear conversation* in AI mode — both wipe the multi-turn history |
| **Fresh one-shot AI** | Hold **F6** while using the AI chord: that capture gets a context-free reply (memory wiped first) |
| **Keep focus in the field** | Prefer non-**Alt** chords; Alt often steals browser focus on release |
| **VS Code note** | Ctrl+` toggles the terminal there -- while Odicto runs it steals that chord |
| **Change hotkey** | Edit `HOTKEY=` / `AI_HOTKEY=` in `.env` then restart |
| **Logs** | Use `run_debug.bat` / `./run_debug.sh`, or check `dictation.log` when using the no-console launcher |

### Stop

Run your OS's stop script (see table above), or close the debug console with Ctrl+C.

---

## Configuration (`.env`)

Copy from `.env.example`. Important knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOTKEY` | `ctrl+grave` | Dictation chord (`grave` = the `` ` `` key) |
| `AI_HOTKEY` | `ctrl+shift+grave` | AI chord (same primary key + Shift) |
| `AI_MODIFIER` | *(empty)* | Legacy third-key AI trigger; leave blank when using `AI_HOTKEY` |
| `RESET_CONTEXT_HOTKEY` | `f5` | Instant clear of AI multi-turn memory (no recording) |
| `CTRL_FORCE_FRESH_KEYS` | `f6` | Key held during a capture forces a fresh, context-free AI reply |
| `WHISPER_MODEL_SIZE` | `tiny.en` | `tiny.en` / `base.en` / `small.en` … |
| `WHISPER_DEVICE` | `auto` | `auto` · `cuda` · `cpu` |
| `LLM_PROVIDER` | `meta` | `meta` (also `meta-api`, `meta_api`) · `ollama` · `openrouter` · `none` |
| `META_API_KEY` | *(empty)* | Meta API key (`MODEL_API_KEY` also accepted) — paste real key in `.env` |
| `META_API_BASE` | `https://api.meta.ai/v1` | Meta API root |
| `META_MODEL` | `muse-spark-1.2-contributor` | Meta model id (also `META_API_MODEL`) |
| `LLM_MODEL` | see example | Ollama model tag (fallback when `OPENROUTER_MODEL`/`META_MODEL` blank) |
| `OPENROUTER_MODEL` | *(empty)* | OpenRouter model slug when `LLM_PROVIDER=openrouter` |
| `OPENROUTER_API_KEY` | *(empty)* | Required for openrouter |
| `OPENROUTER_API_BASE` | `https://openrouter.ai/api/v1` | OpenRouter OpenAI-compatible API root |
| `LLM_MAX_TOKENS` | `1536` | Hard cap on reply length |
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
| `install.sh` | Zero-to-one macOS/Linux installer |
| `odicto.py` | Cross-platform lifecycle CLI (setup/start/stop/status/autostart) |
| `setup_web.py` | Local setup web page (provider + key config) |
| `platforms/` | OS backends for hotkeys, clipboard, process, and window styling |
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
| AI mode pastes raw text (Meta) | Check `META_API_KEY` / `MODEL_API_KEY`, `META_MODEL`, and network; restart after `.env` edits |
| App refuses to start on openrouter | `OPENROUTER_API_KEY` is required when `LLM_PROVIDER=openrouter` |
| Ollama still using RAM on Meta/OpenRouter | Odicto is not calling it; quit the Ollama app / service separately (see resource section above) |
| Slow first run | Whisper/Ollama downloading; later runs are faster |
| CUDA errors | Set `WHISPER_DEVICE=cpu` in `.env` |
| Import errors | Recreate venv and reinstall `requirements.txt` |
| macOS hotkey/paste doesn't work | Grant **Accessibility** and **Input Monitoring**, then fully quit and restart Odicto |
| Linux hotkey/paste doesn't work | Run as root or add your user to the `input` group; on Wayland prefer X11 |

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
- **AI path** stays local if you use Ollama; **Meta / OpenRouter send the transcribed text to a third party**.  
- With `LLM_PROVIDER=meta` / `openrouter` / `none`, Odicto does not start Ollama — but a separately running Ollama install may still be active on the machine.  
- Never commit `.env` (may contain `META_API_KEY` / `MODEL_API_KEY` / `OPENROUTER_API_KEY`).
- No telemetry in this project.

---

## License

Use and modify freely for personal or commercial projects. Attribution appreciated but not required.

---

<p align="center">
  Built for people who think faster than they type.<br/>
  <b>Hold. Speak. Continue.</b>
</p>
