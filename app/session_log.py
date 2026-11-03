"""Shared dictation.log rules: who may attach it, the cross-process write lock, early failures.

Every process that writes the log (the owner, a refused second start, a managed child before
main loads) holds LogLock for each write. Only the instance-lock holder trims, and it trims only
while it holds the same lock; if it cannot get the lock it skips the trim. So a trim never
rewrites lines that another process appended after the trim's read.

Writers never wait long: a peer that holds the lock too long (suspended, debugger) costs one
short wait, then a few milliseconds per write for a while, so a hotkey path that logs cannot
stall. Stdlib only: the managed child imports this before any heavy module.
"""
import os
import threading
import time
from datetime import datetime

_WRITE_WAIT_SECONDS = 0.25   # normal wait for a writer
_DEGRADED_WAIT_SECONDS = 0.02  # after a writer timed out, for _DEGRADED_SECONDS
_DEGRADED_SECONDS = 30.0
TRIM_WAIT_SECONDS = 1.0

_managed_owner = False


def mark_managed_owner(active: bool) -> None:
    """Set by odicto.cmd_managed_owner around running main in this same process.

    It is process memory, not the environment, so no child, test runner or other process
    that inherits the launcher's variables can attach the real data-root log.
    """
    global _managed_owner
    _managed_owner = bool(active)


def is_managed_owner() -> bool:
    return _managed_owner


class LogLock:
    """Re-entrant in-process, exclusive across processes (sidecar ``<log>.lock``, byte 0)."""

    def __init__(self, log_path: str) -> None:
        self._path = log_path + ".lock"
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None
        self._held = False
        self._degraded_until = 0.0

    @property
    def held(self) -> bool:
        """True while this process holds the cross-process lock."""
        return self._held

    def hold(self, wait: float) -> "_Hold":
        """Context manager that waits up to ``wait`` seconds for the cross-process lock."""
        return _Hold(self, wait)

    def __enter__(self) -> "LogLock":
        self._acquire(None)
        return self

    def __exit__(self, *_exc) -> None:
        self._release()

    def _acquire(self, wait) -> None:
        """``wait`` None means a writer: its wait is chosen only once it holds the in-process
        lock, so writers queued behind a timed-out one get the short wait too."""
        self._thread_lock.acquire()
        self._depth += 1
        if self._depth == 1:
            writer = wait is None
            if writer:
                degraded = time.monotonic() < self._degraded_until
                wait = _DEGRADED_WAIT_SECONDS if degraded else _WRITE_WAIT_SECONDS
            self._held = self._lock_os(wait)
            if writer and not self._held:
                self._degraded_until = time.monotonic() + _DEGRADED_SECONDS

    def _release(self) -> None:
        try:
            if self._depth == 1 and self._held:
                self._unlock_os()
                self._held = False
        finally:
            self._depth -= 1
            self._thread_lock.release()

    def _lock_os(self, wait: float) -> bool:
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + wait
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except OSError:
                    if time.monotonic() >= deadline:
                        self._close()
                        return False
                    time.sleep(0.005)
        except OSError:
            self._close()
            return False

    def _unlock_os(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._close()

    def _close(self) -> None:
        # The handle lives only while the lock is held: no open file outlasts a write.
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class _Hold:
    def __init__(self, lock: LogLock, wait: float) -> None:
        self._lock, self._wait = lock, wait

    def __enter__(self) -> LogLock:
        self._lock._acquire(self._wait)
        return self._lock

    def __exit__(self, *_exc) -> None:
        self._lock._release()


def _transcripts_logged() -> bool:
    try:
        from config import Config
        return bool(getattr(Config, "LOG_TRANSCRIPTS", False))
    except Exception:
        return False


def safe_traceback(exc_type, error, tb) -> str:
    """A traceback for dictation.log: frames and exception type names, never message text
    (an exception from a provider call can carry the dictated request)."""
    import traceback
    lines = ["Traceback (most recent call last):\n", *traceback.format_tb(tb)]
    lines.append(f"{getattr(exc_type, '__name__', 'Exception')} (message hidden; LOG_TRANSCRIPTS=false)\n")
    link, seen = error, 0
    while link is not None and seen < 5:
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
        if link is not None:
            lines.append(f"  from {type(link).__name__}\n")
        seen += 1
    return "".join(lines)


def install_safe_excepthooks() -> None:
    """Uncaught exceptions (main thread and other threads) print through safe_traceback
    unless LOG_TRANSCRIPTS is on. Installed only when stdout/stderr go to dictation.log."""
    import sys
    import threading as _threading
    default_hook, default_thread_hook = sys.excepthook, _threading.excepthook

    def hook(exc_type, error, tb):
        if _transcripts_logged():
            return default_hook(exc_type, error, tb)
        try:
            sys.stderr.write(safe_traceback(exc_type, error, tb))
            sys.stderr.flush()
        except Exception:
            pass

    def thread_hook(args):
        if args.exc_type is SystemExit or _transcripts_logged():
            return default_thread_hook(args)
        try:
            name = getattr(args.thread, "name", "?")
            sys.stderr.write(f"Exception in thread {name}:\n"
                             + safe_traceback(args.exc_type, args.exc_value, args.exc_traceback))
            sys.stderr.flush()
        except Exception:
            pass

    sys.excepthook = hook
    _threading.excepthook = thread_hook


def failure_text(error: BaseException) -> str:
    """Log text for an exception that ends a managed start: OS errors keep their message
    (paths and error codes); any other type is named only, since its text is not ours."""
    if isinstance(error, SystemExit):
        code = error.code
        return f"SystemExit (code {code})" if code is None or isinstance(code, int) else "SystemExit"
    if isinstance(error, OSError):
        return f"{type(error).__name__}: {str(error)[:200]}"
    return type(error).__name__


def append_line(log_path: str, text: str) -> None:
    """Append one timestamped line under the log lock. Never raises (failure-path logging)."""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LogLock(log_path):
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {text}\n")
    except Exception:
        pass
