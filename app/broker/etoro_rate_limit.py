"""Process-wide eToro request throttling and rate-limit circuit breaker."""

from __future__ import annotations

import threading
import time
from typing import Any


class EToroRateLimitError(RuntimeError):
    """Raised when eToro is in a local cooldown after API rate limiting."""


_lock = threading.Lock()
_last_request_at = 0.0
_blocked_until = 0.0
_last_reason = ""


def wait_for_etoro_slot(settings: Any) -> None:
    """Throttle eToro calls across all sync clients in this process."""

    min_interval = max(
        0.0,
        float(getattr(settings, "etoro_request_min_interval_seconds", 0.75) or 0.0),
    )

    while True:
        with _lock:
            now = time.monotonic()
            if now < _blocked_until:
                remaining = _blocked_until - now
                raise EToroRateLimitError(
                    f"eToro API temporarily rate-limited; retry after {remaining:.0f}s. "
                    f"Reason: {_last_reason or 'rate_limit'}"
                )

            wait_seconds = (_last_request_at + min_interval) - now
            if wait_seconds <= 0:
                _set_last_request_at(now)
                return

        time.sleep(min(wait_seconds, 2.0))


def mark_etoro_rate_limited(settings: Any, *, status_code: int, body: str) -> bool:
    """Open the local cooldown when eToro or Cloudflare rejects the request."""

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
        global _blocked_until, _last_reason
        _blocked_until = max(_blocked_until, time.monotonic() + cooldown)
        _last_reason = reason
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
    """Expose local limiter state for diagnostics."""

    with _lock:
        remaining = max(0.0, _blocked_until - time.monotonic())
        return {
            "cooldown_active": remaining > 0,
            "cooldown_remaining_seconds": round(remaining, 1),
            "last_reason": _last_reason,
        }


def _set_last_request_at(value: float) -> None:
    global _last_request_at
    _last_request_at = value
