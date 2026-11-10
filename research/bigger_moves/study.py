"""The pre-registered study: trades, random-entry controls, statistics and the pass rule.

``run(period="main")`` simulates every variant on signals from 2011-01-03 to 2025-10-31 (trades
cut at that close) and writes ``OUT/trades_main.parquet`` and ``OUT/results_main.json``.
``run(period="holdout")`` is the sealed holdout (signals from 2025-11-03 to the end of the
data, starting flat); it reads the main results, writes ``OUT/trades_holdout.parquet`` and
``OUT/results_holdout.json`` with the final verdicts. Run it only after the main results are
committed.

Universes (``universe`` column): ``pit`` = S&P 500 members on days they were in the index;
``all`` = the same members on every day, and the three live ETFs (the live-24 view reads
``all`` for the 24 names).
"""

from __future__ import annotations

import json
import math
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from research.bigger_moves import data as data_mod
from research.bigger_moves import paths as paths_mod
from research.bigger_moves import signals as sig_mod
from research.bigger_moves import variants as var_mod
from research.pullback import stats as st

DRAWS = 5
SEED = 20261110
MIN_R = 0.03
MIN_TRADES = 40
MIN_HOLDOUT_TRADES = 10
ALPHA = 0.05
ALPHA_INFO = 0.10
SUB_MIN_TRADES = 10


def _period_bounds(bars: data_mod.Bars, period: str) -> tuple[int, np.ndarray]:
    """(last bar index usable in the period, signal-day mask)."""

    close_day = bars.close_date
    if period == "main":
        inside = close_day <= np.datetime64(data_mod.MAIN_END.date())
        last = int(np.flatnonzero(inside)[-1]) if inside.any() else -1
        mask = inside & (close_day >= np.datetime64(data_mod.EVAL_START.date()))
    else:
        last = len(close_day) - 1
        mask = close_day >= np.datetime64(data_mod.HOLDOUT_START.date())
    return last, mask


def _symbol_job(args: tuple[str, str, str | None, str, int]) -> pd.DataFrame:
    data_dir, symbol, date_added, period, draws = args
    bars_by_tf = data_mod.symbol_bars(Path(data_dir), symbol)
    universes = ("pit", "all") if date_added else ("all",)
    added = np.datetime64(pd.Timestamp(date_added).date()) if date_added else None
    frames = []
    for variant in var_mod.VARIANTS:
        segments = bars_by_tf[variant.timeframe]
        required = sig_mod.REQUIRED_BARS[variant.strategy]
        prepared = []
        for index, bars in enumerate(segments):
            last, mask = _period_bounds(bars, period)
            if last < 1:
                continue
            signals = sig_mod.compute(variant.strategy, bars.frame, sig_mod.LIVE_WINDOW[variant.timeframe],
                                      **variant.signal_kwargs())
            target, max_hold = var_mod.plan(variant, signals)
            break_end = index < len(segments) - 1 and last == len(bars.frame) - 1
            segment = paths_mod.Segment(bars, signals, target, last=last, break_end=break_end)
            positions = np.arange(len(bars.frame))
            usable = (positions >= required - 1) & (positions + 1 <= last)
            years = bars.close_date.astype("datetime64[Y]").astype(int) + 1970
            prepared.append((bars, segment, mask, usable, years))
        for universe in universes:
            rng = np.random.default_rng(SEED ^ zlib.crc32(f"{symbol}|{variant.key}|{universe}|{period}".encode()))
            pool, year_of, records = [], [], []
            for bars, segment, mask, usable, years in prepared:
                allowed = mask & (bars.close_date >= added) if universe == "pit" else mask
                pool.append((segment, np.flatnonzero(allowed & usable)))
                year_of.append(years)
                for trade in paths_mod.chain(segment, allowed, max_hold):
                    records.append((segment, bars, years, trade))
            rows = []
            for segment, bars, years, trade in records:
                s, e, k = trade[0], trade[1], trade[2]
                target = segment.target[s]
                ctrl = paths_mod.control(pool, year_of, int(years[s]), float(segment.price[s]), float(segment.stop[s]),
                                         None if math.isnan(target) else float(target), max_hold, draws, rng)
                rows.append(trade + (str(bars.close_date[s]), str(bars.date[e]), str(bars.date[k])) + ctrl)
            if rows:
                frame = pd.DataFrame(rows, columns=list(paths_mod.TRADE_FIELDS) + [
                    "signal_date", "entry_date", "exit_date", "ctrl_net", "ctrl_gross", "ctrl_nofin", "ctrl_n"])
                frame.insert(0, "universe", universe)
                frame.insert(0, "variant", variant.name)
                frame.insert(0, "strategy", variant.strategy)
                frame.insert(0, "symbol", symbol)
                frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["bars_held"] = out["exit_idx"] - out["signal_idx"]
    return out.drop(columns=["signal_idx", "entry_idx", "exit_idx"])


