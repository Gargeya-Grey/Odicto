# Plan: zero-prerequisite install on macOS and Linux

Written 11 November 2026 against `main` at `099b459` (Odicto 1.5.0).
This file is a work order for a coding agent. A person can read it too.

## 1. Goal

A person has a Mac or a Linux desktop with nothing installed for Odicto: no Python, no Git,
no Homebrew, no knowledge of a terminal. That person downloads one thing from the release page,
does a short list of steps, and dictates a sentence into a text box.

Windows has this since 1.5.0 (`Install-Odicto.cmd`). This plan finds what stops macOS and Linux
from having it, proves each gap on a clean machine, and closes the gaps in a fixed order.

**Done** means the acceptance journey in section 4 passes on each target system in section 5,
done by a person's hands on a real desktop, with the evidence in section 9.

## 2. How to read this plan

Read sections 1 to 5 once. Then work through the tree below from top to bottom.
A phase starts only when the phase above it has its exit check. Inside a phase, the
items are independent unless an item names another item.

```text
Zero-prerequisite install
├─ Phase 0  Baseline: reproduce today's install on clean machines      (section 7)
│    exit: one filled row per target system in the baseline table
├─ Phase 1  Confirm or reject every gap in the register                (section 6)
│    exit: each gap has the status Confirmed or Rejected, with evidence
├─ Phase 2  Owner decisions                                            (section 8)
│    exit: the owner answered D1 to D4; the answers are in this file
├─ Phase 3  Fixes, in this order                                       (section 8)
│    ├─ 3a  Installer tells the truth early (cheap, no decision needed)
│    ├─ 3b  One-step entry for macOS
│    ├─ 3c  One-step entry for Ubuntu and Debian on X11
│    ├─ 3d  After-install parity with Windows (start, sign-in, Setup, app menu)
│    └─ 3e  Scope work that the owner approved in D1 to D4
├─ Phase 4  Proof on real desktops                                     (section 9)
│    exit: the acceptance journey passes, by hand, on each target system
└─ Phase 5  Documents                                                  (section 10)
     exit: docs/install.md, README.md and the release notes match the code
```

Each gap in section 6 has the same fields:

- **ID**: `M` is macOS, `L` is Linux, `X` is every system.
- **Status**: `Confirmed (code)` means this plan's author read the code or the CI file and the
  line reference is given. `To verify` means the claim comes from the author's general knowledge
  of the operating system and nobody ran it. Treat `To verify` as a guess until Phase 1 proves it.
- **Check**: the exact way to confirm or reject it.
- **Fix options**: possible fixes. Do not start a fix before Phase 1 and Phase 2 end.

## Progress

Update this table when a gap changes status. The register in section 6 keeps the original evidence.

| Gap | Status | Evidence or pull request |
| --- | --- | --- |
| M10 | Closed, #36 | The lock pins `sounddevice-0.5.6-py3-none-macosx_10_6_x86_64.macosx_10_6_universal2.whl`, and that wheel holds `_sounddevice_data/portaudio-binaries/libportaudio.dylib` (read 11 Nov 2026). #36 removes Homebrew PortAudio from CI. Its macOS smoke jobs report `bundled_portaudio: pass` and load the library from `_sounddevice_data`. |
| L2 | Closed, #36 | Wayland stops `bootstrap.sh` before `sudo` and any download. A test runs the script with stubbed `sudo`, `apt-get`, `curl` and `uv` and checks that none is called. |
| X3 | Closed, #36 | Plain messages for a rate limit, another HTTP error and a lost connection. The test fakes the answers; the real quota is not used. |
| X6 | Closed, #36 | The printed Setup command is quoted. |
| CI | Closed, #35 | 11 Nov 2026: the Ubuntu jobs failed before their tests (mirror names did not resolve, some `amd64v3` packages answered 404). #35 retries the package installs and skips CI for a pull request that changes only Markdown the tests do not read. |

## 3. Rules for the agent that does this work

1. Read `AGENTS.md`, `docs/architecture.md` and `tools/distribution/README.md` first.
   Read `.agents/skills/verify-odicto/features/managed-install.md` before any proof step.
2. Never run an installer, `odicto start`, `odicto setup`, `odicto autostart` or any start or stop
   script on the owner's computer. That computer runs Odicto for real. Use a disposable virtual
   machine, a CI runner, or a new operating-system user that the owner made for this test.
