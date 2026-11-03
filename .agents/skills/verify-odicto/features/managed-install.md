# Managed installation and updates

Run bootstrap only on a fresh disposable machine or an explicitly designated new
managed home. The owner's source checkout remains the active install on this machine.
Use `odicto setup` and `odicto start` only with the owner's hands on their computer.

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
