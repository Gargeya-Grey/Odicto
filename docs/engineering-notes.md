# Engineering notes — the efficient-modular experiment

Companion to [architecture.md](./architecture.md). That file describes **what the system is**;
this one describes **what was changed, why, what was deliberately not changed, and how it was
proved**. It is written to be transferable: the patterns and the review techniques matter more
than the specific diff.

Historical experiment: `feature/consolidation`. The sections below record the original
layout and accounting. The current layout is documented in [architecture.md](./architecture.md).

The later folder cleanup moves implementations to `app/`, tests to `tests/`, setup markup
to `assets/`, and OS helpers to `scripts/windows/` and `scripts/posix/`. Root launchers
preserve startup targets, and `app/paths.py` keeps the install root stable for settings,
logs, PID files, and the single-instance lock. Test fixture paths were updated without
changing their assertions; layout tests cover launching and loading assets outside the repo.

---

## 1. The honest accounting

The goal was "the least amount of code, with identical behaviour, made modular". This is what
actually happened.

| Measure | Before | After | Change |
|---|---|---|---|
| Python, all modules | 10,623 | **9,031** | **−1,592 (−15.0%)** |
| Setup page markup | 0 (inline in `setup_web.py`) | 1,564 (`setup_template.html`) | relocated |
| **Total code (Python + markup)** | 10,623 | **10,595** | **−28 (−0.3%)** |
| Genuine deletions (nothing moved) | — | — | **−106 lines** |
| Documentation written | 411 (`docs/research/`) | 853 | +442 |
| Verification code written | 0 | 849 | +849 |

**The single most important line is the third one.** Python fell 15%, but total code barely
moved, because 1,487 of those lines were **moved** into an HTML template, not deleted. Reporting
only the Python number would be misleading.

**Genuine deletions were 106 lines**, against a plan that projected 390–520. The plan was
roughly **4× optimistic**, and the adversarial reviews had already flagged the estimates as
inflated *before* implementation. They were right, and still not pessimistic enough — because
review also *removed* several planned changes as unsafe, and each removal cut the projected
saving.

### Per-phase: predicted vs actual

| Phase | Predicted | Actual | Why the gap |
|---|---|---|---|
| 1 — dead code | −40 … −70 | **−44** | at the low end; honest |
| 2b — `refiner.py` | −60 … −70 | **−27** | the client-construction factory was dropped as unsafe (see §3) |
| 2c–2f — `setup_web`, `config`, `indicator`, `main` | part of −350 … −450 | **−35** | the tables that replace repeated code cost lines themselves; `effective_max_output_tokens` turned out not to be a repeated ladder at all |
| 2a — platform layer | −120 … −150 | **not done** | largest single saving, but unverifiable on this machine (see §6) |
| 3 — template | ≈0 (declared LOC-neutral) | **−1,487 Python / +1,564 markup** | as declared — a relocation |
| 4 — declared `__all__` | +60 … +90 | **not done** | explicitly droppable; buys no behaviour and costs lines |
| 4 — `tools/module_graph.py` | — | **+129** | makes one diagram incapable of drifting |

**Lesson 1 — a plan that says "we will remove N lines" is guessing.** The reliable version is
"these specific duplications exist, with file:line evidence; that is the candidate set." Size the
set, don't size the outcome.

**Lesson 2 — the safe subset is much smaller than the duplication you can find.** Roughly half of
the duplication identified was *not* safely removable, because the whiteness of the test suite
makes it load-bearing (§3).

---

## 2. Where the modest savings came from

| File | Before | After | What happened |
|---|---|---|---|
| `setup_web.py` | 2,368 | **877** | markup moved to a template |
| `refiner.py` | 978 | 951 | 4 guards + 3 warnings + 7 history blocks collapsed |
| `transcriber.py` | 635 | 614 | 2 dead functions deleted |
| `main.py` | 1,396 | 1,375 | dead code, a beep helper, a dead parameter |
| `config.py` | 1,291 | 1,277 | two cascade ladders become tables |
| `indicator.py` | 1,022 | 1,008 | a byte-identical chip painter now delegates |
| `platforms/windows.py` | 357 | 353 | dead duplicate of a mutex-name helper |

Verification now costs more than the deletions saved — which is the correct trade for a
"same behaviour, provably" exercise, but it is a real trade and worth naming.

---

## 3. The decision log — what was NOT changed, and why

