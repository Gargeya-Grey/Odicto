"""Startup environment checks that explain silent hotkey/paste failures.

Stdlib only; OS-specific imports are lazy so this module imports everywhere.
``environment_problems()`` never raises and never blocks for long: every
subprocess call (none today) must use a 0.5 s timeout. Each probe is isolated:
a probe that raises is skipped, the others still report.
"""

from __future__ import annotations

import os
import importlib.util
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    severity: str  # "error": hotkeys/paste cannot work; "warning": degraded
    code: str
    message: str  # one plain sentence that includes the fix


# ---- Linux probes: each returns a list of Problems --------------------------

def _linux_x11() -> list[Problem]:
    if os.environ.get("DISPLAY"):
        return []
    return [Problem(
        "error", "linux_missing_display",
        "No X11 desktop session is available; log into an X11 session and launch Odicto "
        "as your normal desktop user, without sudo.",
    )]


def _linux_xlib() -> list[Problem]:
    if importlib.util.find_spec("Xlib") is not None:
        return []
    return [Problem("error", "linux_missing_xlib",
                    "python-xlib is missing; rerun the Odicto installer.")]


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


def running_in_wsl(env=None) -> bool:
    """True inside Windows Subsystem for Linux.

    Reads the two variables that WSL's init sets for every process of the distribution.
    A file check (``/proc/sys/fs/binfmt_misc/WSLInterop``) is left out on purpose: it
    would also be true for a test that clears the environment to model a plain desktop.
    """
    env = os.environ if env is None else env
    return bool(env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"))


def wayland_blocks_x11(env=None) -> bool:
    """True when the session is Wayland and no X server is reachable for Odicto.

    WSLg sets ``WAYLAND_DISPLAY`` but always runs an X server too (``DISPLAY``), so a WSL
    session with ``DISPLAY`` is not blocked. A real Wayland desktop still is.
    """
    env = os.environ if env is None else env
    if env.get("XDG_SESSION_TYPE", "").lower() != "wayland" and not env.get("WAYLAND_DISPLAY"):
        return False
    return not (running_in_wsl(env) and bool(env.get("DISPLAY")))


WAYLAND_ADVICE = (
    "If your login screen offers an Xorg (X11) session, choose it and sign in again. "
    "Ubuntu 25.10 and newer have none, and Odicto does not support them yet."
)


def _linux_wayland() -> list[Problem]:
    if not wayland_blocks_x11():
        return []
    return [Problem(
        "error", "wayland_session",
        "Wayland is not supported by Odicto's X11 global input backend. " + WAYLAND_ADVICE,
    )]


def _linux_wsl() -> list[Problem]:
    if not running_in_wsl():
        return []
    return [Problem(
        "warning", "wsl_testing_only",
        "Odicto in WSL is for testing. Its shortcuts work only while a Linux window has focus, "
        "and it types only into Linux X11 apps, not into Windows apps. "
        "Use the Windows installer for Windows apps.",
    )]


def _linux_clipboard() -> list[Problem]:
    if any(shutil.which(tool) for tool in ("xclip", "xsel")):
        return []
    return [Problem(
        "error", "linux_missing_clipboard_tool",
        "No X11 clipboard tool found; install xclip or xsel, "
        "for example 'sudo apt install xclip'.",
    )]


def _linux_xdotool() -> list[Problem]:
    if shutil.which("xdotool"):
        return []
    return [Problem(
        "error", "linux_missing_xdotool",
        "xdotool is missing, so text insertion and focused-window checks cannot work; "
        "install it, for example 'sudo apt install xdotool'.",
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


def _macos_microphone() -> list[Problem]:
    """Warn when macOS has already refused the microphone; never ask for it.

    "Not determined" says nothing: the first capture shows the system prompt.
    """
    from platforms import macos

    status = macos.audio_authorization_status()
    if status == macos.AUTH_DENIED:
        return [Problem(
            "warning", "macos_microphone",
            "Microphone access is off, so Odicto would record silence; open System Settings > "
            "Privacy & Security > Microphone, turn on the switch for your terminal or Python, "
            "then start Odicto again.",
        )]
    if status == macos.AUTH_RESTRICTED:
        return [Problem(
            "warning", "macos_microphone",
            "A system rule (for example Screen Time or a device profile) blocks the microphone "
            "for Odicto; ask the person who manages this Mac to allow it.",
        )]
    return []


def macos_microphone_problem() -> "Problem | None":
    """The refusal of the microphone by macOS, or None (another system, not asked yet, allowed, or a
    status that cannot be read). A refused microphone records silence, so start-up waits on it."""
    if sys.platform != "darwin":
        return None
    try:
        found = _macos_microphone()
    except Exception:
        return None
    return found[0] if found else None


_LINUX_PROBES = (_linux_x11, _linux_xlib, _linux_root_env, _linux_wayland, _linux_wsl,
                 _linux_clipboard, _linux_xdotool, _linux_portaudio)
_MACOS_PROBES = (_macos_accessibility, _macos_input_monitoring, _macos_microphone)

# Problem code -> (System Settings pane, switch name).
_MACOS_CONSENT = {
    "macos_accessibility": ("accessibility", "Accessibility"),
    "macos_input_monitoring": ("input_monitoring", "Input Monitoring"),
}


def guide_to_consent(problems, out=None) -> list[str]:
    """macOS: for each missing permission, ask macOS to show its prompt and open the pane.

    Called only on the way out of a refused start, never from ``environment_problems()``
    (which setup and status call often). Every call is best effort and never raises, so
    the caller's exit message always follows. Binds no hook. Returns the codes acted on.
    """
    if sys.platform != "darwin":
        return []
    acted: list[str] = []
    try:
        from platforms import macos

        write = out if out is not None else (lambda line: print(line, file=sys.stderr, flush=True))
        program = macos.responsible_program_name()
    except Exception:
        return acted
    for problem in problems:
        entry = _MACOS_CONSENT.get(getattr(problem, "code", None))
        if entry is None:
            continue
        pane, switch = entry
        acted.append(problem.code)
        opened = False
        try:
            if pane == "accessibility":
                macos.request_accessibility_prompt()
            else:
                macos.request_input_monitoring_prompt()
        except Exception:
            pass
        try:
            opened = bool(macos.open_privacy_pane(pane))
        except Exception:
            pass
        try:
            where = (f"System Settings opened at Privacy & Security > {switch}." if opened
                     else f"Open System Settings > Privacy & Security > {switch}.")
            write(f"{where} Turn on the switch for {program}, then start Odicto again.")
        except Exception:
            pass
    return acted


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
