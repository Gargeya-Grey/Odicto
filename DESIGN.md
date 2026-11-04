# Odicto design system

This file records how Odicto looks and moves. It covers the **setup page** (the web UI on
`127.0.0.1`) and the **HUD** (the "leaf" pill at the bottom of the screen). A designer or an
engineer can rebuild both from this file alone.

Source of truth, if this file and the code ever disagree, the code wins:

| Surface | File |
|---|---|
| Setup page markup, CSS, JS | `assets/setup_template.html` |
| Theme maths (Python) | `app/theme.py` |
| HUD painter (all drawing) | `app/hud_leaf.py` |
| HUD window, state, timing, captions | `app/indicator.py` |
| Font | `assets/fonts/InstrumentSans-VF.ttf` (OFL, `OFL-InstrumentSans.txt`) |
| Logo | `assets/odicto-mark.svg`, `assets/odicto-icon.svg` |

Every number below was copied from those files at commit `968bbd3`.

---

## 1. Design idea (read this first)

This file has two layers. Keep them apart when you apply it:

- **Principles** (this section, and the "Why" lines under each component). These are the
  understanding. They transfer to any new screen, widget or platform.
- **Specifications** (numbers, hex, milliseconds). These are one correct answer to the
  principles, tuned by eye. Reuse them to match Odicto exactly. When you build something the
  spec does not cover, derive it from the principles. Do not copy a number into a place where
  its reason does not hold.
- **"Never" and "always" are shorthand.** Each such rule has its reason next to it. Where a
  recipe below shows a documented exception (for example the nav pill eases its own size, the
  looping bars use `ease-in-out`), the recipe is the exception and the rule still holds
  everywhere else.

### 1.1 The feeling

Odicto should feel like **a quiet instrument on a dark desk**: a tool you trust, not an app that
performs for you. A user dictates all day. So the UI must be easy on the eyes, never busy,
and always honest about what is happening.

One visual idea runs through both surfaces: **the leaf**. A leaf is soft, organic and alive,
but it is also precise: veins, a stem, a tip. The UI copies this mix. Soft light and glass for
the organic part. Hairlines, pixel grids and exact snapping for the precise part.

### 1.2 Principles, each with its reason

1. **Depth comes from light, not from borders.** Surfaces are translucent *glass* with a thin
   light on the **top edge** only, and a deep, soft shadow below. Light from above is what the
   eye expects, so the panel reads as a physical object without a heavy outline. A full border
   would flatten it and make it look like a form.
   *Applying it:* give any new raised thing the same top rim and the same shadow family.
   Never box it with a visible outline. (Floating layers such as menus, toasts and dialogs
   add a faint 1 px inset hairline, `--line`, so they separate from busy content behind
   them. It is a whisper, not a frame.)
2. **Inputs are dug in, panels are lifted.** Every field is a dark **well** (inset shadow and a
   faint light line under it). Panels sit above the canvas, wells sit below the panel. The user
   learns "dark and sunken means I can type or choose here".
3. **One accent, borrowed from the leaf, and it means *this one*.** The accent (with its soft
   glow) marks the thing that is current or on: the selected page, the selected segment, the
   "on" switch, the chosen card, the section you are reading, the live voice. If everything glows,
   nothing does. So the accent is also used sparingly as a *text* colour (links, section labels).
   The primary button is the one filled accent surface.
4. **The glowing stem is the signature.** A 2 px vertical bar with a soft halo says "you are
   here". It appears in the nav, in section labels, in the segmented knob, under a chosen
   swatch, and as the HUD's stem. When you need a "current" marker, use a stem, not a
   background block or an underline.
5. **Colour is one dial.** Everything is derived from a hue `--h` and a chroma factor `--k`
   at fixed *lightness*. Fixed lightness is the strategy for keeping contrast and depth steady
   across themes. Only hue and saturation move. That is the intent: a picked colour should keep
   the page readable. `tests/test_theme.py` checks modelled page contrast for the presets and
   eight custom colours (red, green, blue, yellow, white, black, grey, a rose); it does not
   prove every possible colour or any real display.
   Status colours (amber, soft red, moon blue) and the logo do **not** move, because they
   carry meaning that must not change with taste.
6. **Status is never colour alone.** OK is a filled dot, warning a hollow ring, error a
   diamond, info a blue dot. A colour-blind user or a user in a bright room still reads it.
   Words always accompany the mark.
7. **Motion explains; it never decorates.** Every animation answers a question: *where did it
   go* (the nav pill slides), *what changed* (a value swaps in with a small rise), *is it
   alive* (the status bars breathe), *did it work* (a check draws itself), *did it fail* (a
   short shake). The curve is one: fast start, long soft landing (`0.2, 0, 0, 1`). The
   base durations are three: 120, 180, 320 ms (plus two one-offs: 420 ms for a whole-page
   change, 360 ms for the check and the shake). Short enough that the user never waits for it.
   Deliberate exceptions: the looping status bars use `ease-in-out`, the spinner and the HUD
   shimmer loop linearly, and the HUD exit uses an accelerate curve (`0.3, 0, 1, 1`) because
   leaving should speed up.
8. **Things that change size or place move with `transform` and `opacity`.** These do not
   re-lay out the page (that is the intent; this file does not measure it). The segmented knob
   slides with `translateX`, not `left`. The nav pill slides with `translate` (it also eases its
   own width and height; it is out of flow, so nothing else moves). Content in the page flow
   does not animate its size, so the page does not shift under the reader.
9. **Floating things never move the page.** The save bar, toasts, menus and dialogs are
   `fixed` layers on top. A message that appears must not push the content the user is
   reading. Menus live on `<body>` so no panel can clip them.
10. **Hit areas are larger than the drawing.** The switch is drawn 40×24 but clickable 52×40;
    a swatch is drawn 38×16 but clickable 42×28; base buttons are 40 high (small ones 32), inputs 44. Fingers and
    cursors are imprecise, and generous targets feel *calmer*.
11. **Hover is a veil, not a colour.** Hover is `4 %` of the text colour laid over whatever is
    underneath. It works on glass, on wells and on the rail without a custom colour for each.
    Press is a tiny shrink (`scale .98`). Both are quick (120 ms) so the control feels attached
    to the hand.
12. **Nested corners are concentric.** An inner radius is the outer radius minus the gap: the
    segmented control is radius 12 with 3 px padding, so its knob is radius 9. Copy this
    arithmetic whenever you put a rounded thing inside a rounded thing.
13. **Pixels land on pixels.** In the HUD, the small data marks (voice squares, counting dots,
    the AI glyph) have every edge on a whole *device* pixel, so they are crisp on any screen.
    The stem's position and the text baseline are snapped to the device grid too, so they do
    not shimmer between frames; the stem itself is antialiased, so at a fractional scale its
    edges can be slightly soft. On the web, 1 px lines and 2 px stems are whole. Blur is a bug,
    not a style.
