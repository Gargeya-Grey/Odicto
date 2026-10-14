# Hotkey responsiveness

The pill must appear the instant the chord is tapped, also after Odicto sat idle for a long
time while other apps used the RAM.

## Sub-features

- Owner stays resident in RAM (Windows working set) while idle.
- Keyboard hook returns fast (a late hook loses key-up events: stuck modifiers, wrong mode).
- Mic callback keeps up (no idle `input overflow` lines).

## How to get to it (user POV)

Leave Odicto idle for 10+ minutes while using other heavy apps, then tap the raw chord.

## Driving it with the owner and log_report.py

1. Run the doctor. Record the working set. A value far below private memory (< 60 MB) is the
   paged-out state that causes lag.
2. After an idle period, ask the owner to tap the raw chord, say a word, tap again.
3. `log_report.py --since <time>`: `hud_lag` ≤ 1 s, `stt_s` near its normal value (Groq ~0.3 s).
4. Compare the footer's idle overflow count per hour with an earlier window.

## Gotchas

- The doctor cannot read the priority class of an elevated owner; that is expected.
- Machine RAM pressure (many Claude/Chrome/node processes) is the trigger; reproduce under it.
