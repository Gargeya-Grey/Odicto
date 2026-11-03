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
