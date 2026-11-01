"""Parity: the vectorised signals equal ``EMATrendStackStrategy(timeframe="1d")`` bar for bar.

For every bar of each symbol's cleaned history, the real strategy is called on the window the
live bot would hand it (the last ``window`` bars up to and including that bar; 400 is the live
1d fetch, ``app/screener/service_backtests.bars_for_timeframe``). Its action (BUY / SELL / none),
stop and target are compared with ``signals.compute_signals`` run once over the whole history.
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from research.pullback import data as data_mod
from research.pullback import signals as sig_mod

REL_TOL = 1e-6


def _one_symbol(args: tuple[str, str, int]) -> dict[str, object]:
    data_dir, symbol, window = args
    from app.strategies.ema_trend_stack import EMATrendStackStrategy

    cleaned = data_mod.clean_symbol(Path(data_dir), symbol)
    assert cleaned is not None, symbol
    frame, _breaks = cleaned
    bars = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"]).dt.tz_localize("UTC")
    vec = sig_mod.compute_signals(bars)
    strategy = EMATrendStackStrategy(timeframe="1d")
    counts = {"bars": 0, "buy_real": 0, "buy_vec": 0, "sell_real": 0, "sell_vec": 0}
    mismatches: list[dict[str, object]] = []
    max_rel = {"stop": 0.0, "target": 0.0}
    for index in range(len(bars)):
        start = max(0, index + 1 - window)
        signal = strategy.generate_signal(bars.iloc[start : index + 1], symbol)
        counts["bars"] += 1
        action = getattr(getattr(signal, "action", None), "value", None)
        real_buy, real_sell = action == "buy", action == "sell"
        vec_buy, vec_sell = bool(vec["buy"].iat[index]), bool(vec["sell"].iat[index])
        counts["buy_real"] += real_buy
        counts["buy_vec"] += vec_buy
        counts["sell_real"] += real_sell
        counts["sell_vec"] += vec_sell
        row = {"date": str(bars["timestamp"].iat[index].date()), "real": action, "vec_buy": vec_buy, "vec_sell": vec_sell}
        if real_buy != vec_buy or real_sell != vec_sell:
            e9, e20 = float(vec["ema_9"].iat[index]), float(vec["ema_20"].iat[index])
            row["ema9_minus_ema20"] = e9 - e20
            row["close_minus_ema20"] = float(vec["close"].iat[index] - vec["ema_20"].iat[index])
            mismatches.append(row)
            continue
        if real_buy:
            for name, real_value in (("stop", signal.stop_loss), ("target", signal.take_profit)):
                vec_value = float(vec[name].iat[index])
                rel = abs(float(real_value) - vec_value) / max(abs(float(real_value)), 1e-12)
                max_rel[name] = max(max_rel[name], rel)
                if not math.isclose(float(real_value), vec_value, rel_tol=REL_TOL):
                    row.update({f"real_{name}": float(real_value), f"vec_{name}": vec_value})
                    mismatches.append(row)
    return {
        "symbol": symbol,
        "first_bar": str(bars["timestamp"].iat[0].date()),
        "last_bar": str(bars["timestamp"].iat[-1].date()),
        **counts,
        "mismatches": mismatches,
        "max_rel_diff_stop": max_rel["stop"],
        "max_rel_diff_target": max_rel["target"],
    }


def run_parity(data_dir: Path, symbols: list[str], *, window: int = 400) -> dict[str, object]:
    jobs = [(str(data_dir), symbol.strip().upper(), window) for symbol in symbols if symbol.strip()]
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one_symbol, jobs))
    total_mismatch = sum(len(item["mismatches"]) for item in results)
    return {
        "window_bars": window,
        "rel_tol": REL_TOL,
        "symbols": results,
        "all_match": total_mismatch == 0,
        "total_bars": int(np.sum([item["bars"] for item in results])),
        "total_buy": int(np.sum([item["buy_real"] for item in results])),
        "total_sell": int(np.sum([item["sell_real"] for item in results])),
        "total_mismatches": total_mismatch,
    }


def write_report(report: dict[str, object], out: Path | None) -> None:
    lines = [
        f"window {report['window_bars']} bars, rel tol {report['rel_tol']}",
        "| symbol | bars | first | last | BUY real/vec | SELL real/vec | mismatches | max rel diff stop / target |",
        "|---|--:|---|---|--:|--:|--:|--:|",
    ]
    for item in report["symbols"]:
        lines.append(
            f"| {item['symbol']} | {item['bars']} | {item['first_bar']} | {item['last_bar']} | "
            f"{item['buy_real']}/{item['buy_vec']} | {item['sell_real']}/{item['sell_vec']} | "
            f"{len(item['mismatches'])} | {item['max_rel_diff_stop']:.1e} / {item['max_rel_diff_target']:.1e} |"
        )
    text = "\n".join(lines)
    print(text)
    print(json.dumps({k: v for k, v in report.items() if k != "symbols"}, indent=2))
    for item in report["symbols"]:
        for row in item["mismatches"][:10]:
            print(item["symbol"], row)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "parity.json").write_text(json.dumps(report, indent=2, default=str))
        (out / "parity.md").write_text(text + "\n")