This is the most transferable part of the work. Every item below was *identified* as duplication
or dead code, and deliberately left alone.

### 3.1 The log plumbing and lock gate in `main.py` — rejected

They look like obvious extractions (`log.py`, `lock.py`). They cannot be moved, because
`test_units.py` contains:

```python
with patch.object(main_mod, "_LOG_MAX_BYTES", 40), patch.object(main_mod, "_LOG_KEEP_BYTES", 24):
    main_mod._trim_log(path)
...
main_mod._INSTANCE_LOCK_HELD = True
```

`patch.object` on a **module attribute** only affects code that reads that module's global by
name. Extract `_trim_log` into `log.py` and it reads `log._LOG_MAX_BYTES` — the patch silently
stops working, the test still passes, and the rotation thresholds become untested. **A patch
target is a coupling.** Before extracting anything, grep the tests for `patch.object` on its
module.

### 3.2 `effective_api_key` and `effective_llm_api_base` — rejected

They look like the same repeated ladder as `effective_llm_model`. They are not:

- they contain **no `_explicit()` call at all**;
- their attributes (`OPENROUTER_API_KEY`, `META_API_KEY`, `GEMINI_API_KEY`) are **absent from
  `_IMPORT_SNAPSHOT`** — verified, `config.py` lists exactly seven names.

So an override only wins when the *environment* provided the key at import. Routing them through
the new `_provider_override()` helper would have made `patch.object(Config, "OPENROUTER_API_KEY",
"sk-or")` a no-op. That would have **failed in CI, which never creates `.env`, while passing
locally** where a real `.env` masks it. This is the single most dangerous class of bug in the
whole exercise: an environment-dependent test failure.

**Rule adopted: never change `_IMPORT_SNAPSHOT` membership, and always run the suite once with no
`.env`.**

### 3.3 A shared client factory in `refiner.py` — rejected

`TextRefiner.__init__` and `test_provider` construct the same clients. But what they share is
*declarative per-provider configuration*, not logic, and:

- centralising it hides `base_url` / `api_key` / `model` / headers from the call site, where a
  reader wants them;
- it would reorder the openrouter *package* check to after the *api-key* check, changing which
  error string wins when both fail.

The guard (`_require_openai`) and the warnings *were* extracted, because those are behaviour.

### 3.4 Folding the three live-teardown sites — rejected

The three teardown sites look identical. They are not:

| Site | Behaviour |
|---|---|
| `on_live_toggle` already-in-field path | restore clipboard → set event → `_finish_cycle()` |
| `_cleanup_live_session` | **epoch-guarded** restore + `recorder.clear()`, **no** `_finish_cycle()`, **unconditional** `set()` |
| `_finish_live_session` | `_finish_cycle()` on success/error, `set()` only in `finally` |

Folding them would make the **stale-epoch** path acquire `state_lock` and drive a *new* recording
back to `IDLE` — a real double-finish bug. Separately, `_live_cleanup_done.set()` is awaited while
`on_live_toggle` holds `state_lock`, so it must never move under that lock.

**Rule adopted: two code blocks are duplicates only if their *difference* is a parameter. If the
difference is a condition, they are different behaviours that happen to look alike.**

### 3.5 Merging the indicator ring painters — rejected

`_draw_fill_ring` and `_draw_dual_ring` look mergeable. They use **different clocks and sweeps**
(`self._proc_fill` from −90° for `−360°×sweep`, versus `self._t × speed` at a fixed −100°). Same
for `_paint_compact_content` / `_paint_recording_content`, which use **different divider geometry**
(`left + glyph_slot + gap*0.4, ±8` versus `left + 96 + 8.0, ±9`).

And crucially: **no test paints anything.** Only the genuinely byte-identical `_draw_ai_chip` was
collapsed. A pixel-hash gate is not a viable substitute — Qt renders differently per platform, so
the gate would fail on CI for reasons unrelated to the change.

### 3.6 Consolidating the `apply_window_exstyles` no-ops — rejected

Two identical 3-line no-ops in `linux.py` and `macos.py`. But `indicator.py` does
`from platforms import apply_window_exstyles`; moving the no-op to `base.py` would stop the
backends re-exporting it and break that import. **Two lines saved, an ImportError risked.**

### 3.7 Shrinking the test suite — rejected

