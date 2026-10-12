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
- `VERSION`, this changelog, and GitHub Releases: a zip of the app and its SHA-256.

### Changed

- The setup page server stops when you close the last setup tab, or after 30 minutes with no request. It no longer runs as long as Odicto runs. A second launch opens the setup page that is already open.
