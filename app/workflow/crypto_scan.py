"""The 24/7 crypto scan bucket.

Crypto trades around the clock, but every equity scan bucket is gated to US
market hours, so without this bucket crypto is only scanned mid-session. This
bucket scans the crypto pairs on a fixed interval regardless of market day or
hour. It is kept out of ``SignalWorkflowService`` to keep that module within its
size budget; the service wires these functions into its bucket dispatch.
"""

from __future__ import annotations

from typing import Any

from app.broker import crypto as crypto_symbols
from app.models.workflow import WorkflowTaskResponse


def crypto_scan_symbols(service: Any) -> list[str]:
    """Canonical crypto pairs from the operator's ``crypto_symbols`` allowlist."""

    symbols: list[str] = []
    for raw in getattr(service.settings, "crypto_symbols", []) or []:
        if crypto_symbols.is_crypto_symbol(raw):
            canonical = crypto_symbols.to_alpaca_symbol(raw)
            if canonical not in symbols:
                symbols.append(canonical)
    return symbols


def crypto_bucket_enabled(service: Any) -> bool:
    return bool(getattr(service.settings, "crypto_trading_enabled", False)) and int(
        getattr(service.settings, "crypto_scan_interval_minutes", 0) or 0
    ) > 0


def crypto_scan_due(service: Any) -> bool:
    # Crypto trades 24/7, so this bucket is NOT gated to market days/hours.
    interval = int(getattr(service.settings, "crypto_scan_interval_minutes", 10) or 0)
    if interval <= 0 or not crypto_scan_symbols(service):
        return False
    return service._is_due("workflow:last_crypto_scan_at", interval)


def run_crypto_scan(service: Any, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
    """Scan the crypto pairs only, on a 24/7 cadence.

    The symbol list is passed explicitly (small, so the whole set is scanned each
    cycle without the rotating cursor the equity scans use).
    """

    symbols = crypto_scan_symbols(service)
    timeframes = service._normalized_timeframes(
        getattr(service.settings, "crypto_scan_timeframes", ["15m", "1h"])
    )
    return service._execute_guarded(
        "crypto_scan",
        lambda: service._run_scan_task(
            task="crypto_scan",
            state_key="workflow:last_crypto_scan_at",
            origin="crypto_scan",
            timeframes=timeframes,
            notify=notify,
            force_refresh=force_refresh,
            symbols=symbols,
        ),
        bucket_name="crypto_rotation",
    )
