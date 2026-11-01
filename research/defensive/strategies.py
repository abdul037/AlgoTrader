"""Target-weight schedules for the four pre-registered variants and the benchmarks.

Each builder returns ``{execution date: {fund: weight}}``. A signal at a close executes at the
next trading day's open. Month-end signal dates run from the last close before the test start
(2006-12-29) to the last month-end whose next open is still in the data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FIVE = ["SPY", "EFA", "IEF", "GLD", "DBC"]


def month_ends(calendar: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(calendar, index=calendar)
    return pd.DatetimeIndex(s.groupby([calendar.year, calendar.month]).max().to_numpy())


def _next_day(calendar: pd.DatetimeIndex) -> dict[pd.Timestamp, pd.Timestamp]:
    return {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}


def signal_month_ends(calendar: pd.DatetimeIndex, first_signal: pd.Timestamp) -> pd.DatetimeIndex:
    me = month_ends(calendar)
    return me[(me >= first_signal) & (me < calendar[-1])]


def _only_changes(schedule: dict) -> dict:
    """Keep the first entry and every entry whose targets differ from the previous one."""
    out, last = {}, None
    for day in sorted(schedule):
        if schedule[day] != last:
            out[day] = schedule[day]
            last = schedule[day]
    return out


def sma_trend(
    closes: pd.DataFrame,
    funds: list[str],
    months: int,
    first_signal: pd.Timestamp,
    sleeve_weight: float,
) -> tuple[dict, pd.DataFrame]:
    """D1 (one fund, weight 1) and D3 (five sleeves of 20%): fund held when its month-end close
    is above its ``months``-month simple average. Rebalanced every month-end."""
    cal = closes.index
    me = month_ends(cal)
    monthly = closes.loc[me, funds]
    sma = monthly.rolling(months, min_periods=months).mean()
    on = (monthly > sma) & sma.notna()
    nxt = _next_day(cal)
    schedule = {}
    for day in signal_month_ends(cal, first_signal):
        schedule[nxt[day]] = {f: sleeve_weight for f in funds if bool(on.at[day, f])}
    return schedule, on.loc[signal_month_ends(cal, first_signal)]


def d1(closes: pd.DataFrame, first_signal: pd.Timestamp, months: int = 10) -> dict:
    schedule, _ = sma_trend(closes, ["SPY"], months, first_signal, 1.0)
    return _only_changes(schedule)


def d3(closes: pd.DataFrame, first_signal: pd.Timestamp, months: int = 10) -> dict:
    schedule, _ = sma_trend(closes, FIVE, months, first_signal, 0.2)
    return schedule  # rebalanced every month-end


def d2_states(closes: pd.Series, window: int = 200, band: float = 0.01) -> pd.Series:
    """Daily in/out state (True = SPY) after each close, with a +/-1% band around the SMA."""
    sma = closes.rolling(window, min_periods=window).mean()
    state = False
    out = np.zeros(len(closes), dtype=bool)
    c, s = closes.to_numpy(), sma.to_numpy()
    for i in range(len(c)):
        if not np.isnan(s[i]):
            if not state and c[i] > (1 + band) * s[i]:
                state = True
            elif state and c[i] < (1 - band) * s[i]:
                state = False
        out[i] = state
    return pd.Series(out, index=closes.index)


def d2(closes: pd.DataFrame, first_signal: pd.Timestamp) -> dict:
    states = d2_states(closes["SPY"])
    cal = closes.index
    nxt = _next_day(cal)
    schedule = {}
    for day in states.index[(states.index >= first_signal) & (states.index < cal[-1])]:
        schedule[nxt[day]] = {"SPY": 1.0} if states[day] else {}
    return _only_changes(schedule)


def d4_choices(
    closes: pd.DataFrame, cash_index: pd.Series, first_signal: pd.Timestamp
) -> pd.Series:
    cal = closes.index
    me = month_ends(cal)
    monthly = closes.loc[me, ["SPY", "EFA"]]
    r12 = monthly / monthly.shift(12) - 1.0
    cash12 = cash_index.loc[me] / cash_index.loc[me].shift(12) - 1.0
    picks = {}
    for day in signal_month_ends(cal, first_signal):
        spy, efa, cash = r12.at[day, "SPY"], r12.at[day, "EFA"], cash12.at[day]
        if np.isnan(spy) or np.isnan(efa) or np.isnan(cash):
            raise ValueError(f"D4: 12-month return missing at {day.date()}")
        best, best_ret = ("SPY", spy) if spy >= efa else ("EFA", efa)
        picks[day] = best if best_ret > cash else "IEF"
    return pd.Series(picks)


def d4(closes: pd.DataFrame, cash_index: pd.Series, first_signal: pd.Timestamp) -> dict:
    nxt = _next_day(closes.index)
    picks = d4_choices(closes, cash_index, first_signal)
    return _only_changes({nxt[day]: {fund: 1.0} for day, fund in picks.items()})


def fixed_mix(
    closes: pd.DataFrame, mix: dict[str, float], first_signal: pd.Timestamp, rebalance: bool
) -> dict:
    cal = closes.index
    nxt = _next_day(cal)
    days = signal_month_ends(cal, first_signal)
    if not rebalance:
        days = days[:1]
    return {nxt[day]: dict(mix) for day in days}
