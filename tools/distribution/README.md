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
actionable prerequisite message before a managed install is created. macOS wheels
are checked for native imports; Homebrew is not installed automatically. OS
permissions remain prerequisites. An import failure leaves the staged version
inactive. Linux Wayland sessions are unsupported. Windows Admin mode uses a stable
managed task, so updating application files does not change the task's target.

## Install

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
| `odicto status` | Show the managed active and previous release records |
| `odicto run status` | Show the application's runtime status |
| `odicto check` | Read the latest stable release tag without changing an install |
| `odicto stage` | Download, verify, build and check a version without activating it |
| `odicto update` | Apply a pending ready version or check for a newer stable release |
| `odicto rollback` | Restore the previous ready version and block automatic reactivation of the failed version |
| `odicto auto-updates off` | Stop automatic checks and activation |
| `odicto auto-updates on` | Enable automatic update checks on subsequent launches |
| `odicto autostart` | Enable one stable startup entry for this managed home |
| `odicto remove-autostart` | Disable its owned startup entry |
| `odicto admin on` / `odicto admin off` | Change Windows Admin mode with owner-approved UAC when required; leave dictation stopped |
| `odicto migrate --source "PATH" --check` | Inspect a source migration without applying it |
| `odicto migrate --source "PATH"` | Apply or resume a stopped source installation's migration |

Automatic checks run before `start` and `setup`, at most once every eight hours.
Offline checks and staging failures retain the active release. Explicit `update`
can retry a version blocked after rollback. The command does not kill an owner to
activate an update; a busy owner or Setup defers activation and preserves the ready
pending version for the next launch.

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

An explicit migration holds source and destination writer/runtime leases and
requires the source's `source_migration_guard` capability. A pending receipt
blocks legacy source starts before settings or startup changes. Completed,
matching receipts allow supported legacy entry points to forward to the stable
managed launcher. Recovery metadata and owned startup backups stay under
`migrations/<token>` in the managed home. Private source files remain intact.

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
  directory is moved after creation. Interrupted and failed staging evidence stays
  in place; it is never a ready release and is not automatically deleted.
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
