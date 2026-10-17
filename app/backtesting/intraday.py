"""Session helpers for intraday backtests.

Two live behaviours the engine must mirror on 5m/15m bars, or the backtest
measures a different strategy than the one that trades:

* the live bot closes intraday positions before the bell
  (``app/automation/intraday_exit.py``), so a backtest that carries them
  overnight books gap P&L the live strategy never sees;
* a candidate stop-width floor measured in *session* ATR (the typical
  day's range), because a stop of a few 5m-bar ATRs is still only a fraction
  of a day's move for a trade held for hours.

Sessions are grouped by the America/New_York calendar date.
"""

from __future__ import annotations

import pandas as pd

SESSION_ATR_PERIOD = 14


def last_bar_of_session(timestamps: pd.Series) -> list[bool]:
    """Per bar: True when it is the final bar of its New York trading session."""

    ts = pd.to_datetime(timestamps, utc=True).reset_index(drop=True)
    if ts.empty:
        return []
    session = ts.dt.tz_convert("America/New_York").dt.date
    return (session != session.shift(-1)).tolist()


def session_atr_by_bar(
    frame: pd.DataFrame, *, atr_period: int = SESSION_ATR_PERIOD
) -> list[float | None]:
    """Prior-sessions ATR (true range of whole sessions) mapped onto each bar.

    Uses completed sessions only (shifted by one), so no bar sees its own day's
    range: no look-ahead.
    """

    ts = pd.to_datetime(frame["timestamp"], utc=True).reset_index(drop=True)
    session = ts.dt.tz_convert("America/New_York").dt.date
    bars = frame.reset_index(drop=True).assign(_session=session)
    daily = bars.groupby("_session", sort=True).agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")
    )
    prev_close = daily["close"].shift(1)
    true_range = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - prev_close).abs(),
            (daily["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(atr_period, min_periods=max(3, atr_period // 3)).mean().shift(1)
    mapped = session.map(atr.to_dict())
    return [None if pd.isna(value) else float(value) for value in mapped]


def floored_stop_and_target(
    *,
    fill_price: float,
    signal_price: float,
    stop: float,
    target: float | None,
    min_distance: float,
) -> tuple[float, float | None]:
    """Widen a long stop to ``min_distance`` below the fill, keeping the signal's R:R."""

    distance = fill_price - stop
    if min_distance <= 0 or distance >= min_distance:
        return stop, target
    signal_risk = signal_price - stop
    reward_to_risk = (
        (target - signal_price) / signal_risk if target is not None and signal_risk > 0 else None
    )
    new_stop = fill_price - min_distance
    new_target = (
        fill_price + reward_to_risk * min_distance if reward_to_risk is not None else target
    )
    return new_stop, new_target
