# F7 live dictation

Tap F7 to start live dictation (Gemini Live); interim captions show in the pill; tap F7 again
to finalize and insert once.

## Sub-features

- Interim captions in the HUD while speaking.
- Stop waits up to ~2.5 s for the authoritative final; a final or draft skips batch STT.
- Outside terminals the final text stays on the clipboard.

## How to get to it (user POV)

Focus Notepad, tap F7, speak, watch captions in the pill, tap F7 again.

## Driving it with the owner and log_report.py

1. Note the time; ask the owner to run the steps above.
2. Read the log since that time for the live session lines and one paste. `log_report.py` lists
   it only when the log uses the `>>> Recording` line; otherwise read the raw log lines.

## Gotchas

- Needs a Gemini key in the owner's config; without it the log names the missing key.
