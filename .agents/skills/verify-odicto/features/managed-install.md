# Managed installation and updates

Run bootstrap only on a fresh disposable machine or an explicitly designated new
managed home. It installs without starting dictation. Inspect the current source
and managed ownership state before migration; a source checkout may have been
retired and must not be assumed to be the active runtime.
Use migration apply, UAC, `odicto setup` and `odicto start` only with the owner's
hands on their computer. A read-only migration check may be run by the agent.

## Source-to-managed journey

1. Update the source checkout to a version with migration guards. Install through
   root `install-managed.ps1` / `install-managed.sh` into the intended managed home.
2. Run `odicto migrate --source SOURCE --check`. Keep the report free of configuration
   values. Source configuration files are opaque; never inspect API keys or prompt text.
3. The owner quits the source app and closes source Setup, then runs the same
   migration command without `--check`. Unknown startup ownership, configured
   destination conflicts and live leases must block without overwriting them.
4. Verify the reciprocal source/destination receipts and the preserved configuration
   fingerprints. Retained startup/task recovery evidence lives under the managed home.
5. The owner runs the installed `odicto setup` / `odicto start`. Confirm the health
   file names the active managed code root, and one managed owner holds the stable
   data-root lock. Run read-only doctor with active release `-Root` and stable
   `-DataRoot`; run `log_report.py --root` against that data directory.
6. Drive raw dictation and F8 Polish with the owner's hands. Require the owner to
   confirm the text/result card, not just a healthy PID or green synthetic test.

## Private-directory permissions (Windows)

Elevated writes (admin receipts, launch results, the admin marker, PID, health, logs and
migration backups) are owned by Administrators. The same account reads them only through
an explicit grant on the directory. Prove it with the isolated tests:
`python -m unittest tests.test_managed_distribution.TestPrivateDirectoryPolicy tests.test_managed_distribution.TestWindowsPrivateDirectoryAcl`.
On an owner's managed home, a read-only check is allowed; it changes nothing:

```powershell
$managed = Join-Path $env:LOCALAPPDATA 'Odicto'
foreach ($p in @($managed, (Join-Path $managed 'data'), (Join-Path $managed 'releases'))) {
    [IO.Directory]::GetAccessControl($p, 'Access').GetSecurityDescriptorSddlForm('Access') }
```

Expect each protected `D:P` list to hold `(A;OICI;FA;;;<account SID>)` beside SY, BA and OW,
and no WD, AU or BU entry. A file an elevated process wrote shows an inherited
`(A;ID;FA;;;<account SID>)`. Do not use `$HOME` or `$home` as a variable name; PowerShell
reserves it. The tests make no Administrators-owned file. Only an owner-run elevated
action (UAC) proves a real elevated write is readable afterwards.

Admin mode must use a stable, owned elevation task and the same managed launch
reservation/readiness protocol as ordinary startup. Login startup uses one owned
entry. Synthetic adapters and PowerShell parsing cannot establish real UAC,
privileged task launch or elevated-window dictation acceptance.

CI drives `tools/ci_platform_smoke.py` from a committed ZIP. It runs the source
installer twice, checks preservation of fixture settings, imports native libraries,
renders Qt, opens Setup HTTP without opening a browser, and runs the full test gate.
`tools/ci_managed_smoke.py` stages two real virtual environments in final directories,
activates them, holds the actual instance and Setup lock protocols to prove deferral,
rolls back, and checks exact fixture settings bytes. Its future-version ZIP is a
VERSION-only fixture, not a second published release.

Evidence is uploaded per OS as `platform.json`, installer/test logs and
`managed-update.json`. Linux additionally drives real key events against an isolated
Xvfb editor with `tools/x11_integration_check.py`. The Ubuntu VM records its actual
kernel and OS release. Publication requires all six jobs at the exact main commit.

These checks do not grant macOS permissions, record physical microphone audio,
establish Wayland support, or replace an owner-confirmed real dictation journey.

## Double-click installer and Start Menu (Windows)

`tests/test_easy_install.py` runs a built `Install-Odicto.cmd` with a fake bootstrap and a fake
`odicto.cmd` (order autostart, start, setup; exit codes; `wait-ready` answers 0, 20, 21 or 1) and
writes real shortcuts in a temp Programs folder. A real install from the published asset needs a fresh Windows user or machine,
with the owner's hands: download it, pass SmartScreen, and confirm the Start Menu items, the
HUD after start, and Setup in the browser. Never run it on a machine that runs Odicto for real.

### When Odicto waits

The install downloads the speech model in the step "Getting the speech model" (the first start
then has nothing to download). `odicto start` succeeds when Odicto runs, even if it still waits.
It then prints one line that says what for: no microphone, a speech model that did not load, or a
model that still downloads. The installer asks once more, and in that case ends with a yellow
"Odicto is running. It is not ready to dictate yet." instead of "Odicto is ready.". Setup still
opens and the exit code stays 0. `odicto run status` prints a `Start-up:` line that names the
stage and the last failed step. An update that activates a new release is stricter: its first
start must reach full readiness, or the old release comes back. To prove the waiting path on a
fresh user, start with no microphone (disable it in Device Manager) and expect the yellow
summary and the notice; enable the microphone and expect dictation to work without a restart.

### Owner checklist: a fresh Windows user

Use a new local Windows account (Settings, Accounts, Other users) or a Windows Sandbox with a
microphone. No Odicto, Git or Python is installed there.

