"""Backtest metrics."""

from __future__ import annotations

from math import sqrt

import pandas as pd


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Return the maximum drawdown percentage."""

    if not equity_curve:
        return 0.0
    series = pd.Series(equity_curve, dtype="float64")
    running_max = series.cummax()
    drawdowns = (series - running_max) / running_max.replace(0, 1.0)
    return abs(float(drawdowns.min()) * 100)


def compute_sharpe_like(equity_curve: list[float]) -> float:
    """Return a simplified daily-return Sharpe-like metric."""

    if len(equity_curve) < 3:
        return 0.0
    returns = pd.Series(equity_curve, dtype="float64").pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0
    return float((returns.mean() / returns.std()) * sqrt(252))


def summarize_trades(trades: list[dict[str, float]]) -> dict[str, float]:
    """Build common trade-level metrics."""

    if not trades:
        return {
            "number_of_trades": 0,
            "win_rate": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "profit_factor": 0.0,
        }

    trade_frame = pd.DataFrame(trades)
    winners = trade_frame[trade_frame["pnl_usd"] > 0]
    losers = trade_frame[trade_frame["pnl_usd"] < 0]
    gross_profit = float(winners["pnl_usd"].sum()) if not winners.empty else 0.0
    gross_loss = abs(float(losers["pnl_usd"].sum())) if not losers.empty else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0

    return {
        "number_of_trades": int(len(trade_frame)),
        "win_rate": float((len(winners) / len(trade_frame)) * 100),
        "average_win": float(winners["pnl_usd"].mean()) if not winners.empty else 0.0,
        "average_loss": float(losers["pnl_usd"].mean()) if not losers.empty else 0.0,
        "profit_factor": float(profit_factor),
    }


def compute_expectancy(trades: list[dict[str, float]]) -> dict[str, float]:
    """Return expectancy-style metrics from a trade list."""

    if not trades:
        return {
            "expectancy_usd": 0.0,
            "expectancy_pct": 0.0,
            "average_return_pct": 0.0,
        }

    trade_frame = pd.DataFrame(trades)
    pnl_pct_column = "pnl_pct" if "pnl_pct" in trade_frame.columns else None
    expectancy_usd = float(trade_frame["pnl_usd"].mean()) if "pnl_usd" in trade_frame.columns else 0.0
    average_return_pct = float(trade_frame[pnl_pct_column].mean()) if pnl_pct_column else 0.0
    return {
        "expectancy_usd": expectancy_usd,
        "expectancy_pct": average_return_pct,
        "average_return_pct": average_return_pct,
    }


def summarize_recent_trades(trades: list[dict[str, float]], *, window: int = 8) -> dict[str, float]:
    """Return compact recent-performance metrics for ranking and Telegram snapshots."""

    if not trades:
        return {
            "recent_trade_count": 0,
            "recent_win_rate": 0.0,
            "recent_average_return_pct": 0.0,
            "recent_profit_factor": 0.0,
        }

    recent = trades[-max(window, 1) :]
    summary = summarize_trades(recent)
    expectancy = compute_expectancy(recent)
    return {
        "recent_trade_count": float(summary["number_of_trades"]),
        "recent_win_rate": float(summary["win_rate"]),
        "recent_average_return_pct": float(expectancy["average_return_pct"]),
        "recent_profit_factor": float(summary["profit_factor"]),
    }