3. Never read, print or copy `.env` or an API key.
4. `bootstrap.ps1` and `bootstrap.sh` hold the same embedded Python verifier. A change to one
   needs the same change in the other, and `tests/test_managed_distribution.py` must still pass.
5. The single-instance rules in `AGENTS.md` ("STRICT: single instance only") stay true in every fix.
6. This work is Tier 2 under the owner's rules: it has more than three units and it touches
   `workflow` (CI and release), `hook` (keyboard hooks) and `public API` (installer entry points).
7. Run `tools/verify.ps1` (or `python tools/run_tests.py`) before each push.
8. An agent cannot grant macOS permissions, speak into a microphone or pass Gatekeeper. Each of
   those steps needs the owner's hands. Prepare the checklist; do not claim the result.
9. When a `To verify` gap is rejected, write the evidence in the register and delete its fix
   from Phase 3. Do not build a fix for a problem that does not exist.

## 4. Acceptance journey

One journey for each target system. A person who has never used a terminal does it, on a machine
or user account with no Odicto, Python, Git, Homebrew or `uv`.

1. Open the README. Find the download for this system in less than one minute.
2. Download one file.
3. Do no more than three actions to start the installer. No action needs a typed path.
4. The installer asks at most for the account password (Linux system packages) and for the
   operating system's own consent boxes. It asks no other question.
5. At the end, without any further command: Odicto runs, Setup is open in the browser, and the
   screen says how to dictate.
6. Each permission that the system needs is asked for by name, with the exact place to turn it on.
   After the person grants it, Odicto works without a terminal command.
7. The person clicks a text box, taps Ctrl+`, speaks, taps Ctrl+` again, and the words appear.
8. The person logs out and in. Odicto runs again. Dictation works.
9. The person can stop Odicto, start it and open Setup without a terminal.
10. The person runs the installer a second time. It repairs or updates, runs one copy only,
    and keeps the settings.

Record for each step: pass or fail, a screenshot or the exact text, and the time taken.

## 5. Target systems

Minimum set. D2 and D3 in section 8 can add to it.

| System | Why |
| --- | --- |
| macOS 26, Apple silicon, clean user | Current macOS, most Macs. CI runner `macos-26`. |
| macOS 26, Intel, clean user | CI runner `macos-26-intel`. The lock file keeps a separate ONNX Runtime for Intel. |
| macOS 27, Apple silicon, clean user | CI runner `xcode-27`. |
| Ubuntu 26.04 desktop, default install | Current long-term release. Check which session types its login screen offers. |
| Ubuntu 24.04 desktop, default install | Older long-term release that many people still use. Not in CI today. |
| Debian 13 desktop (GNOME), default install | The installer names Debian. Not in CI today. |

## 6. Gap register

### What works today (read from the code, not run by this plan's author)

- `install-managed.sh` calls `tools/distribution/bootstrap.sh`. The bootstrap installs `uv` when
  it is missing, gets Python 3.12 through `uv`, downloads the latest stable release from GitHub,
  checks its SHA-256, and runs the manager's `install` command from that archive
  (`tools/distribution/bootstrap.sh`).
- On Linux the bootstrap installs missing system packages with `sudo apt-get`
  (`bootstrap.sh`, the `packages=` list).
- The manager puts the app in `~/Library/Application Support/Odicto` (macOS) or
  `~/.local/share/odicto` (Linux), and a command at `~/.local/bin/odicto`
  (`tools/distribution/manager.py`, `default_home` and `expose_command`).
- `odicto autostart` writes `~/Library/LaunchAgents/com.odicto.managed.plist` or
  `~/.config/autostart/odicto-managed.desktop` (`app/managed_autostart.py`).
- CI installs from a fresh archive and runs the test gate on `macos-26`, `macos-26-intel`,
  `xcode-27`, `ubuntu-26.04` and an Ubuntu 26.10 virtual machine (`.github/workflows/ci.yml`).
- CI states its own limits: no microphone capture, no permission grant, no real global hook,
  no real editor (`tools/ci_platform_smoke.py`, `LIMITS`).

### Every system

