# Raw dictation chord

Tap the dictation chord, speak, tap again: the transcript is pasted (typed into terminals).

## Sub-features

- Toggle mode (`HOTKEY_TOGGLE=true`): tap to start, tap to stop. Hold mode: hold while speaking.
- Mode choice from the modifiers held when the primary key goes down (`_match_active_chord`).
- Cloud STT (Groq here) with local Whisper fallback on error.

## How to get to it (user POV)

Focus any text field (Notepad is safest). Tap the raw chord from the log's `Tap '...': RECORD
and paste transcript` line. The pill rises showing Listening. Speak. Tap the chord again. The
pill shows Working, then a check, and the text appears.

## Driving it with the owner and log_report.py

1. Note the time. Ask the owner: "Focus Notepad, tap <raw chord>, say one short sentence, tap
   <raw chord> again, tell me when done."
2. Run `log_report.py --since <time>`. Expect one row, `mode=raw`, `hud_lag` ≤ 1 s, an `stt_s`
   and a `paste_s`.
3. Ask the owner whether the text landed and whether the pill appeared at once.

## Gotchas

- A row with `mode=AI` after the raw chord is the stuck-modifier bug: fail.
- `Warning: the microphone had a gap` is a flag, not a failure; the audio is kept.
- Timestamps have 1 s resolution, so `hud_lag` of 1 s is noise.
