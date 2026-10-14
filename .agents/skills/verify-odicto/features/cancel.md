# Cancel (Esc)

While the pill shows Working, Esc cancels: nothing is inserted, late results are dropped.

## Sub-features

- `CANCEL_HOTKEY` (default `esc`), never suppressed, so Esc still reaches the focused app.

## How to get to it (user POV)

Start an AI capture, stop it, and press Esc while the pill shows Working.

## Driving it with the owner and log_report.py

1. Note the time; ask the owner to run the steps above.
2. The log shows `>>> Cancel requested; nothing will be inserted.` and `>>> Cancelled. Nothing
   inserted.`; the report row has no `paste_s`. Ask the owner to confirm nothing appeared.

## Gotchas

- A raw capture is often done before Esc lands; use the AI chord for a reliable window.
