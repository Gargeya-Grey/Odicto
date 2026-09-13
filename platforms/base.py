"""Shared platform-agnostic helpers.

The heavy lifting lives in the per-OS modules (``windows.py``, ``macos.py``,
``linux.py``); this module only holds functions that do not import OS-specific
libraries such as ``keyboard``, ``pynput``, ``ctypes.windll`` or ``fcntl``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

import pyperclip


def install_root() -> str:
    """Absolute path to the Odicto install directory (repo root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_digest() -> str:
    """Stable per-install digest used to scope OS single-instance names."""
    return hashlib.sha256(
        os.path.normcase(os.path.normpath(install_root())).encode("utf-8")
    ).hexdigest()[:16]


def pid_file_path() -> str:
    return os.path.join(install_root(), "dictation.pid")


def lock_file_path() -> str:
    return os.path.join(install_root(), "dictation.lock")


def clipboard_read() -> str:
    try:
        value = pyperclip.paste()
        return value if isinstance(value, str) else ("" if value is None else str(value))
    except Exception:
        return ""


def clipboard_write(text: Optional[str]) -> bool:
    try:
        pyperclip.copy(text if text is not None else "")
        return True
    except Exception:
        return False


# --- Terminal detection -------------------------------------------------
# There is no universal paste chord: Windows Terminal and conhost take Ctrl+V,
# mintty/Git Bash default to Shift+Insert, most Linux terminals use
# Ctrl+Shift+V, macOS uses Cmd+V — and inside a TUI (vim, an agent CLI, an SSH
# session) the foreground app claims Ctrl+V outright. Typing the characters is
# the only injection that works everywhere, so Odicto types instead of pasting
# when the focused window is one of these. Plain Ctrl+C is SIGINT in a
# terminal, so the copy chord is swapped too.
#
# Matching is by window class, process image name, or macOS bundle id, all
# compared lowercased. EXTRA_TERMINAL_APPS extends the set per install.

TERMINAL_WINDOW_CLASSES: frozenset = frozenset(
    {
        "consolewindowclass",  # conhost: cmd, PowerShell, wsl.exe
        "cascadia_hosting_window_class",  # Windows Terminal
        "mintty",  # Git Bash / Cygwin / MSYS2
        "putty",
        "alacritty",
        "wezterm",
        "org.wezfurlong.wezterm",
        "kitty",
        "tabby",
        "hyper",
        "virtualconsole",  # ConEmu
        "xterm",
        "x-terminal-emulator",
        "konsole",
        "tilix",
    }
)

TERMINAL_PROCESS_NAMES: frozenset = frozenset(
    {
        # Windows
        "windowsterminal.exe",
        "wt.exe",
        "conhost.exe",
        "openconsole.exe",
        "mintty.exe",
        "putty.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "wezterm.exe",
        "kitty.exe",
        "conemu.exe",
        "conemu64.exe",
        "cmder.exe",
        "tabby.exe",
        "hyper.exe",
        # Linux
        "gnome-terminal",
        "gnome-terminal-server",
        "kgx",
        "konsole",
        "xfce4-terminal",
        "tilix",
        "terminator",
        "alacritty",
        "kitty",
        "wezterm",
        "xterm",
        "x-terminal-emulator",
        "urxvt",
        "foot",
        # macOS
        "terminal",
        "iterm2",
        "iterm",
        "warp",
        "ghostty",
    }
)

TERMINAL_MAC_BUNDLE_IDS: frozenset = frozenset(
    {
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "dev.warp.warp-stable",
        "dev.warp.warp",
        "com.github.wez.wezterm",
        "net.kovidgoyal.kitty",
        "io.alacritty",
        "co.zeit.hyper",
        "org.tabby",
        "com.mitchellh.ghostty",
    }
)


def is_terminal_identifier(identifiers, extra=()) -> bool:
    """True when any window class / process name / bundle id is a terminal.

    Args:
        identifiers: Candidate names for the focused window, any case.
        extra: Additional names (``EXTRA_TERMINAL_APPS``) that also count.
    """
    extra_set = {str(e).strip().lower() for e in extra or () if str(e).strip()}
    for raw in identifiers:
        if not raw:
            continue
        name = str(raw).strip().lower()
        if not name:
            continue
        if (
            name in extra_set
            or name in TERMINAL_WINDOW_CLASSES
            or name in TERMINAL_PROCESS_NAMES
            or name in TERMINAL_MAC_BUNDLE_IDS
        ):
            return True
        # Windows process names carry ".exe"; POSIX ones usually do not.
        stem = name.rsplit(".", 1)[0]
        if stem in extra_set or stem in TERMINAL_PROCESS_NAMES:
            return True
    return False