| ID | Gap | Status | Check | Fix options |
| --- | --- | --- | --- | --- |
| X1 | The release zip has no file that a person can double-click. Each script in it needs a terminal that is open in the extracted folder. The Windows one-file installer is a separate release asset, not in the zip. | Confirmed (code): `release.yml` packs `git archive`; the repository root has only `install-managed.ps1`, `install-managed.sh`, `install.ps1`, `install.sh`. | List the zip of v1.5.0. | Publish one entry per system as a release asset (as Windows does). Decide in D4 if the zip stays a user download at all. |
| X2 | The bootstrap ignores the zip that it came in. It downloads the latest stable release again. An old zip installs the newest version, and nothing installs without internet. | Confirmed (code): `bootstrap.sh` reads `releases/latest`. | Read the script. | Accept it and say it in the docs, or stop telling users to download the zip (see M2, L4). |
| X3 | The bootstrap calls `api.github.com` without a token. GitHub limits such calls per network address. On a shared network the install can fail. | Confirmed (code) for the call. To verify for the failure text. | Do not use up the real limit: everyone on the same network address shares it, CI included. Give the embedded verifier a fake HTTP 403 rate-limit answer in a test (as `tests/test_managed_distribution.py` fakes downloads) and read the message that the user gets. | Resolve the version through `https://github.com/<repo>/releases/latest` (a redirect, not the API), or print a plain message: "GitHub is busy for this network. Try again in an hour." |
| X4 | No uninstall command exists. The docs do not say how to remove Odicto. | Confirmed (code): no `uninstall` in `manager.py` or `app/odicto.py`. | `odicto help`. | Add `odicto uninstall` (stop, remove the startup entry, the menu entries and the command, then the home; ask before the settings). Document it. |
| X5 | The docs give no install size and no time. | Confirmed (docs). | Measure in Phase 0. | Put the measured numbers in `docs/install.md`. |
| X6 | The installer prints the Setup command without quotes. On macOS the path has a space (`Application Support`), so a pasted command fails. | Confirmed (code): `manager.py`, the `Configure with:` line. | Paste the printed line in Terminal on a Mac. | Print the managed-home command in quotes. Do not print `~/.local/bin/odicto` as the only way: `expose_command` keeps an unrelated command of that name, so that path can run another program. |

### macOS

