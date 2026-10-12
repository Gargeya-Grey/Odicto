# Changelog

Odicto's version is the one line in `VERSION` (MAJOR.MINOR.PATCH). Each version has a
section here; the Release workflow (`.github/workflows/release.yml`) publishes that
section as the release notes and refuses a version that has no section.

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
