"""Opt-in always-admin Odicto on one Windows install.

A Medium-integrity process cannot hook keys typed into an elevated window (an
Administrator Windows Terminal), so on an install that opted in, the lifecycle
runs elevated. Opt-in is the gitignored marker file ``.odicto-admin-enabled`` in the install
root, written by ``scripts/windows/start_dictation_admin.ps1 -Action Configure``
after the logon task and Startup shortcut are in place. No marker, no change.

``run_if_needed`` only decides and delegates. The PowerShell helper owns UAC,
the ownership-validated stop and the start, so Python never calls the shell-execute API.
Stdlib only; it must stay importable before any other Odicto module.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from typing import Optional

MARKER_NAME = ".odicto-admin-enabled"
HELPER_PARTS = ("scripts", "windows", "start_dictation_admin.ps1")
ACTIONS = ("Start", "Stop", "WaitReady", "Configure")

_CREATE_NO_WINDOW = 0x08000000


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_user_an_admin() -> bool:
    """Whether this process holds an elevated Administrator token."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _powershell_exe() -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if not system_root or not os.path.isabs(system_root):
        raise RuntimeError("Cannot locate Windows PowerShell: SystemRoot is not set.")
    return os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def marker_path(root) -> str:
    return os.path.join(os.path.abspath(os.fspath(root)), MARKER_NAME)


def run_if_needed(root, action: str = "Start", timeout: Optional[float] = None) -> Optional[int]:
    """Run ``action`` through the elevated helper when this install needs it.

    Returns None when the caller should carry on unchanged: not Windows, no marker,
    or already elevated. Otherwise returns the helper's exit code. A marker that
    cannot be honoured (admin state unknown, helper or PowerShell missing) raises
    RuntimeError, so an opted-in install never silently runs without elevation.
    """
    if action not in ACTIONS:
        raise ValueError("unknown admin action: %r" % (action,))
    if not _is_windows():
        return None
    root = os.path.abspath(os.fspath(root))
    if not os.path.isfile(marker_path(root)):
        return None
    try:
        elevated = _is_user_an_admin()
    except Exception as error:
        raise RuntimeError(
            "Admin mode is enabled (%s) but the elevation state could not be read: %s"
            % (MARKER_NAME, error)) from error
    if elevated:
        return None

    helper = os.path.join(root, *HELPER_PARTS)
    if not os.path.isfile(helper):
        raise RuntimeError("Admin mode is enabled (%s) but the helper is missing: %s"
                           % (MARKER_NAME, helper))
    command = [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", helper, "-Action", action]
    if timeout is not None:
        command += ["-TimeoutSeconds", str(int(math.ceil(max(0.0, float(timeout)))))]
    try:
        completed = subprocess.run(
            command, cwd=root, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
    except OSError as error:
        raise RuntimeError("Admin mode is enabled (%s) but PowerShell could not run: %s"
                           % (MARKER_NAME, error)) from error
    return completed.returncode