14. **Time, not frames.** The HUD's entry, exit and size springs are functions of real elapsed
    time with exact end values, so they look the same at 30 or 144 fps. The voice envelope and
    the stem's state clock advance by the frame time capped at 50 ms, so a stalled frame
    pauses them briefly instead of making them leap. Effects driven straight by the wall clock
    (the idle first pixel's pulse, the Work wave, the shimmer, the bead, the dots) keep time
    with it, so after a stall they are simply where the clock says.
15. **Leave the way you came, in reverse.** The HUD opens seed → stem → bud, then the words
    begin to fade in just before the tip starts to form, and it closes words → tip → bud →
    stem → seed (the stages overlap; section 4.8 gives each start).
    Reverse order makes the exit feel like the same motion played back, which feels
    intentional and calm. An exit that starts mid-entry never grows first.
16. **Idle is quiet.** When nothing moves, the work drops to a minimum: the HUD slows to a
    250 ms tick on a finished frame (it still repaints from cached images, four times a
    second), the canvases stop when the tab is hidden, polling slows to once a minute.
    Efficient is part of calm.
17. **The user controls the motion.** Full, Reduced, Off, and the system setting all work.
    Nothing important is conveyed by motion alone.
18. **Plain words, short, human.** See section 7.

### 1.3 What is a rule and what is taste

| Fixed (do not vary without a reason) | Flexible (derive from the principles) |
|---|---|
| The easing curve; the three durations | Exact padding of a new, unlisted component |
| Lightness values of the tokens | Which icon to use |
| Top rim + deep soft shadow recipe | The width of a new panel |
| Stem = 2 px, accent, soft halo | Copy for a new feature (follow section 7) |
| Status shapes (dot / ring / diamond) | Number of columns in a new grid |
| Inputs are wells; one primary per area | Order of new settings pages |
| Never animate in-flow layout (the out-of-flow nav pill's own box is the one exception) | Extra theme presets (add a hue, keep `k` and lightness) |
| HUD snaps to device pixels; time-based motion | HUD copy for a new status (keep it to 1–3 short words) |

### 1.4 Quick checks for any new thing you build

1. Cover the accent colour. Can you still tell what is selected? (shape, weight, a stem)
2. Squint. Is there one calm focus, or three competing ones?
3. Does it lift off the canvas by light (rim + shadow), not by a border?
4. Does every state have a look: rest, hover, press, focus, disabled, on, busy, error?
5. Does it still work with motion Off and with `--k` near 0 (Graphite)?
6. Does the number of things that glow stay small?
7. Is the text something a tired person can read in one glance?

---

## 2. Foundations

### 2.1 Font

- **Instrument Sans** (variable, weight 400 to 700), shipped with the app. No network request.
- Fallback stack: `'Instrument Sans', 'Segoe UI Variable Text', 'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif`.
- Mono stack: `'Cascadia Mono', 'SF Mono', ui-monospace, Consolas, 'Liberation Mono', monospace`.
- `-webkit-font-smoothing: antialiased`. Body is 14 px / 20 px.

```css
@font-face {
  font-family: 'Instrument Sans';
  src: url('/assets/fonts/InstrumentSans-VF.ttf') format('truetype');
  font-weight: 400 700;
  font-display: swap;
}
```

### 2.2 Type scale (setup page)

| Role | Size / line | Weight | Extra |
|---|---|---|---|
| Page title `h1` | 34 / 1.1 (28 under 900 px) | 500 | letter-spacing `-0.03em` |
| Page lede `p` | 14.5 / 22 | 400 | `--muted`, max 56ch, 10 px above |
| Hero title | 19 / 26 | 500 | `-0.01em` |
| Section label `h2` | 11.5 / 16 | 600 | UPPERCASE, `0.12em`, `--accent`, glowing stem |
| Row title `h3` | 15 / 20 | 500 | |
| Row text `p` | 12.5 / 18 | 400 | `--muted` |
| Body / input | 14 / 20 | 400 | |
| Button | 14 / 20 (small: 13) | 500 (primary 600) | |
| Nav item | 14.5 | 400 | |
| Brand word `odicto.` | 19 / 24 | 600 | `-0.02em`; the dot is `--logo-stem` |
| Version tag | 11 mono | 500 | `--faint` |
| Chip | 11.5 / 16 | 500 | |
| Keycap | 11.5 mono | 500 | symbol keys 15 px / 600 |
| Textarea / key field | 13 mono | 400 | key field `letter-spacing: 0.02em` |

### 2.3 Colour tokens

Colour is built from two inputs:

- `--h` hue in degrees.
- `--k` chroma factor (1 = full colour, 0.12 = nearly grey).

The themed tokens are `oklch(L calc(C * var(--k)) var(--h))`. Lightness is fixed. Hue and chroma
move. Exceptions: `--on-action` (fixed chroma 0.01, hue still follows `--h`), the fixed colours
below (logo, warn, danger, moon) and the derived tokens (`color-mix`, shadows).
The server writes `--h` and `--k` into the page (no green flash on first paint).

```css
:root {
  color-scheme: dark;
  --h: 165; --k: 1;                       /* Forest. Seeded by the server. */

  /* Surfaces */
  --canvas:    oklch(0.205 calc(0.010 * var(--k)) var(--h));        /* page */
  --rail:      oklch(0.182 calc(0.008 * var(--k)) var(--h));        /* sidebar */
  --glass:     oklch(0.300 calc(0.018 * var(--k)) var(--h) / 0.52); /* panels */
  --glass-hi:  oklch(0.355 calc(0.024 * var(--k)) var(--h) / 0.62); /* segment knob, muted chip */
  --float:     oklch(0.255 calc(0.016 * var(--k)) var(--h));        /* menus, toasts, save bar, dialogs */
  --well:      oklch(0.165 calc(0.007 * var(--k)) var(--h) / 0.7);  /* inputs, inset areas */
  --raised-2:  oklch(0.355 calc(0.024 * var(--k)) var(--h));        /* scrollbar thumb */

  /* Strokes */
  --rim:  oklch(0.96 calc(0.03 * var(--k)) var(--h) / 0.13);        /* light on the TOP edge */
  --line: oklch(0.93 calc(0.012 * var(--k)) var(--h) / 0.08);       /* hairline divider */

  /* Text */
  --text:  oklch(0.945 calc(0.008 * var(--k)) var(--h));
  --muted: oklch(0.79  calc(0.010 * var(--k)) var(--h));
  --faint: oklch(0.65  calc(0.010 * var(--k)) var(--h));

  /* Accent and action */
  --accent:      oklch(0.87 calc(0.088 * var(--k)) var(--h));
  --accent-soft: oklch(0.87 calc(0.088 * var(--k)) var(--h) / 0.11);
  --glow:        oklch(0.87 calc(0.088 * var(--k)) var(--h) / 0.45);
  --action:      oklch(0.50 calc(0.085 * var(--k)) var(--h));       /* primary button fill */
  --action-hi:   oklch(0.53 calc(0.09  * var(--k)) var(--h));
  --on-action:   oklch(0.98 0.01 var(--h));
  /* Leaf colours for the page. Defined, but no rule uses them today (reserved for leaf-tinted surfaces). */
  --leaf-a:      oklch(0.34 calc(0.055 * var(--k)) var(--h) / 0.92);
  --leaf-b:      oklch(0.24 calc(0.045 * var(--k)) var(--h) / 0.94);
  --vein:        oklch(0.90 calc(0.07  * var(--k)) var(--h) / 0.16);

  /* Fixed for every theme */
  --logo-stem: #8edcbb;   /* the logo never follows the theme */
  --warn:   #f2c27a;      /* amber: caution, "starting" */
  --danger: #f2a39b;      /* soft red: error, stopped */
  --moon:   #b2d6f4;      /* pale blue: AI, info */

  /* Derived */
  --hover:      color-mix(in srgb, var(--text) 4%, transparent);
  --border-ctl: color-mix(in srgb, var(--text) 14%, transparent);
  --sel-line:   color-mix(in srgb, var(--accent) 45%, transparent);
  --well-in: inset 0 1px 2px rgb(0 0 0 / 0.35), 0 1px 0 var(--rim);              /* the "well" */
  --halo:    0 0 0 2px var(--accent), 0 0 0 5px var(--accent-soft);               /* focus ring */
  --shadow:  0 18px 40px -18px rgb(0 0 0 / 0.65), 0 2px 6px -2px rgb(0 0 0 / 0.4);/* floating things */
}
```

**Fallback without `oklch()`**: an `@supports not (color: oklch(0.5 0.1 165))` block pins the
Forest colours in hex (canvas `#17191c`, rail `#0f1311`, text `#e7e5df`, muted `#b2b5b5`,
faint `#8a918e`, accent `#8edcbb`, action `#2a745d`, action-hi `#2c7860`, on-action `#f4f6f1`,
float `#1c2521`, raised-2 `#303438`; glass `rgb(38 49 44 / .52)`, glass-hi `rgb(48 64 57 / .62)`,
well `rgb(12 15 14 / .7)`, rim `rgb(224 249 237 / .13)`, line `rgb(225 235 230 / .08)`,
accent-soft `rgb(156 231 198 / .11)`, glow `rgb(156 231 198 / .45)`).
These match the Odicto Mobile app.

### 2.4 Themes

| Name | Hue | k |
|---|---|---|
| forest (default) | 165 | 1.0 |
| ocean | 232 | 1.0 |
| plum | 318 | 0.95 |
| ember | 32 | 1.0 |
| graphite | 230 | 0.12 |
| custom `#rrggbb` | the colour's own OKLCH hue | `clamp(chroma / 0.13, 0.08, 1)` |

- A custom colour with chroma below `0.02` is grey. It takes Graphite's hue (230) and `k = 0.08`.
- Setting: `THEME_COLOR=forest|ocean|plum|ember|graphite|#rrggbb`. Blank or unknown means forest.
- Never changes with the theme: `--logo-stem`, `--warn`, `--danger`, `--moon`, and in the HUD
  `INK` (text colour). They carry meaning.
- The HUD uses the same idea in Python (`app/theme.py`). Each Forest leaf colour keeps its
  **lightness**, takes the theme's hue, and has its **chroma multiplied by `k`**. If the result
  leaves sRGB, **chroma** is lowered (by bisection, 24 steps). Lightness is never changed.
  So any theme keeps Forest's light and dark, and the label stays readable.
  The page repeats this maths in JS; `tests/test_theme.py` keeps the two equal.

### 2.5 Shape

| Token | Value | Used for |
|---|---|---|
| `--r-group` | 18 px | glass panels, hero, cards |
| `--r-control` | 12 px | buttons, inputs, selects, menus, toasts |
| `--r-option` | 9 px | segment knob, menu options, small inner controls |
| `--r-pill` | 999 px | chips, switch track, model chips |
| other | 14 px run-card and provider cards; 16 px save bar; 20 px dialog; 10 px nav item, small button; 7 px keycap |

### 2.6 Motion tokens

```css
--ease: cubic-bezier(0.2, 0, 0, 1);   /* the one curve; fast start, long soft landing */
--t-fast: 120ms;   /* hover, colour, press */
--t-med:  180ms;   /* switch, menus, toasts, keycaps */
--t-slow: 320ms;   /* nav pill, segment knob, save bar rise */
/* page change: 420ms; check draw: 360ms; shake: 360ms */
```

Three motion levels, saved in `localStorage` as `odicto-setup-motion`:

| Level | Effect |
|---|---|
| **Full** (default) | as written |
| **Reduced** | `html[data-motion="reduced"]`: `--t-fast 60ms`, `--t-med 90ms`, `--t-slow 120ms` |
| **Off** | `html[data-motion="off"]`: `animation: none` and `transition: none` on everything; canvases draw one still frame. Side effect to know: the "Connected" check mark is drawn by an animation, so with Off (and with the system reduced-motion setting) it stays undrawn; the word "Connected" and the accent tint carry the result |

`prefers-reduced-motion: reduce` also kills all animation and transition.

### 2.7 Icons

- Inline SVG, `viewBox="0 0 24 24"`, `fill: none; stroke: currentColor; stroke-linecap: round;
  stroke-linejoin: round`. Default 18 px, stroke 1.75.
- Nav icons 17 px, stroke 1.6, opacity 0.9. Tile icons 16 px, stroke 1.7. Chevron rows 16 px.
- Style is Lucide-like (thin, rounded). The nav set, in nav order, with the exact shapes (each
  sits inside `<svg class="i" viewBox="0 0 24 24">`):

```
Home         <path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5"/>
Connections  <path d="M2.6 17.4A2 2 0 0 0 2 18.8V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.2a2 2 0 0 0 1.4-.6l.8-.8a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r="1"/>
AI answers   <path d="M9.9 15.5A2 2 0 0 0 8.5 14l-6.1-1.6a.5.5 0 0 1 0-1L8.5 10A2 2 0 0 0 9.9 8.5l1.6-6.1a.5.5 0 0 1 1 0L14 8.5a2 2 0 0 0 1.5 1.4l6.1 1.6a.5.5 0 0 1 0 1L15.5 14a2 2 0 0 0-1.4 1.5l-1.6 6.1a.5.5 0 0 1-1 0z"/>
Speech       <rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><path d="M12 18v4"/>
Polish       <path d="m4 20 4-1 12-12-3-3L5 16z"/>
Shortcuts    <rect x="2" y="5" width="20" height="14" rx="2.5"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 13h.01M18 13h.01M10 13h4M8 16h8"/>
Appearance   <path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.5 19 2c1 2 2 4.2 2 8 0 5.5-4.8 10-10 10Z"/><path d="M2 21c0-3 1.9-5.6 5.3-7"/>
Prompt       <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/>
System       <path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>
```

  The remaining icons, all in the same style (`viewBox="0 0 24 24"`):

```
Dictate tile        the Speech mic above (16 px, stroke 1.7)
Ask AI tile         the AI answers sparkle above
Live tile           <path d="M2 12h3l3-8 4 16 3-8h7"/>
Meter row chevron   <path d="m9 18 6-6-6-6"/>                        16 px
External link       <path d="M7 17 17 7M8 7h9v9"/>                   rail footer link, 14 px
Result check        <path d="M5 13l4 4L19 7"/>                       14 px, stroke 2.6, drawn by `check-draw`
Select chevron      <path d="m6 9 6 6 6-6"/>                         16 px, stroke 2, drawn by script
Option check        <path d="M20 6 9 17l-5-5"/>                      16 px, stroke 2.2, drawn by script
```

### 2.8 Logo

Five round-capped lines fanning out like leaf veins, stroke `#8EDCBB`, width 7, in a
`viewBox="18 18 72 72"`:

```svg
<svg viewBox="18 18 72 72" fill="none" stroke="#8EDCBB" stroke-width="7" stroke-linecap="round">
  <line x1="26.00" y1="69.00" x2="26.00" y2="39.00"/>
  <line x1="32.71" y1="65.88" x2="43.29" y2="42.12"/>
  <line x1="41.83" y1="61.36" x2="58.17" y2="46.64"/>
  <line x1="52.49" y1="57.09" x2="71.51" y2="50.91"/>
  <line x1="65.00" y1="54.00" x2="83.00" y2="54.00"/>
</svg>
```

Brand lockup: 26 px mark, 10 px gap, wordmark `odicto` + a `.` in `--logo-stem`, then the
version (mono 11 px, `--faint`) pushed to the far right.
Favicon: the same mark on a `#1b1d20` rounded square (`rx=20`, `viewBox="12 12 84 84"`).

---

## 3. Setup page

### 3.1 Layout

```
┌────────── 248px rail ─────────┬──────────────── main ────────────────┐
│ brand                         │   max-width 800px, centred            │
│ run-card (status)             │   padding 56px 40px 160px             │
│ nav (glowing pill)            │   page title + lede                   │
│  …                            │   sections (label + glass group)      │
│ rail-foot (bottom)            │                                       │
└───────────────────────────────┴──── floating save bar (bottom) ──────┘
```

- Reset: `* { box-sizing: border-box; }`. Every width and padding in this file assumes it.
  `body` has margin 0 and `min-height: 100vh`; `html` takes the `--canvas` background.
- `.app` is a 2-column grid: `248px  minmax(0,1fr)`. `min-height: 100vh`. `isolation: isolate`.
- **Ground light** (`.app::before`): 760 px tall, two radial gradients of the accent at
  6 % (`900×520 at 62% -12%`) and 5 % (`700×500 at 100% 0%`). It scrolls away with the page.
- **Grain** (`.app::after`): fixed, full screen, SVG `feTurbulence` noise
  (`baseFrequency 0.9`, 2 octaves, 160 px tile), `opacity 0.045`, `mix-blend-mode: overlay`.
  The tile is the `--grain` token, an inline SVG: a 160×160 `feTurbulence` (`fractalNoise`,
  `stitchTiles="stitch"`) passed through an `feColorMatrix` that makes every fleck white and keeps
  55 % of its alpha (`0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.55 0`). The same token also
  covers the glass surfaces (`.group`, `.hero`, `.howto-card`) at `opacity 0.05`, also `overlay`.
- **Rail**: sticky, full height, `--rail` fill, right edge `inset -1px 0 0 var(--line)`,
  padding `26px 18px 22px`, gap 22 px, scrollbar hidden.
- **Under 900 px** the rail shrinks to 64 px and keeps icons only. Labels stay for screen
  readers (visually hidden). The footer hides. Main padding becomes `34px 26px 150px`.
- **Under 720 px** the how-to cards stack, provider grid becomes 2 columns, field grids become 1 column, rows and the hero are allowed to wrap.

### 3.2 Page change

Each page is a `.view`. Only `.view.active` shows. It plays:

```css
.view.active { display: block; animation: page-in 0.42s var(--ease) backwards; }
@keyframes page-in { from { opacity: 0; transform: translateY(6px); } }
```

Scroll jumps to the top (`behavior: auto`). The URL hash follows the page (`#appearance`).
Field cards inside a page use the same animation at `--t-med`.

### 3.3 Sidebar navigation

This is the "selected item" look. Read it carefully.

**Structure**

```html
<nav class="nav">
  <span class="nav-pill" aria-hidden="true"></span>      <!-- ONE shared highlight -->
  <button class="nav-item" aria-current="true">icon + label + dot</button>
  …
</nav>
```

**Item**: height 40, padding `0 12px 0 18px`, radius 10, gap 12, text `--muted`, 14.5 px.
Items sit 2 px apart. Icon is 17 px.

| State | Look |
|---|---|
| rest | `--muted` text, transparent |
| hover | text → `--text`, background `--hover` (4 % of text colour). 120 ms |
| current (`aria-current="true"`) | text `--text`, own background transparent. The **pill** supplies the look |
| focus-visible | 2 px `--accent` outline, 2 px offset |

**The pill** (`.nav-pill`) is a single absolutely positioned element. It is *not* drawn on each
item. It **slides** from the old item to the new one.

```css
.nav { position: relative; display: flex; flex-direction: column; gap: 2px; }  /* the pill is placed against this */
.nav-item { position: relative; z-index: 1; }                                 /* items sit above the pill */
.nav-pill {
  position: absolute; left: 0; top: 0; width: 100%; height: 40px;
  border-radius: 10px;
  background: linear-gradient(90deg, var(--accent-soft), transparent 75%);  /* fades to the right */
  transition: transform var(--t-slow) var(--ease), height var(--t-slow) var(--ease), width var(--t-slow) var(--ease);
  pointer-events: none;
}
.nav-pill::before {                       /* the glowing stem on the left */
  content: ''; position: absolute; left: 5px; top: 50%;
  width: 2px; height: 20px; border-radius: 2px;
  background: var(--accent);
  box-shadow: 0 0 8px var(--glow), 0 0 2px var(--accent);
  transform: translateY(-50%);
}
```

So the selected item shows: a **2×20 px accent bar with a glow**, 5 px from the left, and a
**soft accent wash** that is strongest at the left and gone by 75 % of the width. Nothing else.

```js
function moveNavPill(instant) {
  var pill = document.querySelector('.nav-pill');
  var active = document.querySelector('.nav-item[aria-current="true"]');
  if (instant) pill.style.transition = 'none';          // first paint and layout: snap
  pill.style.height = active.offsetHeight + 'px';
  pill.style.width  = active.offsetWidth + 'px';
  pill.style.transform = 'translate(' + active.offsetLeft + 'px,' + active.offsetTop + 'px)';
  if (instant) { void pill.offsetWidth; pill.style.transition = ''; }
}
```

- Every page switch slides the pill over `--t-slow` (320 ms at Full motion, 120 ms Reduced,
  none Off). They all go through `showView()`: a nav click, a
  Home meter row, an in-page link such as "Connections", or a change of the URL hash. First
  paint, resize and font load **snap** it.
  Call `moveNavPill(true)` again when `document.fonts.ready` resolves.
- **Attention dot** (`.nav-dot`, 5 px, transparent by default). A `warn` dot is 7 px with a
  1.5 px amber ring (hollow). Use it on items that need action.
- Under 900 px the pill's stem moves to `left: 2px`, and the dot moves to the top-right corner.

**Rail footer**: link "What's new in Odicto ↗" (12.5 px, `--muted`, turns `--accent` on hover, no
underline), and a note (12 px, `--faint`) with inline `code` in mono 11.5 px.

### 3.4 Run card (live status, top of the rail)

A glass chip, radius 14, padding `11px 12px`, `--glass` fill, top rim. It holds **five pixel bars**
and two lines of text ("Running" / the microphone name).

- Bars: a 5-column grid, each bar 3 × 12 px, 2 px gap, radius 0.5 px.
  Resting heights (scaleY): 0.4, 0.75, 1, 0.6, 0.3.
- `data-tone` sets the look:

| Tone | Bars | Title / sub |
|---|---|---|
| `ready` | `--accent`, opacity 0.85, each bar breathes `scaleY 0.25 ↔ 1` | "Running" / mic name |
| `starting` | `--warn`, all bars 1.2 s | "Starting" / "Almost ready" |
| `stopped` | `--danger`, opacity 0.6, flat at scaleY 0.25, no animation | "Not running" / "Start from Home" |
| `unknown` | `--faint`, resting heights | "Checking Odicto" |

- `ready` bar timings (so the bars never move together):

| Bar | duration | delay |
|---|---|---|
| 1 | 2 s | 0 |
| 2 | 1.6 s | -0.5 s |
| 3 | 1.35 s | -0.9 s |
| 4 | 1.8 s | -0.3 s |
| 5 | 2.2 s | -1.2 s |

```css
@keyframes run-bar { 0%, 100% { transform: scaleY(0.25); } 50% { transform: scaleY(1); } }  /* ease-in-out, infinite */
```

- Title 13.5 / 17, weight 500. Sub 12 / 16, `--muted`, one line with ellipsis.
- The page polls `/status` every 4 s (60 s when the tab is hidden).

### 3.5 Page header and sections

```html
<header class="page-head"><h1>…</h1><p>lede</p></header>
<section class="sect">
  <div class="sect-label"><h2>LABEL</h2><span class="sect-note">hint</span></div>
  <ul class="group"> <li class="row">…</li> … </ul>
</section>
```

- `.sect` has 34 px top margin. The label has 12 px below it.
- **Section label**: caps, accent colour, with a **glowing stem before it** (2×11 px,
  radius 2, `--accent`, `box-shadow: 0 0 6px var(--glow)`). The note sits at the right
  (12.5 px, `--faint`), baseline aligned.
- Footnote under a group: `.sect-foot`, 12.5 / 18, `--muted`, 10 px above, 4 px side padding.

### 3.6 Glass group (the main panel)

```css
.group, .hero, .howto-card {
  position: relative;
  background: var(--glass);                       /* translucent tint */
  border-radius: var(--r-group);                  /* 18px */
  box-shadow: inset 0 1px 0 var(--rim),           /* light top rim */
              0 24px 48px -28px rgb(0 0 0 / 0.7); /* soft drop shadow */
}
.group::after { content:''; position:absolute; inset:0; border-radius:inherit;
  background: var(--grain); opacity:.05; mix-blend-mode:overlay; pointer-events:none; }  /* faint grain */
.group { overflow: hidden; }
```

- **Rows** in a group are separated by a hairline: `li + li::before` is a 1 px `--line`
  starting **16 px from the left** and running to the right edge (it does not touch the left).
- **Row**: flex, centred, gap 16, min-height 60, padding `14px 18px`. Text block on the left
  (`flex:1`), controls on the right (`.row-ctl`, gap 10). `.row.block` stacks vertically
  with a 12 px gap (for inputs and segmented controls under the title).

### 3.7 Buttons

Every base `.btn`: height 40, padding `0 18px`, radius 12, gap 8, 14 px, weight 500. Transition on
`transform, background-color, box-shadow, filter, color` at 120 ms. **Press = `scale(0.98)`.**
Disabled = opacity 0.4, default cursor. Segmented-control buttons, theme swatches, keycaps and
the Small variant are sized separately.

| Kind | Look | Hover |
|---|---|---|
| **Primary** | gradient `180deg, --action-hi → --action`; text `--on-action`, weight 600; `inset 0 1px 0 oklch(1 0 0 / .16)` top highlight; glow `0 8px 22px -10px var(--glow)` | `filter: brightness(1.08)` |
| **Secondary** | transparent; hairline `inset 0 0 0 1px var(--border-ctl)` | background `--hover` |
| **Tertiary** (ghost) | padding `0 14px`, text `--muted` | text → `--text`, background `--hover` |
| **Danger** | text `--danger`; hairline `35 %` danger | background 10 % danger |
| **Small** (`.small`) | height 32, padding `0 12px`, 13 px, radius 10 | same as its kind |
| **Recording** (`.recording`) | text `--warn`, hairline 50 % warn. Label becomes "Press keys..." | |

- **One primary per area at most.** Save is the primary. "Start Odicto" is a small primary.
- A busy **Start** or **Save** button disables itself and shows a 13 px spinner then the word
  ("◌ Starting", "◌ Saving"). **Test connection** disables its button and puts the spinner in
  the result tag beside it (section 3.13).

```css
.spinner { width:13px; height:13px; border-radius:50%;
  border:2px solid color-mix(in srgb, currentColor 30%, transparent); border-top-color: currentColor;
  animation: spin .8s linear infinite; }
```

### 3.8 Text inputs and textareas ("the well")

```css
input[type=text], input[type=password], textarea {
  height: 44px; padding: 0 14px; border: 0; border-radius: 12px;
  background: var(--well); box-shadow: var(--well-in);   /* dark, inset, with a light line below */
  transition: box-shadow var(--t-fast) var(--ease);
}
input[type=text]:focus, input[type=password]:focus, textarea:focus
  { outline: none; box-shadow: var(--well-in), var(--halo); }   /* 2px accent + 5px soft accent ring */
```

- Placeholder `--faint`. Read-only text `--muted`.
- **Textarea**: mono 13 / 20, padding `12px 14px`, vertical resize, prompt editor min-height 240.
- **Key field** (`.key-input`): mono, `letter-spacing .02em`, right padding 76 px. A **Show/Hide**
  text button sits inside at the right: `right 6px`, height 32, radius 9, 12.5 px, weight 500,
  `--muted` → `--text` + `--hover` on hover. The placeholder switches back to the sans font.
- Saved keys show as 16 `•` characters. They are never sent back; a new value replaces them.
- Inline `code`: mono 12, padding `1px 6px`, radius 6, `--well` fill, top rim.

### 3.9 Switch

A 52 × 40 hit area holding a **40 × 24 track**.

```css
.switch { position:relative; display:inline-grid; place-items:center; width:52px; height:40px; }
.switch input { position:absolute; inset:0; opacity:0; margin:0; }     /* covers the whole hit area */
.switch-track { position:relative; width:40px; height:24px; border-radius:999px;
  background:var(--well); box-shadow:var(--well-in); pointer-events:none; }
.switch-track::before {                      /* the "on" layer fades in */
  content:''; position:absolute;
  inset:0; border-radius:inherit;
  background: linear-gradient(180deg, var(--action-hi), var(--action));
  box-shadow: inset 0 0 0 1px var(--sel-line), 0 0 12px -2px var(--glow);
  opacity:0; transition: opacity var(--t-med) var(--ease);
}
.switch-thumb { position:absolute; top:3px; left:3px; width:18px; height:18px; border-radius:50%;
  background: var(--muted); transition: transform var(--t-med) var(--ease), background-color var(--t-med) var(--ease); }
:checked + .switch-track::before { opacity:1; }
:checked + .switch-track .switch-thumb { transform: translateX(16px); background: var(--on-action); }
:focus-visible + .switch-track { box-shadow: var(--well-in), var(--halo); }
:disabled + .switch-track { opacity: .4; }
.switch.busy .switch-thumb { animation: thumb-wait .9s var(--ease) infinite alternate; }  /* thumb pulses to .35 opacity */
```

The real checkbox is invisible and covers the whole 52×40 area.

**Busy state** (the Administrator switch needs a Windows approval): on click the switch is
disabled (dims to 0.4), the thumb **pulses** (`.busy`), and a note row opens under it that says
exactly what to do: "Approve the Windows prompt to turn this on. Odicto restarts afterwards."
When the answer arrives the switch snaps to the **true** state (it can flip back), the note shows
the result, and a toast confirms. The user is never left guessing whether it worked.

### 3.10 Segmented control (the "gearbox")

Used for Long ride / Short ride, every Appearance choice, and the Motion level.

- Container: grid with `--n` equal columns, padding 3, radius 12, `--well` fill, `--well-in`.
- **Knob** (`.seg-knob`): one element under the labels. `top/bottom/left: 3px`,
  `width: calc((100% - 6px) / var(--n))`, radius 9, `--glass-hi`, top rim.
  It moves with `transform: translateX(calc(var(--i) * 100%))` over **320 ms**.
  Set `--i` (0-based index) from JS. Never animate `left`.
- **The knob carries a stem**: `::before`, `left 8px`, 2×14 px, `--accent`, glow `0 0 7px`.
- **Buttons**: sit above the knob (`z-index 1`), min-height 34, padding `6px 12px 6px 20px`
  (extra left room for the stem), 13.5 px, weight 500, `--muted`.
  `aria-pressed="true"` → `--text`. Colour changes in 120 ms.
- **Gearbox variant**: buttons 58 px tall with two lines: name (14 px, 600) and hint
  (12 px, `--muted`, 2 px above). Example: **Long ride** / "tap, speak, tap again".
- `focus-visible` outline is drawn inside (`outline-offset: -2px`).

### 3.11 Custom select

A real `<select>` stays in the DOM (invisible) for forms. The app draws the look.

- **Trigger**: height 44, padding `0 40px 0 14px`, radius 12, `--well`, `--well-in`.
  Hover adds `0 0 0 1px var(--border-ctl)`. Open or focus adds the `--halo`.
  A 16 px chevron sits at `right 14px` and **rotates 180°** when open (180 ms).
  Placeholder text uses `--muted`.
- **Menu**: `position: fixed` on `<body>` (nothing can clip it), `z-index 200`, padding 6,
  max-height 320, radius 12, `--float`, `inset 0 1px 0 var(--rim), inset 0 0 0 1px var(--line), var(--shadow)`.
  Width = trigger width. It drops **below** the trigger, 6 px gap. If there is less than
  about 220 px below and more above, it opens **upward**.
- **Entry**: `menu-in`, 120 ms, from `opacity 0; translateY(-4px)` (up-menu: `+4px`).
- **Search box** (when more than 7 options or a model list): height 38, radius 9, 6 px below.
- **Option**: min-height 40, padding `9px 12px`, radius 9, 13.5 / 19.
  Hover or keyboard-active → `--hover`. **Selected** → `--accent-soft` background, `--accent`
  text, and a 16 px check on the right. Small detail line (model id) at 12 px `--muted`.
- Empty result: "No matches for "…"." in 12.5 px `--muted`.

### 3.12 Choice cards and rows

**Provider card** (3-column grid in a group, 12 px padding, 10 px gap):
min-height 92, padding 14, radius 14, `--well`, `--well-in`. Top line: name (15/500) and
an 18 px **radio** ring. Hint 12.5 `--muted`. Footer meta 12 px with a status dot.

| State | Look |
|---|---|
| hover | `+ 0 0 0 1px var(--border-ctl)` |
| active (pressed) | `scale(0.99)` |
| checked | `--accent-soft` fill, `inset 0 0 0 1px var(--sel-line)`, glow `0 0 18px -8px var(--glow)` |

**Radio ring**: `inset 0 0 0 1.5px var(--faint)`; checked → `inset 0 0 0 5px var(--accent)`
(the ring thickens into a filled dot, 120 ms).

**Choice row** (speech engine list): full-width row, min-height 64, padding `12px 18px`,
18 px radio mark on the left, title (15/500) + description (12.5 `--muted`).
Hover `--hover`. Checked `--accent-soft` background. At the far right a cloud engine whose key
field is empty shows an amber **"Needs a key"** chip, so the problem is visible at the moment
of choosing. The check reads the field as it is when the chip is refreshed: a saved key (shown
as a mask) and a key typed but not yet saved both clear it. The chip is refreshed when the
engine is chosen or the Speech state is rebuilt; typing in the key field does not refresh it
by itself. Like the provider cards, only the chosen row is
in the tab order. Choosing an engine reveals only that engine's fields and hides the
"Fallback for the cloud engine" note when Whisper itself is chosen.

**Setup meter row** (Home, "Your setup"): a button row, min-height 60: label (150 px wide, 13.5
`--muted`), value (15/500) over a sub line (12.5 `--faint`, ellipsis), a status dot, and a
16 px chevron. Hover → `--hover`, and the chevron turns `--accent` and **moves 2 px right**.
When the value text changes it replays `swap-in` (180 ms, from `opacity 0; translateY(3px)`).

### 3.13 Chips, dots and result tags

**Chip**: height 22, padding `0 10px`, pill, 11.5 / 500, gap 6.

| Kind | Fill | Text |
|---|---|---|
| default | `--accent-soft` | `--accent` |
| `.muted` | `--glass-hi` | `--muted` |
| `.warn` | 14 % warn | `--warn` |

**Status marks use shape, not only colour:**

| Mark | Shape |
|---|---|
| OK | filled `--accent` dot, 6 px, glow `0 0 8px var(--glow)` |
| Neutral | dot, 70 % `--faint` |
| Warning | **hollow ring**, 8 px, `inset 0 0 0 1.5px var(--warn)` |
| Error | **diamond**, 7 px square rotated 45°, radius 1.5, `--danger` |
| Info | dot, `--moon` |

**Hero chip** (Home): height 26, a dot is drawn before the text (accent glow for ready,
faint for muted, hollow ring for warn).

**Result tag** (after "Test connection"): height 26, pill, 12.5 / 500.
`loading` = `--glass-hi` + spinner. `ok` = accent-soft + a **check that draws itself**:

```css
.check-pop path { stroke-dasharray: 24; stroke-dashoffset: 24; animation: check-draw 360ms var(--ease) forwards; }
@keyframes check-draw { to { stroke-dashoffset: 0; } }
```

`err` = 13 % danger tint. When the server reports the failure, the tag also **shakes** (a
timeout, an unreachable server or an unexpected non-JSON answer turns it red without the
shake; see 3.20):

```css
@keyframes shake { 20%, 60% { transform: translateX(-3px); } 40%, 80% { transform: translateX(3px); } }  /* 360ms */
```

(Restart the shake by removing the class, reading `offsetWidth`, then adding it.)

### 3.14 Keycaps

Show a chord as little keys side by side: [Ctrl] [`] (the `+` here is only notation).

- `kbd`: min-width 26, height 26, padding `0 8px`, radius 7, `--well` fill, mono 11.5 / 500,
  `box-shadow: inset 0 1px 0 var(--rim), 0 1px 0 rgb(0 0 0 / .4)` (light top, dark bottom edge = a key).
- Symbol-only keys (e.g. `` ` ``): 15 px, weight 600.
- Gap 5 px. No separator is drawn between caps: the stylesheet has a `.plus` style (12 px
  `--faint`) but the keycap renderer does not use it. Plain-text summaries, such as the Home
  meters, write the chord in words ("Ctrl + `"). Name map: `ctrl→Ctrl`, `grave→\``, arrows → `↑↓←→`, `f6→F6`.
- Each key plays `key-in` (180 ms, from `opacity 0; translateY(3px)`).
- An empty chord shows a muted "Not set" chip.
- **Change** flow: secondary small button → label "Press keys..." + `.recording` (amber).

### 3.15 Home: hero and how-to cards

**Hero** (glass, radius 18, padding `26px 28px`, gap 26, `margin-top: 34`): left = a live
**canvas of the HUD leaf** (see section 4), middle = title (19/500) and sub (14, `--muted`),
right = status chip or the "Start Odicto" button (`margin-left:auto`). Canvas: 208×62 logical
at zoom 1.15 (the element is 239×71). The hero ignores the Size setting.
The hero leaf is **identity, not telemetry**: it is always fully open and Listening (it never
plays the entry, and it does not follow the real HUD state). The real status is told by the
words, the chip and the run card. It follows the theme colour, the Waveform/Texture/Edge/Shape
choices, and pauses when the tab is hidden or another page is shown.

**How-to cards** (3 columns, gap 12): glass, padding `18px 18px 20px`, gap 12. Each has an
accent icon tile (16 px) + title (15/500), the **keycaps**, then a 13.5/20 `--muted`
description. Dictate / Ask AI / Live.

### 3.16 Appearance page

The page where the look itself is chosen. In order:

1. **Preview** (section "Preview", note "Updates as you choose").
   `.leaf-stage`: radius 18, padding `16px 12px 4px`, content bottom-aligned and centred,
   background `radial-gradient(420px 160px at 50% 70%, var(--accent-soft), transparent 70%)`
   over `--well`, top rim, deep shadow. Inside, a canvas 390×112 at **zoom 1.5** (585×168).
   It plays a loop: **enter 0.62 s → Listening 3 s → Transcribing 1.2 s → Done 1.2 s → exit 0.5 s → 0.5 s gap**.
   The voice is simulated. The canvas runs only while this page is visible and the tab is
   visible (`requestAnimationFrame`, `dt` capped at 50 ms).
   With motion Off (or system reduced-motion) it paints one still frame: Listening, open, fixed voice, at t = 1.1 s.
2. **Colour** row "Theme colour": **swatches**.
3. **Pill** rows (each a `row block` with title, one-line description and a segmented control):

| Setting | Options (value: label, hint) |
|---|---|
| Waveform `HUD_WAVEFORM` | `reach`: Reach, "rows reach left with your voice" · `stream`: Stream, "your voice flows to the left over time" |
| Texture `HUD_TEXTURE` | `veins`: Veins, "fine leaf veins" · `plain`: Plain, "no veins, faint grain" |
| Edge `HUD_EDGE` | `rim`: Light rim, "a thin light edge" · `soft`: Soft, "almost no edge" |
| Shape `HUD_SHAPE` | `leaf`: Leaf, "soft tip" · `capsule`: Capsule, "round ends" |
| Size `HUD_SIZE` (3 segments) | `1x` normal · `1.3x` a little larger · `1.5x` larger |

Choosing a value updates the hidden field, moves the knob and **redraws the preview at once**.
Saving restarts Odicto, and the real HUD reads the values.

**Swatches**: each is a tiny **leaf** (SVG 38×16) in a 42×28 hit area, radius 8, 12 px gap.

```html
<svg viewBox="0 0 38 16">
  <path class="sw-body" d="M0 8 C7 3 14 .8 22 .8 L30 .8 A7.2 7.2 0 0 1 30 15.2 L22 15.2 C14 15.2 7 13 0 8Z"/>
  <rect class="sw-stem" x="21" y="2.6" width="1.4" height="10.8" rx=".7"/>
</svg>
```

```css
.swatch .sw-body { fill: oklch(0.34 calc(0.055 * var(--sk)) var(--sh)); }   /* per-swatch --sh hue, --sk k */
.swatch .sw-stem { fill: oklch(0.86 calc(0.105 * var(--sk)) var(--sh)); }
.swatch:hover { transform: translateY(-1px); }                               /* 120ms */
.swatch { all:unset; box-sizing:border-box; position:relative; display:grid; place-items:center;
  width:42px; height:28px; border-radius:8px; cursor:pointer; transition:transform var(--t-fast) var(--ease); }
.swatch:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }   /* `all:unset` removed the default */
.swatch.selected::after { content:''; position:absolute; left:50%; bottom:-3px; width:14px; height:2px; margin-left:-7px;
  background: var(--accent); box-shadow: 0 0 7px var(--glow); border-radius:2px; }   /* glowing stem UNDER the chosen one */
```

- Order: Forest, Ocean, Plum, Ember, Graphite, **Custom**, then the theme name (12.5, `--muted`, right aligned, min-width 72).
- **Custom** swatch: an empty one is a **dashed** leaf outline (`stroke-dasharray 2 2`, `--faint`)
  with a `+` inside. Once a colour is chosen it fills with that colour. A hidden `<input type="color">` covers it.
- Picking a colour changes the page theme (`--h`, `--k`) and the preview **at once**.

### 3.17 Floating layers

All "floating" surfaces share one recipe:
`background: var(--float); box-shadow: inset 0 1px 0 var(--rim), inset 0 0 0 1px var(--line), var(--shadow);`

- **Save bar**: fixed, 20 px from the bottom, centred over the main column
  (`left: calc(var(--rail-w) + (100vw - var(--rail-w)) / 2)`, `translateX(-50%)`),
  width `min(752px, 100vw - rail - 48px)`. Inner: radius 16, padding `10px 10px 10px 18px`,
  `--float` at 90 %, `backdrop-filter: blur(16px)`. Content: hollow amber dot (8 px) +
  "Unsaved changes" (13.5/500) + "· Save restarts Odicto" (`--muted`, 400), then Discard
  (tertiary) and **Save** (primary). It **rises in**: `from opacity 0; translate(-50%, 16px)`, 320 ms.
  It appears on the first edit (`input` or `change`) and never moves the page.
- **Toasts**: top-right, 20 px from the edges, width `min(380px, 100vw - 40px)`, 8 px gap.
  Radius 12, padding `12px 14px`, 13/18. A leading dot: info = `--moon`, ok = accent glow,
  error = red diamond. Enter 180 ms from `opacity 0; translateY(-6px)`. After **7 s** the toast
  fades and lifts `-4px` over `--t-med` (180 ms); the element is removed 200 ms after the fade
  starts.
- **Offline banner**: bottom centre, max-width 640, radius 12, amber 35 % hairline, hollow amber
  dot. Shown after two failed status polls. Message: the setup page closed; run `setup.bat`.
- **Dialog** (expanded prompt editor): full-screen scrim `#000` at 55 % with `blur(6px)`
  (fade 180 ms), panel `min(1100px, 100%) × min(860px, 100%)`, padding 18, radius 20, `--float`.
- **Stacking order.** `.app` is its own stacking context (`isolation: isolate`). Inside it:
  ground light and grain 0 · the main column (`.main`, z-index 1, itself a stacking context) ·
  the rail 20. The save bar (70) and the dialog scrim (90) live **inside** `.main`, so their
  numbers only order them against each other; the rail, a sibling at 20, paints above both
  (the save bar is centred on the main column, so it never meets the rail). Outside `.app`, on
  `<body>`: toasts 95 · offline banner 99 · select menu 200. A toast therefore shows above an
  open dialog, and the menu above everything. Nav items (in the rail), segment buttons and the
  hero leaf use z-index 1 to sit above their own backgrounds.

### 3.18 Misc

- Selection colour: accent at 35 %.
- Scrollbar: 12 px, thumb `--raised-2` with a 3 px `--canvas` border, transparent track.
- Links: `--accent`, no underline; underline (offset 3 px) on hover.
- Notice box: `--well`, `--well-in`, radius 12, padding `12px 14px`, 13/18, `strong` in `--text`.
- Pull log box: `--well`, mono 11.5/16, max-height 160.
- Model chips: height 28, pill, `--well`, mono 12, top rim; active = accent-soft + `--sel-line`; a 20 px round "×" inside.
- `[hidden]` always wins (`display: none !important`).

### 3.19 Accessibility on the page

- Real buttons, inputs and `aria-pressed` / `aria-current` / `aria-checked` states.
- `:focus-visible` is a 2 px accent outline with 2 px offset (inside controls: `-2px`).
  Wells use the `--halo` ring instead.
- Text roles: `--text` for content, `--muted` for support text, `--faint` for hints only. (Contrast ratios were not measured for this file.)
- Status never relies on colour alone (dot / ring / diamond shapes, plus text).
- Hit targets: 40 px buttons, 40 px nav rows, 52×40 switch area, 44 px inputs, 32 px small buttons. Keycaps (26 px) are display only, not clickable.
- `aria-live="polite"` on the run card, theme name, toasts.

### 3.20 Behaviour: the small interactions

These details are where the page feels finished. Each has a *reason*; keep the reason if you rebuild it.

**Custom select (keyboard and focus)**
- Opening one **closes every other** open menu. Clicking anywhere else, `Esc`, or choosing closes it.
- `↓` / `↑` on the trigger opens the menu and moves a soft "active" highlight (same `--hover` veil
  as mouse hover, so keyboard and mouse look the same). `Enter` picks the active option, or the
  first match if none is active.
- A menu with a search box (more than 7 options, or any model list) **focuses the search box
  at once**. Typing filters live by label *and* value. Search text never marks the form unsaved.
- The active option scrolls into view with `block: nearest` (no jump).
- **Close is instant, open is animated.** Closing with a fade feels slow when you are choosing
  quickly; only the arrival needs the 120 ms drift.
- The menu follows its trigger on scroll and resize, so it never floats away from its field.
- Clicking the field's `<label>` focuses the trigger, as a real select would.
- Why custom at all: the native list cannot carry the rim, the glass and the model-id sub-line.
  The real `<select>` stays in the DOM so forms, values and change events keep working.
- Rough edge to know about: the plain-text model fields with a suggestion list (Groq,
  OpenRouter speech) use the browser's native `datalist`, which is not themed.

**Hotkey capture ("Change")**
1. Press **Change**. The button turns amber and says "Press keys..." (three plain dots). The
   chord is cleared, so the keycap area shows the muted "Not set" chip until the first key
   goes down.
2. As the user holds keys, the **keycaps appear live**, one by one, each with the small
   `key-in` rise. So the user sees their chord form.
3. Capture ends when every tracked key is released after a main key was pressed (modifiers alone do
   not end it), and the new chord stays. Only the first main key is tracked. A **6 s**
   deadline from the start of capture also ends it, even while keys are held: it keeps the
   chord if a main key was pressed and restores the old chord if not. Starting another capture
   cancels the first. Rough edge: the deadline belongs to the button, not to one capture, so
   finishing a capture and pressing the same **Change** again within 6 s lets the first
   deadline end the second capture early.
4. `Esc` is a valid key to bind, so it is captured like any other.
5. Polish also has **Disable** (tertiary): it clears the chord and shows "Not set".
- Why: a settings page that asks users to *type* "ctrl+shift+grave" is hostile. Showing the
  keys as they press them is both feedback and confirmation.

**API key fields**
- Saved keys show 16 dots and are never sent back. Focusing a saved key **selects the whole
  mask**, so typing replaces it in one move (no deleting dots).
- The chip next to the key name tells the truth at every moment:
  `Not set` (muted) → `Saved` (accent) → `Not saved yet` / `Replaces saved key` (amber, while
  there is a typed value that is not yet written). Amber here is a gentle "you still need to hit Save".
- **Show / Hide** flips the field between password and text and the button word swaps.
- "Get a key" is an accent link at the far right of the same line.

**Unsaved changes**
- Any real edit shows the **save bar**. Edits that change no setting (menu search boxes, the Admin
  switch which applies at once) are marked `data-nodirty` and stay silent. A finished
  hotkey capture that changed the chord does raise the bar.
- **Save** turns into a spinner + "Saving" and disables, so a double click cannot double-submit.
  The response opens on the **same page** (`/save#current`).
- **Discard** reloads. Leaving with unsaved edits triggers the browser's own "leave site?" guard.

**Provider cards (a radio group, built from buttons)**
- Only the chosen card is in the tab order (roving `tabindex`). Arrow keys move *and* choose,
  wrapping at the ends, like native radios.
- Each card's footer dot reflects its key: green dot "key saved" or hollow amber ring "no key".
  It reads the key field as it is now, so a key typed but not yet saved already counts.
  Ollama always shows "no key needed"; None says "no AI replies".
- Choosing a provider shows only **its** model fields (`field-card`, 180 ms `page-in`) and
  hides the rest. **None** hides the whole Model and Options area and shows a quiet notice, and
  disables "Test connection". The page never shows fields that cannot matter.
- If the chosen provider lacks a key, the Model heading shows an inline accent link, "Add a
  Gemini key", which jumps to Connections and **focuses that field** after 60 ms.
- Model history appears as chips (mono) for the providers that keep one (Groq does not). Click a
  chip to use it. The "×" removes it. A successful connection test quietly *remembers* the model
  as a new chip, but only when the tested model is not empty and not already in the history.
  A new chip raises the save bar and is stored when the user hits Save; the list does not
  re-sort: a model that is not yet an option is inserted just before "Custom...", and one the
  list already has stays in its place.

**Test connection**
- Button disables; a `Testing` tag with a spinner appears; a neutral line says what is happening.
- Success: tag turns accent with the check **drawing itself**, and a toast confirms.
- Failure reported by the server: the tag turns soft red and **shakes once**, and the reason
  prints in plain words below. A 30 s timeout and a "server not reachable" case have their own
  wording and turn the tag red **without** the shake.

**Home "Your setup" meters** summarise every page in one line each. They use the same
tone vocabulary as the status marks (`ok` accent dot, `warn` hollow ring, `info` blue,
neutral). A `warn` meter also puts the **hollow ring on that page's nav item**, so a problem
is visible from anywhere. Values that change play `swap-in`.
Examples: "2 of 4 keys / Groq, Gemini", "Gemini / needs a key", "Ctrl + ` / Long ride".

**Model pickers**
- The first option of the four generated AI answers lists (Meta, OpenRouter, Gemini, Ollama) is
  **"Backend default (the actual model name)"**, so an empty choice still tells the truth about
  what will run (the Polish list differs; see below). The last option is "Custom...", which
  reveals a plain text field below and focuses it. A saved id the list does not know opens as
  Custom. Groq's chat model is not one of these lists: it is a plain text field with a native
  suggestion list (no default option, no Custom).
- Lists put the newest first (OpenRouter by release date; the other lists show the most recently
  added model first). OpenRouter shows a human name when the catalog has one (otherwise the id
  itself); the id is a tooltip in the AI answers list, and a small second line in the Polish
  list only when the name differs from the id. The search matches both name and id.
- The catalog loads in the background (`/openrouter-models`); the lists quietly rebuild when it
  arrives. Until then, the page works with what it has.
- **OpenRouter's reasoning list filters itself to what the chosen model allows** (the Meta and
  Gemini lists are fixed). A note under it says why in one sentence ("This model requires thinking. Allowed: low, medium…").
  A mandatory-thinking model changes the first option to its lightest allowed effort (for
  example "Fastest allowed (low)"). While the catalog has no entry for the chosen model, the
  list shows every option unfiltered.
- Polish has its own list whose first option is "Use AI answers model", with a live summary line:
  "Follows AI answers: …" or "Polish uses …". A small chip names the provider it borrows.
- Gemini's Smart / Verbatim: the switch flips a small chip in the row title (`Smart` accent,
  `Verbatim` muted).

