"""Backtest metrics.

Two specific corrections from the April 2026 audit:

* Profit factor returns ``math.inf`` when there are winners and no losers. The
  prior code returned ``99.0`` as a sentinel. That sentinel defeats the brief's
  ``PF > 3 -> something is leaking`` tripwire because *any* all-winning run
  looks the same as a leaky run. Callers that apply the tripwire should also
  require a minimum trade count (see ``MIN_TRADES_FOR_LEAKAGE_TRIPWIRE``).
* Sharpe and annualized return now take an explicit ``bars_per_year`` argument.
  The prior code hardcoded 252, which silently produced nonsense for hourly or
  minute bars.
"""

from __future__ import annotations

import math
from math import sqrt

import pandas as pd

DAILY_BARS_PER_YEAR = 252
HOURLY_BARS_PER_YEAR = 1638  # ~6.5h session x 252 days
FIFTEEN_MIN_BARS_PER_YEAR = 6552
FIVE_MIN_BARS_PER_YEAR = 19656
ONE_MIN_BARS_PER_YEAR = 98280

MIN_TRADES_FOR_LEAKAGE_TRIPWIRE = 30


def bars_per_year_for(timeframe: str) -> int:
    """Map a human timeframe string to an annualization factor."""

    normalized = (timeframe or "").strip().lower()
    mapping = {
        "1m": ONE_MIN_BARS_PER_YEAR,
        "5m": FIVE_MIN_BARS_PER_YEAR,
        "15m": FIFTEEN_MIN_BARS_PER_YEAR,
        "1h": HOURLY_BARS_PER_YEAR,
        "60m": HOURLY_BARS_PER_YEAR,
        "1d": DAILY_BARS_PER_YEAR,
        "d": DAILY_BARS_PER_YEAR,
        "daily": DAILY_BARS_PER_YEAR,
    }
    return mapping.get(normalized, DAILY_BARS_PER_YEAR)


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Return the maximum drawdown percentage."""

    if not equity_curve:
        return 0.0
    series = pd.Series(equity_curve, dtype="float64")
    running_max = series.cummax()
    drawdowns = (series - running_max) / running_max.replace(0, 1.0)
    return abs(float(drawdowns.min()) * 100)


def compute_sharpe_like(equity_curve: list[float], *, bars_per_year: int = DAILY_BARS_PER_YEAR) -> float:
    """Return a simplified Sharpe-like metric annualized for the given bar cadence."""

    if len(equity_curve) < 3:
        return 0.0
    returns = pd.Series(equity_curve, dtype="float64").pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0
    return float((returns.mean() / returns.std()) * sqrt(max(bars_per_year, 1)))


def summarize_trades(trades: list[dict[str, float]]) -> dict[str, float]:
    """Build common trade-level metrics.

    Profit factor is ``math.inf`` when all trades are winners and zero when
    there are no trades or no winners. The brief's PF > 3 leakage tripwire
    should gate on ``number_of_trades >= MIN_TRADES_FOR_LEAKAGE_TRIPWIRE`` so
    an all-winning two-trade sample does not fire it.
    """

    if not trades:
        return {
            "number_of_trades": 0,
            "win_rate": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "profit_factor": 0.0,
        }

    trade_frame = pd.DataFrame(trades)
    if "pnl_usd" not in trade_frame.columns:
        return {
            "number_of_trades": int(len(trade_frame)),
            "win_rate": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "profit_factor": 0.0,
        }
    winners = trade_frame[trade_frame["pnl_usd"] > 0]
    losers = trade_frame[trade_frame["pnl_usd"] < 0]
    gross_profit = float(winners["pnl_usd"].sum()) if not winners.empty else 0.0
    gross_loss = abs(float(losers["pnl_usd"].sum())) if not losers.empty else 0.0
    if gross_loss > 0:
        profit_factor: float = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
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
    recent_pf = summary["profit_factor"]
    # Reports serialise via JSON so collapse ``inf`` to a large-but-finite value
    # at the edge. Internal math should use the summary dict from ``summarize_trades``.
    if math.isinf(recent_pf):
        recent_pf = 99.0
    return {
        "recent_trade_count": float(summary["number_of_trades"]),
        "recent_win_rate": float(summary["win_rate"]),
        "recent_average_return_pct": float(expectancy["average_return_pct"]),
        "recent_profit_factor": float(recent_pf),
    }


def leakage_tripwire_triggered(metrics: dict[str, float]) -> tuple[bool, str | None]:
    """Apply the brief's PF>3 / win-rate>70% tripwire.

    Returns ``(True, reason)`` when the metrics look too good to be real and
    the trade count is large enough that the result is not a small-sample
    artefact. Callers should surface the reason in a report rather than paper
    over it.
    """

    n = int(metrics.get("number_of_trades", 0) or 0)
    if n < MIN_TRADES_FOR_LEAKAGE_TRIPWIRE:
        return False, None
    pf = float(metrics.get("profit_factor", 0.0) or 0.0)
    wr = float(metrics.get("win_rate", 0.0) or 0.0)
    if math.isinf(pf) or pf > 3.0:
        return True, f"profit_factor={pf:.2f} exceeds tripwire (n={n})"
    if wr > 70.0:
        return True, f"win_rate={wr:.2f}% exceeds tripwire (n={n})"
    return False, None
