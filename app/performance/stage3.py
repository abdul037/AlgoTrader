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
# Track-record report — the honest metrics a capital decision needs
# ---------------------------------------------------------------------------


@dataclass
class TrackRecordReport:
    trading_days: int
    total_trades: int
    realized_pnl_usd: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    longest_drawdown_days: int
    win_rate: float
    profit_factor: float
    expectancy_usd: float
    avg_win_usd: float
    avg_loss_usd: float
    best_day_usd: float
    worst_day_usd: float
    monthly_returns_pct: dict[str, float]
    profitable_months_pct: float


def _sortino(daily_returns_pct: list[float]) -> float:
    if len(daily_returns_pct) < 3:
        return 0.0
    mean = sum(daily_returns_pct) / len(daily_returns_pct)
    downside = [r for r in daily_returns_pct if r < 0]
    if not downside:
        return 0.0
    dvar = sum(r * r for r in downside) / len(downside)
    dstd = math.sqrt(dvar)
    if dstd == 0:
        return 0.0
    return (mean / dstd) * math.sqrt(TRADING_DAYS_PER_YEAR)


def track_record_report(trades: Iterable[Any], *, capital_usd: float) -> TrackRecordReport:
    """Full honest-metrics report over the paper track record.

    Sharpe/Sortino/Calmar from the daily return series; drawdown (depth and
    longest duration) from the cumulative realized-P&L equity curve; monthly
    returns and profitable-month share for consistency.
    """

    from app.performance.strategy_performance import daily_pnl_series

    trade_list = list(trades)
    series = daily_pnl_series(trade_list)
    trading_days = len(series)
    total_trades = len(trade_list)
    realized = round(sum(float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) for t in trade_list), 2)

    daily_pnls = [row["realized_pnl_usd"] for row in series]
    daily_returns_pct = [(p / capital_usd) * 100.0 for p in daily_pnls] if capital_usd > 0 else []
    mean_r = (sum(daily_returns_pct) / len(daily_returns_pct)) if daily_returns_pct else 0.0
    sharpe = round(_sharpe_from_daily(daily_returns_pct), 4)
    sortino = round(_sortino(daily_returns_pct), 4)

    # Drawdown depth and longest duration (consecutive days below the prior peak).
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    longest = 0
    current = 0
    for p in daily_pnls:
        equity += p
        if equity >= peak:
            peak = equity
            current = 0
        else:
            current += 1
            longest = max(longest, current)
        max_dd = min(max_dd, equity - peak)
    max_dd_usd = round(abs(max_dd), 2)
    max_dd_pct = round((max_dd_usd / capital_usd) * 100.0, 4) if capital_usd > 0 else 0.0

    total_return_pct = round((realized / capital_usd) * 100.0, 4) if capital_usd > 0 else 0.0
    annual = round(mean_r * TRADING_DAYS_PER_YEAR, 4)
    calmar = round((annual / max_dd_pct), 4) if max_dd_pct > 0 else 0.0

    winners = [float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) for t in trade_list if float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) > 0]
    losers = [float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) for t in trade_list if float(getattr(t, "realized_pnl_usd", 0.0) or 0.0) < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else (99.0 if gross_profit else 0.0)

    monthly: dict[str, float] = {}
    for row in series:
        month = row["date"][:7]
        monthly[month] = round(monthly.get(month, 0.0) + row["realized_pnl_usd"], 2)
    monthly_pct = {
        m: round((v / capital_usd) * 100.0, 4) for m, v in monthly.items()
    } if capital_usd > 0 else {}
    profitable_months = sum(1 for v in monthly.values() if v > 0)
    profitable_months_pct = round((profitable_months / len(monthly)) * 100.0, 2) if monthly else 0.0

    return TrackRecordReport(
        trading_days=trading_days,
        total_trades=total_trades,
        realized_pnl_usd=realized,
        total_return_pct=total_return_pct,
        annualized_return_pct=annual,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown_usd=max_dd_usd,
        max_drawdown_pct=max_dd_pct,
        longest_drawdown_days=longest,
        win_rate=round((len(winners) / total_trades) * 100.0, 2) if total_trades else 0.0,
        profit_factor=profit_factor,
        expectancy_usd=round(realized / total_trades, 4) if total_trades else 0.0,
        avg_win_usd=round(sum(winners) / len(winners), 2) if winners else 0.0,
        avg_loss_usd=round(sum(losers) / len(losers), 2) if losers else 0.0,
        best_day_usd=round(max(daily_pnls), 2) if daily_pnls else 0.0,
        worst_day_usd=round(min(daily_pnls), 2) if daily_pnls else 0.0,
        monthly_returns_pct=monthly_pct,
        profitable_months_pct=profitable_months_pct,
    )


# ---------------------------------------------------------------------------
# Capital deployment ladder — scale real capital in, don't dump it in
# ---------------------------------------------------------------------------


@dataclass
class DeploymentStage:
    stage: int
    capital_usd: float
    cumulative_pct: float
    gate: str


def capital_deployment_ladder(
    *, target_capital_usd: float, fractions: list[float] | None = None
) -> list[DeploymentStage]:
    """A staged plan to phase real capital in as the LIVE record confirms paper.

    Never deploy the full size at once: each stage unlocks only after the live
    track record keeps matching paper, so a paper-to-live gap surfaces cheaply.
    """

    fractions = fractions or [0.25, 0.50, 0.75, 1.0]
    gates = [
        "start — 2 weeks live P&L within tolerance of paper",
        "1 month live, Sharpe holding, no gap vs paper",
        "6–8 weeks live, drawdown within the paper envelope",
        "full size — sustained live track record confirms paper",
    ]
    ladder: list[DeploymentStage] = []
    for i, frac in enumerate(fractions):
        ladder.append(
            DeploymentStage(
                stage=i + 1,
                capital_usd=round(target_capital_usd * frac, 2),
                cumulative_pct=round(frac * 100.0, 2),
                gate=gates[i] if i < len(gates) else f"stage {i + 1}",
            )
        )
    return ladder


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
