"""SPY's daily closes for the scorecard's "vs holding SPY" comparison (operator 2026-11-01, item C).

The live scorecard compares the paper account (since 2026-06-11) and the AlgoBot eToro account
(since 2026-10-04) with simply holding SPY over the same dates. This keeps SPY's daily closes
from ``FROM_DATE`` in ``runtime_state[BENCHMARK_KEY]``, refreshed at most every
``REFRESH_HOURS`` from the phase-gate maintenance step. Display only: no trading decision reads
it. Today's bar, while the market is open, is its latest price.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now

BENCHMARK_KEY = "benchmark:spy_daily"
SYMBOL = "SPY"
FROM_DATE = "2026-06-01"  # before the paper account's first day (2026-06-11)
REFRESH_HOURS = 6.0
BARS = 200  # ~9 months of trading days


def daily_closes(history: Any) -> dict[str, float]:
    """{New York date: close} from ``FROM_DATE``; non-finite or non-positive closes left out."""

    import pandas as pd

    closes: dict[str, float] = {}
    for stamp, close in zip(history["timestamp"], history["close"]):
        moment = pd.Timestamp(stamp)
        if moment.tzinfo is not None:
            moment = moment.tz_convert("America/New_York")
        day = moment.date().isoformat()
        value = float(close)
        if day >= FROM_DATE and math.isfinite(value) and value > 0:
            closes[day] = round(value, 4)
    return closes


def refresh_benchmark(service: Any) -> dict[str, Any] | None:
    """Store SPY's closes when the stored ones are ``REFRESH_HOURS`` old; None when not due or
    there is no market data service. A failed read raises (the phase-gate step records it) and
    is retried on the next step."""

    market = getattr(service, "market_data", None)
    if market is None:
        return None
    now = utc_now()
    try:
        stored = json.loads(service.runtime_state.get(BENCHMARK_KEY) or "{}")
        if now - datetime.fromisoformat(stored["updated_at"]) < timedelta(hours=REFRESH_HOURS):
            return None
    except (TypeError, ValueError, KeyError, AttributeError):
        pass  # nothing stored yet, or unreadable: refresh
    history = market.get_history(SYMBOL, timeframe="1d", bars=BARS, write_cache=False)
    closes = daily_closes(history)
    if not closes:
        raise ValueError(f"no {SYMBOL} daily closes since {FROM_DATE}")
    payload = {"symbol": SYMBOL, "updated_at": now.isoformat(), "closes": closes}
    service.runtime_state.set(BENCHMARK_KEY, json.dumps(payload))
    return payload
