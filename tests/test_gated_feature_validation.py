"""Tests for the gated-feature validation logic (regime impact + governor replay)."""

from __future__ import annotations

from types import SimpleNamespace

from app.performance.gated_feature_validation import (
    regime_router_impact,
    simulate_drawdown_governor,
)
from app.screener.regime_router import RegimeSignal


def _spec(name, style):
    return SimpleNamespace(name=name, style=style)


def _trade(pnl, closed_at):
    return SimpleNamespace(realized_pnl_usd=pnl, closed_at=closed_at)


class TestRegimeRouterImpact:
    def test_downtrend_drops_momentum_families(self) -> None:
        specs = {
            "1d": [_spec("a", "breakout"), _spec("b", "mean_reversion"), _spec("c", "trend")],
            "15m": [_spec("d", "intraday"), _spec("e", "reversal")],
        }
        impact = regime_router_impact(specs, RegimeSignal(trend="down"))
        assert impact.allowed_families == ["mean_reversion"]
        # 1d: keep only mean_reversion (b); 15m: keep only reversal (e).
        assert impact.per_timeframe["1d"]["after"] == 1
        assert impact.per_timeframe["15m"]["after"] == 1
        assert impact.total_specs_before == 5
        assert impact.total_specs_after == 2

    def test_healthy_uptrend_keeps_momentum(self) -> None:
        specs = {"1d": [_spec("a", "breakout"), _spec("b", "mean_reversion")]}
        impact = regime_router_impact(specs, RegimeSignal(trend="up", breadth="strong"))
        assert impact.per_timeframe["1d"]["after"] == 1  # breakout kept, mean_reversion dropped


class TestGovernorSimulation:
    def test_governor_cuts_drawdown_on_accumulating_losses(self) -> None:
        # Two losing trades the same day; the governor shrinks the second.
        trades = [_trade(-2500, "2026-01-01T14:00Z"), _trade(-2500, "2026-01-01T15:00Z")]
        sim = simulate_drawdown_governor(trades, capital_usd=100_000, soft_pct=2.0, hard_pct=5.0, floor=0.25)
        assert sim.baseline_pnl_usd == -5000.0
        assert sim.governed_pnl_usd > -5000.0  # second loss scaled down
        assert sim.governed_max_drawdown_usd < sim.baseline_max_drawdown_usd
        assert sim.recommendation.startswith("enable")

    def test_all_green_day_no_change(self) -> None:
        trades = [_trade(100, "2026-01-01T14:00Z"), _trade(200, "2026-01-01T15:00Z")]
        sim = simulate_drawdown_governor(trades, capital_usd=100_000, soft_pct=2.0, hard_pct=5.0, floor=0.25)
        # No intraday drawdown -> governor never scales -> identical.
        assert sim.governed_pnl_usd == sim.baseline_pnl_usd == 300.0
        assert sim.recommendation == "keep off — no drawdown benefit on this history"

    def test_governor_resets_per_day(self) -> None:
        # A loss on day 1 must not govern day 2's first trade.
        trades = [_trade(-4000, "2026-01-01T15:00Z"), _trade(1000, "2026-01-02T15:00Z")]
        sim = simulate_drawdown_governor(trades, capital_usd=100_000, soft_pct=2.0, hard_pct=5.0, floor=0.25)
        # Day 2's trade sees zero prior-day-loss carryover -> full size -> +1000.
        assert sim.governed_pnl_usd == -3000.0  # -4000 + 1000
