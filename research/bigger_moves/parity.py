"""Parity checks; both must pass before the study runs.

1. ``signal_parity``: each real class's ``generate_signal`` on every bar, given the live window
   (the last 400 daily or 520 weekly bars), against ``signals.compute`` run once per segment:
   action, and for a BUY its price, stop and target.
2. ``execution_parity``: the real ``BacktestEngine`` (production fee-inclusive cost model, full
   history, every variant through ``variants.make_strategy``) against ``paths.chain`` on the
   engine's view of the signals (``window=None``): entry and exit dates, fills, reasons,
   financing per dollar of notional and fees.
"""

from __future__ import annotations

import inspect
import json
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from research.bigger_moves import data as data_mod
from research.bigger_moves import paths as paths_mod
from research.bigger_moves import signals as sig_mod
from research.bigger_moves import variants as var_mod

REL_TOL = 1e-6
PARITY_SYMBOLS = ["WMT", "PYPL", "DIS", "TSLA", "AAPL", "JPM"]
EXECUTION_SYMBOLS = ["WMT", "AAPL", "TSLA"]


def _signal_one(args: tuple[str, str, str, str]) -> dict[str, object]:
    from app.strategies import get_strategy

    data_dir, symbol, strategy_name, timeframe = args
    variant = next(v for v in var_mod.VARIANTS if v.strategy == strategy_name
                   and v.name == ("V5" if timeframe == "1w" else "V0"))
    window = sig_mod.LIVE_WINDOW[timeframe]
    strategy = get_strategy(strategy_name, **variant.constructor_kwargs())
    counts = {"bars": 0, "buy_real": 0, "buy_vec": 0, "sell_real": 0, "sell_vec": 0}
    mismatches: list[dict[str, object]] = []
    max_rel = {"price": 0.0, "stop": 0.0, "target": 0.0}
    for bars in data_mod.symbol_bars(Path(data_dir), symbol)[timeframe]:
        frame = data_mod.engine_frame(bars.frame)
        vec = sig_mod.compute(strategy_name, bars.frame, window, **variant.signal_kwargs())
        for index in range(len(frame)):
            signal = strategy.generate_signal(frame.iloc[max(0, index + 1 - window): index + 1], symbol)
            action = getattr(getattr(signal, "action", None), "value", None)
            real_buy, real_sell = action == "buy", action == "sell"
            vec_buy, vec_sell = bool(vec["buy"][index]), bool(vec["sell"][index])
            counts["bars"] += 1
            counts["buy_real"] += real_buy
            counts["buy_vec"] += vec_buy
            counts["sell_real"] += real_sell
            counts["sell_vec"] += vec_sell
            row = {"date": str(bars.date[index]), "real": action, "vec_buy": vec_buy, "vec_sell": vec_sell}
            if real_buy != vec_buy or real_sell != vec_sell:
                mismatches.append(row)
                continue
            if real_buy:
                for name, real_value in (("price", signal.price), ("stop", signal.stop_loss),
                                         ("target", signal.take_profit)):
                    vec_value = float(vec[name][index])
                    rel = abs(float(real_value) - vec_value) / max(abs(float(real_value)), 1e-12)
                    max_rel[name] = max(max_rel[name], rel)
                    if not math.isclose(float(real_value), vec_value, rel_tol=REL_TOL):
                        row.update({f"real_{name}": float(real_value), f"vec_{name}": vec_value})
                        mismatches.append(row)
    return {"symbol": symbol, "strategy": strategy_name, "timeframe": timeframe, "window": window, **counts,
            "mismatches": mismatches, **{f"max_rel_{k}": v for k, v in max_rel.items()}}


def signal_parity(data_dir: Path, symbols: list[str]) -> dict[str, object]:
    jobs = [(str(data_dir), symbol, strategy, timeframe)
            for strategy in var_mod.STRATEGIES for timeframe in ("1d", "1w")
            if not (timeframe == "1w" and strategy == "overnight_drift")
            for symbol in symbols]
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_signal_one, jobs))
    return {"rel_tol": REL_TOL, "results": results,
            "total_bars": int(sum(r["bars"] for r in results)),
            "total_mismatches": int(sum(len(r["mismatches"]) for r in results))}