def simulate(data_dir: Path, period: str, only: list[str] | None = None) -> pd.DataFrame:
    members = data_mod.members(data_dir)
    jobs = [(str(data_dir), row.yf_symbol, str(row.date_added), period, DRAWS) for row in members.itertuples()]
    jobs += [(str(data_dir), etf, None, period, DRAWS) for etf in data_mod.LIVE24_ETFS]
    if only:
        jobs = [job for job in jobs if job[1] in only]
    with ProcessPoolExecutor(max_workers=4) as pool:
        frames = [frame for frame in pool.map(_symbol_job, jobs, chunksize=2) if not frame.empty]
    trades = pd.concat(frames, ignore_index=True)
    trades["member"] = ~trades["symbol"].isin(data_mod.LIVE24_ETFS)
    for column in ("signal_date", "entry_date", "exit_date"):
        trades[column] = pd.to_datetime(trades[column])
    return trades


# --- statistics ------------------------------------------------------------------------------

def _t(values: pd.Series, clusters: pd.Series) -> tuple[float, float]:
    if len(values) < 3:
        return math.nan, math.nan
    t, p, _ = st.clustered_t(values, clusters)
    return float(t), float(p)


def summarize(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"trades": 0}
    month = trades["entry_date"].dt.to_period("M").astype(str)
    day = trades["entry_date"]
    matched = trades[trades["ctrl_n"] > 0]
    excess = matched["r_net"] - matched["ctrl_net"]
    t_month, p_month = _t(excess, matched["entry_date"].dt.to_period("M").astype(str))
    t_day, p_day = _t(excess, matched["entry_date"])
    r_t_month, r_p_month = _t(trades["r_net"], month)
    out: dict[str, object] = {
        "trades": int(len(trades)),
        "symbols": int(trades["symbol"].nunique()),
        "win_rate": float((trades["r_net"] > 0).mean()),
        "r_net": float(trades["r_net"].mean()),
        "r_gross": float(trades["r_gross"].mean()),
        "r_nofin": float((trades["r_net"] + trades["fin_r"]).mean()),
        "spread_usd": float(trades["spread_r"].mean() * 100),
        "fee_usd": float(trades["fee_r"].mean() * 100),
        "fin_usd": float(trades["fin_r"].mean() * 100),
        "cost_usd": float((trades["spread_r"] + trades["fee_r"] + trades["fin_r"]).mean() * 100),
        "notional_median": float(trades["notional"].median()),
        "risk_pct_median": float(trades["risk_pct"].median()),
        "bars_held_median": float(trades["bars_held"].median()),
        "bars_held_mean": float(trades["bars_held"].mean()),
        "r_net_t_month": r_t_month,
        "r_net_p_month": r_p_month,
        "reasons": {str(k): int(v) for k, v in trades["reason"].value_counts().items()},
        "gaps": {str(k): int(v) for k, v in trades["gap"].value_counts().items() if k},
        "matched": int(len(matched)),
        "ctrl_net": float(matched["ctrl_net"].mean()) if len(matched) else math.nan,
        "ctrl_gross": float(matched["ctrl_gross"].mean()) if len(matched) else math.nan,
        "excess": float(excess.mean()) if len(matched) else math.nan,
        "excess_gross": float((matched["r_gross"] - matched["ctrl_gross"]).mean()) if len(matched) else math.nan,
        "excess_t_month": t_month,
        "excess_p_month": p_month,
        "excess_t_day": t_day,
        "excess_p_day": p_day,
        "months": int(month.nunique()),
        "entry_days": int(day.nunique()),
    }
    years = trades["signal_date"].dt.year
    by_year = []
    for year, group in trades.groupby(years):
        g = group[group["ctrl_n"] > 0]
        by_year.append({"year": int(year), "n": int(len(group)), "r_net": float(group["r_net"].mean()),
                        "excess": float((g["r_net"] - g["ctrl_net"]).mean()) if len(g) else math.nan,
                        "excess_sum": float((g["r_net"] - g["ctrl_net"]).sum()) if len(g) else 0.0})
    out["years"] = by_year
    subs = []
    for name, start, end in data_mod.SUB_PERIODS:
        group = trades[(trades["signal_date"] >= start) & (trades["signal_date"] <= end)]
        g = group[group["ctrl_n"] > 0]
        r = float(group["r_net"].mean()) if len(group) else math.nan
        ex = float((g["r_net"] - g["ctrl_net"]).mean()) if len(g) else math.nan
        holds = len(group) >= SUB_MIN_TRADES and r >= MIN_R and ex > 0
        subs.append({"period": name, "n": int(len(group)), "r_net": r, "excess": ex, "holds": bool(holds)})
    out["sub_periods"] = subs
    if by_year:
        best = max(by_year, key=lambda row: row["excess_sum"])
        rest = trades[years != best["year"]]
        g = rest[rest["ctrl_n"] > 0]
        out["drop_best_year"] = {
            "year": best["year"], "n": int(len(rest)),
            "r_net": float(rest["r_net"].mean()) if len(rest) else math.nan,
            "excess": float((g["r_net"] - g["ctrl_net"]).mean()) if len(g) else math.nan,
        }
    return out


