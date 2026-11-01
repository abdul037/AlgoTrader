"""Checks around the main study (none of them votes in the pass rule).

1. ``execution_parity``: this package's trade paths (``engine`` exit rule) against the real
   ``app.backtesting.engine.BacktestEngine`` (zero-cost model) on the same bars.
2. ``production_replication``: production's daily walk-forward (``splitter_for("1d")`` folds,
   train-bar warm-up, eToro fee-inclusive cost model, 1R = $100) on the live 24-name universe
   over the last 500 bars, next to this study's V1 on the same names and window.
3. ``random_entry_control`` (added after the main results were seen; diagnostic only): every
   V1 trade gets matched entries on random bars of the same symbol in the same calendar year,
   with the same stop distance (% of entry) and the same target multiple, exited by the same
   rules. If V1's R is mostly the drift of stocks that went up, the control earns it too.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research.pullback import data as data_mod
from research.pullback import signals as sig_mod
from research.pullback import stats as st
from research.pullback import trades as tr
from research.pullback.study import EVAL_START, LIVE24, LIVE_WINDOW

PARITY_SYMBOLS = ["WMT", "PYPL", "DIS", "TSLA", "AAPL", "JPM"]


def _bars_for_engine(frame: pd.DataFrame) -> pd.DataFrame:
    bars = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"]).dt.tz_localize("UTC")
    return bars


def _parity_one(args: tuple[str, str]) -> dict[str, object]:
    from app.backtesting.cost_model import zero_cost_model
    from app.backtesting.engine import BacktestEngine, EngineConfig
    from app.strategies.ema_trend_stack import EMATrendStackStrategy

    data_dir, symbol = args
    frame, breaks = data_mod.clean_symbol(Path(data_dir), symbol)
    bars = _bars_for_engine(frame)
    engine = BacktestEngine(config=EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=1.0,
                                                cost_model=zero_cost_model()))
    result = engine.run(symbol=symbol, strategy=EMATrendStackStrategy(timeframe="1d"), data=bars,
                        file_path="research", trade_window_start=pd.Timestamp(EVAL_START, tz="UTC"))
    engine_trades = pd.DataFrame(result.trades)
    engine_trades["entry_date"] = pd.to_datetime(engine_trades["entry_time"]).dt.tz_localize(None).dt.normalize()
    engine_trades["exit_date"] = pd.to_datetime(engine_trades["exit_time"]).dt.tz_localize(None).dt.normalize()

    sym_bars = tr.from_signal_frame(symbol, "", sig_mod.compute_signals(frame), breaks)
    ours = tr.chain(tr.signal_trades(sym_bars, "engine", eval_start=EVAL_START.to_datetime64()))
    ours = ours.assign(entry_date=pd.to_datetime(ours["entry_date"]), exit_date=pd.to_datetime(ours["exit_date"]))
    merged = engine_trades.merge(ours, on="entry_date", how="outer", suffixes=("_engine", "_ours"), indicator=True)
    both = merged[merged["_merge"] == "both"]
    same_exit = (both["exit_date_engine"] == both["exit_date_ours"]) & np.isclose(
        both["exit_price"], both["exit"], rtol=1e-9)
    same_entry = np.isclose(both["entry_price"], both["entry"], rtol=1e-9)
    only_engine = merged[merged["_merge"] == "left_only"]
    return {
        "symbol": symbol,
        "engine_trades": int(len(engine_trades)),
        "our_trades": int(len(ours)),
        "matched_entries": int(len(both)),
        "matched_exact": int((same_exit & same_entry).sum()),
        "only_engine": [
            {"entry": str(r.entry_date.date()), "exit": str(r.exit_date_engine.date()), "reason": r.reason_engine}
            for r in only_engine.itertuples()
        ],
        "only_ours": [str(d.date()) for d in merged.loc[merged["_merge"] == "right_only", "entry_date"]],
        "exit_mismatches": [
            {"entry": str(r.entry_date.date()), "engine": [str(r.exit_date_engine.date()), r.exit_price, r.reason_engine],
             "ours": [str(pd.Timestamp(r.exit_date_ours).date()), r.exit, r.reason_ours]}
            for r in both[~(same_exit & same_entry)].itertuples()
        ],
    }


def execution_parity(data_dir: Path) -> list[dict[str, object]]:
    with ProcessPoolExecutor(max_workers=4) as pool:
        return list(pool.map(_parity_one, [(str(data_dir), s) for s in PARITY_SYMBOLS]))


def _production_one(args: tuple[str, str, int]) -> dict[str, object]:
    from app.backtesting.batch import _with_warmup
    from app.backtesting.cost_model import CostModel, with_etoro_trade_fees
    from app.backtesting.engine import BacktestEngine, EngineConfig
    from app.backtesting.intraday_plan import splitter_for
    from app.strategies.ema_trend_stack import EMATrendStackStrategy

    data_dir, symbol, bars_count = args
    frame, _ = data_mod.clean_symbol(Path(data_dir), symbol)
    history = _bars_for_engine(frame).iloc[-bars_count:].reset_index(drop=True)
    config = EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=1.0,
                          cost_model=with_etoro_trade_fees(CostModel(), crypto=False))
    engine = BacktestEngine(config=config)
    splitter = splitter_for("1d", SimpleNamespace())  # every walk_forward_* default
    fold_trades = []
    folds = 0
    for window in splitter.split(history):
        folds += 1
        result = engine.run(symbol=symbol, strategy=EMATrendStackStrategy(timeframe="1d"), data=_with_warmup(window),
                            file_path="research", trade_window_start=window.test_start)
        fold_trades.extend(result.trades)
    # The same engine and costs over the whole window in one run (no fold cut-offs), and with
    # this study's costs replaced by zero for the gross figure.
    full = engine.run(symbol=symbol, strategy=EMATrendStackStrategy(timeframe="1d"), data=history,
                      file_path="research", trade_window_start=history["timestamp"].iloc[0] + pd.Timedelta(days=180))
    return {"symbol": symbol, "folds": folds, "oos_trades": fold_trades, "full_trades": full.trades}


def production_replication(data_dir: Path) -> dict[str, object]:
    jobs = [(str(data_dir), s, 500) for s in LIVE24]
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(_production_one, jobs))
    oos = pd.DataFrame([t for r in rows for t in r["oos_trades"]])
    full = pd.DataFrame([t for r in rows for t in r["full_trades"]])

    def pooled(trades: pd.DataFrame) -> dict[str, float]:
        if trades.empty:
            return {"trades": 0}
        r = trades["pnl_usd"] / 100.0
        held = (pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"])).dt.days
        t, p, _ = st.clustered_t(r, pd.to_datetime(trades["entry_time"]).dt.normalize())
        return {"trades": int(len(trades)), "r": float(r.mean()), "t_day": t,
                "win_rate": float((r > 0).mean()), "median_days_held": float(held.median()),
                "end_of_data_share": float((trades["reason"] == "end_of_data").mean()),
                "financing_r": float(trades["financing_usd"].mean() / 100.0),
                "other_cost_r": float((trades["spread_usd"] + trades["commission_usd"] + trades["slippage_usd"]
                                       + trades["regulatory_usd"]).mean() / 100.0),
                "reasons": trades["reason"].value_counts().to_dict()}

    first_test = min(pd.to_datetime(t["entry_time"]) for r in rows for t in r["oos_trades"]) if len(oos) else None
    return {"folds_per_symbol": rows[0]["folds"], "first_oos_entry": str(first_test),
            "walk_forward_oos": pooled(oos), "same_engine_no_folds": pooled(full)}


def live24_study_v1(data_dir: Path) -> dict[str, object]:
    """V1 with this study's rules and costs on the live 24 names, signals in the live window."""

    frames = []
    for symbol in LIVE24:
        frame, breaks = data_mod.clean_symbol(data_dir, symbol)
        bars = tr.from_signal_frame(symbol, "", sig_mod.compute_signals(frame), breaks)
        for rule in ("bracket", "engine"):
            trades = tr.signal_trades(bars, rule, eval_start=LIVE_WINDOW[0].to_datetime64())
            frames.append(trades.assign(rule=rule))
    pool = pd.concat(frames, ignore_index=True)
    out = {}
    for rule in ("bracket", "engine"):
        chained = tr.chain(pool[pool["rule"] == rule].reset_index(drop=True))
        chained = chained[pd.to_datetime(chained["signal_date"]) <= LIVE_WINDOW[1]]
        regime = pd.Series(dtype=object)
        summary = st.summarize(chained, regime)
        out[rule] = {k: summary[k] for k in ("trades", "win_rate", "r_gross", "r_net", "t_day", "median_bars_held", "reasons")}
        out[rule]["open_at_end"] = int((chained["reason"] == "end_of_data").sum())
    return out


