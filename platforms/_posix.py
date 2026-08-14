"""POSIX single-instance + process helpers (macOS and Linux).

The lock is an exclusive ``fcntl.flock`` on ``dictation.lock``. There is no
Windows named mutex on these platforms; the lock file is the single gate. It
is released automatically by the OS when the process exits.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
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


def enumerate_odicto_pids(exclude_pid: Optional[int] = None) -> set:
    """PIDs running this install's ``main.py`` (best-effort via psutil).

    Matches both absolute command lines (``.../Odicto/main.py``) and relative
    launches (``.venv/bin/python main.py``) by falling back to the process's
    working directory, which the start scripts set to the install root.
    """
    found = set()
    my_pid = os.getpid()
    if exclude_pid is None:
        exclude_pid = my_pid
    if psutil is None:
        return found

    root = os.path.normcase(os.path.normpath(base.install_root()))
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = proc.info.get("cmdline") or []
                cwd = proc.info.get("cwd")
            except Exception:
                continue
            if pid in (0, exclude_pid):
                continue
            cmd = " ".join(cmdline)
            if "main.py" not in cmd:
                continue
            cmd_norm = os.path.normcase(os.path.normpath(cmd))
            if root in cmd_norm:
                found.add(pid)
                continue
            if cwd and os.path.normcase(os.path.normpath(cwd)) == root:
                found.add(pid)
    except Exception:
        pass
    return found


def kill_process_tree(pid: int) -> None:
    """Terminate a process and its children.

    Prefers psutil so arbitrary process trees are handled correctly even when
    ``pid`` is not a process-group leader. Falls back to ``os.kill``/``killpg``.
    """
    if pid == os.getpid():
        return

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            try:
                proc.kill()
            except Exception:
                pass
            return
        except psutil.NoSuchProcess:
            return
        except Exception:
            pass  # fall through to os.kill fallback

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass


def kill_other_odicto_processes(pid_file: Optional[str] = None) -> list:
    """Kill every other ``main.py`` for this install; returns attempted PIDs."""
    my_pid = os.getpid()
    pids = set()

    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                pids.add(old_pid)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass

    pids |= enumerate_odicto_pids()

    killed = []
    for pid in sorted(pids):
        print(f"Killing stale Odicto instance (PID {pid})...")
        try:
            kill_process_tree(pid)
            killed.append(pid)
        except Exception as e:
            print(f"Warning: Could not kill PID {pid}: {e}")

    if killed:
        time.sleep(0.15)
    return killed


def acquire_lock(timeout_ms: int = 8000) -> bool:
    """Take the exclusive ``dictation.lock`` byte-lock."""
    global _lock_file_obj

    if fcntl is None:
        return True  # Windows path never reaches here; keep import-safe.

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


def lock_is_held() -> bool:
    return _lock_file_obj is not None


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
        kill_process_tree(proc.pid)
    except Exception as e:
        print(f"Warning: Failed to terminate process tree: {e}")
