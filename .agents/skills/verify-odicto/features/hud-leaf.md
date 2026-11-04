# The leaf HUD

The bottom-center HUD is a leaf: a stem, a leaf body whose five vein rows show the voice, and a
short label (Listening, Transcribing, Thinking, Done, a notice, an error). F7 live captions sit
in a card above the leaf. `app/hud_leaf.py` paints it; `app/indicator.py` owns the state, the
clock and the window. Five keys choose the style: `HUD_WAVEFORM`, `HUD_TEXTURE`, `HUD_EDGE`,
`HUD_SHAPE`, `HUD_SIZE`. The setup page's Appearance view sets them and shows a live preview.

## Sub-features

- One look per state: stem, voice pixels, label, AI mark (recording and processing with AI only).
- Entry grows from a seed (0.62 s); exit closes in reverse (0.5 s); an exit in the entry never grows.
- The label width eases to fit a new word; the window resizes once, not per frame.
- Voice rows: the highest band lights the top row. A broken recorder reads as silence.
- Error tip droop; Done check and seed; a finished frame repaints at 4 FPS, recording never does.
- F7: caption card above the leaf; the leaf's bottom edge does not move when captions come or go.
- F7 captions fail (a rejected Gemini key, or the Live call fails): the leaf says "Captions off".
  A card with no captions says the cause and that the text still arrives, naming the speech
  service for a rejected key. Captions that came before the error stay.
- Plain texture: no veins and a faint grain (40 % of the Veins grain).
- Size 1x, 1.3x, 1.5x: the whole HUD scales (leaf, voice, label, AI mark, card, shadow). The
  window grows; the distance from the screen bottom and the centring stay. The voice pixels stay
  whole device pixels at every size and display scale.
- Style keys: values and defaults are in `AGENTS.md` ("HUD style").
- Theme colour (`THEME_COLOR`): the leaf, its glow, the voice pixels and the caption card follow the
  theme; the notice, error and AI colours and the label text never do. Forest is today's leaf, pixel for pixel.
  To look: render Listening, Transcribing, Done, an AI leaf and an F7 card with `HudStyle(theme=...)` for each
  preset and one custom colour (`#c06a8f`) at 100 % and 200 %. Check that the label stays readable on the body
  and that Graphite is neutral. The setup page's Colour panel (Appearance view) picks the colour and recolours
  the page and the preview at once; Discard returns to the saved colour.

## How to get to it (user POV)

Tap the raw chord and speak: the leaf opens, the five rows move with the voice. Tap again: the
label becomes Transcribing, then Done. Tap the AI chord: the AI mark and a blue-grey accent show,
and the label becomes Thinking. Tap F7 in Notepad: a card with the captions appears above the leaf.

## Driving it with the agent alone (offscreen, no Odicto process)

The leaf needs eyes. Render the real widget, not the painter alone. Never start Odicto for this.

1. Gate for the logic:
   ```powershell
   $env:QT_QPA_PLATFORM = "offscreen"
   & .\.venv\Scripts\python.exe -m unittest tests.test_hud_motion tests.test_hud_leaf -v
   ```
2. Scratch script (not in the repository, in `work\verify\<stamp>\`): build a `MagicMock` app
   (`state`, `last_status`, `use_llm`, `ready`, `recorder`, `live_active`, `live_preview`), make
   `DictationIndicator(app)`, call `_sync_from_app()`, jump the clocks to the end
   (`_w_tw`/`_h_tw` `snap(b)`, `_phase = "open"`, `_phase_t0` ten seconds back, `_glyph_fade = 1.0`),
   then `hud.grab().toImage().save(...)`. For a mid-entry frame set `_phase = "entering"` and
   `_phase_t0 = time.monotonic() - 0.34`. Give a recorder mock `get_bands(n)` for the voice rows.
3. Render every state at 100 % and at 200 % (`QT_SCALE_FACTOR=2` in a second process): entry
   mid-way, Listening raw, Listening AI, Transcribing, Thinking, Done (check and seed, then
   settled), a notice (Context cleared), an error label (Mic unavailable, and a long one), F7 with
   two caption lines and with none yet.
   For a size, patch `Config.HUD_SIZE` to `"1.3x"` or `"1.5x"` before you make the widget, and
   render Listening and an F7 card at 100 % and 200 %. Check that the voice cells are crisp and the
   card text stays inside the card.
4. Look at every image. Check: the label is whole and inside the leaf; the AI mark shows only for
   AI; the stem is a line, a check, a bang or a cross as the state says; the card is centred above
   the leaf with its text inside; nothing is cut at the window edge; the shadow is soft at 200 %.
5. Also run `tools/hud_snapshots.py --out <folder>` for the painter's own sheets (styles, scales,
   motion, backdrops).

## Driving it with the owner and log_report.py

The owner restarts Odicto on the new code (Start Menu "Odicto", or `start_dictation.bat`). The
agent never restarts it. Then, with the note of the time:

1. Ask the owner for one raw dictation, one AI dictation and one F7 live dictation in Notepad.
2. `log_report.py --since <time>`: three rows, each with `hud_lag` of 1 s or less.
3. Ask the owner to say whether they accept the look: the leaf, the motion, the card.

## Gotchas

- The owner's Odicto is the only instance. Never run `main.py`; a second keyboard hook doubles letters.
- Never read `.env` to learn the style keys. Ask the owner, or read `odicto.py config` output only
  if they ask.
- `time.monotonic()` ticks every 15.6 ms on some Windows builds; a frame grabbed within one tick
  of a spring retarget has not moved yet.
- Offscreen fonts: the brand font loads from `assets/fonts`, so renders match the screen.
