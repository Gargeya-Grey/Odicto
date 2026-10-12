"""Optional admin mode for one Windows install.

A normal-integrity process cannot hook keys typed into an elevated window (an
Administrator Windows Terminal). An install can therefore run Odicto elevated.
Admin mode is off by default. The setup page switches it with ``set_admin_mode``.
The switch is the gitignored marker file ``.odicto-admin-enabled`` in the install root.
``scripts/windows/admin_mode.ps1`` writes and deletes it, inside the one elevated run.

Daily use never shows a UAC prompt while admin mode is on:

* Start and restart run the logon task ``Odicto`` (``schtasks /Run``). The task is
  highest-privilege and registered for this user, so Windows does not ask.
* Quit uses a named event per install. The elevated owner creates it after it holds the
  single-instance lock, with a security descriptor that lets a normal process of the same
  user signal it. A daemon thread waits on it and runs the same shutdown as the tray
  "Quit Odicto". ``request_quit`` signals it from a normal process.

Only a switch change (``set_admin_mode``), a missing task, or an owner without the quit
event (an older version) reaches the PowerShell helper and UAC. Each fallback prints why.
Python never calls the shell-execute API: the helper owns UAC.
Stdlib only at import time; it must stay importable before any other Odicto module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional, Tuple

MARKER_NAME = ".odicto-admin-enabled"
HELPER_PARTS = ("scripts", "windows", "admin_mode.ps1")
TASK_NAME = "Odicto"
HELPER_ACTIONS = ("Start", "Stop", "WaitReady", "Configure", "Disable")
# "Ensure" is decided in Python (start unless already running); it never reaches the helper.
ACTIONS = HELPER_ACTIONS + ("Ensure",)

_CREATE_NO_WINDOW = 0x08000000
_QUIT_EVENT_PREFIX = "Local\\Odicto-quit-"
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
# Read-only rights are enough to wait on the owner; the owner may be elevated.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_ERROR_ALREADY_EXISTS = 183
_SDDL_REVISION_1 = 1


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_user_an_admin() -> bool:
    """Whether this process holds an elevated Administrator token."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _say(text: str) -> None:
    """Print one line. A missing or broken console (pythonw) must never raise."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def _system32(*parts: str) -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if not system_root or not os.path.isabs(system_root):
        raise RuntimeError("Cannot locate Windows: SystemRoot is not set.")
    return os.path.join(system_root, "System32", *parts)


def _powershell_exe() -> str:
    try:
        return _system32("WindowsPowerShell", "v1.0", "powershell.exe")
    except RuntimeError:
        raise RuntimeError("Cannot locate Windows PowerShell: SystemRoot is not set.") from None


def marker_path(root) -> str:
    return os.path.join(os.path.abspath(os.fspath(root)), MARKER_NAME)


def admin_mode_enabled(root) -> bool:
    """True only on Windows when this install's admin-mode marker exists."""
    return _is_windows() and os.path.isfile(marker_path(root))


# ---------------------------------------------------------------------------
# Quit signal: one named event per install
# ---------------------------------------------------------------------------
def quit_event_name(root) -> str:
    """``Local\\Odicto-quit-<12 hex>``: stable for one install, different for another."""
    normal = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(root))))
    return _QUIT_EVENT_PREFIX + hashlib.sha256(normal.encode("utf-8")).hexdigest()[:12]


def quit_event_sddl(user_sid: str) -> str:
    """DACL: the current user may signal and wait. Label: a Medium-integrity process may write.

    Without the label an elevated owner creates a High-integrity object, and the
    no-write-up rule would stop a normal process from signalling it.
    """
    return "D:(A;;0x%X;;;%s)S:(ML;;NW;;;ME)" % (_EVENT_MODIFY_STATE | _SYNCHRONIZE, user_sid)