**Theme picker**: choosing a swatch or dragging the colour picker writes `--h` and `--k` onto the
page at once, so the whole page recolours as the user chooses, repaints the preview, updates the
name ("Ocean" or `#3A7BD5`), moves the glowing marker, and raises the save bar. Nothing is
applied to the real HUD until Save restarts Odicto (the HUD reads its style once, at start).

**The page's own lifecycle**: the page says goodbye (`/bye`) when its tab closes, and starts a
fresh status check the moment the tab becomes visible. The previous text stays on screen until
an answer arrives (a request already in flight goes first, and a failed request brings no new
answer). Canvases
wait for the font, up to 2.5 s, before their first frame, so the label width is normally
measured with the right letters; if the font is slower than that, they start with the fallback.

**Ollama download**: a log box (mono, scrolls) streams progress. The button reads
`Download local model` → `Downloading…` → `Download complete` or `Download failed - retry`.

**Prompt editor**: the textarea **fills the window height** (min 260 px) so there is no inner
scroll fight; it resizes with the window. "Expand editor" moves the *same* textarea into a
centred dialog (scrim + 6 px blur), the button says `Close editor (Esc)`, page scroll locks,
and the cursor goes to the start of the text. `Esc`, the Close button, or a click on the scrim
closes it and puts the textarea back.

