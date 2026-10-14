---
name: verify-odicto
description: Prove Odicto behaviour (Windows desktop dictation app: global hotkeys, bottom-center HUD pill, paste into the focused app) on this machine. Use after any change to hotkeys, HUD, recorder, STT/LLM pipeline, paste or process lifecycle, and whenever the user reports lag, a wrong mode, double letters or a missing paste.
---

# Verify Odicto

Odicto has no window to click. A user taps a global chord (raw dictation or AI), speaks,
taps again, and text appears in the focused app. The bottom-center pill (HUD) shows
Listening → Working → done. Evidence comes from the owner's `dictation.log` (timings and
lengths only, never transcript text) and `dictation-health.json` (heartbeat every 2 s).

## Hard limits (read first)

- **This machine runs Odicto for real.** An agent never runs `start_dictation.bat`,
  `stop_dictation.bat`, `odicto.py start|stop|restart`, `schtasks`, `admin_mode.ps1`, or
  setup Save (it restarts Odicto). The owner restarts Odicto; the agent asks them to.
- **Never start a second Odicto `main.py`**, from any checkout: a second global keyboard hook
  stacks on the first and doubles every typed letter system-wide. The install lock is per
  folder, so a worktree copy is NOT blocked by the owner's lock. There is no isolated
  instance; do not try to make one.
- Never read `.env`. Use `odicto.py config` output only if the owner asks, and never paste it.
- Instances cannot run side by side, so the live journeys below need the owner's hands.

## Where things are

- Owner install (`-Root`): the main checkout, here `W:\AI-Projects\Odicto`. Agents work in
  `.claude/worktrees/<name>`; that folder has no `.venv`, no log and no pid file.
- Python for helpers and tests: `<Root>\.venv\Scripts\python.exe`. In a worktree, link it once:
  `cmd /c mklink /J .venv <Root>\.venv` (`.venv/` is gitignored; delete only the junction).
- Owner runs elevated when admin mode is on: its command line and priority are unreadable
  from a normal shell. That is expected, not a fault.

## Launch

There is no agent launch. The owner's Odicto is the instance. To pick up new code the owner
runs their own restart (Start Menu "Odicto", or `start_dictation.bat` / setup Save). Ready =
`dictation.log` shows `Application ready!` after a `----- session <time> pid=<pid> -----`
header whose time is after the change, and `Code version: git <sha>` names the new commit.

## Doctor (run first, read-only)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .agents\skills\verify-odicto\scripts\doctor.ps1 [-Root W:\AI-Projects\Odicto]
```

Prints the running build, the owner pid, its working set vs private memory, the heartbeat age,
the mic and its host API. Exit 1 with `PROBLEM:` lines when the owner is gone, hung (heartbeat
older than 10 s), not ready, or paged out (working set < 60 MB: the next hotkey waits on disk).

## Drive

1. **Live journey (owner's hands).** Note the time. Ask the owner to do the feature's steps from
   `features/` (for example: focus Notepad, tap Ctrl+Win, say one sentence, tap Ctrl+Win).
   Wait for them to say done. Do not type into their apps yourself.
2. **Read the result.**
   ```powershell
   & <Root>\.venv\Scripts\python.exe -I .agents\skills\verify-odicto\scripts\log_report.py --root <Root> --since "YYYY-MM-DD HH:MM:SS" --out <Root>\work\verify\<stamp>
   ```
   One row per capture: mode (raw/AI), `hud_lag` (seconds between the app's state change and the
   HUD seeing it; > 1 s is flagged LAG), STT seconds, paste seconds, mic overflows in the capture.
   The footer counts idle overflows (overflows while no capture ran) and restarts.
3. **Hook-free harness (agent alone).** Hotkey dispatch, mode choice, HUD motion and the pipeline
   run against fake hooks, offscreen Qt and no microphone:
   ```powershell
   & .\.venv\Scripts\python.exe tools\run_tests.py     # the gate; or .\tools\verify.ps1 for the full gate
   & .\.venv\Scripts\python.exe -m unittest tests.test_hotkey_dispatch -v   # one area
   ```
   These prove branch behaviour, not the live path. A live claim needs step 1.

## Evidence

- Keep the doctor output and `report.txt` / `report.json` from `--out`. Put them in
  `<Root>\work\verify\<YYYYMMDD-HHMMSS>\` (gitignored). Paste the relevant rows into the reply.
- Proof standard: the owner drove the real chord; the report row exists for that time with the
  expected mode, `hud_lag` ≤ 1 s, a paste time, and the owner confirms the text landed.
  A row with the wrong mode is a failure even if text pasted.
- For latency claims compare the same report before and after the change (same `--since`
  window length), and the doctor's working set before and after an idle period.

## Cleanup

Nothing to tear down: the agent starts no process. Remove only scratch files you created.
Never delete `work\verify\` (the evidence) or the owner's log, pid or health files. If you made
a `.venv` junction in a worktree, remove it with `cmd /c rmdir .venv` (removes the link only).

## Features

See `features/README.md`.