# --- 3. Random-entry control ------------------------------------------------------------------

def _control_one(args: tuple[str, str, list[dict], int, int]) -> list[dict]:
    data_dir, symbol, trades, draws, seed = args
    frame, breaks = data_mod.clean_symbol(Path(data_dir), symbol)
    signal_frame = sig_mod.compute_signals(frame)
    bars = tr.from_signal_frame(symbol, "", signal_frame, breaks)
    n = len(bars.close)
    years = pd.DatetimeIndex(bars.dates).year.to_numpy()
    rng = np.random.default_rng(seed)
    blocked = np.zeros(n, dtype=bool)
    for b in bars.breaks:
        blocked[max(b - 1, 0) : b + tr.BREAK_WARMUP_BARS] = True
    out = []
    for trade in trades:
        year = pd.Timestamp(trade["entry_date"]).year
        choices = np.flatnonzero((years == year) & ~blocked & (np.arange(n) < n - 1) & (np.arange(n) >= sig_mod.REQUIRED_BARS))
        if len(choices) == 0:
            continue
        for s in rng.choice(choices, size=draws, replace=True):
            e = s + 1
            entry = float(bars.open[e])
            stop = entry * (1.0 - trade["risk_pct"])
            target = entry + trade["target_multiple"] * (entry - stop)
            later = bars.breaks[bars.breaks > s]
            last_idx = int(later[0]) - 1 if len(later) else n - 1
            k, price, reason, _ = tr._exit_path(bars, e, entry, stop, target, "bracket", last_idx)
            risk = entry - stop
            out.append({"symbol": symbol, "year": year, "entry_date": bars.dates[e], "r_gross": (price - entry) / risk,
                        "cost_r": tr.ROUND_TRIP_COST * entry / risk, "bars_held": k - s, "reason": reason})
    return out


