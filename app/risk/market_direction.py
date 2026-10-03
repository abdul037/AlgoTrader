"""Market-direction filter: no new intraday longs while the market is falling.

The bot only buys. On 2026-09-28 it opened four tech longs (NVDA, AAPL, NVDA,
META) into a down day and all four stopped out (-$601, 44% of realized losses
to date). Before an intraday buy is submitted, this checks SPY -- plus QQQ for
tech-complex names -- and blocks the entry while the benchmark is both below
today's session VWAP and down at least ``market_direction_min_drop_pct`` from
the prior session's close.

Daily/weekly (swing) entries are exempt: some of them buy dips on purpose.
Missing or stale benchmark data never blocks (logged instead); the other risk
gates still apply.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.risk.sectors import correlation_bucket_for_symbol
from app.utils.time import utc_now

INTRADAY_TIMEFRAMES = frozenset({"1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h"})
_NY = "America/New_York"


def benchmark_state(history: pd.DataFrame, *, now: Any = None) -> dict[str, float] | None:
    """Last price, today's session VWAP and % change vs the prior session close.

    None when the frame has no bars for today's New York session or no prior session.
    """

    if history is None or history.empty:
        return None
    frame = history.assign(_ts=pd.to_datetime(history["timestamp"], utc=True)).sort_values("_ts")
    session = frame["_ts"].dt.tz_convert(_NY).dt.date
    today = pd.Timestamp(now or utc_now()).tz_convert(_NY).date()
    today_rows = frame[session == today]
    prior_rows = frame[session < today]
    if today_rows.empty or prior_rows.empty:
        return None
    volume = today_rows["volume"].astype(float)
    typical = (today_rows["high"] + today_rows["low"] + today_rows["close"]).astype(float) / 3.0
    vwap = (
        float((typical * volume).sum() / volume.sum())
        if float(volume.sum()) > 0
        else float(typical.mean())
    )
    last = float(today_rows["close"].iloc[-1])
    prior_close = float(prior_rows["close"].iloc[-1])
    if prior_close <= 0:
        return None
    return {"last": last, "vwap": vwap, "change_pct": (last / prior_close - 1.0) * 100.0}


def market_direction_reasons(
    settings: Any,
    market_data: Any,
    *,
    symbol: str,
    timeframe: str | None,
    side: str,
    logs: Any | None = None,
    now: Any = None,
) -> list[str]:
    """Blocker reasons for a new entry, or [] when the market is not falling."""

    min_drop = float(getattr(settings, "market_direction_min_drop_pct", 0.0) or 0.0)
    if (
        min_drop <= 0
        or str(side).lower() != "buy"
        or str(timeframe or "").lower() not in INTRADAY_TIMEFRAMES
    ):
        return []
    benchmarks = ["SPY"]
    if correlation_bucket_for_symbol(symbol) == "tech_complex":
        benchmarks.append("QQQ")
    reasons: list[str] = []
    for benchmark in benchmarks:
        try:
            state = benchmark_state(
                market_data.get_history(benchmark, timeframe="5m", bars=200), now=now
            )
        except Exception as exc:  # noqa: BLE001 - a data outage must not halt trading
            state, error = None, str(exc)
        else:
            error = "no bars for today's session"
        if state is None:
            if logs is not None:
                logs.log(
                    "market_direction_check_unavailable",
                    {"symbol": symbol, "benchmark": benchmark, "error": error},
                )
            continue
        if state["last"] < state["vwap"] and state["change_pct"] <= -min_drop:
            reasons.append(f"market_direction_down:{benchmark}:{state['change_pct']:.2f}%")
    return reasons