def _views(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "pit": trades[(trades["universe"] == "pit")],
        "all": trades[(trades["universe"] == "all") & trades["member"]],
        "live24": trades[(trades["universe"] == "all") & trades["symbol"].isin(data_mod.LIVE24)],
    }


def main_results(trades: pd.DataFrame) -> dict[str, object]:
    views = _views(trades)
    results: dict[str, object] = {"variants": {}, "skipped": var_mod.SKIPPED}
    for variant in var_mod.VARIANTS:
        entry = {"strategy": variant.strategy, "variant": variant.name, "label": variant.label,
                 "timeframe": variant.timeframe, "change": variant.change}
        for view, frame in views.items():
            subset = frame[(frame["strategy"] == variant.strategy) & (frame["variant"] == variant.name)]
            entry[view] = summarize(subset)
        results["variants"][variant.key] = entry
    pvalues = {key: v["pit"].get("excess_p_month", math.nan) for key, v in results["variants"].items()}
    holm = st.holm({k: (p if p == p else 1.0) for k, p in pvalues.items()})
    for key, value in results["variants"].items():
        pit = value["pit"]
        pit["holm_p"] = float(holm[key])
        rule1_main = pit.get("trades", 0) >= MIN_TRADES and pit.get("r_net", -1) >= MIN_R
        rule2 = pit.get("excess", -1) > 0 and pit["holm_p"] < ALPHA
        rule2_info = pit.get("excess", -1) > 0 and pit["holm_p"] < ALPHA_INFO
        subs = pit.get("sub_periods", [])
        drop = pit.get("drop_best_year", {})
        rule3a = sum(row["holds"] for row in subs) >= 2
        rule3b = bool(drop) and drop.get("r_net", -1) >= MIN_R and drop.get("excess", -1) > 0
        value["checks_main"] = {"rule1_main": bool(rule1_main), "rule2": bool(rule2), "rule2_at_0.10": bool(rule2_info),
                                "rule3a": bool(rule3a), "rule3b": bool(rule3b), "rule4": True}
    results["family_size"] = len(pvalues)
    return results


def holdout_results(trades: pd.DataFrame, main: dict[str, object]) -> dict[str, object]:
    views = _views(trades)
    results: dict[str, object] = {"variants": {}}
    for variant in var_mod.VARIANTS:
        entry = {}
        for view, frame in views.items():
            subset = frame[(frame["strategy"] == variant.strategy) & (frame["variant"] == variant.name)]
            summary = summarize(subset)
            entry[view] = {k: summary.get(k) for k in ("trades", "symbols", "win_rate", "r_net", "r_gross", "r_nofin",
                                                       "cost_usd", "ctrl_net", "excess", "excess_t_month",
                                                       "bars_held_median", "reasons")}
        checks = dict(main["variants"][variant.key]["checks_main"])
        pit = entry["pit"]
        checks["rule1_holdout"] = bool(pit.get("trades", 0) >= MIN_HOLDOUT_TRADES and (pit.get("r_net") or -1) > 0)
        checks["rule1"] = checks["rule1_main"] and checks["rule1_holdout"]
        checks["rule3"] = checks["rule3a"] and checks["rule3b"]
        checks["pass"] = checks["rule1"] and checks["rule2"] and checks["rule3"] and checks["rule4"]
        entry["checks"] = checks
        results["variants"][variant.key] = entry
    results["passing"] = [key for key, value in results["variants"].items() if value["checks"]["pass"]]
    return results


def run(data_dir: Path, out: Path, *, period: str, only: list[str] | None = None) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    if period == "holdout":
        main_path = out / "results_main.json"
        if not main_path.exists():
            raise SystemExit("run the main period first (results_main.json is missing)")
        main = json.loads(main_path.read_text())
    trades = simulate(data_dir, period, only)
    trades.to_parquet(out / f"trades_{period}.parquet", index=False)
    results = main_results(trades) if period == "main" else holdout_results(trades, main)
    results["period"] = period
    results["rows"] = int(len(trades))
    (out / f"results_{period}.json").write_text(json.dumps(results, indent=1, default=str))
    print(f"{period}: {len(trades)} trade rows -> {out}")
    return results
