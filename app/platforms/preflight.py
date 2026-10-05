"""Startup environment checks that explain silent hotkey/paste failures.

Stdlib only; OS-specific imports are lazy so this module imports everywhere.
``environment_problems()`` never raises and never blocks for long: every
subprocess call (none today) must use a 0.5 s timeout. Each probe is isolated:
a probe that raises is skipped, the others still report.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    severity: str  # "error": hotkeys/paste cannot work; "warning": degraded
    code: str
    message: str  # one plain sentence that includes the fix


# ---- Linux probes: each returns a list of Problems --------------------------

def _linux_root() -> list[Problem]:
    if os.geteuid() == 0:
        return []
    return [Problem(
        "error", "linux_not_root",
        "The Linux keyboard backend needs root (membership of the input group is not enough); "
        "start Odicto as root with the session variables kept, for example "
        "'sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_RUNTIME_DIR,WAYLAND_DISPLAY,"
        "PULSE_SERVER,DBUS_SESSION_BUS_ADDRESS .venv/bin/python main.py'.",
    )]


def _linux_root_env() -> list[Problem]:
    if os.geteuid() != 0:
        return []
    missing = [k for k in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR") if not os.environ.get(k)]
    if not missing:
        return []
    return [Problem(
        "warning", "linux_root_session_env",
        f"Running as root without {', '.join(missing)}; Qt, audio and the clipboard may fail, "
        "so pass your session environment (sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_RUNTIME_DIR).",
    )]


def _linux_wayland() -> list[Problem]:
    env = os.environ
    if env.get("XDG_SESSION_TYPE", "").lower() != "wayland" and not env.get("WAYLAND_DISPLAY"):
        return []
    return [Problem(
        "warning", "wayland_session",
        "Wayland session: HUD placement, terminal detection and synthetic paste are limited and "
        "the clipboard needs wl-clipboard; an X11 session is the reliable target.",
    )]


def _linux_clipboard() -> list[Problem]:
    if any(shutil.which(tool) for tool in ("xclip", "xsel", "wl-copy")):
        return []
    return [Problem(
        "error", "linux_missing_clipboard_tool",
        "No clipboard tool found; install xclip (X11) or wl-clipboard (Wayland), "
        "for example 'sudo apt install xclip wl-clipboard'.",
    )]


def _linux_xdotool() -> list[Problem]:
    if shutil.which("xdotool"):
        return []
    return [Problem(
        "warning", "linux_missing_xdotool",
        "xdotool is missing, so the focused window cannot be detected (terminal typing and paste "
        "fall back to defaults); install it, for example 'sudo apt install xdotool'.",
    )]


def _linux_portaudio() -> list[Problem]:
    import ctypes.util

    if ctypes.util.find_library("portaudio") is not None:
        return []
    return [Problem(
        "error", "linux_missing_portaudio",
        "PortAudio is missing, so the microphone cannot open; install it, "
        "for example 'sudo apt install libportaudio2'.",
    )]


# ---- macOS probes -----------------------------------------------------------

def _macos_accessibility() -> list[Problem]:
    import ctypes
    import ctypes.util

    lib = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("ApplicationServices")
        or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    if bool(lib.AXIsProcessTrusted()):
        return []
    return [Problem(
        "error", "macos_accessibility",
        "Accessibility permission is missing, so paste and key synthesis do nothing; open System "
        "Settings > Privacy & Security > Accessibility, enable your terminal or Python, then restart Odicto.",
    )]


def _macos_input_monitoring() -> list[Problem]:
    import ctypes
    import ctypes.util

    iokit = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("IOKit") or "/System/Library/Frameworks/IOKit.framework/IOKit")
    try:
        check = iokit.IOHIDCheckAccess
    except AttributeError:
        return []  # symbol missing: unknown, do not nag
    check.restype = ctypes.c_uint32
    check.argtypes = [ctypes.c_uint32]
    if int(check(1)) == 0:  # kIOHIDRequestTypeListenEvent -> kIOHIDAccessTypeGranted
        return []
    return [Problem(
        "error", "macos_input_monitoring",
        "Input Monitoring permission is missing, so the global hotkeys never fire; open System "
        "Settings > Privacy & Security > Input Monitoring, enable your terminal or Python, then restart Odicto.",
    )]


_LINUX_PROBES = (_linux_root, _linux_root_env, _linux_wayland, _linux_clipboard,
                 _linux_xdotool, _linux_portaudio)
_MACOS_PROBES = (_macos_accessibility, _macos_input_monitoring)


def environment_problems() -> list[Problem]:
    """Return known blockers for this OS. Never raises; a failing probe is skipped."""
    if sys.platform.startswith("linux"):
        probes = _LINUX_PROBES
    elif sys.platform == "darwin":
        probes = _MACOS_PROBES
    else:
        return []
    problems: list[Problem] = []
    for probe in probes:
        try:
            problems.extend(probe())
        except Exception:
            continue
    return problems
