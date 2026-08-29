"""Validation logic for the two default-off features, so they can be checked
against real data before being enabled.

Pure functions (no I/O); the runnable ``scripts/validate_gated_features.py``
feeds them live data and prints the report.

* **Regime router** — given the strategy specs active per timeframe and the
  current market regime, report which whole families the router would switch
  off right now, and how many specs that removes. This shows exactly what
  flipping ``regime_router_enabled`` does today (a full historical P&L
  comparison would need a period-by-period regime backtest — out of scope here;
  this is the honest "what changes now" view for the enable decision).

* **Drawdown governor** — replay the closed paper-trade history and, for each
  trade, scale its P&L by the governor multiplier implied by the day's realized
  loss *before* that trade. Comparing governed vs baseline equity shows whether
  the governor would have helped (it should shrink drawdowns at the cost of some
  upside), grounding the enable decision in the bot's own history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.risk.volatility_target import daily_drawdown_pct, drawdown_governor_multiplier
from app.screener.regime_router import RegimeSignal, allowed_families, family_for_style


@dataclass
class RegimeRouterImpact:
    regime: dict[str, str]
    allowed_families: list[str]
    per_timeframe: dict[str, dict[str, Any]]
    total_specs_before: int
    total_specs_after: int


def regime_router_impact(
    specs_by_timeframe: dict[str, Iterable[Any]], regime: RegimeSignal
) -> RegimeRouterImpact:
    """What the regime router would keep/drop per timeframe under ``regime``."""

    families = allowed_families(regime)
    per_tf: dict[str, dict[str, Any]] = {}
    total_before = 0
    total_after = 0
    for timeframe, specs in specs_by_timeframe.items():
        specs = list(specs)
        kept = [s for s in specs if family_for_style(getattr(s, "style", "")) in families]
        dropped = [s for s in specs if s not in kept]
        dropped_families = sorted({family_for_style(getattr(s, "style", "")) for s in dropped})
        per_tf[timeframe] = {
            "before": len(specs),
            "after": len(kept),
            "dropped": len(dropped),
            "dropped_families": dropped_families,
        }
        total_before += len(specs)
        total_after += len(kept)
    return RegimeRouterImpact(
        regime={"trend": regime.trend, "volatility": regime.volatility, "breadth": regime.breadth},
        allowed_families=sorted(families),
        per_timeframe=per_tf,
        total_specs_before=total_before,
        total_specs_after=total_after,
    )


@dataclass
class GovernorSimulation:
    capital_usd: float
    baseline_pnl_usd: float
    governed_pnl_usd: float
    delta_usd: float
    baseline_max_drawdown_usd: float
    governed_max_drawdown_usd: float
    trades: int
    recommendation: str = ""


def _max_drawdown(equity_steps: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for step in equity_steps:
        equity += step
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return abs(max_dd)


def simulate_drawdown_governor(
    trades: Iterable[Any],
    *,
    capital_usd: float,
    soft_pct: float,
    hard_pct: float,
    floor: float,
) -> GovernorSimulation:
    """Replay closed trades with vs without the intraday drawdown governor.

    Each trade's P&L is scaled by the multiplier implied by the day's realized
    loss accumulated *before* it — the same signal the live governor uses.
    """

    ordered = sorted(trades, key=lambda t: str(getattr(t, "closed_at", "") or ""))
    baseline_steps: list[float] = []
    governed_steps: list[float] = []
    current_day: str | None = None
    day_running_pnl = 0.0

    for trade in ordered:
        day = str(getattr(trade, "closed_at", "") or "")[:10]
        if day != current_day:
            current_day = day
            day_running_pnl = 0.0
        pnl = float(getattr(trade, "realized_pnl_usd", 0.0) or 0.0)
        dd_pct = daily_drawdown_pct(daily_realized_pnl_usd=day_running_pnl, equity_usd=capital_usd)
        multiplier = drawdown_governor_multiplier(
            drawdown_pct=dd_pct, soft_pct=soft_pct, hard_pct=hard_pct, floor=floor
        )
        baseline_steps.append(pnl)
        governed_steps.append(pnl * multiplier)
        day_running_pnl += pnl  # baseline running P&L drives the next trade's dd

    baseline_pnl = round(sum(baseline_steps), 2)
    governed_pnl = round(sum(governed_steps), 2)
    baseline_dd = round(_max_drawdown(baseline_steps), 2)
    governed_dd = round(_max_drawdown(governed_steps), 2)

    if governed_dd < baseline_dd and governed_pnl >= baseline_pnl - abs(baseline_pnl) * 0.1:
        recommendation = "enable — cuts drawdown with little P&L cost"
    elif governed_dd < baseline_dd:
        recommendation = "consider — cuts drawdown but gives up upside; weigh the trade-off"
    else:
        recommendation = "keep off — no drawdown benefit on this history"

    return GovernorSimulation(
        capital_usd=capital_usd,
        baseline_pnl_usd=baseline_pnl,
        governed_pnl_usd=governed_pnl,
        delta_usd=round(governed_pnl - baseline_pnl, 2),
        baseline_max_drawdown_usd=baseline_dd,
        governed_max_drawdown_usd=governed_dd,
        trades=len(ordered),
        recommendation=recommendation,
    )
