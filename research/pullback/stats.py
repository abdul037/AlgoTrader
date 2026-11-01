"""Trade statistics: clustered t-stats, Holm adjustment, year and market-condition splits."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats as sps


def clustered_t(values: pd.Series, clusters: pd.Series) -> tuple[float, float, int]:
    """Mean / cluster-robust standard error (CR1: G/(G-1) correction) and its one-sided p
    (t distribution, G-1 degrees of freedom). Trades in one cluster are not independent."""

    values = values.astype(float)
    n = len(values)
    if n < 2:
        return float("nan"), float("nan"), 0
    mean = float(values.mean())
    resid = (values - mean).groupby(clusters.to_numpy()).sum()
    groups = len(resid)
    if groups < 2:
        return float("nan"), float("nan"), groups
    variance = groups / (groups - 1) * float((resid**2).sum()) / n**2
    se = math.sqrt(variance)
    t = mean / se if se > 0 else float("nan")
    p = float(sps.t.sf(t, df=groups - 1)) if np.isfinite(t) else float("nan")
    return t, p, groups


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values."""

    items = sorted(pvalues.items(), key=lambda kv: (np.nan_to_num(kv[1], nan=1.0)))
    m = len(items)
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, (name, p) in enumerate(items):
        value = min(1.0, (m - rank) * (1.0 if not np.isfinite(p) else p))
        running = max(running, value)
        adjusted[name] = running
    return adjusted


def summarize(trades: pd.DataFrame, regime: pd.Series) -> dict[str, object]:
    """Per-trade summary. ``regime`` maps signal date -> rising / falling / sideways."""

    if trades.empty:
        return {"trades": 0}
    entry_day = pd.to_datetime(trades["entry_date"])
    month = entry_day.dt.to_period("M").astype(str)
    t_day, p_day, days = clustered_t(trades["r_net"], entry_day)
    t_month, p_month, months = clustered_t(trades["r_net"], month)
    t_day_gross, _, _ = clustered_t(trades["r_gross"], entry_day)
    years = entry_day.dt.year
    by_year = trades.groupby(years)["r_net"].agg(["count", "mean"])
    cond = pd.to_datetime(trades["signal_date"]).map(regime).fillna("unknown")
    by_cond = trades.groupby(cond.to_numpy())["r_net"].agg(["count", "mean"])
    reasons = trades["reason"].value_counts().to_dict()
    hold = trades["bars_held"] if "bars_held" in trades else pd.Series(dtype=float)
    out: dict[str, object] = {
        "trades": int(len(trades)),
        "entry_days": int(days),
        "win_rate": float((trades["r_net"] > 0).mean()),
        "r_gross": float(trades["r_gross"].mean()),
        "r_net": float(trades["r_net"].mean()),
        "median_r_net": float(trades["r_net"].median()),
        "sd_r_net": float(trades["r_net"].std()),
        "cost_r": float(trades["cost_r"].mean()) if "cost_r" in trades else float("nan"),
        "risk_pct_median": float(trades["risk_pct"].median()) if "risk_pct" in trades else float("nan"),
        "t_day": t_day,
        "p_day": p_day,
        "t_day_gross": t_day_gross,
        "t_month": t_month,
        "p_month": p_month,
        "months": int(months),
        "years": {int(y): {"n": int(r["count"]), "r": float(r["mean"])} for y, r in by_year.iterrows()},
        "years_positive": int((by_year["mean"] > 0).sum()),
        "years_total": int(len(by_year)),
        "conditions": {str(k): {"n": int(r["count"]), "r": float(r["mean"])} for k, r in by_cond.iterrows()},
        "reasons": {str(k): int(v) for k, v in reasons.items()},
        "median_bars_held": float(hold.median()) if len(hold) else float("nan"),
    }
    return out
