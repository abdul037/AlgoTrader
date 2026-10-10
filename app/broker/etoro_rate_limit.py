"""Process-wide eToro request throttling and rate-limit circuit breaker."""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any


class EToroRateLimitError(RuntimeError):
    """Raised when eToro is in a local cooldown after API rate limiting."""


_lock = threading.Lock()
# Per-account limiter state (review 2026-10-05): one shared cooldown let a 429 on the demo
# or market-data client blind the LIVE account's backup stop for 5 minutes. Keyed by a
# short hash of the API key (never the key itself); no key -> the shared "anon" bucket.
_state: dict[str, dict[str, Any]] = {}


def _bucket(settings: Any) -> dict[str, Any]:
    raw = str(getattr(settings, "etoro_api_key", "") or "")
    key = hashlib.sha256(raw.encode()).hexdigest()[:12] if raw else "anon"
    return _state.setdefault(key, {"last_request_at": 0.0, "blocked_until": 0.0, "reason": ""})


def wait_for_etoro_slot(settings: Any) -> None:
    """Throttle eToro calls per account across all sync clients in this process."""

    min_interval = max(
        0.0,
        float(getattr(settings, "etoro_request_min_interval_seconds", 0.75) or 0.0),
    )

    while True:
        with _lock:
            bucket = _bucket(settings)
            now = time.monotonic()
            if now < bucket["blocked_until"]:
                remaining = bucket["blocked_until"] - now
                raise EToroRateLimitError(
                    f"eToro API temporarily rate-limited; retry after {remaining:.0f}s. "
                    f"Reason: {bucket['reason'] or 'rate_limit'}"
                )

            wait_seconds = (bucket["last_request_at"] + min_interval) - now
            if wait_seconds <= 0:
                bucket["last_request_at"] = now
                return

        time.sleep(min(wait_seconds, 2.0))


def etoro_cooldown_remaining(settings: Any) -> float:
    """Seconds left in this account's local cooldown (0 when calls may go out). Read-only."""

    with _lock:
        return max(0.0, _bucket(settings)["blocked_until"] - time.monotonic())


def mark_etoro_rate_limited(settings: Any, *, status_code: int, body: str) -> bool:
    """Open this account's local cooldown when eToro or Cloudflare rejects the request."""

    normalized = body.lower()
    is_rate_limited = status_code == 429 or (
        "cloudflare" in normalized and "access denied" in normalized
    )
    if not is_rate_limited:
        return False

    cooldown = max(
        30.0,
        float(getattr(settings, "etoro_rate_limit_cooldown_seconds", 300) or 300),
    )
    reason = "cloudflare_access_denied" if "cloudflare" in normalized else "http_429"
    with _lock:
        bucket = _bucket(settings)
        bucket["blocked_until"] = max(bucket["blocked_until"], time.monotonic() + cooldown)
        bucket["reason"] = reason
    return True


def compact_http_body(body: str, *, limit: int = 300) -> str:
    """Return an operator-readable error body without dumping Cloudflare HTML."""

    normalized = " ".join((body or "").split())
    lowered = normalized.lower()
    if "cloudflare" in lowered and "access denied" in lowered:
        return "cloudflare_access_denied_rate_limit"
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def etoro_rate_limit_status() -> dict[str, Any]:
    """Expose local limiter state for diagnostics (the most-blocked account)."""

    with _lock:
        now = time.monotonic()
        worst = max(_state.values(), key=lambda b: b["blocked_until"], default=None)
        remaining = max(0.0, (worst["blocked_until"] - now) if worst else 0.0)
        return {
            "cooldown_active": remaining > 0,
            "cooldown_remaining_seconds": round(remaining, 1),
            "last_reason": (worst or {}).get("reason", ""),
        }
