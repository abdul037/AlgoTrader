"""The 2026-10-26 intraday edge study: universes, the run grid and the pooled table.

RESEARCH ONLY. ``run_grid`` runs every (variant, timeframe, direction, symbol) unit through
``harness.run_symbol`` (in worker processes when asked) and ``summarize`` pools each
(variant, timeframe, direction, universe) with the production verdict code. The caller
supplies the bars; nothing here fetches data, touches a database or reads settings files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from typing import Any

import pandas as pd

from app.research.intraday.harness import (
    ETORO_PROFILE,
    GROSS_PROFILE,
    SymbolRun,
    pooled_verdict,
    run_symbol,
    trade_statistics,
)

# The live scan universe (24 names) and a higher-beta research universe (20 names).
LIVE_UNIVERSE = [
    "AAPL", "ADBE", "AMD", "AMZN", "AVGO", "BAC", "COST", "CRM", "CSCO", "DIS", "GOOGL", "INTC",
    "IWM", "JPM", "META", "MSFT", "NFLX", "NVDA", "PYPL", "QQQ", "SPY", "TSLA", "WMT", "XOM",
]
VOLATILE_UNIVERSE = [
    "TSLA", "NVDA", "AMD", "COIN", "MSTR", "PLTR", "SMCI", "SHOP", "ROKU", "MARA",
    "RIOT", "UBER", "ABNB", "SNOW", "CRWD", "NET", "DKNG", "AFRM", "SOFI", "RIVN",
]
ETF_UNIVERSE = ["SPY", "QQQ", "IWM"]
UNIVERSES = {"live24": LIVE_UNIVERSE, "volatile20": VOLATILE_UNIVERSE, "etf3": ETF_UNIVERSE}
PROFILES = (ETORO_PROFILE, "alpaca", "stress", GROSS_PROFILE)
_TRADE_FIELDS = ("entry_time", "exit_time", "pnl_usd", "pnl_pct", "reason", "notional_usd", "side")

Unit = tuple[str, str, str, str]  # (variant, timeframe, direction, symbol)


def _run_unit(unit: Unit, frame: pd.DataFrame) -> dict[str, Any]:
    variant, timeframe, direction, symbol = unit
    run = run_symbol(frame, symbol=symbol, variant=variant, timeframe=timeframe, direction=direction)
    payload = asdict(run)
    payload["trades"] = {
        profile: {
            kind: [{k: t[k] for k in _TRADE_FIELDS if k in t} for t in trades]
            for kind, trades in kinds.items()
        }
        for profile, kinds in run.trades.items()
    }
    return payload


def run_grid(
    units: Iterable[Unit],
    frame_for: Callable[[str, str], pd.DataFrame],
    *,
    workers: int = 1,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Run each unit; ``frame_for(symbol, timeframe)`` supplies its bars."""

    units = list(units)
    results: list[dict[str, Any]] = []
    if workers <= 1:
        for unit in units:
            results.append(_run_unit(unit, frame_for(unit[3], unit[1])))
            if on_result:
                on_result(results[-1])
        return results
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_unit, unit, frame_for(unit[3], unit[1])) for unit in units]
        for future in futures:
            results.append(future.result())
            if on_result:
                on_result(results[-1])
    return results


def summarize(results: Iterable[dict[str, Any]], universes: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """One row per (variant, timeframe, direction, universe): the production verdict plus
    per-profile OOS and holdout trade statistics."""

    universes = universes or UNIVERSES
    groups: dict[tuple[str, str, str], list[SymbolRun]] = {}
    for item in results:
        run = SymbolRun(**item)
        groups.setdefault((run.variant, run.timeframe, run.direction), []).append(run)
    rows: list[dict[str, Any]] = []
    for (variant, timeframe, direction), runs in sorted(groups.items()):
        for universe, members in universes.items():
            chosen = [run for run in runs if run.symbol in set(members)]
            if not chosen:
                continue
            row: dict[str, Any] = {
                "variant": variant,
                "timeframe": timeframe,
                "direction": direction,
                "universe": universe,
                "symbols": len(chosen),
                "verdict": pooled_verdict(chosen),
            }
            for profile in PROFILES:
                for kind in ("oos", "holdout"):
                    trades = [t for run in chosen for t in run.trades.get(profile, {}).get(kind, [])]
                    row[f"{profile}_{kind}"] = trade_statistics(trades)
            rows.append(row)
    return rows


def gate_columns(row: dict[str, Any]) -> dict[str, Any]:
    """The flattened numbers a report table shows for one summarized row."""

    verdict = row["verdict"]
    profiles = verdict.get("cost_profiles", {})
    return {
        "variant": row["variant"],
        "tf": row["timeframe"],
        "dir": row["direction"],
        "universe": row["universe"],
        "oos_trades": verdict.get("oos_trades", 0),
        "win_rate": row[f"{ETORO_PROFILE}_oos"].get("win_rate"),
        "gross_r": row[f"{GROSS_PROFILE}_oos"].get("expectancy_r"),
        "etoro_r": verdict.get("oos_expectancy_r"),
        "alpaca_r": _r(profiles.get("alpaca", {}).get("oos_expectancy_usd")),
        "stress_r": _r(profiles.get("stress", {}).get("oos_expectancy_usd")),
        "holdout_trades": verdict.get("holdout_trades", 0),
        "holdout_alpaca_r": _r(profiles.get("alpaca", {}).get("holdout_expectancy_usd")),
        "holdout_stress_r": _r(profiles.get("stress", {}).get("holdout_expectancy_usd")),
        "stress_daily_t": row["stress_oos"].get("daily_t_stat"),
        "passed": bool(verdict.get("passed")),
        "paper_full": bool(verdict.get("paper_gate_full")),
        "paper_oos_bar": bool(verdict.get("paper_gate_oos_bar")),
    }


def _r(usd: Any) -> float | None:
    return None if usd is None else round(float(usd) / 100.0, 4)
