# AI chord

Tap the AI chord, speak an instruction, tap again: the LLM reply is pasted. Selected text (or a
clipboard image when enabled) is sent as context.

## Sub-features

- Fresh reply (no memory) by default; hold a keep-context key (F6) with the chord to continue memory.
- Selection probe at capture start in toggle mode (`Context: selection ...` log line).
- LLM deadline; on timeout the raw transcript is kept.

## How to get to it (user POV)

Select some text (optional), tap the AI chord from the log's `Tap '...': RECORD and paste a fresh
AI reply` line, speak, tap again. The pill shows Working while the LLM answers.

## Driving it with the owner and log_report.py

1. Note the time. Ask the owner to run the steps above in Notepad with a short request.
2. `log_report.py --since <time>`: one row, `mode=AI`, a `paste_s`. The log also shows
   `Sending query to <provider>` and `<provider> responded in Ns`.

## Gotchas

- AI captures take seconds (LLM time); judge responsiveness by `hud_lag`, not `paste_s`.
- `finish_reason='length'` with reasoning tokens triggers one retry with more tokens; that doubles the time.