`test_units.py` is 3,749 lines, ~29% of the repo. Consolidating its setup would cut hundreds of
lines. It was left untouched, because it is the equivalence oracle: shrinking the thing that
proves equivalence, at the same time as the code it guards, is circular.

### 3.8 Adding `__all__` to `platforms/_keyboard.py` — rejected

`_keyboard.py` is a star-import *source* for `windows.py` and `linux.py`. Giving it `__all__`
silently changes what they re-export — and a mistake there breaks **Linux only, at runtime**.

---

## 4. Patterns worth keeping

### 4.1 Data over code, when the rows are uniform

`effective_llm_model` was 39 lines of the same ladder four times. It is now a table plus one
helper:

```python
_MODEL_ATTR = {"ollama": "OLLAMA_MODEL", "openrouter": "OPENROUTER_MODEL",
               "meta": "META_MODEL", "gemini": "GEMINI_MODEL"}

@classmethod
def _provider_override(cls, attr_map: dict, require_nonblank: bool = False) -> str:
    attr = attr_map.get(cls.LLM_PROVIDER)
    if not attr or not cls._explicit(attr, (attr,)):
        return ""
    value = getattr(cls, attr)
    if require_nonblank and not value.strip():
        return ""
    return value if value else ""
```

Three things made this safe:

1. it still reads `getattr(cls, attr)` and still calls `cls._explicit(attr, (attr,))`, so
   `patch.object(Config, ...)` keeps working;
2. the *guard* uses `.strip()` while the *returned value* does not — a detail worth preserving
   exactly, and the kind of thing a "tidy-up" destroys;
3. tables were only introduced where the rows were **uniform**. Where they were not
   (`_handle_test`, `effective_max_output_tokens`), the repetition stayed.

### 4.2 Shared skeleton plus an injected primitive

The two platform backends duplicate a lock-file skeleton and a process-kill skeleton. The safe
form is not "move the body to `base.py`", it is **"move the body, and pass the platform-specific
callables in at call time"**:

```python
base._kill_others(pid_file, enumerate_odicto_pids, kill_process_tree)
```

Why call-time matters: two tests do

```python
with patch.object(platforms._posix, "kill_process_tree") as mock_tree:
    ...
mock_tree.assert_called_once_with(333)
```

If the shared skeleton resolved `kill_process_tree` in `base`'s namespace, those patches become
no-ops and the tests pass while testing nothing. **Import-time capture silently defeats mocks.**

### 4.3 Preserve differences inside a de-duplication

The Windows orphan sweep keeps an **8-second `tasklist` poll** that POSIX does not have, because
that poll is what stops a new instance booting while the old one still holds the keyboard hook —
the double-hook hazard. It sleeps 0.45 s where POSIX sleeps 0.15 s. Both differences survived the
de-duplication, and both are now documented. **A de-duplication that erases a deliberate
difference is not a refactor, it is a regression.**

### 4.4 Move generated artifacts out of the language that generates them

`setup_web.py` built a 1,570-line HTML page as a Python f-string, which meant every CSS/JS brace
had to be written `{{`/`}}`. That doubling is precisely what produced the bug documented in
`test_setup_web_page_js_parses`:

> a Python f-string escape bug once emitted a raw newline inside a string literal, breaking the
> whole script

Moving the markup into `setup_template.html` removes the hazard *class*. Python no longer
contains the HTML; the HTML is editable with HTML tooling; and the token contract is explicit.

### 4.5 Extract generated text with an AST, not by hand

The template had to move without changing one byte. Hand-copying the f-string and "un-escaping
it" is where this goes wrong: the source contains `'Custom\\u2026'`, whose *rendered* bytes are
`\u2026`, and `".\\setup.bat"`, whose rendered bytes are `.\setup.bat`. Miss one and the page
changes.

Instead: `ast.parse` the module, find the `page = <JoinedStr>` assignment, and write each
`ast.Constant` **verbatim** — the parser has already applied Python's escape rules *and* converted
`{{`→`{`. Each `ast.FormattedValue` became a `__TOKEN__`, and the token→expression map was emitted
straight from the AST, so **no expression was ever retyped**.

Payoffs, all in one run: escapes correct, braces correct, 53 tokens from 53 sites correct, and
14 golden page hashes **byte-identical on the first attempt**.

Three details that mattered:

- **Select the node by target name.** A first version collected the first `JoinedStr` in `_page()`
  — which is the unrelated `server_status` f-string.
