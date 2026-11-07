# Changelog

Odicto's version is the one line in `VERSION` (MAJOR.MINOR.PATCH). Each version has a
section here; the Release workflow (`.github/workflows/release.yml`) publishes that
section as the release notes and refuses a version that has no section.

## [1.5.0] - 2026-11-07

### Added

- Install on Windows without a terminal. Download `Install-Odicto.cmd` from the release and double-click it. It installs Odicto, turns on start at sign-in, starts Odicto and opens Setup. The file is not signed, so Windows asks once: click "More info", then "Run anyway".
- The Windows install adds three Start Menu items: Odicto, Odicto Setup and Stop Odicto. A Start Menu item of the same name that runs another program (for example a source checkout's) stays as it is.

## [1.4.3] - 2026-11-04

### Changed

- Polish (F8) now replaces the selected text in place. No review window opens. Press Ctrl+Z to get the original back. Polish replaces the text only when the same window and the same selection are still there when the result arrives; otherwise your text stays as it was and the pill says "Selection changed - text kept". The pill says "Polished" when it is done.
- Live captions (F7) are easier to read: slightly larger, medium weight, the newest line at full strength and the older line stronger than before.

## [1.4.2] - 2026-11-04

### Fixed

- Polish (F8) works again with cloud models. It waited only 2 seconds, and a normal request takes 1 to 3 seconds, so it often showed "Polish unavailable". It now waits at least 10 seconds (longer for text over 600 characters) and never longer than the AI time limit. Esc still cancels it; a new polish can start when the cancelled request has ended.
- Polish on OpenRouter is faster. It now asks the model not to think first (the lowest effort the model allows), even when your AI answers use a high effort. In a test with Claude Haiku, one polish took 1.1 seconds instead of 3.0 seconds.

## [1.4.1] - 2026-11-04

### Changed

- `odicto update` shows a short list of steps and one result line, like an application update: "Odicto 1.4.1 is ready", with a link to what is new. The package and tool output goes to `update.log` in the data folder. If an update fails, it says that your current version still works and where the details are. You see this from the first update after 1.4.1, because an update runs the update code of the version you have.
- `odicto help` no longer ends with an "options" block for advanced flags, and it says where to change the indicator's look.

### Fixed

- The setup page shows the original Odicto logo again. 1.4.0 replaced it with a small leaf by mistake. The logo keeps its own green in every theme colour.

## [1.4.0] - 2026-11-04

### Added

- Setup has an **Appearance** page with a live preview of the pill. Choose its waveform (Reach or Stream, `HUD_WAVEFORM`), texture (Veins, or Plain: no veins and a faint grain, `HUD_TEXTURE`), edge (Light rim or Soft, `HUD_EDGE`) and shape (Leaf or Capsule, `HUD_SHAPE`).
- Pill size on Appearance: 1x, 1.3x or 1.5x (`HUD_SIZE`). The whole pill grows; its place on the screen stays.
- Theme colour on Appearance: Forest, Ocean, Plum, Ember, Graphite or any custom colour (`THEME_COLOR`). It colours the pill and the setup page. Brightness stays the same, so text stays readable. Warning and error colours do not change.

### Changed

- The pill is now a small leaf. A glowing stem shows the state, and five rows of pixels show your voice.
- The setup page has a new look: glass panels, a glowing stem that marks the selected item, and a warning mark that is a ring, not only a colour.

### Fixed

- When F7 live captions fail, the pill says "Captions off". Before any captions appear, the card above it says why and that your text still arrives. For a rejected Gemini key, the card names the service that still makes your text (Groq, OpenRouter or local Whisper). The old label, "Live off · Gemini key rejected", did not say that the text still arrives.

## [1.3.0] - 2026-11-04

### Added

- `odicto help` and `odicto --help` list every command with examples. `odicto help COMMAND` and `odicto COMMAND --help` show the help of one command, and never run it.

### Changed

- F7 live dictation returns its text as soon as the final arrives: about 0.5 s after the second tap, not 2.6 s.
- The F7 pill shows the live words on two larger lines. The words appear smoothly, the older line is dimmer, and a new line slides up.

### Fixed

- When Google rejects the Gemini API key, the F7 pill shows "Live off · Gemini key rejected" while you speak, and the stop does not send the audio to Gemini again with the same key.
- The "Transcribing" pill after an F7 stop no longer cuts off its label.
- A command with an unknown word, for example `odicto stop now`, is refused before anything runs.

## [1.2.6] - 2026-11-04

### Documentation

- The engineering notes state the one case where, with transcript logging off, an uncaught error's message still reaches the log: a main-thread exit with a text message. The app's only such message holds no user text.

## [1.2.5] - 2026-11-04

### Fixed

- With transcript logging off, a crash report in the log shows where it happened and the error type, not the error message.
- Gemini Live errors and OpenRouter empty-reply notices in the log no longer include provider text.
- Many log lines at once no longer wait long when another Odicto process holds the log lock.

## [1.2.4] - 2026-11-04

### Fixed

- Starts that overlap no longer lose log lines. Every process writes `dictation.log` under one lock, and the running app trims the log only while it holds that lock.
- A managed start that fails before the app loads now writes one line to `dictation.log`, so the launcher's pointer to the log has a record.
- When a cloud transcription fails and local fallback is off, the log keeps the HTTP status of the cause.
- With transcript logging off, Meta response lines in the log show only known status values and token counts, and AI errors keep their HTTP status.
- Only the running app writes the installed app's log; a test or helper process that inherits its settings no longer can.

## [1.2.3] - 2026-11-04

### Fixed

- The installed app keeps its log again. Started through admin mode, it wrote no `dictation.log` because its output went to a console nobody sees; it now always logs to the data folder, so startup problems and the verification report have a record.
- With transcript logging off, provider errors in the log name only the error type and HTTP status, so a provider that echoes your words cannot put them in the log.

## [1.2.2] - 2026-11-04

### Fixed

- The installed Windows app no longer stalls at startup. The startup lease waited on the launcher's pipe in a way that blocked other reads of standard input, so loading the audio libraries hung until the 60-second startup timeout and the app exited without a log.
- Private installation folders stay readable when an elevated helper writes into them. Migration and admin mode no longer fail with "access denied" on files the administrator step created.
- Setup and admin mode name an "access denied" error and the folder to check, instead of a generic failure or a false success.

## [1.2.1] - 2026-11-03

### Added

- Top-level `install-managed.ps1` and `install-managed.sh` entrypoints for the standard installed-app journey. Daily operation uses the same `odicto` command outside the development checkout.
- An explicit source-to-managed migration with a read-only check, opaque settings import, ownership checks, recoverable receipts and no automatic application launch.
- Managed Windows admin mode with stable task, launcher and data-root ownership across compatible release updates. Existing source admin/startup preferences can be carried through an owner-approved migration.

### Changed

- The README now focuses on installation, Setup and daily use. Source setup, manual dependencies and advanced configuration moved to `docs/development.md`.
- Migration guards protect against accidentally launching a retired source checkout alongside the installed application. Unknown files and startup entries remain preserved.

### Fixed

- Windows installation selects the first `uv` executable on PATH even when several are installed.
- Re-running the verified installer can repair an administrator-enabled installation during an update without importing helpers from the extracted checkout.
- Migration recognizes source processes launched through filesystem aliases or relative script paths.
- Migration/admin commands use the installed release's dependencies. Startup receipt recovery, legacy prompt checks and launcher ownership checks prevent incomplete or misleading transfers.
- The elevated task recognizes an existing owner before spawning a child. Closing a normal source debug session no longer falls through into a background restart.

## [1.2.0] - 2026-11-03

### Added

- Personal vocabulary on Speech for exact names, brands and technical terms. Gemini receives up to 100 terms; Groq and local Whisper receive whole spelling hints within a conservative UTF-8 budget. Polish also receives the preferred spellings.
- On-demand selection polish. Select text in an editor and press F8, or configure a different shortcut. Minimal editing instructions preserve meaning, emotion, voice, deliberate emphasis, language and paragraph breaks.
- A dedicated Polish page with the shared key recorder, keycap display and Disable control. Its searchable model picker supports an independent model choice or a custom model ID using the AI provider's saved key.
- A managed installer with bundled Python provisioning, hash-locked dependencies, checksum-verified releases, preserved settings, staged automatic updates and rollback. No Node or system Python is required.
- Fresh-install and update CI on native Windows and macOS ARM/Intel runners, plus Ubuntu and a separate Ubuntu 26.10 QEMU guest. Publication requires every platform job at the exact main commit.

### Changed

- Raw and live transcription no longer run an automatic polish call, including installations with the legacy `POLISH_DICTATION=true` setting. Polish runs only when explicitly requested.
- Selection polish leaves the original document untouched and shows the original and polished text in a review window with explicit Copy and Dismiss controls. Cancellation and failed requests never insert text. Terminal targets are skipped.
- Linux X11 hotkeys use a rootless server-grab backend. Unusable desktop sessions are rejected before claiming the instance lock.
- X11 grabs only the configured modifier combinations, leaving desktop window shortcuts available. Terminal text is sent through a private pipe; text that cannot be typed safely is offered for explicit copying before any input is sent.
- Managed startup confirms the exact child is ready before releasing its launch lease. Failed or uncertain starts cannot silently activate a broken update.

### Verification limits

- Window and copy-based selection checks cannot establish field identity in every editor. Real microphone, global-hook and editor journeys require device verification; automated and offline UI checks do not establish that acceptance.
- Wayland remains unsupported. Managed Windows admin mode is unavailable. Exact hosted macOS builds can lag Apple's latest patch; runner metadata is retained with each test result.

## [1.1.1] - 2026-10-14

### Fixed

- The hotkey answers at once again after Odicto sat idle while other apps used the memory. Windows had moved almost all of Odicto out of memory (14 MB of 94 MB stayed) and ran it on the slow cores, so the first tap waited seconds for the pill, the microphone dropped audio and speech-to-text took up to 7 s. The admin-mode logon task also started Odicto at below-normal priority. On Windows, Odicto now keeps 192 MB in memory, runs at above-normal priority with normal memory and disk priority, and opts out of power throttling. The log shows a `Responsiveness:` line at start.
- The raw chord no longer starts AI mode. A late keyboard hook could lose a Shift release, and Odicto then counted Shift as held. On Windows, Ctrl, Shift, Alt and Win now come from Windows' own key state.

## [1.1.0] - 2026-10-13

### Changed

- The dictation pill now looks like Odicto Mobile and the setup page. It uses the same graphite surface, pale jade accent and Instrument Sans font.
  - A status disc at the left end, the same as the phone's voice bubble: green with a microphone while listening (a soft halo grows with your voice), a turning jade arc while starting or working, pale jade with a check for Done, amber for Done with a notice, soft red with a cross for an error.
  - The waveform is jade and stronger in the middle. It rises fast with your voice and falls slowly; in a pause it shows a row of dots that breathe.
  - The AI and Live chips are jade.
- Smoother motion. A size change follows a smooth spring with no bounce, and a change in mid-move carries on from the current speed. The pill opens in about 0.38 s and closes in about 0.34 s (before: 0.64 s and 0.74 s). The disc pops a little when it changes. All animation runs on real time, so it looks the same when the computer is busy.
- A softer shadow that reads well on light windows.
- Lighter to draw: a pill that holds still draws its shadow once and reuses it, so each frame costs about 40 % less.

## [1.0.1] - 2026-10-12

### Fixed

- The installers (`install.ps1`, `install.sh`) no longer move a broken `.venv` aside while a program still runs from it. Before, they missed a program that was started through another spelling of the install folder: a Windows short path such as `C:\Users\RUNNER~1\...`, a symlinked folder, or a command relative to the folder, such as `.venv/bin/python main.py` (the macOS and Linux launchers start Odicto this way). Now the installer stops and shows the PID of that program.

## [1.0.0] - 2026-10-12

First versioned release.

### Added

- A new setup page in the design of the Odicto mobile app, made for the desktop:
  - A side rail with the Odicto logo, the version and a live "Running" card.
  - Home: what each shortcut does, and a summary of your setup.
  - One page each for Connections (all API keys), AI answers, Speech, Shortcuts, Prompt and System.
  - The save bar shows only when you change something. Its own font ships with Odicto, so the page makes no network request for fonts.
- A Start button on the setup page when Odicto is not running.
- The Odicto logo from the mobile app: the setup page, its tab icon, and `assets/odicto.ico`.
- `VERSION`, this changelog, `odicto.py version`, and GitHub Releases: a zip of the app and its SHA-256.
- Windows: Start Menu entries **Odicto**, **Odicto Setup** and **Stop Odicto**, with the Odicto icon. `install.ps1 -NoShortcuts` skips them.
- Admin mode (Windows) is a switch on the setup page, off by default. With it on, Odicto can type into windows that run as administrator. Windows asks for permission once when you change the switch, not at each start or stop.

### Changed

- Three launchers at the install root: `start_dictation`, `setup` and `stop_dictation` (`.bat` on Windows, `.sh` on macOS and Linux). A double-click on `start_dictation.bat` starts Odicto; it no longer restarts a running Odicto.
- The setup page server stops when you close the last setup tab, or after 30 minutes with no request. It no longer runs as long as Odicto runs. A second launch opens the setup page that is already open.
- Lighter at rest: Odicto commits about 480 MB less memory and runs about 15 fewer threads (numpy's math library now uses one thread). Speech recognition speed is the same.
- With a cloud speech engine, the backup Whisper model (loaded after a cloud failure) is released after 10 idle minutes. That frees about 700 MB. The next cloud failure loads it again in 1 to 5 seconds. Whisper as your main engine stays loaded.
- A standing HUD message (for example "Mic unavailable") no longer redraws 60 times a second. It looks the same.
- The tray icon is the Odicto logo.

### Removed

- The second, administrator start file. Use the admin mode switch on the setup page.
