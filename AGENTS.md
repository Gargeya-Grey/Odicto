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

- Default hotkeys: hold **Ctrl+`** (`HOTKEY=ctrl+grave`) for raw dictation; hold **Ctrl+Shift+`** (`AI_HOTKEY=ctrl+shift+grave`) for AI. Keyboard lib name for `` ` `` is `grave`. Avoid Alt chords (browser focus loss on Alt release).
- First Whisper load downloads model weights (~75MB for `tiny.en`). Whisper always loads for STT, independent of LLM provider.
- First Ollama pull downloads the LLM (size depends on model). Odicto only starts/calls Ollama when `LLM_PROVIDER=ollama`.
- **OpenRouter:** set `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, and `OPENROUTER_MODEL`. Localhost `LLM_API_BASE` is auto-rewritten to `OPENROUTER_API_BASE` (`https://openrouter.ai/api/v1`). Odicto will **not** spawn Ollama in this mode; a pre-existing Ollama tray/service may still use RAM until the user quits it.
- **Provider `none`:** raw dictation only; no LLM client; Ollama not started by Odicto.
- App may need **admin** or elevated rights only if the `keyboard` hook fails on some locked-down machines; try normal user first.
- GPU: if CUDA is available, Whisper uses it automatically (`WHISPER_DEVICE=auto`).
- Stop with `stop_dictation.bat` or kill PID in `dictation.pid`.

## STRICT: single instance only (never stack keyboard hooks)

**Why normal typing is related to Odicto:** `keyboard.hook_key(..., suppress=True)` installs a **system-wide** Windows low-level keyboard hook (`WH_KEYBOARD_LL`). While Odicto is running, **every keypress in every app** (Notepad, browser, etc.) goes through that hook — not only the hold-to-talk chord and not only while recording. If a second Odicto process also installs a hook, Windows delivers each character twice (`tthhiiss`). That is not the mic, Whisper, or paste path; it is the global hook layer.

### What actually caused two instances (root cause)

Not a mysterious self-fork — Odicto never spawns a second `main.py`. Duplicates came from **failed cleanup + relaunch**:

1. **Broken `stop_dictation.bat` orphan killer (primary bug)**  
   The `for /f "tokens=2 delims== "` loop over `wmic ... /format:csv` did **not** parse ProcessId. WMIC CSV is `Node,CommandLine,ProcessId`; token 2 with those delimiters is a **command-line fragment**, so `taskkill` never hit real orphan PIDs. Starting again left the old `pythonw main.py` alive.

2. **PID-file-only kill inside Python (secondary)**  
   Old `_kill_stale_instance` only `taskkill`’d the PID in `dictation.pid`. If that file was stale, pointed at a dead process, or was overwritten by a third short-lived start, **live orphans were ignored**. Observed: PID file `42516` while two live processes were `41356` and `25428` — both `pythonw ... main.py` (two background starts, not debug+tray).

3. **Startup race (secondary)**  
   Kill/write-PID happened at the beginning of init; hotkeys bound only **after** Whisper load (~1s+). Two near-simultaneous starts could both pass “kill the other”, both load, both `hook_key`.

4. **How a second start happens in practice**  
   Double-click `start_dictation.bat` twice, run start while an old instance was already up, or start again after a “stop” that only deleted the PID file. App does **not** auto-restart itself.

**Mitigations now (layered):** fixed PowerShell-based stop script; process enum kill in Python; install-scoped named mutex (`Global\\` preferred, else `Local\\`) before hooks; exclusive `dictation.lock` byte-lock as second gate (covers Admin vs non-Admin split); refuse `hook_key` without both; `unhook_all` on shutdown.

**Not “mathematically perfect” residual risks:** (1) another app’s own keyboard hook can still interact badly with `suppress=True`; (2) process enum is best-effort if WMI/PowerShell is blocked — mutex+lockfile still block a second bind; (3) intentional take-over: a new start may kill the old instance after ~8s wait rather than stacking.

**Hard rules for agents and operators:**

1. **Never run two Odicto `main.py` processes** for the same install (debug + tray, two terminals, stale pythonw, etc.).
2. Before a new start, prefer `stop_dictation.bat` or `start_dictation.bat` / `run_debug.bat` (they stop first).
3. Startup **must** acquire the install-scoped named mutex and kill orphan `main.py` processes; if the lock cannot be taken, **exit without** calling `hook_key`.
4. **Never** call `keyboard.hook_key` / global hooks unless the single-instance lock is held.
5. On shutdown, always `keyboard.unhook_all()` and release the mutex so typing returns to normal.
6. If the user reports **double letters while typing**, assume stacked instances first: stop all, confirm no `main.py` left, start **once**.
7. Do **not** “fix” orphan kill by restoring the old WMIC `for /f tokens=2 delims==` batch loop.

## Do not

- Do not publish `.env` or API keys (`OPENROUTER_API_KEY` especially).
- Do not require Mac/Linux paths in install docs (unsupported).
- Do not replace `keyboard` / paste behavior without user request.
- Do not assume Ollama is stopped system-wide just because `LLM_PROVIDER` is not `ollama` — only Odicto’s own spawn path is skipped.
- Do not allow multiple Odicto instances / stacked keyboard hooks (causes system-wide double typing).