| ID | Gap | Status | Check | Fix options |
| --- | --- | --- | --- | --- |
| M1 | Install needs Terminal: open it, change to the extracted folder, type a command. | Confirmed (code and README). | Acceptance step 3 fails. | See M2. |
| M2 | A double-click script (`Install Odicto.command`) from a downloaded zip is stopped by Gatekeeper, because the download carries the quarantine flag and the file is not signed or notarized. The person must go to System Settings and choose **Open Anyway**. | To verify on macOS 26 and 27. | Put a one-line `.command` file in a zip, download it with Safari, double-click it. Record every box. | (a) One pasted line: `curl -fsSL <release asset URL>/install.sh \| bash`. No Gatekeeper box, but needs Terminal one time. (b) A signed and notarized `.pkg` or `.app` that runs the bootstrap. Needs an Apple Developer account (D1). (c) An unsigned `.command` plus the **Open Anyway** steps in the guide. |
| M3 | `bootstrap.sh` was never run through a pipe (`curl ... \| bash`). A step that reads standard input can eat the rest of the script. On Linux `sudo apt-get` is such a step. | To verify. | In a clean VM run `curl -fsSL https://raw.githubusercontent.com/Gargeya-Grey/Odicto/main/tools/distribution/bootstrap.sh \| bash`. Compare the result with a run from a file. | Wrap the script body in a function that is called on the last line, and give `apt-get` `</dev/null`. Or tell the person to download the script and run `bash <file>`. |
| M4 | The installer does not start Odicto, does not open Setup and does not turn on start at login. The Windows one-file installer does all three. | Confirmed (code): the bootstrap ends after `install`; compare `tools/distribution/build_easy_installer.py`, `AFTER`. | Read the last lines of an install run. | A POSIX easy installer: bootstrap, then `odicto autostart` (new installs only), `odicto start`, `odicto setup`, with the same rules as the Windows one (existing source copy: install but do not start). |
| M5 | `~/.local/bin` is not on the default macOS `PATH`. A new Terminal window says `odicto: command not found`. | To verify. The `uv` installer can add this folder to the shell profile; check if it does, and if a new window is needed. | After a clean install, open a new Terminal window and type `odicto version`. | With M4 and M7 the person never needs the command. Else print the full quoted path (X6). Do not edit shell profiles (`tools/distribution/README.md` promises that). |
| M6 | Odicto needs Accessibility, Input Monitoring and Microphone consent. The app has probes for the first two only. No probe or message exists for a refused microphone. | Confirmed (code): `app/platforms/preflight.py`, `_MACOS_PROBES` has two entries. | Refuse the microphone box on a real Mac. Start Odicto and dictate. Record what the indicator and `odicto run status` say. | Add a microphone check (AVFoundation authorization status). A probe with severity `error` shows nothing: see M13. Solve M13 first, then give the microphone the same visible path. |
| M7 | The name in the permission lists is not "Odicto". A start from Terminal makes macOS ask for **Terminal**. A start from the login item runs `python` from the release folder, so macOS asks for a different program, with a name the person does not know. | To verify. Record the exact name in each of the three lists for (a) a start from Terminal and (b) a start at login. | Real Mac, both starts, screenshots of the three lists. | A small `Odicto.app` bundle in `~/Applications` with a stable path and bundle id, whose executable starts the stable launcher. Then the lists say "Odicto", and Launchpad and Spotlight can start it (also closes M9). Check that consent follows the bundle and not the Python file inside it. |
| M8 | Each update makes a new release folder with a new Python environment. macOS can then treat Odicto as a new program and ask for all permissions again. The README says an update "can require renewing OS permissions". | To verify: how often, and for which permission. | Real Mac: grant all three, run `odicto update` to a newer release (or stage a test release), dictate again. | The stable `Odicto.app` of M7, signed with a stable identity (D1). An ad-hoc signature may not keep consent across updates; measure it. |
| M9 | Nothing in Launchpad, the Dock or Spotlight starts Odicto, stops it or opens Setup. Each needs a terminal. | Confirmed (code): `manager.py` makes shortcuts on Windows only (`install_shortcuts`). | Search "Odicto" in Spotlight after an install. | The `Odicto.app` of M7. Check that the menu-bar icon shows and has **Quit Odicto** (`app/indicator.py` makes a tray icon when the system has a tray). Add **Open Setup** to that menu if it is missing. |
| M10 | CI installs PortAudio with Homebrew before the smoke test. A Mac without Homebrew is not proved. | Confirmed (CI): `.github/workflows/ci.yml`, "Install macOS audio dependency". To verify: does the `sounddevice` wheel carry its own PortAudio on both Mac architectures. | Remove that CI step on a branch and run CI. Also run `python -c "import sounddevice; print(sounddevice.query_devices())"` in a clean VM without Homebrew. | When the wheel is enough: delete the CI step, so CI matches a real user. When it is not: ship or install PortAudio in the bootstrap. |
| M11 | A clean Mac has no Xcode Command Line Tools. A script that calls `git`, `python3`, `make` or `cc` opens Apple's "install developer tools" box. | To verify. The bootstrap seems to use only `bash`, `curl`, `mktemp`, `uname`, `id` and `sh`. | Run the full acceptance journey in a clean VM and watch for that box. Search the POSIX scripts for such calls. | Remove the call. |
| M13 | A Mac without Accessibility or Input Monitoring consent cannot start Odicto at all. `main.py` prints `Cannot start Odicto [...]` to the error stream and exits with 2 before it makes the indicator. The probes only read the state (`AXIsProcessTrusted`, `IOHIDCheckAccess`); they do not ask, so macOS may add no entry to the lists. A start at login shows nothing. The indicator labels **Allow Accessibility** and **Allow Input Monitoring** are then never seen. | Confirmed (code): `app/main.py`, `startup_blockers` and `sys.exit(2)`; `app/platforms/preflight.py`. To verify: what the person sees, and which entry appears in each list. | Real Mac, clean user: `odicto start` from Terminal and from the login item. Record the text, the lists and each box. | (a) Ask instead of only reading: `AXIsProcessTrustedWithOptions` with the prompt option and `IOHIDRequestAccess`, then open the right System Settings pane. (b) Let the app start far enough to show a window that names the missing permission and has a button to the pane, with no keyboard hook bound until consent exists. (c) A first-run page in Setup that walks through the three permissions and checks them again. This gap blocks acceptance steps 5 to 7. |
| M12 | The first dictation downloads the Whisper model. The person sees no progress. | To verify. | Clean Mac, first dictation, time it and record what the indicator shows. | Download the model in the installer (the Windows source installer warms Whisper), or show a state in the indicator. |

