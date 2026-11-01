"""Data-quality checks run before any strategy result (coverage, opens, jumps, cash series)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import data


def run(data_dir: Path) -> dict:
    spy = data.load_adjusted(data_dir, "SPY")
    calendar = spy.index
    report: dict = {"calendar": [str(calendar[0].date()), str(calendar[-1].date()), len(calendar)]}
    rows = []
    for symbol in data.FUNDS:
        adj = data.load_adjusted(data_dir, symbol)
        raw = data.load_raw(data_dir, symbol)
        first = adj.index[0]
        expected = calendar[calendar >= first]
        missing = expected.difference(adj.index)
        extra = adj.index.difference(calendar)
        close_ret = adj["Close"].pct_change()
        overnight = adj["Open"] / adj["Close"].shift(1) - 1.0
        intraday = adj["Close"] / adj["Open"] - 1.0
        open_eq_close = (adj["Open"] == adj["Close"]).mean()
        bad_open = ((adj["Open"] <= 0) | adj["Open"].isna()).sum()
        # adjusted close should equal raw "Adj Close" up to the latest adjustment factor
        ratio = (adj["Close"] / raw["Adj Close"].reindex(adj.index)).dropna()
        rows.append({
            "symbol": symbol,
            "first": str(first.date()),
            "bars": len(adj),
            "missing_vs_spy": len(missing),
            "extra_vs_spy": len(extra),
            "open_eq_close_share": round(float(open_eq_close), 4),
            "open_eq_close_before_2008": round(float(
                (adj.loc[:"2007-12-31", "Open"] == adj.loc[:"2007-12-31", "Close"]).mean()), 4)
            if first < pd.Timestamp("2008-01-01") else None,
            "bad_open": int(bad_open),
            "max_abs_close_ret": round(float(close_ret.abs().max()), 4),
            "max_abs_close_ret_date": str(close_ret.abs().idxmax().date()),
            "max_abs_overnight": round(float(overnight.abs().max()), 4),
            "max_abs_overnight_date": str(overnight.abs().idxmax().date()),
            "max_abs_intraday": round(float(intraday.abs().max()), 4),
            "max_abs_intraday_date": str(intraday.abs().idxmax().date()),
            "adj_vs_raw_adjclose_ratio_range": [round(float(ratio.min()), 6),
                                                round(float(ratio.max()), 6)],
        })
    report["funds"] = rows
    irx = data.load_adjusted(data_dir, data.YIELD_INDEX)["Close"]
    report["irx_missing_vs_spy"] = len(calendar.difference(irx.index))
    report["irx_range_pct"] = [float(irx.min()), float(irx.max())]
    cash, note = data.cash_daily_returns(data_dir, calendar)
    report["cash_note"] = note
    report["cash_max_daily"] = round(float(cash.max()), 6)
    report["cash_min_daily"] = round(float(cash.min()), 6)
    report["cash_days_negative"] = int((cash < 0).sum())
    bil = data.load_adjusted(data_dir, "BIL")["Close"].pct_change().dropna()
    report["bil_daily_abs_gt_0.2pct"] = [
        (str(d.date()), round(float(v), 5)) for d, v in bil[bil.abs() > 0.002].items()]
    yearly = (1 + cash).groupby(calendar.year).prod() - 1
    report["cash_by_year"] = {int(y): round(float(v), 4) for y, v in yearly.items()}
    for key, value in report.items():
        if key == "funds":
            print(pd.DataFrame(value).to_string(index=False))
        else:
            print(f"{key}: {value}")
    np.seterr(all="ignore")
    return report