**Status polling and "offline"**: the page polls every 4 s (60 s when hidden). Two failures in
a row show the offline banner. It is deliberately calm: the setup page is meant to be closed
when you are done, and Odicto keeps running.

**Reveal-on-demand sections**: speech-engine fields (Gemini, Groq, OpenRouter) appear only for
the engine that uses them; the Administrator section appears only on Windows.

### 3.21 Spacing and rhythm

There is no formal scale, but the numbers repeat. Use these relationships:

| Gap | Where | Meaning |
|---|---|---|
| 2 | between nav items; title to description inside a row | siblings that belong together |
| 6 | label to its control; between model chips | tight pair |
| 8–10 | between buttons in a row; a footnote below its group | near |
| 12 | section label → panel; panel inner rows block gap; cards in a grid | related group |
| 14–18 | row padding (vertical 14, horizontal 18); two-column grid gap 14 | comfortable interior |
| 22–26 | rail sections; hero padding | zones |
| 34 | between sections (and hero → first section) | new topic |
| 56 top / 160 bottom | main column | top: breathing room under the window edge; bottom: room so the floating save bar never covers the last row |

Rhythm rules: **inside** a group, rows are 60 px or more; **between** groups, 34 px; the
section label always sits 12 px above its panel and is *visually closer to the panel than to the
previous one*. This "near/far" spacing alone tells the reader what belongs together.