### Linux

| ID | Gap | Status | Check | Fix options |
| --- | --- | --- | --- | --- |
| L1 | Odicto refuses a Wayland session. Ubuntu, Debian (GNOME) and Fedora start a Wayland session by default. Recent Ubuntu releases may offer no Xorg session at all. If so, a default Ubuntu desktop cannot run Odicto. | Confirmed (code) for the refusal: `app/platforms/preflight.py` `_linux_wayland`, `app/platforms/_x11.py`. CI hides it: it sets `XDG_SESSION_TYPE=x11` on a virtual screen (`ci.yml`). To verify: which target systems still offer an Xorg session. | On each Linux target system: read `echo $XDG_SESSION_TYPE` after a default login, and list the session choices on the login screen. | This is the largest gap. (a) Where an Xorg session exists: tell the person how to choose it (the guide does). (b) Where none exists: Odicto cannot run until it has a Wayland backend. That is a feature, not an installer fix: global shortcuts and text input through the desktop portals or `uinput`, clipboard through `wl-clipboard`. Owner decision D2. |
| L2 | The bootstrap checks the session type only when Odicto starts. A Wayland user gets the password prompt, the system packages and the full install, and learns at the first start that Odicto cannot run. | Confirmed (code): `bootstrap.sh` has no session check. | Run the bootstrap in a Wayland session. | Phase 3a: check `XDG_SESSION_TYPE` and `WAYLAND_DISPLAY` at the top of the bootstrap. Say what to do and stop before `sudo`. |
| L3 | Only `apt` systems are supported. Fedora, Arch, openSUSE and others get a message and no install. | Confirmed (code): `bootstrap.sh`, the `apt-get` and `dpkg-query` check. | Read the script. | Per-family package lists (`dnf`, `pacman`, `zypper`). Owner decision D3. Each family needs its own clean-VM proof. |
| L4 | Install needs a terminal. A double-click on a `.sh` file in Files opens a text editor. | Confirmed (README) for the terminal. To verify for the double-click on each target desktop. | Double-click `install-managed.sh` in Files on each target system. | (a) One pasted line (`curl ... \| bash`, see M3). (b) A `.desktop` launcher or a `.run` file; check that the desktop lets a downloaded file run without a terminal step. (c) A `.deb` package (D3). |
| L5 | The installer does not start Odicto, open Setup or turn on start at login. | Confirmed (code). Same as M4. | Same as M4. | The POSIX easy installer of M4. |
| L6 | No entry in the application menu. Start, stop and Setup need a terminal. | Confirmed (code): `manager.py` writes shortcuts on Windows only; `managed_autostart.py` writes an autostart entry only. | Search "Odicto" in the Activities overview after an install. | Write `~/.local/share/applications/odicto.desktop`, `odicto-setup.desktop` and `odicto-stop.desktop` with an icon, with the ownership rules of the Windows shortcuts (keep an entry that Odicto did not write). |
| L7 | `~/.local/bin` is on `PATH` only after the next login on Ubuntu and Debian, because `~/.profile` adds it only when the folder exists at login. | To verify. | Clean install, new terminal, `odicto version`. Then log out, log in, repeat. | With L5 and L6 the person never needs the command. Else print the full path. |
| L8 | GNOME has no system tray by default. Ubuntu adds one with an extension; Debian's GNOME may not. Without a tray, nothing on the screen shows that Odicto runs, and **Quit Odicto** is not reachable. | To verify per target system. `app/indicator.py` skips the tray when the system has none. | Start Odicto on each target system and look for the icon. | The menu entries of L6 give start and stop without a tray. Say it in the guide. |
| L9 | The bootstrap needs `sudo`. An account without it stops with a message for an administrator. | Confirmed (code). | Read the script. | Accept. Keep the message. Put the package list in the guide for the administrator. |
| L10 | CI proves Ubuntu 26.04 and 26.10 only. The oldest system that works is not known (C library version of the wheels, package names such as `libxcb-cursor0`, Qt requirements). | Confirmed (CI). | Run Phase 0 on Ubuntu 24.04 and Debian 13. Optional: Ubuntu 22.04 and Debian 12. | State the oldest supported version in the docs. Make the bootstrap refuse an older one before `sudo`. |
| L11 | Sound on a real desktop uses PipeWire. CI uses PulseAudio with a null sink. The microphone path on PipeWire is not proved. | Confirmed (CI) for the difference. To verify for the result. | Real desktop with a microphone: `odicto run status` after start shows the input device and a level. | Add a missing package (for example a PipeWire ALSA or Pulse bridge) to the bootstrap list if the check fails. |
| L12 | ARM Linux (for example a Raspberry Pi 5 or an ARM laptop) is not tested. | To verify, low priority. | `uv pip sync --require-hashes` against the lock file on an ARM VM. | Out of scope unless the owner asks (D3). |

