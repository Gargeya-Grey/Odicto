# Odicto feature map

One file per user-facing feature. Each says how a user reaches it, how to drive it with
the owner's hands plus `log_report.py`, and what proves it.

| Feature | File | Proof in short |
|---|---|---|
| Raw dictation chord | [raw-dictation.md](raw-dictation.md) | row `mode=raw`, paste time, text in the app |
| AI chord | [ai-chord.md](ai-chord.md) | row `mode=AI`, LLM reply pasted |
| Hotkey responsiveness | [responsiveness.md](responsiveness.md) | `hud_lag` ≤ 1 s after idle; doctor working set stays high |
| F7 live dictation | [live-f7.md](live-f7.md) | interim captions in the pill; one insert at stop |
| Vocabulary and Polish | [vocabulary-polish.md](vocabulary-polish.md) | spelling hints reach STT; careful editing; owner confirms text |
| Managed installation, migration and updates | [managed-install.md](managed-install.md) | customer install path, source retirement, stable admin/settings, activation and rollback |
| Cancel (Esc) | [cancel.md](cancel.md) | `Cancelled. Nothing inserted.`; nothing pasted |
| The leaf HUD | [hud-leaf.md](hud-leaf.md) | offscreen renders of every state at 100 % and 200 %; owner accepts the look; `hud_lag` ≤ 1 s |

The chords come from the owner's `.env` (`HOTKEY`, `AI_HOTKEY`). Read them from the log's
`Hotkeys bound:` and `Tap '...'` lines, never from `.env`. On this machine (2026-10):
raw = Ctrl+Win, AI = Shift+Win, live = F7, cancel = Esc.
