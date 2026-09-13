# Engineering notes — the efficient-modular experiment

Companion to [architecture.md](./architecture.md). That file describes **what the system is**;
this one describes **what was changed, why, what was deliberately not changed, and how it was
proved**. It is written to be transferable: the patterns and the review techniques matter more
than the specific diff.

Branch: `feature/consolidation` (never pushed to `main`).

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