class _Win32:
    """The few kernel32/advapi32 calls this module needs, with explicit signatures."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.k32, self.adv = k32, adv
        handle, void_p = wintypes.HANDLE, ctypes.c_void_p

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        self.SecurityAttributes = SecurityAttributes
        k32.CreateEventW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.BOOL,
                                     wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = handle
        k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k32.OpenEventW.restype = handle
        k32.SetEvent.argtypes = [handle]
        k32.SetEvent.restype = wintypes.BOOL
        k32.ResetEvent.argtypes = [handle]
        k32.ResetEvent.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [handle]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = handle
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = handle
        k32.LocalFree.argtypes = [void_p]
        k32.LocalFree.restype = void_p
        adv.OpenProcessToken.argtypes = [handle, wintypes.DWORD, ctypes.POINTER(handle)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [handle, ctypes.c_int, void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.ConvertSidToStringSidW.argtypes = [void_p, ctypes.POINTER(wintypes.LPWSTR)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(void_p), ctypes.POINTER(wintypes.ULONG)]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


_win32_cache: Optional[_Win32] = None


def _win32() -> _Win32:
    global _win32_cache
    if _win32_cache is None:
        _win32_cache = _Win32()
    return _win32_cache


def _current_user_sid() -> str:
    """The SID string of the user this process runs as."""
    w = _win32()
    ct = w.ctypes
    token = w.wintypes.HANDLE()
    if not w.adv.OpenProcessToken(w.k32.GetCurrentProcess(), 0x0008, ct.byref(token)):  # TOKEN_QUERY
        raise ct.WinError(ct.get_last_error())
    try:
        needed = w.wintypes.DWORD(0)
        w.adv.GetTokenInformation(token, 1, None, 0, ct.byref(needed))  # TokenUser
        buffer = ct.create_string_buffer(needed.value)
        if not w.adv.GetTokenInformation(token, 1, buffer, needed, ct.byref(needed)):
            raise ct.WinError(ct.get_last_error())
        sid_pointer = ct.cast(buffer, ct.POINTER(ct.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = w.wintypes.LPWSTR()
        if not w.adv.ConvertSidToStringSidW(sid_pointer, ct.byref(text)):
            raise ct.WinError(ct.get_last_error())
        try:
            return text.value
        finally:
            w.k32.LocalFree(ct.cast(text, ct.c_void_p))
    finally:
        w.k32.CloseHandle(token)


def _create_quit_event(name: str, sddl: str):
    """Create (or open) the manual-reset quit event with ``sddl``; returns its handle."""
    w = _win32()
    ct = w.ctypes
    descriptor = ct.c_void_p()
    if not w.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ct.byref(descriptor), None):
        raise ct.WinError(ct.get_last_error())
    try:
        attributes = w.SecurityAttributes(ct.sizeof(w.SecurityAttributes), descriptor.value, False)
        ct.set_last_error(0)
        handle = w.k32.CreateEventW(ct.byref(attributes), True, False, name)
        error = ct.get_last_error()
    finally:
        w.k32.LocalFree(descriptor)
    if not handle and error == _ERROR_ACCESS_DENIED:
        # The event exists already (an old owner is still exiting) and its DACL grants this
        # user only signal and wait, which CreateEvent does not accept. Open it with those.
        handle = w.k32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, name)
        error = _ERROR_ALREADY_EXISTS if handle else ct.get_last_error()
    if not handle:
        raise ct.WinError(error)
    if error == _ERROR_ALREADY_EXISTS:
        # A previous owner that is still exiting may leave the event set. A new owner must
        # not quit because of a request that was meant for the old one.
        w.k32.ResetEvent(handle)
    return handle


def _wait_for_event(handle) -> bool:
    """Block until the event is signalled (no polling)."""
    return _win32().k32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0


def _signal_quit_event(name: str) -> bool:
    """Open the event of an owner and set it. False when no such event exists."""
    w = _win32()
    handle = w.k32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(w.k32.SetEvent(handle))
    finally:
        w.k32.CloseHandle(handle)


def _post_qt_quit(qt_app, tray) -> None:
    """Queue the tray Quit steps on the Qt thread: hide the tray icon, then quit the loop."""
    from PySide6.QtCore import QMetaObject, Qt

    if tray is not None:
        QMetaObject.invokeMethod(tray, "hide", Qt.ConnectionType.QueuedConnection)
    QMetaObject.invokeMethod(qt_app, "quit", Qt.ConnectionType.QueuedConnection)


def graceful_quit(get_app: Callable[[], object], poll: float = 0.25) -> None:
    """Run the tray "Quit Odicto" shutdown from any thread.

    ``get_app`` returns the DictationApp, or None while it is still being built; a quit
    request that arrives early waits for it. The Qt event loop ends, ``DictationApp.run``
    leaves its loop and calls ``_shutdown`` exactly as it does for the tray menu.
    """
    while True:
        try:
            app = get_app()
        except Exception:
            app = None
        if app is not None:
            break
        time.sleep(poll)
    indicator = getattr(app, "indicator", None)
    qt_app = getattr(indicator, "_qt_app", None) if indicator is not None else None
    if qt_app is not None:
        _post_qt_quit(qt_app, getattr(indicator, "_tray", None))
        return
    # No event loop (the HUD did not start): the main thread waits in the keyboard backend.
    try:
        app._shutdown()
    except Exception:
        pass
    os._exit(0)


def _quit_listener_loop(handle, get_app) -> None:
    try:
        signalled = _wait_for_event(handle)
    except Exception as error:
        _say("Warning: the quit signal listener stopped: %s" % error)
        return
    if signalled:
        _say(">>> Quit requested through the Odicto stop signal.")
        graceful_quit(get_app)


def start_quit_listener(root, get_app: Callable[[], object]) -> Optional[threading.Thread]:
    """Create this install's quit event and wait on it in a daemon thread.

    Call it only after the single-instance lock is held. Never raises. Windows only;
    elsewhere it does nothing and returns None.
    """
    if not _is_windows():
        return None
    try:
        handle = _create_quit_event(quit_event_name(root), quit_event_sddl(_current_user_sid()))
    except Exception as error:
        _say("Warning: could not create the quit signal (%s); stop uses the administrator prompt." % error)
        return None
    thread = threading.Thread(target=_quit_listener_loop, args=(handle, get_app),
                              name="odicto-quit-listener", daemon=True)
    thread.start()
    return thread


def _owner_pid(root) -> Optional[int]:
    """The PID in dictation.pid when that process is alive, else None."""
    try:
        with open(os.path.join(os.path.abspath(os.fspath(root)), "dictation.pid"), encoding="ascii") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and _pid_alive(pid) else None


def _open_process_for_wait(pid: int):
    """(handle, error). A handle of 0 and error 87 mean the process is gone."""
    w = _win32()
    w.ctypes.set_last_error(0)
    handle = w.k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    return handle, w.ctypes.get_last_error()


def _pid_alive(pid: int) -> bool:
    handle, error = _open_process_for_wait(pid)
    if not handle:
        return error != _ERROR_INVALID_PARAMETER  # access denied: it exists
    try:
        return _win32().k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _win32().k32.CloseHandle(handle)


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    """True when the process is gone within ``timeout`` seconds."""
    w = _win32()
    handle, error = _open_process_for_wait(pid)
    if handle:
        try:
            return w.k32.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000)) == _WAIT_OBJECT_0
        finally:
            w.k32.CloseHandle(handle)
    if error == _ERROR_INVALID_PARAMETER:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _request_quit_detail(root, timeout: float) -> Tuple[bool, str]:
    """Signal the owner to quit and wait for it. Returns (ok, reason when not ok)."""
    root = os.path.abspath(os.fspath(root))
    deadline = time.monotonic() + max(0.0, timeout)
    pid = _owner_pid(root)
    if pid is None:
        return False, "no running Odicto was found"
    try:
        signalled = _signal_quit_event(quit_event_name(root))
    except Exception as error:
        return False, "the quit signal could not be opened (%s)" % error
    if not signalled:
        return False, "this Odicto has no quit signal (it is an older version)"
    if _wait_pid_exit(pid, deadline - time.monotonic()):
        return True, ""
    return False, "Odicto did not quit in %d seconds" % int(timeout)


def request_quit(root, timeout: float = 10.0) -> bool:
    """Ask the running owner to quit through its event, then wait for its PID to exit.

    Needs no elevation and shows no prompt. False when there is no event (no owner, or an
    owner without the listener) or when the owner did not exit in time. Windows only.
    """
    if not _is_windows():
        return False
    try:
        return _request_quit_detail(root, timeout)[0]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lifecycle with admin mode on and no elevation
# ---------------------------------------------------------------------------
def _venv_python(root: str) -> str:
    candidate = os.path.join(root, ".venv", "Scripts", "python.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def _wait_ready(root: str, timeout: float, quiet: bool = False) -> int:
    """``odicto.py wait-ready``: a live owner heartbeat and microphone callbacks."""
    command = [_venv_python(root), os.path.join(root, "odicto.py"), "wait-ready",
               "--timeout", str(int(math.ceil(max(0.0, timeout))))]
    extra = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    try:
        return subprocess.run(command, cwd=root, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
                              **extra).returncode
    except OSError as error:
        _say("Admin mode: could not check readiness: %s" % error)
        return 1


def _owner_ready(root: str) -> bool:
    return _wait_ready(root, 2, quiet=True) == 0


def _schtasks_run() -> Tuple[bool, str]:
    """Run the logon task. No prompt: it is highest-privilege and belongs to this user."""
    try:
        done = subprocess.run([_system32("schtasks.exe"), "/Run", "/TN", TASK_NAME],
                              capture_output=True, text=True, timeout=30, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if done.returncode == 0:
        return True, ""
    return False, ((done.stderr or done.stdout or "").strip() or "exit code %d" % done.returncode)


def _run_helper(root: str, action: str, timeout: Optional[float] = None,
                extra: Tuple[str, ...] = (), run_timeout: Optional[float] = None) -> int:
    """Run one helper action; it elevates itself through UAC. Returns its exit code."""
    helper = os.path.join(root, *HELPER_PARTS)
    if not os.path.isfile(helper):
        raise RuntimeError("Admin mode is enabled (%s) but the helper is missing: %s"
                           % (MARKER_NAME, helper))
    command = [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", helper, "-Action", action, *extra]
    if timeout is not None:
        command += ["-TimeoutSeconds", str(int(math.ceil(max(0.0, float(timeout)))))]
    try:
        completed = subprocess.run(
            command, cwd=root, check=False, timeout=run_timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
    except OSError as error:
        raise RuntimeError("Admin mode is enabled (%s) but PowerShell could not run: %s"
                           % (MARKER_NAME, error)) from error
    return completed.returncode


def _start_through_task(root: str, previous_pid: Optional[int] = None) -> int:
    """Start the owner with the logon task and wait until it is ready."""
    ok, why = _schtasks_run()
    if not ok:
        _say("Admin mode: the Odicto task could not start (%s). Using the administrator prompt instead." % why)
        return _run_helper(root, "Start", None)
    if previous_pid is not None and not _wait_for_new_owner(root, previous_pid, 5.0):
        # The task may still count the old run as running (policy: ignore a new start).
        ok, why = _schtasks_run()
        if not ok:
            _say("Admin mode: the Odicto task could not start (%s)." % why)
            return 1
    return _wait_ready(root, 30)


def _wait_for_new_owner(root: str, old_pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        pid = _owner_pid(root)
        if pid is not None and pid != old_pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _ensure_running(root: str) -> int:
    if _owner_ready(root):
        _say("Odicto is already running (admin mode).")
        return 0
    _say("Starting Odicto (admin mode, no administrator prompt)...")
    return _start_through_task(root)


def _restart(root: str, timeout: Optional[float]) -> int:
    pid = _owner_pid(root)
    if pid is None:
        _say("Starting Odicto (admin mode, no administrator prompt)...")
        return _start_through_task(root)
    _say("Restarting Odicto (admin mode, no administrator prompt)...")
    ok, why = _request_quit_detail(root, 10.0)
    if not ok:
        _say("Admin mode: %s. Using the administrator prompt instead." % why)
        return _run_helper(root, "Start", timeout)
    return _start_through_task(root, previous_pid=pid)


def _stop(root: str) -> Optional[int]:
    pid = _owner_pid(root)
    if pid is None:
        return None  # nothing the PID file knows about; the ordinary stop sweeps the rest
    ok, why = _request_quit_detail(root, 10.0)
    if ok:
        _say("Stopped Odicto (admin mode).")
        return 0
    _say("Admin mode: %s. Using the administrator prompt instead." % why)
    return _run_helper(root, "Stop", None)


def run_if_needed(root, action: str = "Start", timeout: Optional[float] = None) -> Optional[int]:
    """Run ``action`` the admin-mode way when this install needs it.

    Returns None when the caller should carry on unchanged: not Windows, admin mode off,
    already elevated, or nothing for this step to do. Otherwise returns an exit code.

    * ``Start`` is a restart: quit the owner through its event, then run the logon task.
    * ``Ensure`` starts only when Odicto is not already running and ready.
    * ``Stop`` quits the owner through its event.
    * ``WaitReady`` needs no elevation: None.
    * ``Configure`` and ``Disable`` use the helper (UAC).

    Without a working event or task the step falls back to the helper and says why.
    A marker that cannot be honoured (admin state unknown, helper or PowerShell missing)
    raises RuntimeError, so an install in admin mode never silently runs without it.
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
    if elevated or action == "WaitReady":
        return None
    if action == "Ensure":
        return _ensure_running(root)
    if action == "Start":
        return _restart(root, timeout)
    if action == "Stop":
        return _stop(root)
    return _run_helper(root, action, timeout)