def random_entry_control(data_dir: Path, v1: pd.DataFrame, draws: int = 5) -> dict[str, object]:
    v1 = v1.assign(target_multiple=(v1["target"] - v1["entry"]) / (v1["entry"] - v1["stop"]))
    jobs = []
    for i, (symbol, group) in enumerate(v1.groupby("symbol")):
        records = group[["entry_date", "risk_pct", "target_multiple"]].to_dict("records")
        jobs.append((str(data_dir), symbol, records, draws, 20261101 + i))
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = [row for chunk in pool.map(_control_one, jobs, chunksize=4) for row in chunk]
    control = pd.DataFrame(rows)
    control["r_net"] = control["r_gross"] - control["cost_r"]
    # Difference per V1 trade vs its own year-and-symbol control mean, clustered by entry day.
    keyed = control.groupby(["symbol", "year"])["r_net"].mean()
    v1_years = pd.to_datetime(v1["entry_date"]).dt.year
    matched = v1["r_net"].to_numpy() - keyed.reindex(list(zip(v1["symbol"], v1_years, strict=True))).to_numpy()
    diff = pd.Series(matched, index=v1.index).dropna()
    t, p, _ = st.clustered_t(diff, pd.to_datetime(v1.loc[diff.index, "entry_date"]))
    t_m, p_m, _ = st.clustered_t(diff, pd.to_datetime(v1.loc[diff.index, "entry_date"]).dt.to_period("M").astype(str))
    by_year = pd.DataFrame({"v1": v1.groupby(v1_years)["r_net"].mean(), "control": control.groupby("year")["r_net"].mean()})
    return {
        "draws_per_trade": draws,
        "control_trades": int(len(control)),
        "control_r_gross": float(control["r_gross"].mean()),
        "control_r_net": float(control["r_net"].mean()),
        "control_win_rate": float((control["r_net"] > 0).mean()),
        "control_mean_bars_held": float(control["bars_held"].mean()),
        "v1_r_net": float(v1["r_net"].mean()),
        "v1_minus_control": float(diff.mean()),
        "t_day": t, "p_day": p, "t_month": t_m, "p_month": p_m,
        "by_year": {int(y): {"v1": float(r["v1"]), "control": float(r["control"])} for y, r in by_year.iterrows()},
    }


def run(data_dir: Path, out: Path) -> dict[str, object]:
    results: dict[str, object] = {}
    results["execution_parity"] = execution_parity(data_dir)
    results["production_replication"] = production_replication(data_dir)
    results["live24_study"] = live24_study_v1(data_dir)
    v1 = pd.read_parquet(out / "trades_V1.parquet")
    results["random_entry_control"] = random_entry_control(data_dir, v1)
    (out / "calibration.json").write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=1, default=str)[:6000])
    return results

