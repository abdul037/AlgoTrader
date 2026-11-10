"""V0 against production's walk-forward evidence (informational; not part of the pass rule).

1. ``production_replication``: the real engine through production's daily walk-forward on the
   live 24 names' last 500 daily bars: ``splitter_for("1d")`` folds (180-day train, 14-day
   test and step, 1-day embargo, 28-day holdout), ``_with_warmup`` with the deep warm-up
   (``_deep_warmup_bars``), the fee-inclusive cost model, a fresh $10,000 engine per fold, every
   OOS trade pooled (1R = $100) as ``app/performance/strategy_evidence.py`` pools them.
2. ``study_paths``: this study's V0 trade paths (live-window signals, no folds, trades run to
   their own exits) on the same names, for signals in the same OOS span.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research.bigger_moves import data as data_mod
from research.bigger_moves import paths as paths_mod
from research.bigger_moves import signals as sig_mod
from research.bigger_moves import variants as var_mod
from research.pullback import stats as st

# Production's strategy_evidence:verdicts (2026-11-10), from the brief: OOS trades, R before and
# after costs.
PRODUCTION = {
    "inside_bar_narrow_range_breakout": (264, 0.047, -0.038),
    "overnight_drift": (1101, 0.038, -0.017),
    "connors_rsi2_reversion": (361, 0.023, -0.032),
    "relative_strength_momentum": (288, 0.014, -0.056),
    "mean_reversion": (6, None, 0.18),
}


def _history(data_dir: Path, symbol: str) -> pd.DataFrame:
    from app.backtesting.intraday_plan import history_bars_for

    segment = data_mod.daily_segments(data_dir, symbol)[-1]
    return data_mod.engine_frame(segment).iloc[-history_bars_for("1d", SimpleNamespace()):].reset_index(drop=True)


def _production_one(args: tuple[str, str, str]) -> dict[str, object]:
    from app.backtesting.batch import _deep_warmup_bars, _with_warmup
    from app.backtesting.cost_model import CostModel, with_etoro_trade_fees
    from app.backtesting.engine import BacktestEngine, EngineConfig
    from app.backtesting.intraday_plan import splitter_for
    from app.strategies import get_strategy

    data_dir, symbol, name = args
    history = _history(Path(data_dir), symbol)
    strategy = get_strategy(name, **var_mod.SPEC_KWARGS[name])
    warmup = _deep_warmup_bars("1d", strategy)
    splitter = splitter_for("1d", SimpleNamespace())
    engine = BacktestEngine(config=EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=1.0,
                                                cost_model=with_etoro_trade_fees(CostModel(), crypto=False)))
    oos, spans, early = [], [], []
    stamps = pd.to_datetime(history["timestamp"])
    missing_folds = 0
    for window in splitter.split(history):
        result = engine.run(symbol=symbol, strategy=strategy, data=_with_warmup(window, history, warmup),
                            file_path="research", trade_window_start=window.test_start)
        oos.extend(result.trades)
        spans.append((window.test_start, window.test_end))
        # The fold frame is the warm-up (bars before train_end) plus the test window: a session
        # in the embargo [train_end, test_start) is in neither, so the test bars' indicators
        # span a missing session the live bot never skips.
        missing_folds += int(((stamps >= window.train_end) & (stamps < window.test_start)).any())
        early += [str(pd.Timestamp(day).date()) for day in pd.to_datetime(window.test_df["timestamp"]).iloc[:2]]
    holdout_window = splitter.holdout_window(history)
    holdout = engine.run(symbol=symbol, strategy=strategy, data=_with_warmup(holdout_window, history, warmup),
                         file_path="research", trade_window_start=holdout_window.test_start).trades
    return {"symbol": symbol, "strategy": name, "oos": oos, "holdout": holdout, "folds": len(spans),
            "folds_missing_a_session": missing_folds, "first_two_test_days": early,
            "oos_start": str(spans[0][0]) if spans else None, "oos_end": str(spans[-1][1]) if spans else None,
            "holdout_start": str(holdout_window.test_start)}


def _pool(trades: list[dict]) -> dict[str, object]:
    if not trades:
        return {"trades": 0}
    frame = pd.DataFrame(trades)
    costs = frame["spread_usd"] + frame["commission_usd"] + frame["financing_usd"]
    r = frame["pnl_usd"] / paths_mod.RISK_USD
    entry = pd.to_datetime(frame["entry_time"])
    held = (pd.to_datetime(frame["exit_time"]) - entry).dt.days
    t, _, _ = st.clustered_t(r, entry.dt.tz_convert(None).dt.to_period("M").astype(str))
    return {"trades": int(len(frame)), "r_net": float(r.mean()), "r_gross": float(((frame["pnl_usd"] + costs) / 100).mean()),
            "r_nofin": float(((frame["pnl_usd"] + frame["financing_usd"]) / 100).mean()), "t_month": float(t),
            "spread_usd": float(frame["spread_usd"].mean()), "fee_usd": float(frame["commission_usd"].mean()),
            "fin_usd": float(frame["financing_usd"].mean()), "cost_usd": float(costs.mean()),
            "win_rate": float((r > 0).mean()), "median_days_held": float(held.median()),
            "end_of_data_share": float((frame["reason"] == "end_of_data").mean()),
            "reasons": {str(k): int(v) for k, v in frame["reason"].value_counts().items()}}


def production_replication(data_dir: Path) -> tuple[dict[str, object], dict[str, tuple[str, str]]]:
    jobs = [(str(data_dir), symbol, name) for name in var_mod.STRATEGIES for symbol in data_mod.LIVE24]
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(_production_one, jobs))
    out, spans = {}, {}
    for name in var_mod.STRATEGIES:
        mine = [row for row in rows if row["strategy"] == name]
        out[name] = {"oos": _pool([t for row in mine for t in row["oos"]]),
                     "holdout": _pool([t for row in mine for t in row["holdout"]]),
                     "folds_per_symbol": mine[0]["folds"], "oos_start": mine[0]["oos_start"],
                     "oos_end": mine[0]["oos_end"], "holdout_start": mine[0]["holdout_start"],
                     "folds_missing_a_session": int(sum(row["folds_missing_a_session"] for row in mine)),
                     "folds": int(sum(row["folds"] for row in mine)),
                     "early_days": sorted({(row["symbol"], day) for row in mine for day in row["first_two_test_days"]}),
                     "oos_entries": sorted({(row["symbol"], str(pd.Timestamp(t["entry_time"]).date()))
                                            for row in mine for t in row["oos"]})}
        spans[name] = (mine[0]["oos_start"], mine[0]["oos_end"])
    return out, spans


def study_paths(data_dir: Path, start: str, end: str) -> dict[str, object]:
    """This study's V0 (live-window signals, no folds) on the live 24 names, signals in
    [start, end], trades run to their own exits (at most to the end of the data)."""

    begin = np.datetime64(pd.Timestamp(start).tz_convert(None).date())
    finish = np.datetime64(pd.Timestamp(end).tz_convert(None).date())
    out = {}
    for name in var_mod.STRATEGIES:
        variant = next(v for v in var_mod.VARIANTS if v.strategy == name and v.name == "V0")
        rows, signal_entries = [], set()
        for symbol in data_mod.LIVE24:
            bars = data_mod.symbol_bars(data_dir, symbol)["1d"][-1]
            signals = sig_mod.compute(name, bars.frame, sig_mod.LIVE_WINDOW["1d"], **variant.signal_kwargs())
            target, max_hold = var_mod.plan(variant, signals)
            segment = paths_mod.Segment(bars, signals, target, last=len(bars.frame) - 1, break_end=False)
            allowed = (bars.close_date >= begin) & (bars.close_date <= finish)
            for s in np.flatnonzero(signals["buy"] & allowed).tolist():
                if s + 1 < len(bars.frame):
                    signal_entries.add((symbol, str(bars.date[s + 1])))
            for trade in paths_mod.chain(segment, allowed, max_hold):
                rows.append((symbol, str(bars.date[trade[1]])) + trade)
        frame = pd.DataFrame(rows, columns=["symbol", "entry_date"] + list(paths_mod.TRADE_FIELDS))
        if frame.empty:
            out[name] = {"trades": 0, "entries": [], "signal_entries": sorted(signal_entries)}
            continue
        month = pd.to_datetime(frame["entry_date"]).dt.to_period("M").astype(str)
        t, _, _ = st.clustered_t(frame["r_net"], month)
        out[name] = {"trades": int(len(frame)), "r_net": float(frame["r_net"].mean()),
                     "r_gross": float(frame["r_gross"].mean()), "r_nofin": float((frame["r_net"] + frame["fin_r"]).mean()),
                     "t_month": float(t), "cost_usd": float((frame["spread_r"] + frame["fee_r"] + frame["fin_r"]).mean() * 100),
                     "fin_usd": float(frame["fin_r"].mean() * 100),
                     "median_bars_held": float((frame["exit_idx"] - frame["signal_idx"]).median()),
                     "entries": sorted(zip(frame["symbol"], frame["entry_date"], strict=True)),
                     "signal_entries": sorted(signal_entries)}
    return out


def run(data_dir: Path, out: Path) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    production, spans = production_replication(data_dir)
    start, end = spans["inside_bar_narrow_range_breakout"]
    study = study_paths(data_dir, start, end)
    results = {"production_brief": PRODUCTION, "replication": {}, "study": {}, "oos_span": [start, end]}
    for name in var_mod.STRATEGIES:
        replicated = production[name]
        replicated_entries = {tuple(x) for x in replicated.pop("oos_entries")}
        ours = study[name]
        our_entries = {tuple(x) for x in ours.pop("entries", [])}
        signal_entries = {tuple(x) for x in ours.pop("signal_entries", [])}
        replicated["entries_also_in_study"] = len(replicated_entries & our_entries)
        # Replicated entries whose signal bar is also a live-window (400-bar) BUY: the rest come
        # from indicators seeded in the short fold frames, or the feed.
        replicated["entries_with_live_signal"] = len(replicated_entries & signal_entries)
        early_days = {tuple(x) for x in replicated.pop("early_days")}
        replicated["off_signal_entries_on_test_bars_1_2"] = len((replicated_entries - signal_entries) & early_days)
        results["replication"][name] = replicated
        results["study"][name] = ours
    (out / "calibration.json").write_text(json.dumps(results, indent=1, default=str))
    print(json.dumps(results, indent=1, default=str)[:4000])
    return results