1. Open the README's **Download for Windows** link. Expect `Install-Odicto.cmd` (about 10 KB).
   Keep the file when the browser asks.
2. Double-click it. Expect "Windows protected your PC". Click **More info**, then **Run anyway**.
3. Expect a console window with "Step 1 of 3" to "Step 3 of 3" and no question. At the end,
   expect "Odicto is ready.", the Odicto icon near the clock, Setup in the browser, and "Press
   any key to close this window." The HUD is not visible while Odicto waits; that is correct.
4. Dictate in Notepad, or in the browser's address bar (Windows Sandbox has no Notepad): tap Ctrl+\`. Expect the HUD at the bottom of the screen. Speak, tap
   Ctrl+\` again, and expect the words at the cursor.
5. Start Menu: expect the folder **Odicto** with Odicto, Odicto Setup and Stop Odicto, each with
   the Odicto logo. Stop Odicto removes the icon near the clock; Odicto brings it back (dictate
   once more); Odicto Setup opens Setup.
6. Task Manager, Startup apps: expect "Odicto managed", enabled. Sign out and in: the icon near
   the clock returns, and dictation works.
7. Repair: double-click `Install-Odicto.cmd` again. Expect the same three steps, one running
   Odicto (type a sentence; no letter twice), and the start-at-sign-in choice unchanged.
8. Optional, needs a fresh clone of 1.5.0 or later in the same account that was never migrated
   (a migrated checkout forwards its launchers to the managed copy), installed with
   `install.ps1 -NoShortcuts` (without the flag it takes over the Start Menu items). Call the
   managed launcher by its path, never through a shortcut: `M` = `%LOCALAPPDATA%\Odicto\bin\odicto.cmd`.
   First prove that the checkout starts by itself: `M stop`, run the checkout's
   `start_dictation.bat`, and confirm in Task Manager (Details, Command line column) that a
   `pythonw.exe` runs the checkout's `main.py`, not one under `%LOCALAPPDATA%\Odicto`. Dictate
   once, then run its `stop_dictation.bat`. Then run `M start` and
   `M run status`, and confirm the managed owner runs. Run `start_dictation.bat` again. Expect a
   start failure, the line `Could not acquire Odicto single-instance lock` at the end of the
   checkout's `dictation.log`, and no letter twice while you type. A failure without that line,
   or with no managed owner running, does not prove the lock.
9. Clean up: open Stop Odicto, then delete the test account. Its files go with it.

## One-line installer (macOS and Linux)

`tests/test_posix_easy_install.py` runs a built `install-odicto.sh` from a pipe (`cat | bash`) with
fake `uname`, `curl`, `uv`, `sudo`, `apt-get`, `dpkg-query` and `ps`, and a fake managed `odicto`
(order autostart, start, the readiness check, setup; exit codes; a source copy that blocks the start).
CI builds the file and pipes it into `bash` on every macOS and Linux job. A real install needs a
clean machine and the owner's hands: grant the Mac permissions, dictate, and sign out and in.
Never run it on a machine that runs Odicto for real.

### Owner checklist: a Mac (for example a new Mac Studio)

Use a new macOS user (System Settings, Users & Groups) with a microphone. No Odicto, Homebrew,
Git or Python is needed.

1. Open Terminal (Command + Space, `Terminal`, Return). Paste the line from the README and press
   Return. Expect "Step 1 of 3" and no question.
2. Expect macOS to ask for Accessibility and for Input Monitoring, and System Settings to open on
   the pane. Turn on the entry it names (Terminal, or `python3.12`), for both permissions.
3. Paste the line again. Expect "Odicto is ready." (or "not ready to dictate yet" while a microphone
   prompt is open: click Allow), and Setup in the browser.
4. Dictate in TextEdit: tap Control+\`, speak, tap Control+\` again. Expect the words at the cursor
   and the HUD at the bottom of the screen.
5. Sign out and in. Expect Odicto to run again without Terminal (the LaunchAgent
   `com.odicto.managed`), and dictation to work. This is the first proof that the permissions hold
   for a start at sign-in.
6. `"$HOME/Library/Application Support/Odicto/bin/odicto" update`. Expect "is up to date" or a working
   update, also when Terminal's PATH has no `~/.local/bin`.
7. Clean up: `odicto stop`, then delete the test user.

### Owner checklist: Linux (an Ubuntu 24.04 or Linux Mint machine or VM, Xorg session)

1. Log in with an Xorg session. Open a terminal, paste the README line. Expect one password
   question (for system packages), then "Step 1 of 3" to "Step 3 of 3" and Setup in the browser.
2. Dictate in the text editor: tap Ctrl+\`, speak, tap again. Expect the words at the cursor.
3. Log out and in. Expect Odicto to run again (`~/.config/autostart/odicto-managed.desktop`).
4. `~/.local/share/odicto/bin/odicto update` from the autostarted session: expect no
   "uv was not found".
5. Ubuntu 25.10 or newer (no Xorg session): expect the installer to stop before the password
   question with "Nothing was installed" and the reason.

### WSL (testing only)

WSLg runs an X server beside its Wayland one, so Odicto runs in WSL with `DISPLAY` set. It types
only into Linux X11 apps, and its keys work only while a Linux window has focus. The installer
adds `libasound2-plugins`, without which PortAudio has no microphone. Measured on Ubuntu 24.04 in
WSL (16 Nov 2026): install, start, `wait-ready` ("microphone receiving audio"), stop, and an update
from 1.5.0 to 1.5.1.
