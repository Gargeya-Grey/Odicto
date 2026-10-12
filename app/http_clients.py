"""Process-wide HTTP connection pools for cloud providers.

Every cloud call used to open a fresh TCP+TLS connection (urllib has no
reuse, and httpx's default pool drops idle sockets after 5 s). These lazy
singletons keep one pool per transport for the life of the process, so a
dictation and the AI reply that follows it reuse a warm connection.

``requests`` and ``httpx`` are imported on first use: Whisper-only boots
never pay for them.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# Idle connections stay open this long. Providers usually close an idle
# socket after 60-350 s; a stale socket costs one transparent reconnect.
KEEPALIVE_SECONDS = 120.0

_lock = threading.Lock()
_requests_session = None
_httpx_client = None


def full_timeout(
    connect: float,
    read: float,
    write: Optional[float] = None,
    pool: Optional[float] = None,
):
    """An ``httpx.Timeout`` with all four phases set.

    A 2-tuple given to openai/httpx becomes ``Timeout(write=None, pool=None)``,
    so an upload or a pool wait could block forever. ``write`` defaults to
    ``read`` and ``pool`` to ``connect``.
    """
    import httpx

    return httpx.Timeout(
        connect=float(connect),
        read=float(read),
        write=float(read if write is None else write),
        pool=float(connect if pool is None else pool),
    )


def shared_requests_session():
    """One ``requests.Session`` with a keep-alive pool and no automatic retries."""
    global _requests_session
    with _lock:
        if _requests_session is None:
            import requests
            from requests.adapters import HTTPAdapter

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _requests_session = session
        return _requests_session


def shared_httpx_client():
    """One ``httpx.Client`` shared by every openai client and the speech uploads."""
    global _httpx_client
    with _lock:
        if _httpx_client is None:
            import httpx

            _httpx_client = httpx.Client(
                limits=httpx.Limits(
                    max_connections=32,
                    max_keepalive_connections=16,
                    keepalive_expiry=KEEPALIVE_SECONDS,
                ),
                timeout=full_timeout(5.0, 60.0),
                follow_redirects=True,
            )
        return _httpx_client


def origin_of(url: str) -> str:
    """``scheme://host[:port]/`` of a URL; connection pools are keyed by it."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def warm_httpx_origin(url: str, timeout: float = 2.0) -> None:
    """Open (or refresh) a pooled connection to ``url``'s host. Status is ignored."""
    origin = origin_of(url)
    if origin:
        shared_httpx_client().head(origin, timeout=full_timeout(timeout, timeout))


def warm_requests_origin(url: str, timeout: float = 2.0) -> None:
    origin = origin_of(url)
    if origin:
        shared_requests_session().head(origin, timeout=(timeout, timeout))


PREWARM_INTERVAL_SECONDS = 20.0


class Prewarmer:
    """Run a connection warm-up on a daemon thread, at most once per interval.

    ``fire`` returns at once and never raises; the warm-up's own errors are
    swallowed because a failed warm-up only means the real call connects.
    """

    def __init__(self, interval: float = PREWARM_INTERVAL_SECONDS) -> None:
        self._interval = float(interval)
        self._lock = threading.Lock()
        self._last: Optional[float] = None

    def fire(self, work: Callable[[], None], name: str = "odicto-prewarm") -> bool:
        try:
            with self._lock:
                now = time.monotonic()
                if self._last is not None and now - self._last < self._interval:
                    return False
                self._last = now

            def run() -> None:
                try:
                    work()
                except BaseException:
                    pass

            threading.Thread(target=run, daemon=True, name=name).start()
            return True
        except Exception:
            return False
