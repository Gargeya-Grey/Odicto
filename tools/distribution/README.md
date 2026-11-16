# Managed Odicto installation

The bootstrap scripts install their own `uv` in a bootstrap folder outside the managed home (see "Private
uv"), obtain its managed Python 3.12,
and verify the stable GitHub release ZIP against its adjacent SHA-256 file before
executing the installer from that archive. No system Python, Git, Node or npm is
required. Both scripts run the same embedded bootstrap verifier.

Run the bootstrap as your desktop user. It does not start a microphone or global
keyboard hook. Installation ends with the command to open Setup; the user chooses
when to start the application. On Ubuntu/Debian, missing native audio, clipboard
and display packages are installed through the user's normal sudo prompt. The
application itself is never started as root. Other Linux distributions receive an
actionable prerequisite message before a managed install is created. A Linux Wayland session stops the bootstrap before `sudo` and before any
download. It says to choose an Xorg session if the login screen offers one, and that Ubuntu 25.10 and newer have none. A GitHub rate limit or a lost
connection stops it with a plain message, and nothing is installed. macOS wheels
are checked for native imports; Homebrew is not installed automatically. OS
permissions remain prerequisites. An import failure leaves the staged version
inactive. Linux Wayland sessions are unsupported. Windows Admin mode uses a stable
managed task, so updating application files does not change the task's target.

## Private uv, and WSL

The bootstrap never touches the managed home. It installs `uv` in its own folder, outside the home:
Linux `${XDG_CACHE_HOME:-~/.cache}/odicto-bootstrap/uv` (an `XDG_CACHE_HOME` that is not absolute is
ignored), macOS `~/Library/Caches/Odicto-bootstrap/uv`, Windows `%LOCALAPPDATA%\Odicto-bootstrap\uv`. It never
uses a `uv` that is already on `PATH`, so the version is the one Odicto installs. A `uv` that already sits in
that folder and answers `--version` is reused, because the folder is the user's own cache. The Astral
installer runs with `UV_INSTALL_DIR` and `UV_UNMANAGED_INSTALL` set to that folder, and with
`UV_NO_MODIFY_PATH`, `INSTALLER_NO_MODIFY_PATH` and `UV_DISABLE_UPDATE` on (`UV_UNMANAGED_INSTALL` alone
also stops the profile edit, the env script and the updater receipt; the others guard against a change in
the installer). So no shell profile, `PATH` entry or `~/.local/bin` file is written for `uv`. For the
install process only, the bootstrap puts that folder first on `PATH` and sets `ODICTO_BOOTSTRAP_UV` to the
absolute path of its `uv`; `bootstrap.ps1` puts `PATH` and the variable back in a `finally` block, also
when the install fails.

The manager makes the home first, with its private-directory policy and its ownership marker. Then
`ensure_private_uv` copies the `uv` it was started with (`ODICTO_BOOTSTRAP_UV`, else the `uv` on `PATH`; on
Windows a `uv.exe` file) into `<home>/tools/uv/` (a private folder; copy under a temporary name, then an
atomic rename; `uvx` too when it sits beside `uv`). It does this when it installs dependencies (install and
update) and when it writes the launchers, only when the copy is missing. `find_uv(home)` looks in
`<home>/tools/uv/` first, then on `PATH`, so an update that starts at sign-in, from a macOS LaunchAgent or a
desktop autostart with a minimal `PATH`, finds `uv`. A home that holds `tools/uv` before Odicto has made its
marker is not empty: the installer refuses it, as it refuses any other file.

Versions work together both ways. An older manager (v1.5.x) knows no `tools/uv`. The new bootstrap leaves
the home alone, so that manager finds an empty or missing home, takes `uv` from `PATH` (the bootstrap
folder is first) and writes that absolute path into its launchers, which stays valid. A newer manager
started by an older bootstrap copies the `uv` on `PATH`. If the user clears the bootstrap folder, an old
manager's launchers need the installer run again; a newer manager's launchers point into the home.

The POSIX launcher `bin/odicto` holds the absolute, quoted paths of `uv` and of `<home>/launcher.py`. It
does not derive them from `$0`, because `~/.local/bin/odicto` is a link to it. The Windows `.cmd` and
`.ps1` launchers need no such fix: the user-level `.cmd` calls the absolute `<home>\bin\odicto.cmd`, and
`%~dp0` and `$PSScriptRoot` then point into the home.

On Linux, `sudo apt-get` and the `uv` installer read no input (`</dev/null`), so the bootstrap works when
the script comes through a pipe.

WSL (the file `/proc/sys/fs/binfmt_misc/WSLInterop` exists, or `WSL_DISTRO_NAME` is set) is for testing.
It is checked on its own, before `sudo` and any download: without `DISPLAY` the bootstrap stops, whatever
the Wayland variables say, and names `wsl --update`. WSLg sets `WAYLAND_DISPLAY` but runs an X server, so
with `DISPLAY` set the bootstrap goes on, says that the shortcuts work only while a Linux window has focus
and that Odicto types only into Linux (X11) apps, and adds `libasound2-plugins` (the ALSA `pulse` device
for the WSLg microphone). A tester can point `ODICTO_WSL_INTEROP_FILE` at another file to fake or hide WSL.

## Install

On Windows, the release also has `Install-Odicto.cmd`. It holds `bootstrap.ps1` unchanged
(built by `build_easy_installer.py` in the Release workflow). Double-clicked, it runs that
bootstrap, then `odicto autostart` (new installs only), `odicto start` and `odicto setup`,
and waits for a key. It starts nothing when the Start Menu item `Odicto` runs another program
or a Startup `Odicto.lnk` exists (a source checkout), unless `migration.json` says the
migration is complete; the user migrates first. The check reads only those two shortcuts, under
`%APPDATA%`: a source copy without them, or a running source owner, is not detected. It reports
each failed step and exits 1. After a start that worked it asks `odicto wait-ready --timeout 5
--report-waiting`; when Odicto still waits for a microphone or its speech model (exit 20 or 21),
the summary is yellow ("Odicto is running. It is not ready to dictate yet."), Setup still
opens, and the exit code stays 0. Any answer other than 0, 20 or 21 gives a yellow "Odicto
started. It did not confirm that it is ready."; only exit 0 gives "Odicto is ready." A failed start says "Odicto did not start. The lines above say
why." and to click Odicto in the Start Menu after the cause is fixed. Pre-releases do not get the file, because it installs the latest
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
convenience entry in `~/.local/bin` when that name is free. If it is not already on
PATH, use the absolute command printed at installation or add that bin directory
to PATH. Existing unrelated commands and shell profiles are left untouched; the
installer edits no profile, not even through `uv`.

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
| `odicto uninstall` / `odicto uninstall --all` | Stop Odicto and remove it; keep `data/` (settings) / delete it too. See Application menu and removal |
| `odicto uninstall --ask` | The same, after one question (keep or delete the settings, or cancel). The Remove Odicto shortcut runs it |

Automatic checks run before `start` and `setup`, at most once every eight hours.
Offline checks and staging failures retain the active release. Explicit `update`
can retry a version blocked after rollback. The command does not kill an owner to
activate an update; a busy owner or Setup defers activation and preserves the ready
pending version for the next launch.

`odicto update` prints one line per step (check, download and verify, install, health
check) and one result line. The output of `uv` and of the health check goes to
`<data>/update.log`, so a failed step names that file and says the active version still
works.

## Application menu and removal

On Linux, `install` and every activation of a release (`update`, an automatic update at `start` or
`setup`, `rollback`) write `odicto.desktop`, `odicto-setup.desktop`, `odicto-stop.desktop` and
`odicto-remove.desktop` into `$XDG_DATA_HOME/applications` (default `~/.local/share/applications`). The
first three run the quoted stable `bin/odicto` with `start`, `setup` or `stop`. The last runs
`uninstall --ask` and has `Terminal=true`. The release icon is copied to `<home>/odicto.png`. A home
that an older Odicto installed gets its entries at its first activation. The marker
`.odicto-managed.json` keeps the SHA-256 of every text written (`menu_entries`) and of the icon
(`menu_icon`). A file of that name with another hash, a link or a folder is kept and the command prints
`Kept ...`, as for a Windows shortcut that runs another program. Odicto deletes or replaces a menu file
only after it read it through one open handle (`O_NOFOLLOW`) and checked that the name still holds the
same file (device and inode); a new entry is made with a link that fails if anything exists, so a file
that appeared meanwhile is never overwritten. On Windows the same four names are Start Menu shortcuts
(`Remove Odicto` opens a normal window; the others run minimized). macOS has no menu entries.

Each start (`start`, `setup`, and an activation) also reconciles the Linux menu: a missing entry is made
when its name is free, an out-of-date entry that Odicto wrote is rewritten, and a current menu is not
touched. On Windows, a start or an update adds the `Remove Odicto` shortcut that an older install lacks,
when the other three shortcuts are Odicto's (best effort). On POSIX, a home that an older Odicto
installed gets its `launcher.py` refreshed with the other launchers.

**Threat model of `odicto uninstall`.** It protects against accidents: a link or junction where a
folder should be, a mount point inside the home, a file of the same name that Odicto did not write, two
commands at once, a crash or a failed delete halfway, a file that cannot be read, a home that was
moved. It does not try to win a race against a hostile program that already runs as the same user: such
a program can already change `launcher.py`. So a menu file, the start entry, the `odicto` command and a
shortcut are checked by hash and then deleted in two steps, and a swap between them is not stopped.
A folder on another device than the home, or a mount point below it (Linux: read from
`/proc/self/mountinfo`, bind mounts included), is never entered; the stage fails and keeps it whole.

`odicto uninstall` (`--all` to delete the settings too, `--ask` to be asked) works in this order and
prints one line for each step. It stops at the first stage that fails and runs nothing after it:

1. Refuse, before anything changes, a home that is a link (the launcher passes the home as it was
   reached in `ODICTO_LAUNCHER_HOME`, because the manager itself only sees the resolved path), a home with
   no Odicto marker that names it, a home that holds the user's own home folder, an unfinished
   migration, and (Windows) admin mode that is on (the user turns it off in Setup first; the command
   never runs `schtasks` or `admin_mode.ps1`). The checked folder stays open to the end: POSIX keeps a
   folder descriptor (`O_NOFOLLOW`) and every later call is relative to it, Windows keeps a handle that
   forbids a rename of the folder and of every folder above it. The marker is read through it too.
   Before the locks, before the stop and before every stage, the path of the home must still name the
   held folder, else the command stops and deletes nothing more (a moved home).
2. Take `manager.lock` and `launch.lock` and hold them to the end, so a start, an update and a
   rollback find them busy. Stop Odicto with the active release's own `stop` (not the managed `run`,
   which would take the same locks). A release record that cannot be read ends the command with nothing
   deleted. Then take `data/setup.lock` and `data/dictation.lock`: an open Setup page or a running app
   refuses the uninstall.