Row anatomy: title (15/500) over a one-line description (12.5, muted). Controls on the right,
vertically centred. If a row has a wide input or a segmented control it goes **under** the title
(`.row.block`) at full width, never squeezed beside it. A short field (for example "Longest
answer", a 140 px number box) may sit beside the title like any other control.

---

## 4. HUD: the leaf

A small pill at the **bottom centre** of the screen. It opens like a bud, shows what Odicto
is doing, and closes. It is one frameless, always-on-top, **click-through** Qt window that never takes focus (so it can
never steal a keystroke or a click from the app you are dictating into), and that paints
a single `LeafFrame` per tick. `app/hud_leaf.py` paints. `app/indicator.py` owns the clock.

### 4.0 Why the HUD feels alive (the understanding behind the numbers)

The HUD is seen for one or two seconds at a time, while the user's attention is on something
else. It must therefore be **glanceable, honest, and gentle**. These ideas explain nearly every
number in this section.

- **It is a plant, so it grows and it breathes.** It opens like a bud and closes in reverse.
  While waiting, a light moves *along* it. While listening, its glow follows the voice. Nothing
  loops mechanically in lock-step: the five status bars on the page and the five voice rows
  each have their own rhythm, because the eye reads unison as a machine and offsets as life.
- **State is told three ways at once**: the **stem** (shape), the **label** (words), the
  **accent colour** (jade, moon blue for AI, amber, soft red). Any one is enough, so the
  HUD works on a small, bright, or colour-blind screen.
- **Sound becomes light, in the leaf's own grammar.** Five rows are the five frequency bands.
  They are drawn as the leaf's *veins*: level near the stem, bending together toward the tip,
  fading with distance. The voice is not a foreign waveform pasted on; it lives inside the leaf.
- **Waiting is quiet, results are sharp.** While Listening/Working the label ink is dimmer
  (`0.78`); when there is a result it brightens (`0.92`). The eye is pulled to the answer.
- **Mood lives in the tip.** The tip droops a little (6 % of the height). On error it droops
  more (11 %) and the body takes a faint red wash. A notice takes a faint amber wash. This is
  a small, human cue: a leaf that sags.
- **Done is a small celebration, not a fanfare.** The stem folds into a check that draws
  itself, and one seed of light drifts up and fades. It lasts about a second.
- **Never in the way.** Click-through, no focus, bottom centre, small, transparent, and it
  leaves fully after use. It does not bounce, flash or pulse at the screen edge.
- **Crisp over smooth where it matters.** The pixel voice is drawn *without* antialiasing and
  snapped to device pixels. It is meant to look like tiny lit cells. Everything soft (body,
  shadow, glow) is smooth; everything that is "data" is crisp. That contrast is the look.
- **Length follows the words.** The pill's width follows its label (voice room + label +
  insets), and when the label changes the width *springs* to the new size. In normal use it
  does not jump, and a spring that starts from rest does not overshoot (the exact snap rule
  is in 4.9).

