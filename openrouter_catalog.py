"""Live OpenRouter model catalog: names + per-model reasoning levels.

GET /api/v1/models includes a ``reasoning`` object (mandatory, supported_efforts,
default_effort). Cached in-process so setup and the OpenRouter client can clamp
``reasoning.effort`` without a hardcoded model list.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from config import Config, ENV_DEFAULTS

_TTL_SECONDS = 6 * 3600
_FETCH_TIMEOUT = 12.0
_VARIANT_SUFFIXES = (
    "free",
    "batch",
    "nitro",
    "floor",
    "extended",
    "exacto",
    "online",
)
_EFFORTS_LIGHTEST_FIRST = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

_lock = threading.Lock()
_cache: Optional[dict[str, Any]] = None
_cache_at = 0.0


def reset_openrouter_catalog() -> None:
    """Drop the in-process catalog (tests)."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


def peek_openrouter_catalog() -> dict[str, Any]:
    """Return the cached payload without hitting the network."""
    with _lock:
        return _cache if _cache is not None else {}


def peek_openrouter_reasoning(model: str) -> Optional[dict[str, Any]]:
    payload = peek_openrouter_catalog()
    reasoning = payload.get("reasoning") or {}
    for key in _lookup_keys(model):
        spec = reasoning.get(key)
        if isinstance(spec, dict):
            return spec
    return None


def ensure_openrouter_catalog(force: bool = False) -> dict[str, Any]:
    """Fetch and cache the catalog when missing or stale."""
    global _cache, _cache_at
    now = time.time()
    with _lock:
        if (
            not force
            and _cache is not None
            and now - _cache_at < _TTL_SECONDS
        ):
            return _cache
    fetched = fetch_openrouter_catalog()
    with _lock:
        if fetched.get("ok"):
            _cache = fetched
            _cache_at = time.time()
            return _cache
        if _cache is not None:
            return _cache
        _cache = fetched
        _cache_at = time.time()
        return _cache


def prefetch_openrouter_catalog() -> None:
    threading.Thread(
        target=ensure_openrouter_catalog,
        daemon=True,
        name="or-catalog",
    ).start()


def fetch_openrouter_catalog() -> dict[str, Any]:
    """HTTP fetch. Isolated so tests can patch it."""
    base = (
        Config.OPENROUTER_API_BASE.strip()
        or ENV_DEFAULTS.get("OPENROUTER_API_BASE")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    url = f"{base}/models?sort=most-popular"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Odicto", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as e:
        return {"ok": False, "models": [], "reasoning": {}, "error": str(e)}
    return parse_openrouter_models(raw)


def parse_openrouter_models(raw: Any) -> dict[str, Any]:
    """Turn an OpenRouter /models JSON body into {models, reasoning}."""
    rows = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {"ok": False, "models": [], "reasoning": {}, "error": "unexpected catalog shape"}
    models: list[dict[str, str]] = []
    reasoning: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        name = str(row.get("name") or mid).strip() or mid
        models.append({"id": mid, "name": name})
        spec = _normalize_reasoning(row.get("reasoning"))
        if spec is not None:
            reasoning[mid] = spec
            lower = mid.lower()
            if lower != mid:
                reasoning[lower] = spec
    return {"ok": True, "models": models, "reasoning": reasoning}


def _normalize_reasoning(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    efforts_raw = raw.get("supported_efforts")
    efforts: Optional[list[str]]
    if efforts_raw is None:
        efforts = None
    elif isinstance(efforts_raw, list):
        efforts = [
            str(e).strip().lower()
            for e in efforts_raw
            if str(e).strip().lower() in _EFFORTS_LIGHTEST_FIRST
        ]
    else:
        efforts = None
    default_effort = str(raw.get("default_effort") or "").strip().lower()
    if default_effort not in _EFFORTS_LIGHTEST_FIRST:
        default_effort = ""
    return {
        "mandatory": bool(raw.get("mandatory")),
        "supported_efforts": efforts,
        "default_effort": default_effort,
        "default_enabled": bool(raw.get("default_enabled")),
    }


def _lookup_keys(model: str) -> list[str]:
    slug = (model or "").strip()
    if not slug:
        return []
    keys = [slug, slug.lower()]
    if ":" in slug:
        base, _, variant = slug.rpartition(":")
        if base and "/" in base and variant.lower() in _VARIANT_SUFFIXES:
            keys.extend([base, base.lower()])
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def lightest_openrouter_effort(spec: dict[str, Any]) -> str:
    """Fastest legal effort for this spec (skip none when mandatory)."""
    allowed = spec.get("supported_efforts")
    if isinstance(allowed, list) and allowed:
        allowed_set = {e.lower() for e in allowed}
    else:
        allowed_set = set(_EFFORTS_LIGHTEST_FIRST)
    if spec.get("mandatory"):
        allowed_set.discard("none")
    for effort in _EFFORTS_LIGHTEST_FIRST:
        if effort in allowed_set:
            return effort
    return "low"


def nearest_openrouter_effort(effort: str, spec: dict[str, Any]) -> str:
    """Map an unsupported effort onto the closest allowed rung."""
    allowed = spec.get("supported_efforts")
    if isinstance(allowed, list) and allowed:
        allowed_set = {e.lower() for e in allowed}
    else:
        allowed_set = set(_EFFORTS_LIGHTEST_FIRST)
    if spec.get("mandatory"):
        allowed_set.discard("none")
    if effort in allowed_set:
        return effort
    order = _EFFORTS_LIGHTEST_FIRST
    try:
        idx = order.index(effort)
    except ValueError:
        return lightest_openrouter_effort(spec)
    for dist in range(0, len(order)):
        for cand_idx in (idx - dist, idx + dist):
            if 0 <= cand_idx < len(order) and order[cand_idx] in allowed_set:
                return order[cand_idx]
    return lightest_openrouter_effort(spec)


def clamp_openrouter_effort(model: str, effort: str) -> str:
    """Return an effort this model will accept, using the live catalog.

    ``none`` is kept when the model allows disabling thinking. Mandatory
    models (GLM-5.3, …) are clamped to the lightest supported rung.
    """
    chosen = (effort or "none").strip().lower()
    if chosen not in _EFFORTS_LIGHTEST_FIRST:
        chosen = "none"
    spec = peek_openrouter_reasoning(model)
    if spec is None:
        if "glm-5.3" in (model or "").lower():
            if chosen in ("low", "high", "max"):
                return chosen
            if chosen == "xhigh":
                return "max"
            return "low"
        return chosen
    if chosen == "none" and not spec.get("mandatory"):
        return "none"
    if chosen == "none" and spec.get("mandatory"):
        return lightest_openrouter_effort(spec)
    allowed = spec.get("supported_efforts")
    if isinstance(allowed, list) and allowed and chosen not in {e.lower() for e in allowed}:
        return nearest_openrouter_effort(chosen, spec)
    return chosen