- **Assert what you assume.** The script asserts `format_spec is None` and *handles* `!r`/`!s`
  rather than assuming their absence. The assertion fired during development.
- **Never retype an expression.** The mapping was pasted from the AST, not transcribed.

### 4.6 Hash, don't fixture, when the artifact is huge

The equivalence oracle pins the setup page with **SHA-256 per environment state**. Committing a
1,564-line HTML fixture per state would have dwarfed the savings this refactor was chasing. On
mismatch the page is written to disk and the diff command is printed, so diagnosis is still easy.

---

## 5. How the change was proved (and how the proof was itself tested)

Four layers, cheapest first:

1. **`test_units.py` — 150 tests, untouched.** Its SHA-256 is asserted by the gate, so no phase
   could quietly "fix" a test to pass.
2. **`test_equivalence.py` — an independent oracle**, 466 lines. It pins the setup page by hash
   across 14 environment states, the cascade resolvers for every provider, and the pure helpers.
   It imports **only `setup_web` and `config`** — never `main`, which writes to the log at import.
3. **A clean-environment run.** The suite is run once with `.env` and `prompt.txt` moved outside
   the repo. This exists because the local `.env` and CI disagree; several bugs of this class pass
   locally and fail in CI.
4. **A mutation test of the oracle.** An oracle nobody has seen fail is worthless, so two
   deliberate breaks were introduced:
   - changing `<html lang="en">` → **all 14 page hashes failed**, with a dump path for diagnosis;
   - adding a backend `__all__` that omitted `KEY_DOWN` → the export guard failed, **and** two
     unrelated tests errored because `platforms.send_text` had vanished.

   That second result is the whole justification for the guard: the mutation did not merely fail an
   assertion, it broke a real interface — and it would have broken it **only in production**,
   because `KEY_DOWN` is read inside hotkey handlers that never run under test.

### 5.1 A machine-dependent golden, caught in the act

The first oracle capture produced `eff_openrouter = [..., "high"]`. The second produced
`"none"` — same code, same run command. The cause: `OPENROUTER_REASONING_EFFORT` is **not** in
`_IMPORT_SNAPSHOT`, so `patch.object(Config, ...)` alone does not make it "explicit"; the first
capture had picked up the value from the developer's own `.env`. That golden would have passed
here and failed on CI.

The fix was to pin `config._PRESENT_AT_IMPORT` to a fixed frozenset in the oracle. **If a golden
can depend on ambient environment, it is not a golden.**

### 5.2 A latent defect found while gating

The clean-environment gate hung once at 180 s, then passed twice, then hung again. The cause is
**pre-existing**, not introduced here: the suite prints `Ran 150 tests / OK`, then the interpreter
can stall at shutdown with

```
Exception ignored in: <function BaseEventLoop.__del__>
AttributeError: 'ProactorEventLoop' object has no attribute '_ssock_'
```

`transcriber.GeminiLiveSession` manages an asyncio proactor loop; whether the finalizer blows up
depends on GC timing. **It is not fixed here** — the live path has no test coverage and cannot be
exercised without a real Gemini session, so changing it would be unverifiable. What was fixed is
the *gate*: python now runs in a child job with a timeout, streaming to a file, and verdicts are
output-based, so a shutdown linger is a warning while a genuine hang (which never prints `OK`)
still fails. A self-healing step restores a stashed `.env` at start-up, so a killed run cannot
strand your configuration.

---

## 6. What is left, and why it was left

| Item | Why it is not done | Risk if done blind |
|---|---|---|
| **Phase 2a — platform de-duplication** (~120–150 lines) | it is the largest single saving, but `platforms/macos.py` and `platforms/linux.py` **cannot be imported on this Windows machine**, and the POSIX branches of the tests never execute locally. The only real gate is the 3-OS CI job, which requires a push. | a silent macOS/Linux regression that passes every local gate |
| **Phase 4 — declared `__all__`** (~60–90 lines *added*) | it changes no behaviour and costs lines; the value is a declared interface. The `ast`-scan guard for it is already written and passing, so it can be added later with a safety net. | low |
| **Unified `providers/` layer** | the largest structural simplification available — one table-driven provider registry across `refiner`, `transcriber`, `config`, `setup_web`. Declined as scope. | medium |
| **`test_units.py` consolidation** | it is the oracle. | circular |

