# Changelog

Odicto's version is the one line in `VERSION` (MAJOR.MINOR.PATCH). Each version has a
section here; the Release workflow (`.github/workflows/release.yml`) publishes that
section as the release notes and refuses a version that has no section.

## [1.2.2] - 2026-11-04

### Fixed

- The installed Windows app no longer stalls at startup. The startup lease waited on the launcher's pipe in a way that blocked other reads of standard input, so loading the audio libraries hung until the 60-second startup timeout and the app exited without a log.
- Private installation folders stay readable when an elevated helper writes into them. Migration and admin mode no longer fail with "access denied" on files the administrator step created.

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