## 7. Phase 0 and Phase 1: baseline and gap proof

### Machines

- **Linux**: a local virtual machine made from the official desktop image, with a snapshot of the
  clean state. Give it a virtual microphone or pass a real one through. The Ubuntu 26.10 harness in
  `tools/ci_latest_linux.sh` shows how CI builds a guest; it uses a server image, so it has no
  desktop session and cannot prove L1, L4, L6 or L8.
- **macOS**: an agent has no clean Mac. Two ways exist. (1) GitHub-hosted macOS runners prove
  everything that needs no screen and no consent (M3, M10, M11, X3, X6). (2) The owner, or a
  tester the owner names, does the hand steps on a real Mac in a new user account
  (System Settings, Users & Groups, Add User). Write a checklist like the Windows one in
  `.agents/skills/verify-odicto/features/managed-install.md` ("Owner checklist: a fresh Windows user").

### Baseline table (fill one row per target system)

| System | Session type by default | Xorg session offered | Install command result | Install time | Size of the managed home | First start result | First dictation result | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | |

Steps for each row, on a clean snapshot or user:

1. Follow `docs/install.md` for that system exactly as written. Do not fix anything on the way.
2. Record each point where the written step does not match what the screen shows.
3. Record the full terminal text of the installer.
4. Run the managed-home command with `run status` after the start and record it.
5. Measure: time from the first command to "is active", and `du -sh` of the managed home.

Then do the **Check** of every gap in section 6 and set its status to `Confirmed` or `Rejected`
with one line of evidence. Commit the filled register to this file.

**Exit check for Phase 1**: no gap has the status `To verify`.

## 8. Phase 2 and Phase 3: decisions and fixes

### Owner decisions (ask once, together, after Phase 1)

Give the owner the Phase 1 evidence with each question. These are not the agent's to decide.

| ID | Question | Why the owner | Default when the owner does not answer |
| --- | --- | --- | --- |
| D1 | Buy an Apple Developer membership (yearly fee) to sign and notarize a Mac installer and app? | Money, and the owner's own Apple account. | No. Use the pasted one-line install and an unsigned `Odicto.app` made on the user's machine. |
| D2 | Build a Wayland backend, or support Xorg sessions only and say so? | Large feature; changes the hook layer that the single-instance rules protect. | Xorg only. The installer and the guide say it before anything installs. |
| D3 | Which Linux families beyond Ubuntu and Debian? Package format (`.deb`, Flatpak, AppImage) or script only? | Scope and long-term upkeep. | Ubuntu and Debian, script only. |
| D4 | Keep the zip as a user download, or make it a developer download and give each system one small installer asset? | Changes the public release layout. | Give each system one installer asset. Keep the zip for the updater and for developers. |

### 3a. Installer tells the truth early (no decision needed)

- L2: session check before `sudo`.
- L10: version check before `sudo`, when Phase 1 found a floor.
- X3: a plain message for a GitHub limit or network error.
- X6: a printed command that works when pasted.
- M10: remove the Homebrew step from CI when Phase 1 proved the wheel is enough.

Acceptance: a test in `tests/` for each message (the bootstrap can run with a fake `uname`,
`apt-get` and environment, as `tests/test_easy_install.py` does with a fake bootstrap for Windows).

### 3b. One-step entry for macOS (M1, M2, M3)

Default design, when D1 is "no":

1. The Release workflow publishes `install-odicto.sh` as a release asset: `bootstrap.sh` unchanged,
   plus the after-install steps of 3d. Build it with a script next to
   `tools/distribution/build_easy_installer.py`, so the bootstrap stays one source.