**Phase 2a is the one that needs your decision.** It is the biggest remaining code reduction and
the least verifiable thing in the repo. Verifying it means pushing the branch and reading the
macOS/Linux CI results.

---

## 7. How to evaluate a refactor plan — the checklist this exercise produced

Derived from what the adversarial reviews actually caught. Apply in order; each item killed a real
defect here.

**Numbers**
1. Does the plan **net** unrelated budgets together? (It mixed documentation lines into a
   production-code delta, and the figure did not reconcile.)
2. Is every deletion justified by **pasted grep evidence**, or by an assertion that it is dead?
   (A function whose docstring claimed "used by tests" had zero references. The docstring was
   wrong; the grep was right.)
3. Are line references treated as **hints**? Re-read every region before editing it. Three ranges
   in this plan were wrong, and two would have deleted working code — one would have frozen an
   animation with no test coverage.

**Coupling**
4. Did you grep the **tests** for the symbols you plan to move? `patch.object(module, "NAME")` and
   `from module import name` are coupling, not decoration.
5. Does anything read the **environment at import**? An import-time snapshot makes behaviour depend
   on the machine, and produces tests that pass locally and fail in CI.
6. Are there **two environments** in play (developer machine vs CI)? If so, one gate must run in
   the cleaner one.

**Verification**
7. Is the equivalence argument **durable**? A scratchpad golden is not a proof. Commit it.
8. Has the oracle been **seen to fail**? Mutation-test it. An oracle that cannot fail is decoration.
9. Does any gate **silently skip**? Here, the JavaScript syntax check self-skips when `node` is
   missing, so a bare `OK` was not a sufficient gate — the skip count had to be asserted.
10. Which changes have **no gate at all**? (HUD painting, lock acquire/release, the terminal-typing
    cap.) Either add a minimal gate or declare them out of scope — do not silently refactor them
    and call the result "proved".

**Judgement**
11. For each "duplicate": is the difference a **parameter or a condition**? If a condition, they
    are different behaviours.
12. Is the plan's **modularity ceiling** stated? (No package moves, because tests import by
    top-level module name.) An unstated ceiling becomes an invisible constraint later.
13. Does the plan say what it will **not** do, and does it say **why**? The rejected seams in §3
    are more valuable than the accepted ones.

---

## 8. If you want to continue

1. Push `feature/consolidation` and read the macOS/Linux CI result — that alone validates the
   existing work on platforms this machine cannot exercise.
2. Then attempt Phase 2a with that CI gate in place: share the lock-file scaffolding and the
   `_kill_others` prologue, passing platform callables as call-time arguments, and keeping the
   Windows epilogue intact.
3. Consider a `providers/` registry if the if/elif ladders in four modules start drifting.
4. Run `.\tools\verify.ps1` after every change. It is the whole safety net in one command.

## September 30 reliability build

The adversarial review changed the F7 contract: previews live in the HUD, followed by
one final insertion. The app remains PROCESSING until that insertion completes. This
removes the long-lived caret worker, speculative backspaces, clipboard restoration and
two cleanup branches; delayed callbacks carry a capture epoch and cannot edit a later
session. Listener removal compares bound callbacks by equality, and the five-second
audio ring counts frames in a deque rather than comparing sample counts with bytes.

Direct Groq chat reuses the saved speech key but has its own model. Optional transcript
polish reuses existing provider transports with a fixed prompt and separate model
override. It waits at most two seconds, allows one outstanding request, and cannot
change assistant memory or deliver a late result. Raw text remains available on failure.
Gemini Smart already edits its transcript and skips the extra call. Screenshot context
is explicit and its Qt access runs on the GUI thread. The Windows INPUT union now
matches the native ABI; partial injection raises instead of replaying the entire text.
WM_COPY is bounded to 100ms. Auto Whisper probes CUDA once (at most two seconds), uses
CPU if unavailable, and always uses CPU on macOS; explicit CUDA waits briefly and fails
without loading an unready native driver. Capture is validated at 16kHz.

Historical unit-test checkpoints were intentionally updated for these changed contracts;
three tests were removed with the deleted caret editor and unused context helper. The independent reliability
suite covers the races, failures, SDK image schema and polish isolation. The setup page
goldens were regenerated for Groq and the new controls; config/platform goldens remain
unchanged. No language rewrite was needed: inference runs in native libraries, and the
reliability costs came from orchestration and resource policy.