# ---------------------------------------------------------------------------
# The switch (setup page)
# ---------------------------------------------------------------------------
def _read_receipt(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _short(text: object, limit: int = 300) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."


def set_admin_mode(root, enabled: bool, timeout: float = 180.0) -> Tuple[bool, str]:
    """Turn admin mode on or off. Never raises. Returns (ok, one short sentence).

    One elevated helper run does the work, so Windows shows one UAC prompt per switch change.
    On: register the task and the Startup shortcut, write the marker, restart Odicto elevated.
    Off: stop the elevated owner, remove the task, point the Startup shortcut at pythonw,
    delete the marker; then a normal Odicto starts here.
    """
    try:
        return _set_admin_mode(root, bool(enabled), float(timeout))
    except Exception as error:
        return False, "Admin mode was not changed: %s" % _short(error)


def _set_admin_mode(root, enabled: bool, timeout: float) -> Tuple[bool, str]:
    if not _is_windows():
        return False, "Admin mode is only available on Windows."
    root = os.path.abspath(os.fspath(root))
    helper = os.path.join(root, *HELPER_PARTS)
    if not os.path.isfile(helper):
        return False, "Admin mode was not changed: the helper script is missing."
    scratch = tempfile.mkdtemp(prefix="odicto-admin-")
    try:
        receipt_path = os.path.join(scratch, "receipt.json")
        action = ("-Action", "Start", "-InstallStartup") if enabled else ("-Action", "Disable")
        command = [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper,
                   *action, "-ReceiptPath", receipt_path, "-TimeoutSeconds", "30"]
        try:
            completed = subprocess.run(
                command, cwd=root, check=False, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
        except subprocess.TimeoutExpired:
            return False, "Admin mode change timed out. Check whether Odicto is running."
        except OSError as error:
            return False, "Admin mode was not changed: PowerShell could not run (%s)." % _short(error)
        receipt = _read_receipt(receipt_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if "elevation not granted" in (receipt.get("steps") or []):
        return False, "The administrator prompt was cancelled; nothing changed."
    if completed.returncode != 0:
        reason = receipt.get("error") or "the helper exited with code %d" % completed.returncode
        return False, "Admin mode was not changed: %s" % _short(reason)
    if enabled:
        if not os.path.isfile(marker_path(root)):
            return False, "Admin mode was not changed: the helper did not finish."
        return True, "Admin mode is on. Odicto restarted with administrator rights."
    if os.path.isfile(marker_path(root)):
        return False, "Admin mode was not changed: the helper did not finish."
    return _start_normal_odicto(root)


def _start_normal_odicto(root: str) -> Tuple[bool, str]:
    pythonw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    main_py = os.path.join(root, "main.py")
    if not os.path.isfile(pythonw):
        return True, "Admin mode is off. Start Odicto from the Start Menu; .venv is missing."
    try:
        import platforms

        platforms.spawn_detached([pythonw, main_py])
    except Exception as error:
        return True, "Admin mode is off. Start Odicto from the Start Menu (%s)." % _short(error, 100)
    return True, "Admin mode is off. Odicto restarted without administrator rights."
