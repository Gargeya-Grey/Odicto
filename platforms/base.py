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
