# Managed Odicto installation

The bootstrap scripts install `uv` if needed, obtain its managed Python 3.12,
and verify the stable GitHub release ZIP against its adjacent SHA-256 file before
executing the installer from that archive. No system Python, Git, Node or npm is
required. Both scripts run the same embedded bootstrap verifier.

Run the bootstrap as your desktop user. It does not start a microphone or global
keyboard hook. Installation ends with the command to open Setup; the user chooses
when to start the application. On Ubuntu/Debian, missing native audio, clipboard
and display packages are installed through the user's normal sudo prompt. The
application itself is never started as root. Other Linux distributions receive an
actionable prerequisite message before a managed install is created. A Linux Wayland session stops the bootstrap before `sudo` and before any
download, with the advice to log in to an Xorg session. A GitHub rate limit or a lost
connection stops it with a plain message, and nothing is installed. macOS wheels
are checked for native imports; Homebrew is not installed automatically. OS
permissions remain prerequisites. An import failure leaves the staged version
inactive. Linux Wayland sessions are unsupported. Windows Admin mode uses a stable
managed task, so updating application files does not change the task's target.

## Install

On Windows, the release also has `Install-Odicto.cmd`. It holds `bootstrap.ps1` unchanged
(built by `build_easy_installer.py` in the Release workflow). Double-clicked, it runs that
bootstrap, then `odicto autostart` (new installs only), `odicto start` and `odicto setup`,
and waits for a key. It starts nothing when the Start Menu item `Odicto` runs another program
or a Startup `Odicto.lnk` exists (a source checkout), unless `migration.json` says the
migration is complete; the user migrates first. The check reads only those two shortcuts, under
`%APPDATA%`: a source copy without them, or a running source owner, is not detected. It reports
each failed step and exits 1. Pre-releases do not get the file, because it installs the latest
stable release.
It is not signed. On Windows, install also writes the Start Menu folder `Odicto` with
Odicto, Odicto Setup and Stop Odicto, which run the stable `bin\odicto.cmd`. A shortcut of
the same name that runs another program is kept.

From 1.5.0 every Odicto takes a session keyboard-hook lock before it hooks the keyboard
(`docs/architecture.md` section 7), so two installs of 1.5.0 or later never run together in one
user session. A copy older than 1.5.0 does not take the lock: stop it and retire its startup
entries before you rely on the lock. When another copy holds the lock, the owner exits 75, and
`odicto start` or `odicto restart` prints "Another copy of Odicto is running. Quit it, then start
Odicto again." The updater keeps the new release in that case. A lock error exits 2, which the
updater handles like any other failed start (see On-disk contract).