2. The README gives one line to paste in Terminal:
   `curl -fsSL https://github.com/Gargeya-Grey/Odicto/releases/latest/download/install-odicto.sh | bash`
3. The script is safe when piped (M3) and safe when run two times.

When D1 is "yes": a notarized package that runs the same script, and a **Download for macOS** link.

Acceptance: section 4, steps 1 to 5, on the three macOS targets.

### 3c. One-step entry for Ubuntu and Debian on X11 (L4)

The same `install-odicto.sh` and the same pasted line. The guide says how to open a terminal
(Ctrl+Alt+T). When Phase 1 shows that a double-click launcher works on the target desktops
without a terminal step, add it as a second way.

Acceptance: section 4, steps 1 to 5, on each Linux target that offers an Xorg session.

### 3d. After-install parity with Windows (M4, M7, M9, M13, L5, L6, M6, M12)

1. The easy installer turns on start at login for a new install, starts Odicto, and opens Setup.
   It keeps the choice of an existing install. It starts nothing when a source copy's startup
   entry exists (`~/Library/LaunchAgents/com.odicto.plist`, `~/.config/autostart/odicto.desktop`;
   see `app/odicto.py`), unless a finished migration receipt exists. A startup entry is not enough:
   a source copy that was started by hand has none, and a copy older than 1.5.0 does not take the
   session hook lock. So before any start, the installer also looks for a running owner (a process
   that runs an Odicto `main.py` outside the managed home) and starts nothing when it finds one.
   When it cannot tell, it asks the person to confirm that no other Odicto runs. Mirror each rule
   and each test of the Windows installer. The Windows installer reads two shortcuts only and has
   the same hole; give it the same check.
2. The manager's `install` writes app-menu entries on Linux and an `Odicto.app` on macOS, as it
   writes Start Menu shortcuts on Windows. Same ownership rule: keep what Odicto did not write.
3. macOS: a visible way to grant the permissions on a first start (M13), then the microphone (M6).
   Permission texts name the real entry that the person must turn on (M7).
4. First-run model download has a visible state or happens in the installer (M12).
5. `odicto uninstall` (X4).

Acceptance: section 4, steps 5 to 10.

### 3e. Approved scope work

Only what D1 to D4 approved. Each item gets its own plan, branch and pull request.
A Wayland backend (D2) needs a design document with its failure cases before any code.

### Order and size

Do 3a first: it is small, and it helps users today. 3b and 3c share one script. 3d is the largest
part without a decision. Open one pull request per sub-phase. Do not mix 3e into them.

## 9. Phase 4: proof

For each target system, the acceptance journey of section 4, by a person's hands, on a clean
user or machine, from the published release asset (not from a branch build).

Evidence to keep, per system, in `outputs/handoffs/` (ignored by Git) and summarized in the
pull request:

- the filled journey table (step, pass or fail, time);
- the installer's full text;
- `odicto run status` after the first start and after the next login;
- screenshots of each consent box and each permission list (macOS);
- one line from the person: "I dictated a sentence and it appeared."

Automated proof to add to CI, so the path does not break later:

- `install-odicto.sh` runs from a pipe on every macOS and Linux runner, into an isolated home
  (`--home`), with the after-install steps replaced by fakes;
- the Linux job fails when the bootstrap continues in a Wayland environment;
- the macOS jobs run without the Homebrew PortAudio step (after M10).

CI cannot prove consent boxes, a real microphone or real typing. Do not write that it does.

## 10. Phase 5: documents

Update together, in the pull request that changes the behaviour:

- `docs/install.md`: the user guide. Remove the sentence that says nobody tested macOS by hand
  when Phase 4 has passed there.
- `README.md`: one download line per system at the top, as Windows has now.
- `tools/release/release-notes.md` and `tools/distribution/README.md`.
- `.agents/skills/verify-odicto/features/managed-install.md`: an owner checklist for a fresh Mac
  user and a fresh Linux user, in the shape of the Windows one.
- `AGENTS.md`, when an installer entry point or flag changes.
- This file: set each gap to `Closed` with the pull request number.

## 11. Report format for the agent

End each working session with:

1. The phase you are in and its exit check: met or not.
2. The gap register rows that changed, with the evidence.
3. What a person must do next with their hands, as a numbered list.
4. Open owner decisions, word for word.
5. Checks run and their results. Anything skipped, with the reason.
