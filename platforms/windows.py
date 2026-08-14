"""Windows platform backend.

This preserves the existing single-instance enforcement verbatim: a named
mutex plus an exclusive ``msvcrt`` byte-lock on ``dictation.lock``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional, Tuple

from platforms import base
from platforms._keyboard import *  # noqa: F401,F403

_INSTANCE_MUTEX_HANDLE: Optional[int] = None
_INSTANCE_LOCK_FILE = None
_INSTANCE_MUTEX_NAME: str = ""

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_ERROR_ALREADY_EXISTS = 183
_CREATE_NO_WINDOW = 0x08000000


def _mutex_names_for_install() -> Tuple[str, ...]:
    digest = base.install_digest()
    return (
        f"Global\\Odicto_SingleInstance_{digest}",
        f"Local\\Odicto_SingleInstance_{digest}",
    )


def _mutex_name_for_install() -> str:
    return _mutex_names_for_install()[0]


def _enumerate_odicto_pids(exclude_pid: Optional[int] = None) -> set:
    my_pid = os.getpid()
    if exclude_pid is None:
        exclude_pid = my_pid
    root_fwd = os.path.normcase(os.path.normpath(base.install_root())).replace("\\", "/")
    found: set = set()

    ps = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "ForEach-Object { "
        "  if ($_.CommandLine) { "
        "    Write-Output (($_.ProcessId).ToString() + \"`t\" + $_.CommandLine) "
        "  } "
        "}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout:
            return found
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            pid_s, cmd = line.split("\t", 1)
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == exclude_pid:
                continue
            cmd_n = os.path.normcase(cmd.replace('"', "").replace("\\", "/"))
            if "main.py" in cmd_n and root_fwd in cmd_n:
                found.add(pid)
    except Exception as e:
        print(f"Warning: process enum failed: {e}")

    return found


def kill_other_odicto_processes(pid_file: Optional[str] = None) -> list:
    my_pid = os.getpid()
    pids_to_kill: set = set()

    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                pids_to_kill.add(old_pid)
        except Exception as e:
            print(f"Warning: Could not read PID file: {e}")
        try:
            os.remove(pid_file)
        except Exception:
            pass

    pids_to_kill |= _enumerate_odicto_pids()

    killed: list = []
    for pid in sorted(pids_to_kill):
        print(f"Killing stale Odicto instance (PID {pid})...")
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
            killed.append(pid)
        except Exception as e:
            print(f"Warning: Could not kill PID {pid}: {e}")

    if killed:
        time.sleep(0.45)
    return killed


def _try_acquire_mutex(timeout_ms: int) -> bool:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_NAME

    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = None
    name = ""
    for candidate in _mutex_names_for_install():
        h = kernel32.CreateMutexW(None, False, candidate)
        if h:
            handle = h
            name = candidate
            break

    if not handle:
        _INSTANCE_MUTEX_HANDLE = None
        _INSTANCE_MUTEX_NAME = ""
        return False

    last_err = kernel32.GetLastError()
    if last_err == _ERROR_ALREADY_EXISTS:
        print(f"Mutex busy ({name}); stopping orphans, then waiting...", flush=True)
        kill_other_odicto_processes(base.pid_file_path())

    wait = kernel32.WaitForSingleObject(handle, int(timeout_ms))
    if wait in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        _INSTANCE_MUTEX_HANDLE = int(handle)
        _INSTANCE_MUTEX_NAME = name
        return True

    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""
    return False


def _try_acquire_lockfile() -> bool:
    global _INSTANCE_LOCK_FILE

    import msvcrt

    path = base.lock_file_path()
    try:
        fh = open(path, "a+b")
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode("ascii", errors="ignore"))
            fh.flush()
        except Exception:
            pass
        _INSTANCE_LOCK_FILE = fh
        return True
    except Exception as e:
        print(f"Warning: lock file acquire failed: {e}")
        return False


def _release_mutex_only() -> None:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_NAME

    if _INSTANCE_MUTEX_HANDLE:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
            kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""


def acquire_lock(timeout_ms: int = 8000) -> bool:
    if not _try_acquire_mutex(timeout_ms):
        return False
    if not _try_acquire_lockfile():
        _release_mutex_only()
        return False
    return True


def release_lock() -> None:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE

    unhook_all()

    if _INSTANCE_LOCK_FILE is not None:
        try:
            import msvcrt

            _INSTANCE_LOCK_FILE.seek(0)
            msvcrt.locking(_INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            _INSTANCE_LOCK_FILE.close()
        except Exception:
            pass
        _INSTANCE_LOCK_FILE = None

    if _INSTANCE_MUTEX_HANDLE:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
            kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass

    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_NAME = ""


def lock_is_held() -> bool:
    return _INSTANCE_MUTEX_HANDLE is not None and _INSTANCE_LOCK_FILE is not None


def spawn_detached(args) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def terminate_process_tree(proc) -> None:
    if proc is None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        print(f"Warning: Failed to terminate process tree: {e}")


def apply_window_exstyles(widget) -> None:
    """Keep the HUD topmost and non-activating without breaking Qt alpha."""
    try:
        import ctypes

        hwnd = int(widget.winId())
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_TRANSPARENT = 0x00000020
        user32 = ctypes.windll.user32

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception as e:
        print(f"Warning: could not apply Win32 exstyles: {e}")


def hotkey_backend_name() -> str:
    return "keyboard"