### 4.1 Anatomy

```
          ┌──────────── caption card (live only) ────────────┐
          └──────────────────────────────────────────────────┘
   ╭─────────────────────────────────────────────────────╮
  <  ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ pixel voice   │ stem   Listening ▪▪▪  )
   ╰─────────────────────────────────────────────────────╯
   tip (left)   voice room 84 px (cells 68 px)   stem   12   label    inset (right, round)
```

Left to right: a **soft pointed tip** (drooping slightly), the **pixel voice** (5 rows by 23
columns), a gap to the **stem** (2 px bar with halo; its centre is 8 px from the nearest cell
edge, so about 7 px of air sit beside the bar), 12 px from the stem to the **label**, 18 px
inset, then a fully round right end.

The voice room is **84 px measured from the stem's centre to the leaf's left edge**. Inside it
the first cell starts 8 px from the stem's centre, the 23 columns (pitch 3 px, cell 2 px) span
68 px, and the last ~8 px are the tip end.

| Constant | Value |
|---|---|
| Leaf height | 36 |
| Stem height / width | 32 / 2.0 |
| Voice room (stem centre to left edge) | 84 |
| Stem gap (stem centre to first voice cell) | 8 |
| Tip length | 58 (capped at 40 % of width) |
| Text gap (stem to label) | 12 |
| Right inset | 18 |
| Label font | Instrument Sans, 13 px, Medium |
| AI mark width + gap | 14 + 10 |
| Listening dots width | 17 |
| Entry rise | 12 px |
| Tip droop | 0.06 (error 0.11) × height |
| Tip soft-round radius | 4 |

Width = `84 + 12 + labelWidth + 18` (voice room from the stem's centre, stem to label, the
label, right inset). The pill grows and shrinks with its label.

### 4.2 Palette (Forest; other themes recolour via OKLCH)

| Name | RGB | Use |
|---|---|---|
| `jade` (accent) | 142, 220, 187 | voice pixels, stem, glow, seed |
| `surface_top` | 27, 64, 46 | body gradient, top |
| `surface_bottom` | 9, 31, 22 | body gradient, bottom |
| `cool` | 3, 18, 24 | darker, cooler tip |
| `vein` | 205, 245, 222 | vein lines |
| `sheen` | 225, 255, 238 | light along the top |
| `rim` | 215, 250, 230 | 1 px edge light |
| `card_top` / `card_bottom` | 24, 58, 42 / 10, 32, 23 | caption card |
| `glint` | 240, 255, 248 | label shimmer, seed core |
| `stem_hi` | 244, 255, 250 | stem centre line |
| `bead` | 238, 255, 248 | Transcribing bead |

Fixed in every theme: `warn` 242,194,122 · `danger` 242,163,155 · `ink` (label) 231,229,223 ·
`moon` (AI) 178,214,244.

### 4.3 Materials, bottom to top

The body is built as three cached layers, all clipped to the leaf outline:

1. **Shadow** (under everything): a Gaussian of **σ 8, alpha 0.5, dropped 5 px**, plus a tight
   contact shadow of **σ 1.5, alpha 0.32, dropped 1 px**. The big one is a *real* blur (three
   box blurs on a grid a quarter of the size, then smoothly scaled up), not stacked outlines,
   because stacked outlines make a narrow tip as dark as a broad edge. A real blur lets the tip
   fade out naturally. While the leaf opens or resizes the shadow is rebuilt every frame; once
the leaf settles it is cached and only rebuilt when the shape changes.
2. **Under layer**: vertical gradient `surface_top @ 227/255 → surface_bottom @ 240/255`.
   Then a horizontal **cool** gradient over the tip: `cool @ 107/255` at the left edge fading to 0 at the stem.
   Notice/error tint: a flat wash `rgb(130 95 20 / 15/255)` (notice) or `rgb(120 45 45 / 15/255)` (error).
3. **Pool**: a radial light (radius 52) centred 16 px left of the stem, in the **state accent**.
   Opacity breathes with the voice: `0.08 + 0.07 × level`.
4. **Over layer**:
   - **Veins** (texture "veins" only): five parallel veins that bend together toward the tip
     (the middle one brightest: alpha 0.2, width 0.9; others alpha 0.12, width 0.6; each fades
     with distance, 70 %), two edge veins (alpha 0.07, width 0.5, fade 60 %), and **30 short
     cross veins** (seeded LCG `9`, so they never move).
   - **Grain**: a 128 px noise tile (seed `3`, fleck alpha up to 16/255; light and dark flecks).
     Strength 1.0 for Veins, **0.4 for Plain**.
   - **Sheen and shade**: vertical gradient: `sheen @ 22/255` at the top → 0 at 38 % → clear at 70 % → black `51/255` at the bottom.
5. **Rim** (top layer): a 1 px outline with a vertical gradient: alpha `0.18` at the top,
   `0.054` at 55 %, `0.0216` at the bottom. In **Soft** edge, the base alpha is `0.07` instead of `0.18`.

