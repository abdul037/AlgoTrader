"""Performance metrics and tests for monthly return series."""

from __future__ import annotations

import numpy as np
from scipy import stats as st


def cagr(final_equity: np.ndarray, years: float) -> np.ndarray:
    return np.power(np.maximum(final_equity, 1e-12), 1.0 / years) - 1.0


def max_drawdown(equity: np.ndarray) -> np.ndarray:
    """Most negative peak-to-trough fall along the last axis (as a negative fraction)."""

    peak = np.maximum.accumulate(equity, axis=-1)
    return (equity / peak - 1.0).min(axis=-1)


def sharpe(monthly: np.ndarray, cash: np.ndarray) -> np.ndarray:
    excess = monthly - cash
    return excess.mean(axis=-1) / excess.std(axis=-1, ddof=1) * np.sqrt(12.0)


def t_test(series: np.ndarray) -> dict:
    series = np.asarray(series, dtype=float)
    n = len(series)
    mean = float(series.mean())
    se = float(series.std(ddof=1) / np.sqrt(n))
    t = mean / se
    return {"mean": mean, "t": t, "p_one_sided": float(st.t.sf(t, n - 1)), "n": n,
            "nw_t": newey_west_t(series, 3)}


def newey_west_t(series: np.ndarray, lags: int) -> float:
    x = np.asarray(series, dtype=float) - np.mean(series)
    n = len(x)
    gamma0 = float(x @ x) / n
    var = gamma0
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        var += 2.0 * weight * float(x[lag:] @ x[:-lag]) / n
    return float(np.mean(series) / np.sqrt(var / n))


def holm(p_values: list[float]) -> list[float]:
    """Holm step-down adjusted p values, in the input order."""

    m = len(p_values)
    order = np.argsort(p_values)
    adjusted = np.empty(m)
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, (m - rank) * p_values[index])
        adjusted[index] = min(1.0, running)
    return adjusted.tolist()


def calendar_years(labels: list[str], monthly: np.ndarray) -> dict[str, float]:
    years: dict[str, float] = {}
    for label, value in zip(labels, monthly, strict=True):
        year = label[:4]
        years[year] = (1.0 + years.get(year, 0.0)) * (1.0 + value) - 1.0
    return years


def relative_drawdown(labels: list[str], monthly: np.ndarray, reference: np.ndarray) -> dict:
    """Deepest fall of cumulative (1 + r) / (1 + r_ref), with the peak and trough months."""

    ratio = np.cumprod(1.0 + monthly) / np.cumprod(1.0 + reference)
    ratio = np.concatenate([[1.0], ratio])
    peak_index = np.maximum.accumulate(np.arange(len(ratio)) * (ratio >= np.maximum.accumulate(ratio)))
    dd = ratio / np.maximum.accumulate(ratio) - 1.0
    trough = int(np.argmin(dd))
    peak = int(peak_index[trough])
    names = ["start", *labels]
    recovered = next((names[i] for i in range(trough, len(ratio)) if ratio[i] >= ratio[peak]), None)
    return {"depth": float(dd[trough]), "peak_end": names[peak], "trough_end": names[trough],
            "recovered_by": recovered}
