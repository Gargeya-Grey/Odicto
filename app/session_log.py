"""Cross-process lock for the shared dictation.log, plus a one-line append for early failures.

Every process that writes the log (the owner, a refused second start, the managed launcher
child before main loads) holds this lock for each write, and the owner holds it while it trims.
So a trim can never erase lines another process wrote between the trim's read and its rewrite.
Stdlib only: the managed child imports it before any heavy module.
"""
import os
import threading
import time
from datetime import datetime

_WAIT_SECONDS = 2.0  # never hang logging on a stuck peer; write unlocked after this


class LogLock:
    """Re-entrant in-process, exclusive across processes (sidecar ``<log>.lock``, byte 0)."""

    def __init__(self, log_path: str) -> None:
        self._path = log_path + ".lock"
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None
        self._held = False

    def __enter__(self) -> "LogLock":
        self._thread_lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._held = self._lock_os()
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if self._depth == 1 and self._held:
                self._unlock_os()
                self._held = False
        finally:
            self._depth -= 1
            self._thread_lock.release()

    def _lock_os(self) -> bool:
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + _WAIT_SECONDS
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
                    time.sleep(0.01)
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


def append_line(log_path: str, text: str) -> None:
    """Append one timestamped line under the log lock. Never raises (failure-path logging)."""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LogLock(log_path):
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {text}\n")
    except Exception:
        pass