**Shape "capsule"** is a plain rounded rectangle (no tip, radius = half height).
**Shape "leaf"** has a bezier tip that opens from a bud. The outline, for a box `x, y, w, h`
(the leaf's left, top, width and height at this moment):

```
r  = min(h/2, w/2)                     the round right end
tf = tip openness, 0 (bud) to 1 (full leaf)
ee = r + max(0, min(58, 0.4·w) − r)·tf  how far the pointed end reaches in from the left edge
ty = y + h·(0.5 + droop·tf)            the point sits a little below centre (droop 0.06; error 0.11)
rr = 4·tf                              the tiny rounded point;   k = 0.5523 (circle-arc constant)
lerp(a, b) = a + (b − a)·tf
b1 = lerp((x+ee−ee·k, y+h), (x+ee·0.62, y+h))
b0 = lerp((x, ty+(y+h−ty)·k), (x+ee·0.3, ty+(y+h−ty)·0.5))
t0 = lerp((x, ty−(ty−y)·k), (x+ee·0.3, ty−(ty−y)·0.5))
t1 = lerp((x+ee−ee·k, y), (x+ee·0.62, y))
path: move (x+ee, y) → line (x+w−r, y) → arc (top right) → line (x+w, y+h−r) → arc (bottom right)
      → line (x+ee, y+h) → cubic b1, b0 to (x+rr, ty+0.45·rr)
      → quad via (x−rr, ty) to (x+rr, ty−0.45·rr) → cubic t0, t1 to (x+ee, y) → close
```

### 4.4 Pixel voice

Five rows by 23 columns of **2 px squares**, pitch 3 px across, 4 px from one row's centre to
the next (a 2 px gap at 1x). They sit left of the stem, the first cell's right edge 8 px from
the stem's centre.

- Rows converge toward the tip like parallel veins. For a cell `d` px from the stem's centre,
  `s = smoothstep((d − 24) / 58)`: the rows stay parallel for the first 24 px, then close up.
  Row pitch is `4 × (1 − 0.62·s)` px, but never less than the cell size plus one device pixel, so
  neighbours never touch. The centre line sinks by `droop × height × tipOpen × s`. Every cell
  edge snaps to a **whole device pixel** (crisp at any DPI). In device pixels, with `dpr` the
  device pixel ratio **multiplied by the HUD size** (1, 1.3 or 1.5; `pixel_ratio()` in the
  painter), and `round(v)` meaning `floor(v + 0.5)` (halves go up, not to even):

```
cell    = max(1, floor(2·dpr))                         every cell has this size
x_dev   = round((stem − 8 − 3·(j+1) + 1)·dpr)          column j, counted from the stem
d       = stem − x_dev/dpr − 1                         then s = smoothstep((d − 24) / 58)
pitch   = max(cell + 1, round(4·(1 − 0.62·s)·dpr))
mid     = floor((cy + tipDrop)·dpr − cell/2 + 0.5)
y_dev   = mid + (i − 2)·pitch                          rows i = 0..4
```
- Rows are drawn **without antialiasing**. Neighbours always keep at least 1 device pixel apart.
- Row 0 is the top row and shows the **highest** frequency band. Five bands come from the recorder.
- Alpha falls with distance from the stem: `1 - 0.62 × (j / 23)`.
- **Reach** mode: row `i` lights columns up to `rows[i] × 23`, and the last, partly reached cell
  gets the fraction: `alpha = clamp(rows[i] × 23 − j) × fall(j)`, with `fall(j) = 1 − 0.62·(j / 23)`.
  It reads as a bar that reaches left with loudness.
- **Stream** mode: every column shows history (`hist[row][col] × 1.6`), newest at the stem,
  flowing left. History shifts every **45 ms**, 24 columns.
- **Idle in Listening**: when silent, the first pixel of each row pulses:
  `alpha = 0.26 + 0.1 × sin(now × 2.2 + row × 0.9)`.
- **Transcribing**: a travelling wave of light: `phase = (now × 1.1 + row × 0.23 + col × 0.055) mod 1`,
  alpha `max(0, 1 − |col − (1 − phase) × 23| / 1.4) × 0.6 × fall`.
- **Entry sweep**: during the open, a soft bright band runs outward through the veins
  ("sap fills the veins"): `sweep = seg(u, 0.6, 0.42) × 27 − 2`, alpha `0.6 × exp(−(sweep−col)²/2) × fall`.
- Envelope smoothing (exponential, frame-rate independent): rows attack **25 ms**, release **140 ms**;
  overall level attack **30 ms**, release **200 ms**.
- Voice colour is the state accent: jade, or **moon blue when AI**.

### 4.5 States

| State | Label | Stem | Accent | Extra |
|---|---|---|---|---|
| Starting (boot) | "Starting" | work | jade | the Work effects below |
| **Listening** | "Listening" | listen | jade (moon with AI) | 3 **dots** after the word; voice live |
| **Transcribing** / **Thinking** (AI) | "Transcribing" / "Thinking" | work | jade (moon with AI) | label **shimmer**, stem **bead**, voice wave |
| Polishing / Finalizing | "Polishing" / "Finalizing" | work | jade (moon with AI) | the Work effects below |
| **Done** | "Done" | done | jade | stem shrinks to a **check**; a **seed** floats up |
| Done with a caveat | e.g. "AI failed · raw text", "Mic gap · check text" | notice | amber | "!" stem; amber tint |
| Error / nothing inserted | "No speech", "Cancelled", "Speech timed out", "Mic unavailable", "Failed" … | error | soft red | cross stem; droop 0.11; red tint |
| Context cleared | "Context cleared" | notice | amber | |
| Live captions off | "Captions off" | listen | | card explains |

The label shimmer, the stem bead and the travelling voice wave belong to the **Work stem**, not
to a word. Every state that uses it (Starting, Transcribing, Thinking, Polishing, Finalizing)
gets all three.

Label ink: `ink` at **0.78** alpha while Listening/Working; **0.92** for the rest. Notice label =
`warn`; error label = `danger`.

**AI mark**: when AI is on (recording or processing), the voice, stem and glow use **moon blue**,
and a pixel **"A I"** glyph (3×5 pixel font, 2 px cells, 8 px per letter, alpha 0.74) sits before the label.

```
A: 010  I: 111
   101     010
   111     010
   101     010
   101     111
```

### 4.6 The stem

A 2 px rounded bar, 32 px tall, centred on the pill. It has:

- **Halo**: two soft rounded rects around it (grow 1.6 px and 3.2 px) at alpha `0.12 × halo × (1 − i/3)`.
  `halo = 0.5 + 0.5 × voiceLevel` while Listening (it **glows brighter as you speak**), else 0.6.
- A **centre highlight** line (1 px, `stem_hi` at 128/255) from 3 px below the top to 3 px above the bottom.
- Main fill alpha 0.94.

Per state:

- **Work**: the stem dims to alpha 0.45 and a **bead** (5 px tall, `bead` at 242/255) slides up and
  down it: `y = top + 3 + (h − 10) × (0.5 + 0.5 × sin(now × π × 1.4))`.
- **Notice**: a bar with a round dot below it, like "!" (bar height `h − 7`, dot radius 1.6).
- **Done / Error**: the stem **shrinks to 38 %** of its height over 0.28 s (`ease`) and fades,
  then a glyph **draws itself** (starts at 0.08 s, 0.3 s long, `ease`): a **check**
  (`(−4.5,+0.5) → (−1.2,+4.0) → (+5.0,−4.5)` from the stem centre) or a **cross** (two diagonals, each reaching 4 px from the centre each way, drawn one after the other).
  Glyph stroke 2.2 px, round caps/joins, alpha 0.98.
- **Seed (Done only)**: a tiny light leaves the stem and floats up 22 px (`ease`) while drifting
  `sin(prog × 6) × 1.5` px sideways, from 0.32 s for 1.0 s. A radial glow (radius 6, jade 0.45)
  with a 1.3 px `glint` core; alpha `(1 − p²) × min(1, p × 8)`.

### 4.7 The label motion

- **Transcribing shimmer** ("glossing over" the text): the word is drawn once in `ink`, then
  again with a 1 px pen filled by a **horizontal gradient** `accent@0 → glint@1 → accent@0`
  that is **36 px wide**, sweeping left to right. The band travels `wordWidth + 50` px, starting 25 px
  before the word, and loops every **2.5 s** (`at = ((now × 0.6) mod 1.5) / 1.5 × span − 25 + x`).
  Net effect: a soft light passes through the letters.
- **Listening dots**: three 2×2 px squares after the word, 4 px pitch, 5 px after the word.
  A counter `int(now × 4) mod 4` lights 0–3 of them (alpha 0.8 lit, 0.22 off). They **count up
  four times a second**, no antialiasing.
- The label slides in from the left by up to 4 px as the content fades in.
- Baseline: `cy + 4.5` snapped to device pixels.

### 4.8 Entry and exit timeline

Entry is **0.62 s**. Eight parts, each an eased segment `seg(u, start, span)` of `u = t / 0.62`,
all with `ease(0.2, 0, 0, 1)`:

| Part | Start | Span | What |
|---|---|---|---|
| `rise` | 0.00 | 0.22 | the whole growing form rises 12 px into place |
| `grow` | 0.14 | 0.20 | stem grows from a 2.5 px dot, 4.5 px wide |
| `open_h` | 0.26 | 0.26 | bud opens in height (5 px → 36 px) |
| `open_r` | 0.30 | 0.34 | opens to the right (label side) |
| `open_l` | 0.32 | 0.40 | opens to the left (voice side) |
| `tip` | 0.50 | 0.32 | the pointed tip forms |
| `content` | 0.42 | 0.30 | words and voice fade in |
| `fade` | | | 1.0 |

Story: **a seed rises, a stem grows, a bud opens into a leaf, and the words fade in as the tip
forms.** The stages overlap: the words start (at 0.42) just before the tip begins to form
(0.50 to 0.82).

Exit is **0.5 s**, the same parts **in reverse order**, each as `1 − accel(seg)` with
`accel = cubic-bezier(0.3, 0, 1, 1)`. The one exception is `fade`, which is linear (`1 − seg`):

| Part | Start | Span |
|---|---|---|
| `content` | 0.00 | 0.10 |
| `tip` | 0.02 | 0.12 |
| `open_l` / `open_r` | 0.06 | 0.18 |
| `open_h` | 0.14 | 0.14 |
| `grow` | 0.22 | 0.10 |
| `rise` | 0.26 | 0.14 |
| `fade` | 0.30 | 0.10, **linear**; gone by 0.40; the window is fully done at 0.5 |

Every part takes `min(current, exit curve)`, so an exit that starts mid-entry never grows. A
show during an exit resumes from the larger of the two. Every animated value **reaches its end
value exactly** (`enter == OPEN` from 0.62 s, `fade == 0` from 0.5 s).

### 4.9 Resizing: springs

When the label changes ("Listening" → "Transcribing") the pill does not jump.

- Width and height follow a **critically damped spring**, `ω = 20.5` (within 1 % in about 0.33 s).
  It is solved in closed form, so a 16 ms tick and a 250 ms jump land on the same value. It
  keeps velocity if retargeted mid-flight. It snaps to the target only when the distance is
  under 0.05 px **and** the speed is under 2 px/s. A critically damped spring from rest never
  overshoots; a retarget that keeps velocity can carry it slightly past a nearby new target
  before it settles.
- The stem's state timeline is **1.4 s**: morph 0.28 s, glyph to 0.38 s, seed to 1.32 s.
- The tip droop eases over **0.5 s** after a state change.
- A "Done" pill holds for about **1200 ms counted from delivery**, then exits. The exit waits
  until processing is idle (the clipboard restore, `PASTE_DELAY_SECONDS`, default 1 s, can still
  be running), so a slow paste keeps Done up longer. It does not leave early, unless a new
  recording or another state replaces it first.

### 4.10 Caption card (Live, F7)

A glass card **above** the leaf while live captions run.

- Size: min width **420** (or the leaf's width, if wider), height **58**, gap **6** above the leaf,
  radius **14**. It rises 6 px into place as it fades in, driven by the height spring.
- Surface: vertical gradient `card_top @ 230/255 → card_bottom @ 240/255`, the same grain tile,
  a top sheen (`sheen @ 18/255` → 0 at 30 %), and a 1 px rim at alpha **0.15** (Soft edge: 0.06).
  Shadow: σ 7, alpha 0.4, dropped 4 px.
- Text: Instrument Sans 14 px (regular weight), line pitch 20, **two lines**, **left-aligned**
  with 18 px padding; the two-line block is centred vertically in the card. The **newest line** is
  `ink` at 0.92. The **older line recedes** to 0.55 of that (about 0.5). A new line **slides up**
  (decay τ = 70 ms) and the leaving line fades out at the top. Text is clipped to the block.
- A word too long for its row keeps its **end** (left elision), because the end is what was
  just said. The word being revealed keeps its own room; older words on that line lose their start first.
- The empty-card prompt and failure explanations use the muted text colour (178,181,181),
  left-aligned, wrapped to at most two lines, and vertically centred as a block.
- **Words appear at a steady pace**, not in bursts: at least 9 words per second; a large
  backlog catches up faster (τ = 180 ms). The word being revealed fades in.
- With no captions yet it shows a muted prompt: "Speak, then tap F7 to insert". If the live call
  fails, the leaf says "Captions off" and the card says why ("Gemini key rejected. Your text still
  goes through … when you tap F7." / "Live captions failed. Your text still arrives when you tap F7.").

### 4.11 Window and rendering

- Canvas margin (bleed) **32 px** all round so the shadow (up to 28 px) is never clipped.
  Window size rounds up to a multiple of 4.
- The leaf's bottom edge sits **64 px above the work-area bottom**, centred horizontally.
  The window grows **upward** for the caption card; the leaf never moves.
- **Size** (1x / 1.3x / 1.5x) scales the whole drawing (leaf, voice, label, card, shadow). Bottom
  gap and centring stay.
- Paint tick **16 ms** while animating, **250 ms** when a frame is static (e.g. a standing error).
  `dt` is capped at 50 ms.
- Pixel crispness: the stem's x and the label baseline are snapped to the **device grid**; voice
  cells, the AI glyph and the dots also have each edge on a whole device pixel. Body, shadow and card are cached images, rebuilt only when geometry changes;
  a settled frame is a few blits.
- Label font hinting is `PreferVerticalHinting` so paint and measurement agree.
- Tray: the Odicto mark as icon, tooltip "Odicto", menu `Setup…` and `Quit Odicto`. If the
  logo file cannot load, a plain rounded action-green square (16 px in a 24 px icon, radius 5).
- Window flags: frameless, always on top, tool window, transparent for input, does not accept
  focus; transparent background; shown without activating.

### 4.11b Lifecycle: when it shows, how long it stays

| Moment | Behaviour |
|---|---|
| App not ready | "Starting" (work stem) stays until ready |
| Recording starts | leaf opens (or morphs if already open); voice is cleared first |
| Processing | label becomes "Transcribing" / "Thinking" (AI); the pill springs to the new width |
| Text delivered | **Done shows at the moment of delivery**, while the clipboard restore still finishes. It stays about **1.2 s from delivery**; the exit waits until processing is idle, so a slow paste keeps it up longer |
| Done becomes a caveat | e.g. "AI timed out · raw text": it keeps the open leaf and just eases the width and recolours; the hold restarts at 1.2 s |
| Same Done reaches idle | **no replay** of the check; the leaf stays one leaf |
| Error | holds **1.5 s**, then exits |
| Setup problem (preflight) | holds **6 s** so a user who wonders why hotkeys are silent has time to read it |
| Init error ("Mic unavailable" etc.) | stays until init succeeds, and drops to the 250 ms tick |
| "Context cleared" | amber, holds **1.2 s** |
| Exit | the reverse of entry (0.5 s); the label stays the last word while it closes |

Why: the same leaf is reused for every state. An open leaf never re-plays its entry; it only
changes what it says. That keeps quick back-to-back actions calm instead of flickery.

### 4.12 The web port of the leaf

The setup page draws the same leaf on `<canvas>` (hero and preview) with a JS port of the painter.
The sizes (`LEAF_K`), the colours (`LEAF_C`) and the entry and exit times in `LEAF_TIME` mirror
the Python values, and `tests/test_leaf_preview_parity.py` keeps them equal. The other
`LEAF_TIME` entries (how long the demo loop holds Listening, Transcribing, Done and the gap)
belong to the preview only; the real HUD follows the real pipeline.
The loop pauses when the tab is hidden or the page is not Home / Appearance. The Qt painter is
the reference; the canvas copy is faithful but not identical. Known differences:

- **Shadow**: canvas `shadowBlur` is twice the Gaussian σ, so blur 16 / offset 5 / alpha 0.5 and
  blur 3 / offset 1 / alpha 0.32 give the same σ 8 and σ 1.5 as the Qt shadow.
- **Still mode** (motion Off, or the system setting): one fixed frame. Listening has a steady
  first pixel (alpha 0.32) and all three dots lit. Transcribing shows two static pixels per row,
  no shimmer, the bead centred. No seed. No pool breathing. The state is told by shape and
  word, never by motion alone.
- It draws only four states (Listening, Transcribing, Done and the exit). Notice, error and the
  AI mark exist only in the real HUD.
- **Label width**: the canvas eases the width over 320 ms (`ease`); Qt uses the critically
  damped spring of 4.9.
- **Voice envelope**: rows attack 30 ms and release 160 ms in the canvas, 25 ms and 140 ms in
  Qt. The overall level matches (30 ms and 200 ms).
- **Voice cells**: the canvas snaps each cell's origin but keeps the 2 px logical size. Qt gives
  every cell the same whole-device size and keeps at least one device pixel between rows.
- **Tip point**: the small rounded point uses a quadratic control point 0.35 × the radius left
  of the box in the canvas, and 1 × the radius in Qt.

---

## 5. Recreation checklist

To rebuild this look in another stack:

1. Load Instrument Sans. Set body 14/20, antialiased.
2. Define the tokens in 2.3 from `--h` and `--k`. Add the ground light and the grain overlay.
3. Build the shell: 248 px rail, `--rail` fill, right hairline; main column 800 px.
4. Build **one** `.nav-pill` that moves with `transform`; add the glowing 2×20 stem.
5. Make panels from `--glass` + top rim + grain + deep soft shadow; hairlines start 16 px in.
6. Make every input a **well**; focus = the two-ring `--halo`.
7. Buttons: 40 px, three kinds, press = `scale(.98)`, primary gradient with top highlight.
8. Switch (40×24 + 18 px thumb moving 16 px); segmented control with a **sliding knob that carries a stem**.
9. Custom select as a fixed layer on `<body>`, 6 px gap, up/down aware, 120 ms entry.
10. Status marks by **shape** (dot, ring, diamond).
11. Floating layers share the `--float` recipe; save bar rises 16 px; toasts live 7 s.
12. One easing curve and three base durations (the looping bars, the spinner and the HUD exit
    are the listed exceptions, see 1.2). Three motion levels.
13. HUD: 36 px leaf, 84 px of room for the pixel voice (5×23 of 2 px squares), 2 px stem with
    halo, 13 px label.
    Entry seed → stem → bud, with the words fading in as the tip forms (0.62 s, overlapping
    stages; see 4.8). Exit in reverse (0.5 s).
    Shimmer, dots, bead, check and seed as in section 4.
14. Snap everything small to device pixels. Run animation at 16 ms only when something moves.

## 6. Do and don't

| Do | Don't |
|---|---|
| Change the whole product by changing `--h` and `--k` | Hard-code a green |
| Use shape (dot, ring, diamond) plus text for status | Use colour alone |
| Animate `transform` and `opacity` | Animate `left`, the knob's `width`, or in-flow layout |
| Keep one primary button per area | Add a second filled button |
| Let the logo, `warn`, `danger`, `moon` stay fixed | Theme the status colours |
| Give each raised surface a top rim | Use a plain border |
| Snap HUD pixels to the device grid | Let the voice pixels blur |
| Let an exit never grow mid-entry | Restart a closing leaf from zero |
| Hide fields that cannot matter right now | Show every option at once |
| Tell the user what a control will do before they press it | Surprise them with a restart or a prompt |

---

## 7. Voice and copy

The words are part of the design. Odicto talks like a calm, competent friend.

- **Short, plain, sentence case.** "Start Odicto", "Unsaved changes", "Not running". No
  exclamation marks, no jargon where a plain word works, no emoji.
- **Name the thing, then the state.** Titles are nouns ("Connections", "Speech"). Status lines
  say what is true now: "Running", "Almost ready", "Start from Home".
- **Metaphor, lightly.** The two shortcut styles are **Long ride** ("tap, speak, tap again")
  and **Short ride** ("hold while you talk"). The hint always translates the metaphor into
  the literal action.
- **Say what happens, including the cost.** "Save restarts Odicto." "Windows asks for approval
  once when you change this." "Reset clears saved API keys…". No hidden consequences.
- **Errors say what, why, and what next**, in one or two sentences. "The test timed out after
  30 seconds. Check your network and key, then try again."
- **Reassure where trust matters.** "A saved key is never shown again." "Settings stay in
  `.env` on this computer." "Odicto keeps running" when the setup tab closes.
- **HUD words are one to three words**, in sentence case, with a middle dot to add a short
  reason: `AI timed out · raw text`, `Mic gap · check text`, `Cloud slow · local speech`.
  Verbs for work in progress (`Listening`, `Transcribing`, `Thinking`, `Polishing`), a plain
  word for the result (`Done`, `No speech`, `Cancelled`).
- **Hints are lower-case fragments** under a bold name: *Reach* / "rows reach left with your voice".
- Section labels are one or two words in capitals (the CSS capitalises them; write them normally).

---

## 8. Recipes for things this file does not list

Use these to stay on-system when you add something new.

**A new setting row** → `li.row` in a `.group`: `h3` title (15/500), `p` one-line help (12.5 muted),
control on the right. Toggle → switch. 2–3 exclusive choices with short hints → segmented
control with a sliding knob (set `--n` and `--i`). 4+ choices or long lists → custom select
(searchable above 7). Free text → well input; secret → mono key field with Show/Hide. Add a
nav dot only if the page can be in a "needs attention" state.

**A new page** → add a nav item (icon 17 px, thin stroke) and a `.view`. Write a 34 px title
and a lede of one or two sentences. Split into sections with a caps label (with stem) and a
glass group. Give it a Home "meter" row if it has a state worth summarising. Page change is
the standard 420 ms `page-in`.

**A new floating message** → use the `--float` recipe (rim + hairline inset + shadow), a leading
status mark by shape, 13/18 text, 7 s life, a 180 ms fade (removed 200 ms after it starts).
Never push the page.

**A new status or tone** → pick from the four fixed marks (dot, ring, diamond, blue dot) and
four fixed colours. Do not invent a fifth colour. If it needs a new meaning, change the text, not the hue.

**A new HUD state** → give it (1) a stem look, (2) a 1–3 word label, (3) an accent
(jade / moon / amber / red), (4) a hold time, (5) whether it clips the voice. Reuse the same
leaf: it should *morph*, not re-enter. Keep the label spring and the 0.28 s stem morph.

**A new theme** → add a hue and a `k`. Do not touch lightness. Check Graphite-like (`k ≈ 0.1`)
still reads: structure must come from lightness and shape, not from hue.

**A new animation** → ask "what question does this answer?". If none, do not add it. If one,
use `transform`/`opacity`, the one easing, 120/180/320 ms, make it interruptible, and make
sure it vanishes with motion Off.

**Porting to another platform** (native, mobile, a film) → keep the *relationships*: rim on
top, wells for inputs, one accent that means "current", the stem, one easing, concentric
radii, status by shape, reverse exits. Re-derive sizes for the platform's touch targets. The
palette is shared with Odicto Mobile, so tokens carry over unchanged.