The HUD follow-up reproduced stale F7 captions when a later ordinary or AI capture
entered PROCESSING. Clear capture-owned preview and committed text at ordinary capture
start and on every pipeline exit. Keep current F7 captions visible until finalization ends.

## October review: decision record

- **The gap rule was reversed.** An overflow or callback gap used to discard the capture.
  The invariant "dictation never fails" outranks the risk of one bad segment, so the audio is
  now kept, processed, and flagged. The user sees "Mic gap · check text" in the HUD and can
  judge the text. Only a dead stream with no audio errors.
- **Clipboard restore is synchronous and guarded.** An immediate restore can run before the
  target app reads the paste, which pastes the old content, so the restore waits
  `PASTE_DELAY_SECONDS` and runs only if the clipboard still holds Odicto's payload. A deferred
  (background) restore was built first and rejected: three review rounds each found races in its
  pending-restore state (a retry adopting a user copy, a probe settling a newer paste early, a
  lost original on a second failure). Holding `_CLIPBOARD_LOCK` through the wait removes that
  state. The pill shows "Done" when the paste key is sent (`paste_text`'s delivery callback),
  but the clipboard wait still blocks the next capture, AI probe or paste for up to the delay. A restore that still fails after ~2 s of retries is dropped with
  a HUD notice; no state carries to the next paste. A carry-over record (retry the lost original
  before the next paste) was removed after three review rounds each found a fault in it (a stale
  original restored over a newer user copy, a fresh snapshot skipping the typing fallback).
- **Linux hotkeys without root stay unsupported.** No XGrabKey or evdev backend exists, and it
  cannot be tested on the development machine. The `keyboard` library needs euid 0; the `input`
  group is not enough. `odicto.py status` and the HUD report it.
- **Abandoned provider calls may keep running.** After a deadline or a cancel, the worker thread
  of a cloud call cannot be killed. It can finish in the background. Its result is dropped and
  is never pasted.

## Resting resource use (October)

Goal: Odicto costs as little CPU and memory as possible while it waits, with no change to
dictation behaviour. Every number below was measured on a Windows 11 laptop (i7-12650H,
16 threads, Python 3.11, PySide6, MME input at 16 kHz mono) with throw-away harnesses that
never bind a keyboard hook and never start `main.py`. CPU is process cycle counts divided
by a calibrated clock (`QueryProcessCycleTime`); the 15.6 ms tick of `GetProcessTimes` is too
coarse for sub-1 % loads. "Private" is Windows private bytes (committed memory); RSS is the
working set. A running install, sampled read-only for 30 s, showed 0.42 % of one core,
573 MB private, 37 MB working set and 30 threads while idle.

### What each part costs at rest

| Part | Idle CPU (% of one core) | Memory | Notes |
|---|---|---|---|
| Bare Python, sleeping | 0.001 | 18 MB RSS / 11 MB private | baseline |
| Capture stream (`AudioRecorder`, MME, 1024-frame blocks) | 0.58 | about 1 MB | 15.6 callbacks per second |
| ...of which the callback body | 0.013 | | 8 µs per call (`_callback`, called directly) |
| ...of which PortAudio and the Python-to-C hand-off | about 0.57 | | a no-op callback costs the same: 0.8 at 1024 frames, 0.47 at 4096, WASAPI 0.95 (single runs, about ±0.2) |
| Heartbeat thread (2 s) | 0.13 | negligible | 2.7 ms per beat: snapshot, JSON, write, replace |
| HUD hidden (tick stopped), offscreen or real platform | 0.015 | see the PySide6 row | tray included |
| HUD pill shown, offscreen | 6.7 to 8.4 | | 60 FPS repaint; a standing error pill did this forever |
| `import numpy`, default OpenBLAS | | +18 MB RSS / **+496 MB private**, +15 threads | one memory pool per CPU thread |
| `import numpy`, `OPENBLAS_NUM_THREADS=1` | | +18 MB RSS / +13 MB private, +0 threads | |
| PySide6 (QtWidgets, QtGui) | | 0.18 s, +14 MB RSS / +7 MB private | |
| `openai` | | 1.2 to 1.4 s, +31 MB RSS / +28 MB private | |
| `google.genai` (with types) | | 1.3 to 1.6 s, +40 MB RSS / +36 MB private | |
| `httpx` / `requests` / `pydantic` | | 0.25 / 0.37 / 0.13 s, +11 / +13 / +5 MB RSS | |
| `faster_whisper` + `ctranslate2` (after numpy) | | 0.4 s, +19 MB RSS / +20 MB private | |
| Whisper `tiny.en` CPU int8, loaded | within noise | 1.4 s warm (3.9 s cold disk), +100 MB RSS / **+741 MB private** | commit is the same at 1, 4 or 8 `cpu_threads` |
| `soundfile`, `keyboard`, `python-dotenv`, `sounddevice` | | each under 0.4 s and 8 MB RSS | |

Whole process, built like `DictationApp._init_attempt` (recorder, speech engine, refiner,
HUD hidden, heartbeat), 45 s idle window, offscreen Qt:

| Configuration | Private MB before / after | RSS MB before / after | Threads before / after | Idle CPU % before / after |
|---|---|---|---|---|
| Whisper speech, no AI | 1277 / 796 | 179 / 179 | 38 / 24 | 1.08 / 0.70 |
| Gemini speech, no AI | 551 / 69 | 100 / 101 | 28 / 13 | 0.87 / 0.79 |
| Groq speech and Groq AI | 547 / 66 | 98 / 97 | 28 / 13 | 0.87 / 0.80 |
| Persistent "Mic unavailable" pill (HUD alone) | 27 / 28 | 49 / 49 | 11 / 13 | **6.7 / 0.5** |

The CPU differences between the first three rows are within the run-to-run noise
(about ±0.15); the memory and thread differences are not.

### What changed, and why

- **`OPENBLAS_NUM_THREADS=1` before numpy loads** (`app/main.py`, `setdefault`, so an explicit
  value still wins). numpy's bundled OpenBLAS creates one worker and one large memory pool per
  CPU thread at import: about 480 MB of committed memory and 15 idle threads. Odicto does no
  large matrix work. Whisper decodes in ctranslate2, which has its own pool and is not
  affected. Checked: decode of 5 / 30 / 60 s of audio took 0.33 / 0.35 / 0.69 s with the
  default and 0.33 / 0.36 / 0.70 s with one thread. Whisper features differ by at most one
  float32 step (1.2e-7 on values near 1.0) because a threaded dot product sums in a different
  order; that is far below the int8 model's own rounding. Effect: 480 MB less commit and 15
  fewer threads, at no cost in speed. It does not change the working set (RSS), only the
  commit charge, which is the figure that task managers report as "committed" or "private".
- **A finished, unchanging HUD pill repaints at 4 FPS instead of 60** (`app/indicator.py`).
  A standing "Mic unavailable" or "Hotkeys failed" pill (`init_error` has no auto-hide) kept
  the 16 ms paint timer running forever for 6.7 % of a core. When the check or cross has
  finished fading in, nothing is resizing and no clock drives the glyph, every frame is
  pixel-identical, so the interval becomes 250 ms. Any app notification (`_sync_from_app`)
  or state change (`_sync_tick`) puts it back to 16 ms. Recording, processing and the
  entrance and exit animations are never slowed. A test compares a grab before and after
  forty skipped frames.
- **Tray icon is the Odicto logo.** `logo_icon()` loads `assets/odicto-icon.svg`, then
  `assets/odicto-icon-256.png`, from `paths.ROOT` (never the working directory). A file counts
  only if it renders, so a missing Qt SVG plugin or a damaged file moves to the next one. If
  neither loads, the old plain green square is still used so the tray never loses its icon.
  The app does not set a window icon (the HUD is a tool window with no taskbar entry), so
  there was nothing else to change.
- **`Odicto <version>` is logged** beside `Code version: git <rev>` at start.

### Already in place, now pinned by tests

- Provider SDKs load only when configured. After `import main` no SDK, no Qt and no Whisper
  is loaded. Cloud speech does not import `faster_whisper`; Gemini speech imports only
  `google.genai`; an OpenAI-compatible chat provider imports only `openai`. A configured
  provider still loads at startup, so first-use latency is unchanged.
- The HUD's paint timer already stops once the pill has left (hidden: 0.015 % of a core).

### Left as it is, and why

- **Capture block size (1024 frames).** A 4096-frame block saves about 0.4 % of a core, but
  `stop()` cannot see audio still inside the driver block, so a larger block clips up to
  256 ms (now 64 ms) off the end of the last word. The persistent stream and its 0.4 s
  pre-roll are what protect the first word; the block size protects the last.
- **Heartbeat interval (2 s).** `odicto.py wait-ready` requires
  `callback_age_s + heartbeat age < 3`, so the interval cannot grow without a change in
  `app/odicto.py`. The beat costs 0.13 % of a core. Not applied.
- **WASAPI instead of MME** costs the same (0.95 % against 0.86 %) and changes how device
  endpoints behave; the reconnect and recovery paths were exercised on the current host API
  only. Not applied.
- **The HUD at 60 FPS while it is genuinely animating** (recording, thinking). That is the
  design; it lasts seconds.
- **`soundfile`** is only needed for the debug WAV, but `test_units` patches `recorder.sf`, so
  the module-level import stays (1.4 MB).
- **`psutil`** (+6 MB RSS) is imported by the platform layer on Windows.
- **The 2.7 % idle figure** from an earlier sample could not be reproduced: the harness
  parts above add up to 0.7 to 1.1 %, and a read-only sample of the running install showed
  0.42 %. Typing system-wide through the keyboard hook, or a dictation in progress, would
  explain a higher short sample.

### Applied (owner decision, 12 Oct): the backup Whisper is released after 10 idle minutes

At this commit Whisper is already loaded lazily when a cloud engine is active:
`CloudTranscriber` and `GeminiTranscriber` build `WhisperTranscriber` only inside
`local_fallback`, so a Gemini, Groq or OpenRouter install pays nothing for it until the first
failure. Measured cost of that first failure, warm disk: import `faster_whisper` 0.3 to 0.5 s,
model load 1.4 s, then a 5 s clip decodes in 0.33 s: about 2.2 s in all. On a cold disk the
load took 3.9 s (about 5 s in all). Both fit inside the 20 s `STT_DEADLINE_SECONDS` that
the local retry gets. It adds +100 MB RSS and **+741 MB private**, and the model then stays resident until the
process exits (a process that has also decoded a clip holds 1.46 GB private before the BLAS
limit and 0.98 GB after it).

The proposal was to release the fallback model after a long idle period, so a rare cloud
failure does not leave 741 MB committed for the rest of the day. The owner chose 10 minutes.
The price is one more 1 to 5 s load on the next failure (0.9 to 2.0 s measured warm with
tiny.en on the i7-12650H, about 5 s on a cold disk). Pre-loading Whisper at startup for a
cloud install would be the opposite trade: +741 MB for a 2 s gain on a rare failure.

How it works (`_LocalWhisperFallback` in `app/transcriber.py`): each backup use restarts a
daemon `threading.Timer` (`WHISPER_FALLBACK_IDLE_SECONDS = 600`). The timer drops the model
only when no backup decode runs and no newer use has restarted it (a generation number), then
runs `gc.collect()`. The next failure loads the model again. The primary Whisper
(`STT_PROVIDER=whisper`) and a live-only Whisper are separate objects and stay loaded.

**MKL keeps memory per released model.** The first version released the model but kept
about 124 MB per load-and-release cycle: committed memory after each release went 262, 386,
511 MB (tiny.en, CPU int8). A load-only cycle did not grow (69, 66, 67, 68 MB); a decode did.
The cause is the fast memory manager of the MKL inside ctranslate2: with
`MKL_DISABLE_FAST_MM=1` the same decode cycles stay flat (104, 106, 104, 108 MB). Turning it
off costs decode speed (5 s clip, two runs each: 0.29 s with the cache, 0.36 to 0.37 s
without), so it is not set process-wide. `_backup_only_mkl_settings` sets it only for a
backup load, only when neither the dictation nor the live engine is Whisper, and only before
ctranslate2 loads (MKL reads it once); a value already in the environment wins. Measured
through the real `local_fallback` path with a cloud engine: 819 to 822 MB loaded, 104 to
108 MB after each of three releases.

Children inherit the environment: the tray's setup page, and the Odicto that its Save
starts. So the value goes with a marker, `ODICTO_SET_MKL_DISABLE_FAST_MM=1`, and the top of
`app/main.py` removes a marked value before anything can load ctranslate2. Without that, a
switch from a cloud engine to Whisper through Save would start a primary Whisper with the
cache off (about 70 ms slower per 5 s clip). A value the user set has no marker and stays.

### By eye

Tray icon at 16, 24, 32, 48 and 64 px (graphite rounded square, jade mark) renders cleanly
offscreen. Check it once on the real taskbar (light and dark), and check that a standing
error pill looks the same as before.
