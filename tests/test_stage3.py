"""Tests for Stage 3: capital sizing, readiness gates, and the real-capital preflight."""

from __future__ import annotations

import math
from types import SimpleNamespace

from app.performance.stage3 import (
    Stage3Gates,
    annualized_return_pct,
    assess_stage3,
    build_capital_plan,
    capital_required_for_daily_target,
    daily_return_pct,
    projected_daily_usd,
    real_capital_preflight,
)


def _trade(pnl: float, day: str):
    return SimpleNamespace(realized_pnl_usd=pnl, closed_at=f"{day}T15:00:00Z", payload={})


class TestCapitalSizing:
    def test_daily_return_and_projection_roundtrip(self) -> None:
        # $30k realized over 100 days on $100k == $300/day == 0.3%/day.
        r = daily_return_pct(total_realized_pnl_usd=30_000, trading_days=100, capital_usd=100_000)
        assert math.isclose(r, 0.3)
        assert math.isclose(projected_daily_usd(capital_usd=100_000, daily_return_pct=r), 300.0)

    def test_capital_required_for_target(self) -> None:
        # 0.3%/day -> $1000/day needs $1000 / 0.003 == ~$333,333.
        cap = capital_required_for_daily_target(daily_target_usd=1000, daily_return_pct=0.3)
        assert cap is not None and math.isclose(cap, 1000 / 0.003, rel_tol=1e-6)

    def test_no_edge_has_no_capital_answer(self) -> None:
        assert capital_required_for_daily_target(daily_target_usd=1000, daily_return_pct=0.0) is None
        assert capital_required_for_daily_target(daily_target_usd=1000, daily_return_pct=-0.1) is None

    def test_plan_flags_fantasy_extrapolation(self) -> None:
        # 2%/day annualizes to an absurd number -> flagged, not celebrated.
        plan = build_capital_plan(daily_target_usd=1000, total_realized_pnl_usd=20_000, trading_days=10, capital_usd=100_000)
        assert plan.feasibility == "implausible_extrapolation"
        assert annualized_return_pct(2.0) > 300.0

    def test_plan_plausible_band(self) -> None:
        # ~0.1%/day -> ~29% annualized -> plausible.
        plan = build_capital_plan(daily_target_usd=1000, total_realized_pnl_usd=6_000, trading_days=60, capital_usd=100_000)
        assert plan.feasibility == "plausible"
        assert plan.capital_required_usd is not None


class TestStage3Readiness:
    def _winning_track_record(self, days: int, per_day: float, capital: float):
        # One winning trade per day for `days` days.
        return [_trade(per_day, f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}") for i in range(days)]

    def test_not_ready_when_too_few_days(self) -> None:
        trades = self._winning_track_record(days=20, per_day=100, capital=100_000)
        r = assess_stage3(trades, capital_usd=100_000)
        assert not r.ready
        assert "track_record_days" in r.blockers
        assert "trade_count" in r.blockers

    def test_drawdown_gate_blocks(self) -> None:
        gates = Stage3Gates(min_track_record_days=3, min_trades=3, min_sharpe=-99.0)
        # A big loss creates a >8% drawdown on $100k.
        trades = [_trade(100, "2026-01-01"), _trade(-12_000, "2026-01-02"), _trade(100, "2026-01-03")]
        r = assess_stage3(trades, capital_usd=100_000, gates=gates)
        assert "drawdown" in r.blockers

    def test_ready_track_record_passes_and_has_capital_plan(self) -> None:
        gates = Stage3Gates(min_track_record_days=60, min_trades=60, min_sharpe=-99.0, max_drawdown_pct=100.0)
        trades = self._winning_track_record(days=60, per_day=200, capital=100_000)
        r = assess_stage3(trades, capital_usd=100_000, gates=gates)
        assert r.ready
        assert r.capital_plan is not None and r.capital_plan.capital_required_usd is not None


class TestRealCapitalPreflight:
    def test_blocks_until_stage3_ready(self) -> None:
        trades = [_trade(100, "2026-01-01")]
        r = assess_stage3(trades, capital_usd=100_000)
        pre = real_capital_preflight(readiness=r, enable_real_trading=False)
        assert pre.decision_allowed is False
        assert "stage3_gates_met" in pre.blockers

    def test_reports_current_real_flag_and_never_enables(self) -> None:
        gates = Stage3Gates(min_track_record_days=3, min_trades=3, min_sharpe=-99.0, max_drawdown_pct=100.0)
        trades = [_trade(200, f"2026-01-0{i+1}") for i in range(5)]
        r = assess_stage3(trades, capital_usd=100_000, gates=gates)
        pre = real_capital_preflight(readiness=r, enable_real_trading=False)
        # The preflight only reports; it has no power to enable anything.
        assert pre.real_trading_currently_enabled is False
        assert "explicit human decision" in pre.note


class TestTrackRecordReport:
    def _trades(self, specs):
        # specs: list of (pnl, "YYYY-MM-DD")
        return [SimpleNamespace(realized_pnl_usd=p, closed_at=f"{d}T15:00:00Z", payload={}) for p, d in specs]

    def test_report_computes_full_metrics(self) -> None:
        from app.performance.stage3 import track_record_report
        trades = self._trades([
            (100, "2026-01-05"), (-40, "2026-01-06"), (200, "2026-02-03"), (-60, "2026-02-10"),
        ])
        rep = track_record_report(trades, capital_usd=100_000)
        assert rep.total_trades == 4
        assert rep.realized_pnl_usd == 200.0
        assert rep.trading_days == 4
        assert set(rep.monthly_returns_pct.keys()) == {"2026-01", "2026-02"}
        assert rep.profitable_months_pct == 100.0  # both months net positive
        assert rep.profit_factor > 1.0
        assert rep.best_day_usd == 200.0 and rep.worst_day_usd == -60.0

    def test_longest_drawdown_duration(self) -> None:
        from app.performance.stage3 import track_record_report
        # Up, then three down days, then up -> longest DD run == 3.
        trades = self._trades([
            (100, "2026-01-01"), (-10, "2026-01-02"), (-10, "2026-01-03"),
            (-10, "2026-01-04"), (500, "2026-01-05"),
        ])
        rep = track_record_report(trades, capital_usd=100_000)
        assert rep.longest_drawdown_days == 3


class TestCapitalLadder:
    def test_ladder_scales_in(self) -> None:
        from app.performance.stage3 import capital_deployment_ladder
        ladder = capital_deployment_ladder(target_capital_usd=400_000)
        assert [s.capital_usd for s in ladder] == [100_000, 200_000, 300_000, 400_000]
        assert ladder[0].cumulative_pct == 25.0
        assert ladder[-1].cumulative_pct == 100.0
        assert all(s.gate for s in ladder)

    def test_custom_fractions(self) -> None:
        from app.performance.stage3 import capital_deployment_ladder
        ladder = capital_deployment_ladder(target_capital_usd=100_000, fractions=[0.5, 1.0])
        assert len(ladder) == 2 and ladder[0].capital_usd == 50_000
