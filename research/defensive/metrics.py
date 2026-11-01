"""Performance metrics, episodes and the paired block bootstrap of the Sharpe difference."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.defensive.strategies import month_ends


def years_between(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return (b - a).days / 365.25


def cagr(equity: pd.Series) -> float:
    return float(
        (equity.iloc[-1] / equity.iloc[0]) ** (1 / years_between(equity.index[0], equity.index[-1]))
        - 1
    )


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp]:
    dd = equity / equity.cummax() - 1.0
    trough = dd.idxmin()
    peak = equity.loc[:trough].idxmax()
    return float(dd.min()), peak, trough


def monthly_returns(equity: pd.Series) -> pd.Series:
    me = month_ends(equity.index[1:])
    points = equity.loc[pd.DatetimeIndex([equity.index[0]]).append(me)]
    return points.pct_change().dropna()


def monthly_cash(cash_daily: pd.Series, months: pd.DatetimeIndex, base: pd.Timestamp) -> pd.Series:
    grown = (1.0 + cash_daily.loc[cash_daily.index > base]).cumprod()
    level = pd.concat([pd.Series([1.0], index=[base]), grown])
    return level.loc[pd.DatetimeIndex([base]).append(months)].pct_change().dropna()


def sharpe(excess: np.ndarray) -> float:
    return float(excess.mean() / excess.std(ddof=1) * np.sqrt(12))


def sortino(excess: np.ndarray) -> float:
    downside = np.sqrt(np.mean(np.minimum(excess, 0.0) ** 2))
    return float(excess.mean() / downside * np.sqrt(12))


def calendar_years(equity: pd.Series) -> pd.Series:
    idx = equity.index
    ends = pd.Series(idx, index=idx).groupby(idx.year).max()
    points = equity.loc[pd.DatetimeIndex([idx[0]]).append(pd.DatetimeIndex(ends.to_numpy()[1:]))]
    rets = points.pct_change().dropna()
    rets.index = rets.index.year
    return rets


def find_episode(spy_close: pd.Series, lo: str, hi: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    window = spy_close.loc[lo:hi]
    trough = window.idxmin()
    peak = window.loc[:trough].idxmax()
    return peak, trough


def episode(equity: pd.Series, peak: pd.Timestamp, trough: pd.Timestamp) -> dict:
    after = trough + pd.DateOffset(months=12)
    after = equity.index[equity.index <= after][-1]
    span = equity.loc[peak:after]
    dd = float((span / span.cummax() - 1.0).min())
    return {
        "peak_to_trough": float(equity.loc[trough] / equity.loc[peak] - 1),
        "max_dd_to_trough_plus_12m": dd,
        "peak_to_trough_plus_12m": float(equity.loc[after] / equity.loc[peak] - 1),
        "end": str(after.date()),
    }


def period(equity: pd.Series, start: pd.Timestamp) -> dict:
    span = equity.loc[start:]
    return {
        "return": float(span.iloc[-1] / span.iloc[0] - 1),
        "max_dd": float((span / span.cummax() - 1.0).min()),
    }


def block_bootstrap_sharpe_diff(
    a: np.ndarray, b: np.ndarray, block: int = 12, reps: int = 10_000, seed: int = 20261101
) -> dict:
    """Paired circular block bootstrap of Sharpe(a) - Sharpe(b) on monthly excess returns."""
    n = len(a)
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(reps, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n] % n
    sa, sb = a[idx], b[idx]
    sh_a = sa.mean(axis=1) / sa.std(axis=1, ddof=1) * np.sqrt(12)
    sh_b = sb.mean(axis=1) / sb.std(axis=1, ddof=1) * np.sqrt(12)
    diff = sh_a - sh_b
    return {
        "diff": sharpe(a) - sharpe(b),
        "ci95": [float(np.percentile(diff, 2.5)), float(np.percentile(diff, 97.5))],
        "share_le_0": float((diff <= 0).mean()),
    }
