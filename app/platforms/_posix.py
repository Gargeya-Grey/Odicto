"""POSIX single-instance + process helpers (macOS and Linux).

The lock is an exclusive ``fcntl.flock`` on ``dictation.lock``. There is no
Windows named mutex on these platforms; the lock file is the single gate. It
is released automatically by the OS when the process exits.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from typing import Optional

from platforms import base

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not have fcntl
    fcntl = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

_lock_file_obj = None
_hook_lock_fd: Optional[int] = None
_hook_lock_socket = None


class _VerifiedPid(int):
    """A PID retaining the process object that established its identity."""

    def __new__(cls, pid, process):
        value = super().__new__(cls, pid)
        value.process = process
        return value


def _self_and_parent_pids() -> set:
    pids = {os.getpid()}
    try:
        ppid = os.getppid()
        if ppid and ppid > 0:
            pids.add(ppid)
    except Exception:
        pass
    return pids


def enumerate_odicto_pids(exclude_pid: Optional[int] = None) -> set:
    """PIDs running this install's ``main.py`` (best-effort via psutil).

    Matches both absolute command lines (``.../Odicto/main.py``) and relative
    launches (``.venv/bin/python main.py``) by falling back to the process's
    working directory, which the start scripts set to the install root.
    """
    found = set()
    protected = _self_and_parent_pids()
    if exclude_pid is not None:
        protected.add(exclude_pid)
    if psutil is None:
        return found

    try:
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = proc.info.get("cmdline") or []
                cwd = proc.info.get("cwd")
            except Exception:
                continue
            if pid in protected or pid == 0:
                continue
            if base.is_odicto_command(cmdline, cwd):
                try:
                    proc.create_time()
                    if proc.is_running():
                        found.add(_VerifiedPid(pid, proc))
                except (psutil.Error, OSError):
                    continue
    except Exception:
        pass
    return found


def kill_process_tree(pid: int) -> None:
    """Request termination, propagating any unconfirmed or denied operation."""
    if pid in _self_and_parent_pids():
        return
    if psutil is None:
        raise RuntimeError("Cannot verify process ownership without psutil")
    try:
        proc = pid.process if isinstance(pid, _VerifiedPid) else psutil.Process(pid)
        proc.create_time()
        if not proc.is_running():
            if psutil.pid_exists(pid):
                raise RuntimeError("Process identity changed because its PID was reused")
            return
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if not proc.is_running():
        if psutil.pid_exists(pid):
            raise RuntimeError("Process identity changed because its PID was reused")
        return
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass


def kill_other_odicto_processes(pid_file: Optional[str] = None) -> list:
    """Stop verified owners, retaining metadata until shutdown is proven."""
    if psutil is None:
        raise RuntimeError("Cannot verify Odicto process ownership without psutil")
    protected = _self_and_parent_pids()
    pids = set()
    verified_pids = enumerate_odicto_pids()
    saved_pid_text = None
    old_pid = None
    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                saved_pid_text = f.read()
            old_pid = int(saved_pid_text.strip())
        except (OSError, ValueError) as error:
            raise RuntimeError("Could not verify saved Odicto ownership; restart cancelled") from error
    if old_pid is not None and old_pid not in protected and old_pid not in verified_pids:
        try:
            process = psutil.Process(old_pid)
            process.create_time()
            name = process.name().lower()
            if (not name or name.startswith(("python", "pypy"))) and base.is_odicto_command(
                process.cmdline(), process.cwd()
            ):
                pids.add(_VerifiedPid(old_pid, process))
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as error:
            raise RuntimeError(f"Could not verify saved Odicto PID {old_pid}; restart cancelled") from error
    pids |= verified_pids
    pids -= protected
    killed = []
    for pid in sorted(pids):
        print(f"Stopping Odicto process (PID {pid})...")
        try:
            kill_process_tree(pid)
            killed.append(pid)
        except Exception as error:
            raise RuntimeError(f"Could not stop Odicto PID {pid}; restart cancelled: {error}") from error
    if killed:
        deadline = time.monotonic() + 8.0
        pending = set(killed)
        while pending:
            try:
                pending = {pid for pid in pending if psutil.pid_exists(pid)}
            except Exception as error:
                raise RuntimeError("Could not verify Odicto shutdown; restart cancelled") from error
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Odicto processes did not exit: {sorted(pending)}; restart cancelled")
            time.sleep(min(0.1, remaining))

    from contextlib import ExitStack
    from state_lock import StateLease
    with ExitStack() as ownership:
        if _lock_file_obj is None:
            try:
                ownership.enter_context(StateLease(base.lock_file_path()))
            except OSError as error:
                raise RuntimeError("An existing Odicto owner still holds its runtime lock; restart cancelled") from error
        if pid_file and saved_pid_text is not None:
            try:
                with open(pid_file) as handle:
                    if handle.read() != saved_pid_text:
                        raise RuntimeError("Odicto ownership changed during shutdown; restart cancelled")
                os.remove(pid_file)
            except FileNotFoundError:
                pass
    return killed


def _try_acquire_hook_lock() -> bool:
    """Take the keyboard-hook lock that every install shares.

    Linux: an abstract Unix socket named for the X display. It has no file to delete or to own,
    root and the desktop user share it, and the kernel frees it when the process dies.
    macOS: an flock on a file whose path comes from the account database (base.hook_lock_path).
    """
    global _hook_lock_fd, _hook_lock_socket

    if sys.platform.startswith("linux"):
        import socket

        name = "\0odicto-keyboard-hooks-%s-%s" % (
            base.hook_lock_scope(), base.hook_lock_display_key(os.environ.get("DISPLAY", "")))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(name)
        except OSError:
            sock.close()
            return False
        _hook_lock_socket = sock
        return True

    path = base.hook_lock_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        print(f"Warning: keyboard-hook lock unavailable: {e}")
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A file replaced between open and lock is a different lock: refuse, never guess.
        held, named = os.fstat(fd), os.stat(path)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise OSError("keyboard-hook lock file was replaced")
    except OSError:
        os.close(fd)
        return False
    _hook_lock_fd = fd
    return True


def _release_hook_lock() -> None:
    global _hook_lock_fd, _hook_lock_socket

    if _hook_lock_socket is not None:
        try:
            _hook_lock_socket.close()
        except Exception:
            pass
        _hook_lock_socket = None
    if _hook_lock_fd is not None:
        try:
            fcntl.flock(_hook_lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(_hook_lock_fd)
        except Exception:
            pass
        _hook_lock_fd = None


def acquire_lock(timeout_ms: int = 8000) -> bool:
    """Take the exclusive ``dictation.lock`` byte-lock, then the per-user hook lock."""
    if fcntl is None:
        return True  # Windows path never reaches here; keep import-safe.
    if not _acquire_install_lock():
        return False
    if not _try_acquire_hook_lock():
        release_lock()
        return False
    return True


def _acquire_install_lock() -> bool:
    global _lock_file_obj

    path = base.lock_file_path()
    try:
        fh = open(path, "a+b")
        fh.seek(0)
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                fh.close()
                return False
            raise
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode("ascii", errors="ignore"))
            fh.flush()
        except Exception:
            pass
        _lock_file_obj = fh
        return True
    except Exception as e:
        print(f"Warning: lock file acquire failed: {e}")
        return False


def release_lock() -> None:
    global _lock_file_obj

    if _lock_file_obj is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_lock_file_obj.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            _lock_file_obj.close()
        except Exception:
            pass
        _lock_file_obj = None
    _release_hook_lock()


def lock_is_held() -> bool:
    return _lock_file_obj is not None and (_hook_lock_fd is not None or _hook_lock_socket is not None)


def spawn_detached(args) -> subprocess.Popen:
    """Start a process without a console and in its own process group."""
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def terminate_process_tree(proc) -> None:
    """Terminate a ``subprocess.Popen`` we started, then detach it.

    We do not ``wait()`` here because the Ollama server can outlive the
    request loop; detaching the object avoids holding a zombie handle.
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        owner = psutil.Process(proc.pid)
        owner.create_time()
        kill_process_tree(_VerifiedPid(proc.pid, owner))
    except Exception as e:
        print(f"Warning: Failed to terminate process tree: {e}")


def keep_responsive(resident_mb: int = 0) -> list:
    """Windows-only process settings (see platforms.windows); nothing to do here."""
    return []
