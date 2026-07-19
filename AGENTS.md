# Agent install guide — Odicto

This file is for coding agents (and power users) automating setup on a **fresh Windows machine**.

## Goal

Make the app runnable end-to-end: venv, Python deps, optional Ollama LLM, Whisper weights, `.env`, then verify with unit tests / a dry launch.

## Constraints

- **Windows only** (global hotkeys + paste simulation are Windows-oriented).
- Never commit `.env` (may contain API keys).
- Prefer the project `install.ps1` over ad-hoc steps.

## One-shot install (preferred)

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Flags:

| Flag | Meaning |
|------|---------|
| `-SkipOllama` | Skip Ollama install/pull (raw dictation only) |
| `-OllamaModel qwen2.5:1.5b-instruct` | Model to `ollama pull` |
| `-WhisperModel tiny.en` | Whisper size to pre-download |

## Manual checklist (if script fails)

1. Install **Python 3.10+** (`winget install Python.Python.3.12`).
2. `py -3 -m venv .venv`
3. `.\.venv\Scripts\python.exe -m pip install -U pip wheel`
4. `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
5. `copy .env.example .env`
6. Optional AI: install [Ollama](https://ollama.com/download), then `ollama pull qwen2.5:1.5b-instruct`
7. Warm Whisper:  
   `.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
8. Tests: `.\.venv\Scripts\python.exe -m unittest test_units -v`
9. Start: `.\start_dictation.bat` or `.\run_debug.bat`

## Verify success

| Check | Expected |
|-------|----------|
| `.\.venv\Scripts\python.exe -c "import PySide6, faster_whisper, keyboard"` | No import error |
| `unittest test_units` | All tests OK |
| `run_debug.bat` | Log shows `Application ready!` and `HUD enabled` |
| Hold hotkey | Bottom-center pill shows **Listening** |

## Runtime notes for agents

- First Whisper load downloads model weights (~75MB for `tiny.en`).
- First Ollama pull downloads the LLM (size depends on model).
- App may need **admin** or elevated rights only if the `keyboard` hook fails on some locked-down machines; try normal user first.
- GPU: if CUDA is available, Whisper uses it automatically (`WHISPER_DEVICE=auto`).
- Stop with `stop_dictation.bat` or kill PID in `dictation.pid`.

## Do not

- Do not publish `.env` or API keys.
- Do not require Mac/Linux paths in install docs (unsupported).
- Do not replace `keyboard` / paste behavior without user request.