Download the release ZIP from [GitHub Releases](https://github.com/Gargeya-Grey/Odicto/releases/latest)
and extract it. Run the bootstrap from its `Odicto` directory as your desktop user.

Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-managed.ps1
```

macOS, Ubuntu or Debian:

```bash
bash install-managed.sh
```

The bootstrap prints the stable command path. Use it to run `odicto setup`, then
`odicto start`. On macOS grant Accessibility and Input Monitoring when requested.
Both Mac architectures need their own native installation. An update can require
renewing OS permissions for the new executable. The installer does not bypass
the operating system's consent.

The bootstrap does not migrate or stop an existing source owner. To transfer an
updated source checkout, run `odicto migrate --source "PATH" --check`, quit its
dictation and Setup yourself, then repeat without `--check`. Migration copies
only known settings as opaque bytes, preserves recovery files, transfers owned
Windows startup/admin preferences, and records reciprocal receipts. An unknown
startup entry or configured destination blocks the operation. It never launches
dictation. Keep the printed recovery path and repeat the same command to resume
an interrupted transaction. Use the managed command afterward. Two installations
must never run together.

Only the verified source task shape can be retired automatically. Older limited
login tasks and changed task settings block preflight, including when no source
shortcut exists. The owner must inspect and retire that task, then enable managed
autostart if desired.

Legacy `SYSTEM_PROMPT_FILE` references must first be normalized by saving the
prompt in source Setup. Migration checks that reference without displaying
configuration values and refuses a transfer that could silently select a different
prompt. A custom destination prompt blocks migration even when the source has none.

macOS/Linux source login-startup entries require owner retirement before
migration; they are not automatically rewritten. After migration, use
`odicto autostart` to enable the managed entry.

## Commands

The installed command is placed in the managed home's `bin` directory, with a
convenience entry in `~/.local/bin` when that name is free. The uv installer also
uses this user bin location on common configurations. If it is not already on
PATH, use the absolute command printed at installation or add that bin directory
to PATH. Existing unrelated commands and shell profiles are left untouched.

| Command | Behavior |
| --- | --- |
| `odicto setup` | Open Setup using the active release and stable settings |
| `odicto start` | Check for updates when due, then invoke the active lifecycle CLI |
| `odicto stop` | Stop through the active lifecycle CLI; never update first |
| `odicto restart` | Stop, then start through the active lifecycle CLI; it does not check for updates |
| `odicto config` / `odicto version` | Show the main resolved settings (keys show only as `<set>`) / the version |
| `odicto status` | Show the managed active and previous release records |
| `odicto run status` | Show the application's runtime status |
| `odicto help [command]` | List every command, or show one command's help. `odicto COMMAND --help` is the same and never runs the command |
| `odicto check` | Read the latest stable release tag without changing an install |
| `odicto stage` | Download, verify, build and check a version without activating it |
| `odicto update` | Apply a pending ready version or check for a newer stable release |
| `odicto rollback` | Restore the previous ready version and block automatic reactivation of the failed version |
| `odicto auto-updates off` | Stop automatic checks and activation |
| `odicto auto-updates on` | Enable automatic update checks on subsequent launches |
| `odicto autostart` | Enable one stable startup entry for this managed home |
| `odicto remove-autostart` | Disable its owned startup entry |
| `odicto admin on` / `odicto admin off` | Change Windows Admin mode with owner-approved UAC when required; when the mode changes, dictation is left stopped |
| `odicto migrate --source "PATH" --check` | Inspect a source migration without applying it |
| `odicto migrate --source "PATH"` | Apply or resume a stopped source installation's migration |

Automatic checks run before `start` and `setup`, at most once every eight hours.
Offline checks and staging failures retain the active release. Explicit `update`
can retry a version blocked after rollback. The command does not kill an owner to
activate an update; a busy owner or Setup defers activation and preserves the ready
pending version for the next launch.

`odicto update` prints one line per step (check, download and verify, install, health
check) and one result line. The output of `uv` and of the health check goes to
`<data>/update.log`, so a failed step names that file and says the active version still
works.

## On-disk contract

```text
managed-home/
  .odicto-managed.json       ownership and update preferences
  launcher.py               stable entry selecting the active app and updater
  bin/odicto[.cmd/.ps1]      shell entry points
  active.json               atomically replaced ready-release pointer
  pending.json              complete staged release awaiting activation
  manager.lock              serializes updater operations
  launch.lock               closes the activation-versus-launch gap
  data/                     private settings, prompt, runtime state and shared locks
    setup.lock
    dictation.lock
  releases/vX.Y.Z-HASH-ID/   immutable final code directory and final-location .venv
    .odicto-ready.json      written only after installation and hook-free checks
  releases/.trash-vX.Y.Z-HASH-ID/   a retired release, deleted after the lock is released
```

`ODICTO_DATA_ROOT` is the absolute `data` path and `ODICTO_MANAGED_HOME` is the
absolute managed home. All versions must use the same data-root instance lock and
Setup writer lock. The manager uses the app's first-byte Windows file-lock and
POSIX `flock` protocol. An activation takes launch, Setup and instance locks in
that order before atomically replacing the pointer. Private settings are created
from the shipped example only if absent; existing settings are never merged or
overwritten by an archive. Source checkouts and unmanaged directories are not
automatically migrated.

Windows administrator tasks are named per managed home and invoke the stable
`bin/odicto.ps1 admin-task` entry. They have no login trigger. A single owned
Startup entry supplies login startup when enabled. Admin mode changes preserve
that startup preference and leave the app stopped; use `odicto start` afterward.
The supervisor retains the launch reservation until the exact child proves
readiness, and cancels an unconfirmed child if its controlling pipe disappears.
An abandoned elevated reservation uses its recorded release acknowledgement after
the supervisor exits; a medium-integrity caller does not need to inspect the owner's
command line to recognize that handoff. Instance and Setup leases still defer updates.

An explicit migration holds source and destination writer/runtime leases and
requires the source's `source_migration_guard` capability. A pending receipt
blocks legacy source starts before settings or startup changes. Completed,
matching receipts allow supported legacy entry points to forward to the stable
managed launcher. Recovery metadata and owned startup backups stay under
`migrations/<token>` in the managed home. Private source files remain intact.
The transaction records absent configuration files too, and checks that they remain
absent on retry. An existing destination prompt blocks a source-without-prompt
handoff so it cannot override the source's effective instructions. An already owned
managed login-startup preference is preserved during POSIX migration.

The stable launcher reads `active.json` before loading the updater from the active
verified release. This switches the updater and app together. A failed start can
restore the prior pointer only after the failed owner has released its lock. It
does not pretend a failure-returning but still-running owner has exited.

Managed startup keeps its launch lease until the exact child reports a live
microphone and readiness with the matching launch token and code root. A private
pipe cancels unconfirmed children if the supervising command disappears. An
uncertain child keeps activation deferred. Setup hands off to its writer lease,
so an open page blocks version switching while another start or Setup command can
still proceed.

## Release and integration requirements

- Ship `tools/distribution/capabilities.json` containing schema `1`,
  `stable_data_root: true` and `setup_writer_lock: true` only after integration tests
  establish those behaviors.
- Ship `tools/distribution/requirements.lock`, generated for Python 3.12 with
  universal platform resolution and hashes. The resolution includes Python 3.10+
  compatibility markers for source installs; managed installs use Python 3.12.
  Intel Macs retain an ONNX Runtime version with a native Intel wheel.
  The manager uses `uv pip sync
  --require-hashes`; it refuses an unlocked dependency release.
- Build each virtual environment in its final unique release directory. No venv
  directory is moved after creation. Failed staging evidence is never a ready release.
  It stays in place until a newer folder without `.odicto-ready.json` replaces it as the
  newest one; then the old-release rule below retires it.
- Retire old releases. Odicto keeps the active release, its `previous` one, the `pending`
  one, and the newest folder without `.odicto-ready.json`. Every other
  `releases/vX.Y.Z-HASH-ID` folder that is a real folder (not a link or a junction) is
  renamed to `.trash-<name>` under `manager.lock`; a rename that fails is skipped. After
  the lock is released, `.trash-*` folders are deleted, and what is left goes on the next
  run. The retire and delete steps share one budget of 15 seconds (`CLEANUP_BUDGET`). The
  time is checked before every file operation, so no operation starts after it; one call
  into the file system that blocks is not interrupted. The delete never trusts an earlier
  check: on Windows it holds each folder open while it empties it (a held folder, and every
  folder above it, cannot be renamed or replaced), and on macOS and Linux every step is
  relative to an open folder and never goes through a link. This runs after `odicto start` returned success (not
  after `restart` or `setup`) and after `odicto update` or `install` activated a release.
  A pointer that cannot be read retires nothing. Each retired folder is one line in
  `<data>/update.log`.
- Publish `odicto-release.json` with every stable release (not a pre-release). The
  bootstrap and `odicto update` read it first, from
  `https://github.com/Gargeya-Grey/Odicto/releases/latest/download/odicto-release.json`.
  The GitHub API answers only when that request gets an HTTP error or the file is
  invalid. A lost connection does not try the API. `tools/distribution/build_release_manifest.py`
  builds it.
- An admin-enabled home activates or rolls back only to releases declaring
  `managed_admin_mode: true`. Its stable task and data-root quit event must remain
  usable across a version switch. No update silently downgrades elevation.
- Setup must retain its writer lock for its full lifetime, including draining
  request writers before release. Main must take the stable data-root instance
  lock before installing hooks. Every lifecycle launcher must honor those paths.
- The adjacent checksum detects corruption through the trusted GitHub release
  channel. It is not an independent publisher signature or macOS notarization.

Synthetic tests cover hostile archives, checksum failures, settings preservation,
locking, interrupted activation, rollback and offline starts. They do not prove
real native permissions, microphone capture, global shortcuts or editor insertion.
