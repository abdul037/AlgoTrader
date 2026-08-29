"""Stage 3 of the profit roadmap: track record, capital sizing, and the
real-capital go/no-go — built ahead of the calendar-bound track record itself.

Three pure, side-effect-free pieces:

* **Capital sizing** — turn a measured daily return (the honest $/day the paper
  book actually produces per dollar of capital) into the capital required to hit
  a dollar-per-day target, and the reverse. This is the roadmap's core reframe:
  don't force an insane return out of small capital; measure the return, then
  size the capital to the goal.
* **Stage 3 readiness** — assess the paper track record against honest gates
  (track-record length, trade count, Sharpe, drawdown, positive expectancy) and
  return a go/no-go for opening the capital conversation.
* **Real-capital preflight** — a checklist that reports whether every condition
  for a real-capital decision is met. It NEVER enables real trading; flipping
  ENABLE_REAL_TRADING is always an explicit human act, and this only reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Capital sizing
# ---------------------------------------------------------------------------


def daily_return_pct(*, total_realized_pnl_usd: float, trading_days: int, capital_usd: float) -> float:
    """Average realized return per trading day, as a percent of capital."""

    if trading_days <= 0 or capital_usd <= 0:
        return 0.0
    avg_daily_usd = float(total_realized_pnl_usd) / trading_days
    return (avg_daily_usd / capital_usd) * 100.0


def projected_daily_usd(*, capital_usd: float, daily_return_pct: float) -> float:
    """Expected dollars per day for a given capital at a measured daily return."""

    return float(capital_usd) * (float(daily_return_pct) / 100.0)


def capital_required_for_daily_target(
    *, daily_target_usd: float, daily_return_pct: float
) -> float | None:
    """Capital needed to average ``daily_target_usd`` at a measured daily return.

    Returns None when the measured daily return is non-positive — you cannot size
    your way to a target with no (or negative) edge; more capital only scales a
    losing system's losses.
    """

    if daily_return_pct <= 0:
        return None
    return float(daily_target_usd) / (float(daily_return_pct) / 100.0)


def annualized_return_pct(daily_return_pct: float) -> float:
    """Compounded annualized return implied by a daily return, for a reality check."""

    daily = float(daily_return_pct) / 100.0
    return ((1.0 + daily) ** TRADING_DAYS_PER_YEAR - 1.0) * 100.0


@dataclass
class CapitalPlan:
    daily_target_usd: float
    measured_daily_return_pct: float
    capital_required_usd: float | None
    implied_annualized_return_pct: float
    feasibility: str
    note: str


def build_capital_plan(
    *,
    daily_target_usd: float,
    total_realized_pnl_usd: float,
    trading_days: int,
    capital_usd: float,
) -> CapitalPlan:
    """Assemble the capital plan from a paper track record.

    ``feasibility`` grades how plausible the implied annualized return is, so a
    tiny sample that extrapolates to a fantasy number is flagged rather than
    celebrated.
    """

    measured = daily_return_pct(
        total_realized_pnl_usd=total_realized_pnl_usd,
        trading_days=trading_days,
        capital_usd=capital_usd,
    )
    required = capital_required_for_daily_target(
        daily_target_usd=daily_target_usd, daily_return_pct=measured
    )
    annualized = annualized_return_pct(measured) if measured > 0 else 0.0

    if measured <= 0:
        feasibility = "no_edge"
        note = "Measured daily return is not positive — no capital size reaches the target; fix the edge first."
    elif annualized > 300.0:
        feasibility = "implausible_extrapolation"
        note = "Implied annualized return is unsustainably high — likely too small a sample; keep accruing track record."
    elif annualized > 100.0:
        feasibility = "top_decile"
        note = "Achievable but top-decile; treat the capital figure as optimistic until the track record lengthens."
    else:
        feasibility = "plausible"
        note = "Return is in a sustainable band; the capital figure is a reasonable basis for the decision."

    return CapitalPlan(
        daily_target_usd=round(float(daily_target_usd), 2),
        measured_daily_return_pct=round(measured, 4),
        capital_required_usd=round(required, 2) if required is not None else None,
        implied_annualized_return_pct=round(annualized, 2),
        feasibility=feasibility,
        note=note,
    )


# ---------------------------------------------------------------------------
# Stage 3 readiness
# ---------------------------------------------------------------------------


@dataclass
class Stage3Gates:
    min_track_record_days: int = 60
    min_trades: int = 100
    min_sharpe: float = 1.5
    max_drawdown_pct: float = 8.0
    require_positive_expectancy: bool = True


@dataclass
class Stage3Readiness:
    trading_days: int
    total_trades: int
    realized_pnl_usd: float
    expectancy_usd: float
    sharpe: float
    max_drawdown_pct: float
    gates: dict[str, bool]
    blockers: list[str]
    ready: bool
    capital_plan: CapitalPlan | None = None


def _sharpe_from_daily(daily_returns_pct: list[float]) -> float:
    if len(daily_returns_pct) < 3:
        return 0.0
    mean = sum(daily_returns_pct) / len(daily_returns_pct)
    var = sum((r - mean) ** 2 for r in daily_returns_pct) / (len(daily_returns_pct) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def assess_stage3(
    trades: Iterable[Any],
    *,
    capital_usd: float,
    daily_target_usd: float = 1000.0,
    gates: Stage3Gates | None = None,
) -> Stage3Readiness:
    """Assess a paper track record against the Stage 3 acceptance gates.

    ``trades`` are closed paper trades (objects with ``realized_pnl_usd`` and an
    ISO ``closed_at``). Sharpe is computed from the daily return series on the
    given capital; drawdown from the cumulative realized-P&L equity curve.
    """

    from app.performance.strategy_performance import daily_pnl_series

    gates = gates or Stage3Gates()
    trade_list = list(trades)
    series = daily_pnl_series(trade_list)
    trading_days = len(series)
    total_trades = len(trade_list)
    realized = round(sum(float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) for t in trade_list), 2)
    expectancy = round(realized / total_trades, 4) if total_trades else 0.0

    daily_returns_pct = [
        (row["realized_pnl_usd"] / capital_usd) * 100.0 for row in series
    ] if capital_usd > 0 else []
    sharpe = round(_sharpe_from_daily(daily_returns_pct), 4)

    # Max drawdown over the cumulative realized equity curve, as % of capital.
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for row in series:
        equity += row["realized_pnl_usd"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    max_drawdown_pct = round((abs(max_dd) / capital_usd) * 100.0, 4) if capital_usd > 0 else 0.0

    gate_results = {
        "track_record_days": trading_days >= gates.min_track_record_days,
        "trade_count": total_trades >= gates.min_trades,
        "sharpe": sharpe >= gates.min_sharpe,
        "drawdown": max_drawdown_pct <= gates.max_drawdown_pct,
        "positive_expectancy": (expectancy > 0) if gates.require_positive_expectancy else True,
    }
    blockers = [name for name, ok in gate_results.items() if not ok]

    plan = build_capital_plan(
        daily_target_usd=daily_target_usd,
        total_realized_pnl_usd=realized,
        trading_days=trading_days,
        capital_usd=capital_usd,
    )

    return Stage3Readiness(
        trading_days=trading_days,
        total_trades=total_trades,
        realized_pnl_usd=realized,
        expectancy_usd=expectancy,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        gates=gate_results,
        blockers=blockers,
        ready=not blockers,
        capital_plan=plan,
    )


# ---------------------------------------------------------------------------
# Real-capital preflight — reports readiness, never enables anything
# ---------------------------------------------------------------------------


@dataclass
class RealCapitalPreflight:
    stage3_ready: bool
    real_trading_currently_enabled: bool
    checklist: dict[str, bool]
    blockers: list[str]
    decision_allowed: bool
    note: str = field(
        default=(
            "This is a readiness report only. Enabling real-money trading is always an "
            "explicit human decision (setting ENABLE_REAL_TRADING); nothing here flips it."
        )
    )


def real_capital_preflight(
    *,
    readiness: Stage3Readiness,
    enable_real_trading: bool,
) -> RealCapitalPreflight:
    """Report whether the conditions for a real-capital decision are met.

    ``decision_allowed`` being True means the *conversation* is warranted — not
    that real trading should be turned on. The guardrail deliberately reports the
    current ENABLE_REAL_TRADING state so a surprising 'already on' is visible.
    """

    checklist = {
        "stage3_gates_met": readiness.ready,
        "capital_plan_available": readiness.capital_plan is not None
        and readiness.capital_plan.capital_required_usd is not None,
        "edge_is_positive": readiness.expectancy_usd > 0,
        "feasibility_not_fantasy": bool(
            readiness.capital_plan
            and readiness.capital_plan.feasibility in {"plausible", "top_decile"}
        ),
    }
    blockers = [name for name, ok in checklist.items() if not ok]
    return RealCapitalPreflight(
        stage3_ready=readiness.ready,
        real_trading_currently_enabled=bool(enable_real_trading),
        checklist=checklist,
        blockers=blockers,
        decision_allowed=not blockers,
    )
