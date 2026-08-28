"""Per-strategy live performance analysis and live-vs-backtest decay monitoring.

Stage 1 of the profit roadmap is "prove expectancy on paper": run the graduated
strategies live on paper, measure what each one actually earns, and demote the
ones whose live edge decays away from what the backtest promised. The portfolio
summary in ``PaperTradeRepository.summary`` answers the book-level question; this
module answers the per-strategy one and turns it into a keep/watch/demote verdict.

Everything here is pure computation over trade-like objects (anything exposing
``strategy_name``, ``realized_pnl_usd``, ``closed_at`` and a ``payload`` dict), so
it is trivially testable and works with ``PaperTradeRecord`` unchanged.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class StrategyLivePerformance:
    """Realized live-paper performance for a single strategy."""

    strategy_name: str
    trades: int
    win_rate: float
    expectancy_usd: float
    profit_factor: float
    realized_pnl_usd: float
    gross_profit_usd: float
    gross_loss_usd: float
    average_r_multiple: float
    max_drawdown_usd: float


# Decay statuses, ordered from healthiest to worst.
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_HEALTHY = "healthy"
STATUS_DECAYING = "decaying"
STATUS_DEAD = "dead"

ACTION_KEEP = "keep"
ACTION_WATCH = "watch"
ACTION_DEMOTE = "demote"


@dataclass
class StrategyDecayVerdict:
    """A keep/watch/demote decision for one strategy from live-vs-backtest edge."""

    strategy_name: str
    trades: int
    live_expectancy_usd: float
    backtest_expectancy_usd: float | None
    retention_ratio: float | None
    status: str
    action: str
    reasons: list[str] = field(default_factory=list)


# A run with winners and zero losers has an infinite profit factor. ``inf`` is
# not valid JSON, so we collapse it to the same large-but-finite sentinel the
# rest of the codebase uses (see ``summarize_recent_trades``).
PROFIT_FACTOR_INF_SENTINEL = 99.0


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    if gross_loss > 0:
        return gross_profit / gross_loss
    return PROFIT_FACTOR_INF_SENTINEL if gross_profit > 0 else 0.0


def _max_drawdown_usd(pnls_in_order: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls_in_order:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return abs(max_dd)


def analyze_by_strategy(trades: Iterable[Any]) -> list[StrategyLivePerformance]:
    """Group closed trades by strategy and compute per-strategy performance.

    Returned newest-earning first (highest realized P&L). Trades are sorted by
    ``closed_at`` within each strategy so the per-strategy drawdown is real.
    """

    grouped: dict[str, list[Any]] = defaultdict(list)
    for trade in trades:
        name = str(getattr(trade, "strategy_name", None) or "unknown")
        grouped[name].append(trade)

    results: list[StrategyLivePerformance] = []
    for name, group in grouped.items():
        ordered = sorted(group, key=lambda t: str(getattr(t, "closed_at", "") or ""))
        pnls = [float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) for t in ordered]
        n = len(pnls)
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        realized = sum(pnls)
        r_values = [
            float((getattr(t, "payload", {}) or {}).get("realized_r_multiple") or 0.0)
            for t in ordered
            if (getattr(t, "payload", {}) or {}).get("realized_r_multiple") not in (None, "")
        ]
        results.append(
            StrategyLivePerformance(
                strategy_name=name,
                trades=n,
                win_rate=round((len(winners) / n) * 100.0, 2) if n else 0.0,
                expectancy_usd=round(realized / n, 2) if n else 0.0,
                profit_factor=round(_profit_factor(gross_profit, gross_loss), 2),
                realized_pnl_usd=round(realized, 2),
                gross_profit_usd=round(gross_profit, 2),
                gross_loss_usd=round(gross_loss, 2),
                average_r_multiple=round(sum(r_values) / len(r_values), 2) if r_values else 0.0,
                max_drawdown_usd=round(_max_drawdown_usd(pnls), 2),
            )
        )
    results.sort(key=lambda item: item.realized_pnl_usd, reverse=True)
    return results


def decay_verdict(
    live: StrategyLivePerformance,
    *,
    backtest_expectancy_usd: float | None,
    min_trades: int = 20,
    retention_threshold: float = 0.5,
) -> StrategyDecayVerdict:
    """Decide whether a strategy's live edge still justifies trading it.

    * Fewer than ``min_trades`` closed trades -> ``insufficient_data`` / keep
      (not enough evidence to judge; let it run).
    * Live expectancy <= 0 -> ``dead`` / demote (it is losing money live).
    * Live expectancy positive but below ``retention_threshold`` of the backtest
      expectancy -> ``decaying`` / watch (edge is eroding, put it on notice).
    * Otherwise -> ``healthy`` / keep.

    When no backtest expectancy is available we can still catch a dead strategy;
    a positive live expectancy with no baseline is treated as healthy.
    """

    reasons: list[str] = []
    ratio: float | None = None
    if backtest_expectancy_usd and backtest_expectancy_usd > 0:
        ratio = round(live.expectancy_usd / backtest_expectancy_usd, 3)

    if live.trades < min_trades:
        reasons.append(f"only {live.trades} trades (<{min_trades}); need more evidence")
        return StrategyDecayVerdict(
            strategy_name=live.strategy_name,
            trades=live.trades,
            live_expectancy_usd=live.expectancy_usd,
            backtest_expectancy_usd=backtest_expectancy_usd,
            retention_ratio=ratio,
            status=STATUS_INSUFFICIENT,
            action=ACTION_KEEP,
            reasons=reasons,
        )

    if live.expectancy_usd <= 0:
        reasons.append(f"live expectancy ${live.expectancy_usd:.2f} <= 0 over {live.trades} trades")
        status, action = STATUS_DEAD, ACTION_DEMOTE
    elif ratio is not None and ratio < retention_threshold:
        reasons.append(
            f"live expectancy is {ratio:.0%} of backtest (<{retention_threshold:.0%}); edge decaying"
        )
        status, action = STATUS_DECAYING, ACTION_WATCH
    else:
        if ratio is not None:
            reasons.append(f"live expectancy is {ratio:.0%} of backtest")
        else:
            reasons.append("positive live expectancy; no backtest baseline to compare")
        status, action = STATUS_HEALTHY, ACTION_KEEP

    return StrategyDecayVerdict(
        strategy_name=live.strategy_name,
        trades=live.trades,
        live_expectancy_usd=live.expectancy_usd,
        backtest_expectancy_usd=backtest_expectancy_usd,
        retention_ratio=ratio,
        status=status,
        action=action,
        reasons=reasons,
    )
