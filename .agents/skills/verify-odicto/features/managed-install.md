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
`odicto.cmd` (order autostart, start, setup; exit codes) and writes real shortcuts in a temp
Programs folder. A real install from the published asset needs a fresh Windows user or machine,
with the owner's hands: download it, pass SmartScreen, and confirm the Start Menu items, the
HUD after start, and Setup in the browser. Never run it on a machine that runs Odicto for real.

### Owner checklist: a fresh Windows user

Use a new local Windows account (Settings, Accounts, Other users) or a Windows Sandbox with a
microphone. No Odicto, Git or Python is installed there.

1. Open the README's **Download for Windows** link. Expect `Install-Odicto.cmd` (about 10 KB).
   Keep the file when the browser asks.
2. Double-click it. Expect "Windows protected your PC". Click **More info**, then **Run anyway**.
3. Expect a console window with "Step 1 of 3" to "Step 3 of 3" and no question. At the end,
   expect "Odicto is ready.", the Odicto icon near the clock, Setup in the browser, and "Press
   any key to close this window." The HUD is not visible while Odicto waits; that is correct.
4. Dictate in Notepad: tap Ctrl+\`. Expect the HUD at the bottom of the screen. Speak, tap
   Ctrl+\` again, and expect the words at the cursor.
5. Start Menu: expect the folder **Odicto** with Odicto, Odicto Setup and Stop Odicto, each with
   the Odicto logo. Stop Odicto removes the icon near the clock; Odicto brings it back (dictate
   once more); Odicto Setup opens Setup.
6. Task Manager, Startup apps: expect "Odicto managed", enabled. Sign out and in: the icon near
   the clock returns, and dictation works.
7. Repair: double-click `Install-Odicto.cmd` again. Expect the same three steps, one running
   Odicto (type a sentence; no letter twice), and the start-at-sign-in choice unchanged.
8. Optional, needs a source checkout of 1.5.0 or later in the same account. First prove that the
   checkout starts by itself: Stop Odicto, run its `start_dictation.bat`, dictate once, then run its
   `stop_dictation.bat`. Then open Odicto (the managed copy) and run `start_dictation.bat` again.
   Expect a start failure, the line `Could not acquire Odicto single-instance lock` at the end of
   the checkout's `dictation.log`, and no letter twice while you type. A failure without that line
   does not prove the lock.
9. Clean up: open Stop Odicto, then delete the test account. Its files go with it.