3. Write `uninstalling.json` into the home. Start, update, rollback and every other manager command except `install` refuse
   while it exists. A failed uninstall leaves it, so nothing starts between two tries; `odicto
   uninstall` finishes the job, and `odicto install` clears it.
4. Remove the start-at-sign-in entry (`managed_autostart.remove`; on macOS the LaunchAgent is first
   booted out of launchd, and only when launchd says it was loaded from this very file), the Start Menu
   shortcuts or menu entries, and `~/.local/bin/odicto` (or `odicto.cmd`), in that order. Each is deleted
   only when it is still Odicto's own. A foreign item (another program's file of that name) stays and is
   not a failure. "Could not check" is not "foreign": an unreadable file or receipt, a failed `launchctl
   print` or `bootout`, an exception or a timeout, or an owned item that cannot be deleted, is a
   failure. The command says so, deletes nothing more, keeps the receipts and exits 1.
5. Delete the home's entries, every link as a link and never through it, in this order: everything
   except the files that run this command again, the releases other than the active one, (`--all`)
   `data/`, then the final group: the active release, `bin`, `launcher.py`, `active.json`, the private uv,
   whatever is left, the marker (rewritten to the bare ownership record without `--all`; deleted with
   it), then the two lock files and the notice. The locks and the notice are held to the last delete
   (Windows cannot delete an open file, so there the locks are released just before). With `--all` the
   home folder goes last. A stage before the final group that fails is named, and `odicto uninstall`
   finishes the job when run again. From the final group on, the command that would run again is
   gone, so a failure there prints the full path of the home and says to delete it by hand (the `data`
   folder first moved aside when the settings are to stay).

