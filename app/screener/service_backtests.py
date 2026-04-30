"""Backtest and sizing helpers for the market screener service."""

from __future__ import annotations

from typing import Any


def backtest_validation(service: Any, symbol: str, strategy_name: str, timeframe: str | None = None) -> dict[str, Any]:
    if service.backtests is None:
        return {"passes": False, "reason": "no_backtest_repository", "summary": None}
    summary = get_latest_backtest_summary(service, symbol.upper(), strategy_name, timeframe)
    if summary is None:
        return {"passes": False, "reason": "no_backtest_summary", "summary": None}
    metrics = summary.get("metrics", {})
    failures: list[str] = []
    if not bool(summary.get("out_of_sample", metrics.get("out_of_sample", False))):
        failures.append("in_sample_only")
    if int(metrics.get("number_of_trades", 0) or 0) < service.settings.min_backtest_trades_for_alerts:
        failures.append("too_few_trades")
    if float(metrics.get("profit_factor", 0.0) or 0.0) < service.settings.min_backtest_profit_factor:
        failures.append("profit_factor_below_threshold")
    if float(metrics.get("annualized_return_pct", 0.0) or 0.0) < service.settings.min_backtest_annualized_return_pct:
        failures.append("annualized_return_below_threshold")
    if float(metrics.get("max_drawdown_pct", 9999.0) or 9999.0) > service.settings.max_backtest_drawdown_pct:
        failures.append("drawdown_above_threshold")
    return {
        "passes": not failures,
        "reason": ",".join(failures) if failures else "passed",
        "summary": summary,
    }


def get_latest_backtest_summary(
    service: Any,
    symbol: str,
    strategy_name: str,
    timeframe: str | None,
) -> dict[str, Any] | None:
    if service.backtests is None:
        return None
    if timeframe:
        try:
            return service.backtests.get_latest_summary(symbol, strategy_name, timeframe=timeframe)
        except TypeError:
            return service.backtests.get_latest_summary(symbol, strategy_name)
    return service.backtests.get_latest_summary(symbol, strategy_name)


def compute_risk_reward(signal: Any) -> float | None:
    if signal.price is None or signal.stop_loss is None or signal.take_profit is None:
        return None
    if signal.action.value == "buy":
        risk = max(float(signal.price) - float(signal.stop_loss), 0.01)
        reward = float(signal.take_profit) - float(signal.price)
    else:
        risk = max(float(signal.stop_loss) - float(signal.price), 0.01)
        reward = float(signal.price) - float(signal.take_profit)
    if reward <= 0:
        return None
    return round(reward / risk, 2)


def bars_for_timeframe(timeframe: str) -> int:
    mapping = {"1d": 400, "1h": 320, "15m": 300, "5m": 320, "1m": 360}
    return mapping.get(timeframe, 250)
