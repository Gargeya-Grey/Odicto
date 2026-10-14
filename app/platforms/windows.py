"""Windows platform backend.

This preserves the existing single-instance enforcement verbatim: a named
mutex plus an exclusive ``msvcrt`` byte-lock on ``dictation.lock``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import psutil
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


def _self_and_parent_pids() -> set:
    """This interpreter and its launcher stub.

    Windows venv ``pythonw.exe`` is a parent stub plus a child that actually
    runs ``main.py``. ``taskkill /T`` on the parent kills this process too.
    """
    pids = {os.getpid()}
    try:
        ppid = os.getppid()
        if ppid and ppid > 0:
            pids.add(ppid)
    except Exception:
        pass
    return pids


def _enumerate_odicto_pids(exclude_pid: Optional[int] = None) -> set:
    protected = _self_and_parent_pids()
    if exclude_pid is not None:
        protected.add(exclude_pid)
    found: set = set()

    # psutil is required; relative launches resolve against the actual cwd.
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.pid in protected or (process.info.get("name") or "").lower() not in ("python.exe", "pythonw.exe"):
                continue
            if base.is_odicto_command(process.cmdline(), process.cwd()):
                found.add(process.pid)
        except (psutil.Error, OSError):
            continue
    return found


def kill_other_odicto_processes(pid_file: Optional[str] = None) -> list:
    """Stop verified install processes, retaining ownership metadata on failure."""
    protected = _self_and_parent_pids()
    pids_to_kill: set = set()
    verified_pids = _enumerate_odicto_pids()

    old_pid = None
    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
        except Exception as e:
            print(f"Warning: Could not read PID file: {e}")
    if old_pid is not None and old_pid not in protected and old_pid not in verified_pids:
        # Enumeration may have missed an inaccessible live owner. Distinguish
        # that uncertainty from a dead or recycled, unrelated PID before clearing
        # ownership metadata; a PID file alone never authorizes termination.
        try:
            process = psutil.Process(old_pid)
            # After reboot Windows can reuse the saved PID for an unrelated,
            # protected service. Its name is readable even when cmdline/cwd
            # are not. Apply the same interpreter filter as enumeration before
            # asking for those privileged fields; unknown names stay fail-closed.
            name = process.name().lower()
            if (not name or name in ("python.exe", "pythonw.exe")) and base.is_odicto_command(process.cmdline(), process.cwd()):
                pids_to_kill.add(old_pid)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as e:
            raise RuntimeError(f"Could not verify saved Odicto PID {old_pid}; restart cancelled") from e
    pids_to_kill |= verified_pids
    pids_to_kill -= protected

    killed: list = []
    for pid in sorted(pids_to_kill):
        # A venv launcher and its interpreter are separate processes belonging
        # to one app instance; an explicit restart does not mean either is stale.
        print(f"Stopping Odicto process (PID {pid})...")
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=5,
            )
            if isinstance(result.returncode, int) and result.returncode != 0:
                if psutil.pid_exists(pid):
                    raise PermissionError(f"Windows refused to stop Odicto PID {pid} (exit {result.returncode})")
            killed.append(pid)
        except PermissionError:
            raise  # preserve the PID file and cancel restart instead of claiming success
        except Exception as e:
            raise RuntimeError(f"Could not stop Odicto PID {pid}: {e}; restart cancelled") from e

    if killed:
        # taskkill can return before the tree has exited. Direct native PID
        # checks avoid repeatedly spawning tasklist and a fixed delay when all
        # targets have already exited. Unknown liveness always cancels restart.
        deadline = time.monotonic() + 8.0
        pending = set(killed)
        while pending:
            try:
                pending = {pid for pid in pending if psutil.pid_exists(pid)}
            except Exception as e:
                raise RuntimeError(f"Could not verify Odicto shutdown: {e}; restart cancelled") from e
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Odicto processes did not exit: {sorted(pending)}; restart cancelled")
            time.sleep(min(0.1, remaining))
    if pid_file:
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
    return killed


def _try_acquire_mutex(timeout_ms: int) -> bool:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_NAME

    import ctypes

    kernel32 = ctypes.windll.kernel32
    # HANDLE is pointer-sized. ctypes' default int return truncates it on Win64.
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int
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


# Generic modifier names -> virtual keys. Side-specific names keep the keyboard
# library's scan-code logic (is_pressed_exclusive).
_MODIFIER_VKS = {
    "ctrl": (0x11,), "control": (0x11,),
    "shift": (0x10,),
    "alt": (0x12,),
    "cmd": (0x5B, 0x5C), "windows": (0x5B, 0x5C), "win": (0x5B, 0x5C),
}
_keyboard_is_pressed = is_pressed  # noqa: F405 - the table-based one from _keyboard
_user32 = None


def _async_key_state():
    global _user32
    if _user32 is None:
        import ctypes

        dll = ctypes.WinDLL("user32")
        dll.GetAsyncKeyState.argtypes = [ctypes.c_int]
        dll.GetAsyncKeyState.restype = ctypes.c_short
        _user32 = dll
    return _user32.GetAsyncKeyState


def is_pressed(key) -> bool:
    """Modifiers come from the OS key state; other keys from the keyboard library.

    The keyboard library learns key state only from its own hook. When that hook
    runs late (the process was paged out or starved), Windows skips it for that
    event, a key-up is lost, and the library reports Shift held until the next
    Shift press. A stuck Shift turned the raw chord into the AI chord.
    """
    vks = _MODIFIER_VKS.get(key.strip().lower()) if isinstance(key, str) else None
    if vks:
        try:
            state = _async_key_state()
            return any(state(vk) & 0x8000 for vk in vks)
        except Exception:
            pass
    return _keyboard_is_pressed(key)


# Process settings for a hotkey app that must answer at once after hours idle.
_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_THROTTLE_EXECUTION_SPEED = 0x1
_THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4
_PROCESS_MEMORY_PRIORITY = 0  # PROCESS_INFORMATION_CLASS.ProcessMemoryPriority
_MEMORY_PRIORITY_NORMAL = 5
_PROCESS_IO_PRIORITY = 33  # PROCESSINFOCLASS.ProcessIoPriority (ntdll)
_IO_PRIORITY_NORMAL = 2
_HARDWS_MIN_ENABLE = 0x1
_HARDWS_MAX_DISABLE = 0x8
RESIDENT_MIN_MB = 192  # above the owner's peak working set (~140 MB), so nothing hot is trimmed


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def keep_responsive(resident_mb: int = RESIDENT_MIN_MB) -> list:
    """Keep the owner fast under memory and CPU pressure. Never raises.

    Without this, Windows pages an idle Odicto out (a 14 MB working set of
    94 MB private was measured) and runs a windowless background process on
    efficiency cores. The next hotkey then waits seconds on page-ins, the hook
    misses key-ups and the mic callback overflows. Call once, as the lock owner.
    Returns one short note per setting for the log.
    """
    import ctypes
    from ctypes import wintypes

    notes = []
    try:
        k32 = _kernel32()
        proc = wintypes.HANDLE(-1)  # GetCurrentProcess() pseudo-handle
    except Exception as e:
        return [f"process settings unavailable ({e})"]

    try:
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        ok = k32.SetPriorityClass(proc, _ABOVE_NORMAL_PRIORITY_CLASS)
        notes.append("priority above normal" if ok else f"priority unchanged (error {ctypes.get_last_error()})")
    except Exception as e:
        notes.append(f"priority unchanged ({e})")

    try:
        class _Throttle(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        # ControlMask names the policies we decide; StateMask 0 turns them off.
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        ok = False
        # Windows 10 does not know the timer-resolution flag and refuses the whole call.
        for mask in (_THROTTLE_EXECUTION_SPEED | _THROTTLE_IGNORE_TIMER_RESOLUTION, _THROTTLE_EXECUTION_SPEED):
            state = _Throttle(1, mask, 0)
            if k32.SetProcessInformation(proc, _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)):
                ok = True
                break
        notes.append("power throttling off" if ok else f"power throttling unchanged (error {ctypes.get_last_error()})")
    except Exception as e:
        notes.append(f"power throttling unchanged ({e})")

    # The admin-mode logon task starts Odicto at Task Scheduler priority 7: below-normal
    # CPU, and low memory and I/O priority, so its pages are trimmed and read back first
    # and last. Put both back to normal.
    try:
        mem = wintypes.ULONG(_MEMORY_PRIORITY_NORMAL)
        ok = k32.SetProcessInformation(proc, _PROCESS_MEMORY_PRIORITY, ctypes.byref(mem), ctypes.sizeof(mem))
        io = wintypes.ULONG(_IO_PRIORITY_NORMAL)
        nt = ctypes.WinDLL("ntdll")
        nt.NtSetInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
        nt.NtSetInformationProcess.restype = ctypes.c_long
        io_ok = nt.NtSetInformationProcess(proc, _PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io)) == 0
        notes.append("memory and I/O priority normal" if ok and io_ok
                     else f"memory priority {'normal' if ok else 'unchanged'}, I/O priority {'normal' if io_ok else 'unchanged'}")
    except Exception as e:
        notes.append(f"memory and I/O priority unchanged ({e})")

    try:
        k32.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        k32.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
        low = int(resident_mb) * 1024 * 1024
        ok = k32.SetProcessWorkingSetSizeEx(proc, low, low * 4, _HARDWS_MIN_ENABLE | _HARDWS_MAX_DISABLE)
        notes.append(f"{int(resident_mb)} MB kept resident" if ok
                     else f"working set unchanged (error {ctypes.get_last_error()})")
    except Exception as e:
        notes.append(f"working set unchanged ({e})")
    return notes