## On-disk contract

```text
managed-home/
  .odicto-managed.json       ownership and update preferences
  launcher.py               stable entry selecting the active app and updater
  tools/uv/                 the manager's private copy of uv (uv, uvx); the bootstrap never writes it
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

Managed startup keeps its launch lease until the exact child proves it runs, with the
matching launch token and code root. A private pipe cancels unconfirmed children if the
supervising command disappears. An uncertain child keeps activation deferred. Setup hands
off to its writer lease, so an open page blocks version switching while another start or
Setup command can still proceed.

What counts as started depends on the start. The owner publishes `initialized` (its own
ready flag, before the microphone clause) and `init` (`stage` and `reason`) in
`dictation-health.json`; `stage` is `speech-download` while the speech model is not in the
local cache. `odicto.py wait-ready --report-waiting` reads them for a verified owner (same
pid, token, code root, heartbeat age and process checks as a ready owner). It prints
`ODICTO-WAITING <state>` and exits 20 for `mic` and `speech`, or 21 for `speech-download`.
The manager accepts a state only when the exit code and the marker line agree; a dead probe,
a timeout or any other code is `none`.

- A start that follows an activation (an update) is strict. Only a fully ready owner counts;
  anything else cancels the child and rolls back, as before. The admin launcher writes
  `"strict": true` in `admin-request.json`, and the admin task reads it. A request without the
  field is strict.
- Any other start accepts `mic` and `speech` at once, and `speech-download` after 45 seconds
  (`DOWNLOAD_ACCEPT_SECONDS`; the admin launcher gives up at 70 seconds and Setup Save at 90).
  The hand-off is the same as for a ready owner (the `R` byte under `launch-intent.lock`, the
  admin `released` state). The command exits 0 and prints one line on stderr that says what the
  owner waits for. The admin task stores the state as `notice` in its result file, and the
  launcher prints it. The "already running" checks accept the same states. The recovery of a
  launcher killed after the hand-off accepts a waiting owner only with proof of that hand-off:
  the supervising launcher is gone and the child still runs `HANDOFF_SETTLE` (1 second) later.
  A child that never got the `R` byte ends itself when the launcher's pipe closes, so it does
  not pass. So a waiting owner does not block `odicto stop` or an update for ever.
- A child that exits, a crash, any other stage and a missing heartbeat still fail the start.

The launcher deletes `admin-result-<32 hex>.json` files of earlier admin launches when a new
one starts, and deletes its own result file after it reads it. `managed_admin.helper` deletes
its `admin-receipt-<uuid>.json` after it reads it; after a timeout or an unreadable receipt the
file stays as the record of what the elevated helper did. `launch-receipt-*` files go only together
with their intent.

`odicto install` also gets the speech model, in the step "Getting the speech model", after the
settings exist. It runs the hidden app command `_prepare-speech` with the release's Python and
the managed environment (time limit 900 seconds; output goes to `<data>/update.log`). The
command downloads the configured `WHISPER_MODEL_SIZE` for every speech provider, because local
Whisper is also the backup of the cloud engines, unless the value is an existing folder. If the
step fails or times out, the install prints "The speech model was not downloaded now. Odicto
downloads it at its first start." and continues.

Known limits:

- On the start that updates from 1.5.0, the old manager supervises the new app. The rules above
  apply from the next start.
- A start is strict only when the same command activated the release (`odicto start` with a due
  update). After a separate `odicto update`, or an update that `odicto setup` applied, the next
  start is not strict. Those starts never rolled back before this rule either; they now succeed
  with a notice when Odicto waits for a microphone or its speech model. A marker for "this
  release has not had a fully ready start yet" would close the gap; it does not exist.
- The download state covers the model constructor (download, then load). A load that hangs
  inside it after the download still shows as `speech-download`.

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