def _execution_one(args: tuple[str, str, str]) -> dict[str, object]:
    from app.backtesting.cost_model import CostModel, with_etoro_trade_fees
    from app.backtesting.engine import BacktestEngine, EngineConfig

    data_dir, symbol, key = args
    variant = next(v for v in var_mod.VARIANTS if v.key == key)
    segments = data_mod.symbol_bars(Path(data_dir), symbol)[variant.timeframe]
    if len(segments) != 1:
        return {"symbol": symbol, "variant": key, "skipped": f"{len(segments)} segments"}
    bars = segments[0]
    config = EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=1.0,
                          cost_model=with_etoro_trade_fees(CostModel(), crypto=False))
    start = pd.Timestamp(data_mod.EVAL_START, tz="UTC")
    result = BacktestEngine(config=config).run(symbol=symbol, strategy=var_mod.make_strategy(variant),
                                               data=data_mod.engine_frame(bars.frame), file_path="research",
                                               trade_window_start=start)
    engine = pd.DataFrame(result.trades)

    signals = sig_mod.compute(variant.strategy, bars.frame, None, **variant.signal_kwargs())
    target, max_hold = var_mod.plan(variant, signals)
    segment = paths_mod.Segment(bars, signals, target, last=len(bars.frame) - 1, break_end=False)
    allowed = bars.close_date >= np.datetime64(data_mod.EVAL_START.date())
    ours = pd.DataFrame(paths_mod.chain(segment, allowed, max_hold), columns=paths_mod.TRADE_FIELDS)

    out: dict[str, object] = {"symbol": symbol, "variant": key, "engine_trades": int(len(engine)),
                              "our_trades": int(len(ours))}
    if engine.empty or ours.empty:
        out["identical"] = 0  # both empty match (0 = 0 = 0); one empty is a difference
        return out
    engine_entry = pd.to_datetime(engine["entry_time"]).dt.tz_convert(None).dt.normalize().to_numpy()
    engine_exit = pd.to_datetime(engine["exit_time"]).dt.tz_convert(None).dt.normalize().to_numpy()
    our_entry = bars.date[ours["entry_idx"].to_numpy()].astype("datetime64[ns]")
    our_exit = bars.date[ours["exit_idx"].to_numpy()].astype("datetime64[ns]")
    n = min(len(engine), len(ours))
    our_entry_fill = ours["entry_open"].to_numpy() * (1 + paths_mod.HALF_SPREAD)
    our_exit_fill = ours["exit_raw"].to_numpy() * (1 - paths_mod.HALF_SPREAD)
    our_fin_rate = ours["fin_r"].to_numpy() * paths_mod.RISK_USD / ours["notional"].to_numpy()
    # The engine records financing rounded to 6 decimals; compare in dollars at its own notional.
    engine_fin = engine["financing_usd"].to_numpy()
    engine_notional = engine["notional_usd"].to_numpy()
    same = (
        (engine_entry[:n] == our_entry[:n]) & (engine_exit[:n] == our_exit[:n])
        & (engine["reason"].to_numpy()[:n] == ours["reason"].to_numpy()[:n])
        & np.isclose(engine["entry_price"].to_numpy()[:n], our_entry_fill[:n], rtol=1e-9)
        & np.isclose(engine["exit_price"].to_numpy()[:n], our_exit_fill[:n], rtol=1e-9)
        & np.isclose(engine_fin[:n], our_fin_rate[:n] * engine_notional[:n], rtol=0.0, atol=2e-6)
        & np.isclose(engine["commission_usd"].to_numpy()[:n], 2.0)
    )
    first_bad = int(np.flatnonzero(~same)[0]) if (~same).any() else None
    out.update({"identical": int(same.sum()), "first_difference": None if first_bad is None else {
        "engine": {k: str(engine.iloc[first_bad][k]) for k in ("entry_time", "exit_time", "reason", "entry_price",
                                                               "exit_price", "financing_usd", "notional_usd")},
        "ours": {"entry": str(our_entry[first_bad]), "exit": str(our_exit[first_bad]),
                 "reason": ours["reason"].iat[first_bad], "entry_fill": float(our_entry_fill[first_bad]),
                 "exit_fill": float(our_exit_fill[first_bad]), "fin_rate": float(our_fin_rate[first_bad])},
    }})
    return out


def execution_parity(data_dir: Path, symbols: list[str]) -> list[dict[str, object]]:
    jobs = [(str(data_dir), symbol, variant.key) for variant in var_mod.VARIANTS for symbol in symbols]
    with ProcessPoolExecutor(max_workers=4) as pool:
        return list(pool.map(_execution_one, jobs))


def class_defaults() -> dict[str, dict[str, object]]:
    """The constructor defaults the variant grid assumes, read from the classes."""

    from app.strategies import STRATEGY_REGISTRY

    out = {}
    for name in var_mod.STRATEGIES:
        params = inspect.signature(STRATEGY_REGISTRY[name].__init__).parameters
        out[name] = {k: p.default for k, p in params.items() if k != "self"}
    return out


def run(data_dir: Path, out: Path, *, symbols: list[str] | None = None, execution: list[str] | None = None) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    defaults = class_defaults()
    assumed_ok = all(defaults[name].get("risk_multiple") == value for name, value in var_mod.RISK_MULTIPLE.items())
    assumed_ok &= defaults["overnight_drift"].get("target_atr_mult") == 1.0
    assumed_ok &= defaults["overnight_drift"].get("max_hold_bars") == 1
    report = {"class_defaults": {k: {a: str(b) for a, b in v.items()} for k, v in defaults.items()},
              "assumed_defaults_ok": bool(assumed_ok)}
    report["signal"] = signal_parity(data_dir, symbols or PARITY_SYMBOLS)
    report["execution"] = execution_parity(data_dir, execution or EXECUTION_SYMBOLS)
    (out / "parity.json").write_text(json.dumps(report, indent=1, default=str))
    sig = report["signal"]
    print(f"signal parity: {sig['total_bars']} bars, {sig['total_mismatches']} mismatches")
    for r in sig["results"]:
        print(f"  {r['symbol']:5} {r['strategy']:34} {r['timeframe']} buy {r['buy_real']}/{r['buy_vec']} "
              f"sell {r['sell_real']}/{r['sell_vec']} mism {len(r['mismatches'])} "
              f"maxrel stop {r['max_rel_stop']:.1e} target {r['max_rel_target']:.1e}")
        for row in r["mismatches"][:3]:
            print("     ", row)
    bad_exec = 0
    for r in report["execution"]:
        ok = r.get("identical") == r.get("engine_trades") == r.get("our_trades")
        bad_exec += not ok and "skipped" not in r
        print(f"  exec {r['symbol']:5} {r['variant']:38} engine {r.get('engine_trades')} ours {r.get('our_trades')} "
              f"identical {r.get('identical')} {r.get('skipped', '')}")
        if not ok and r.get("first_difference"):
            print("     ", r["first_difference"])
    print("assumed defaults ok:", assumed_ok)
    return bool(assumed_ok and sig["total_mismatches"] == 0 and bad_exec == 0)
