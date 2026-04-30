"""Backtest context helpers for the live signal service."""

from __future__ import annotations

from typing import Any

from app.live_signal_schema import LiveSignalSnapshot, SignalState


def attach_backtest_context(service: Any, snapshot: LiveSignalSnapshot) -> LiveSignalSnapshot:
    metadata = dict(snapshot.metadata)
    metadata.setdefault("data_source", "eToro")
    metadata.setdefault("data_source_verified", True)

    validation = service._backtest_validation(snapshot)
    metadata.update(
        {
            "backtest_validated": validation["passes"],
            "backtest_validation_reason": validation["reason"],
        }
    )
    summary = validation.get("summary")
    if summary:
        metrics = summary.get("metrics", {})
        metadata.update(
            {
                "backtest_strategy_name": summary.get("strategy_name"),
                "backtest_completed_at": summary.get("completed_at"),
                "backtest_number_of_trades": metrics.get("number_of_trades"),
                "backtest_profit_factor": metrics.get("profit_factor"),
                "backtest_annualized_return_pct": metrics.get("annualized_return_pct"),
                "backtest_max_drawdown_pct": metrics.get("max_drawdown_pct"),
                "backtest_win_rate": metrics.get("win_rate"),
            }
        )
    return snapshot.model_copy(update={"metadata": metadata, "indicators": dict(snapshot.indicators or metadata)})


def backtest_validation(service: Any, snapshot: LiveSignalSnapshot) -> dict[str, Any]:
    if service.backtests is None:
        return {"passes": False, "reason": "no_backtest_repository", "summary": None}

    summary = None
    for strategy_name in backtest_strategy_candidates(snapshot.strategy_name):
        summary = service.backtests.get_latest_summary(snapshot.symbol, strategy_name)
        if summary is not None:
            break
    if summary is None:
        return {"passes": False, "reason": "no_backtest_summary", "summary": None}

    metrics = summary.get("metrics", {})
    trade_count = int(metrics.get("number_of_trades", 0) or 0)
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    annualized_return = float(metrics.get("annualized_return_pct", 0.0) or 0.0)
    max_drawdown = float(metrics.get("max_drawdown_pct", 9999.0) or 9999.0)

    failures: list[str] = []
    if not bool(summary.get("out_of_sample", metrics.get("out_of_sample", False))):
        failures.append("in_sample_only")
    if trade_count < service.settings.min_backtest_trades_for_alerts:
        failures.append("too_few_trades")
    if profit_factor < service.settings.min_backtest_profit_factor:
        failures.append("profit_factor_below_threshold")
    if annualized_return < service.settings.min_backtest_annualized_return_pct:
        failures.append("annualized_return_below_threshold")
    if max_drawdown > service.settings.max_backtest_drawdown_pct:
        failures.append("drawdown_above_threshold")

    return {
        "passes": not failures,
        "reason": ",".join(failures) if failures else "passed",
        "summary": summary,
    }


def backtest_strategy_candidates(strategy_name: str) -> list[str]:
    candidates = [strategy_name]
    if strategy_name.startswith("pullback_trend_"):
        candidates.append("pullback_trend")
    if strategy_name.startswith("gold_momentum"):
        candidates.append("gold_momentum")
    if strategy_name.startswith("ma_crossover_"):
        candidates.append("ma_crossover")
    return candidates


def ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, float]:
    state_rank = {
        SignalState.BUY: 3,
        SignalState.NONE: 2,
        SignalState.SELL: 1,
    }[snapshot.state]
    return state_rank, snapshot.score
