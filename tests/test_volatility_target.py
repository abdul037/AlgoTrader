"""Tests for vol-target sizing, fractional Kelly, portfolio heat, drawdown governor."""

from __future__ import annotations

import math

from app.risk.volatility_target import (
    drawdown_governor_multiplier,
    fits_within_heat,
    fractional_kelly_weight,
    heat_budget_remaining_usd,
    portfolio_heat_pct,
    time_stop_hit,
    volatility_target_quantity,
)


class TestVolatilityTargetQuantity:
    def test_targets_daily_dollar_volatility(self) -> None:
        # equity 100k, target 0.2%/day -> $200 daily move; ATR $2/share -> 100 shares.
        q = volatility_target_quantity(
            equity_usd=100_000, target_daily_vol_pct=0.2, atr=2.0, price=50.0,
            max_position_pct=100.0,
        )
        assert math.isclose(q, 100.0)

    def test_position_cap_binds(self) -> None:
        # Uncapped would be huge; cap at 5% of 100k / $50 = 100 shares.
        q = volatility_target_quantity(
            equity_usd=100_000, target_daily_vol_pct=5.0, atr=0.10, price=50.0,
            max_position_pct=5.0,
        )
        assert math.isclose(q, 100.0)

    def test_degenerate_inputs_return_zero(self) -> None:
        assert volatility_target_quantity(equity_usd=0, target_daily_vol_pct=1, atr=1, price=1) == 0.0
        assert volatility_target_quantity(equity_usd=1000, target_daily_vol_pct=1, atr=0, price=1) == 0.0


class TestFractionalKelly:
    def test_positive_edge_scaled_and_capped(self) -> None:
        # W=0.6, R=2 -> kelly = 0.6 - 0.4/2 = 0.4; half-kelly=0.2; under 0.25 cap.
        w = fractional_kelly_weight(win_rate=0.6, win_loss_ratio=2.0, fraction=0.5, cap=0.25)
        assert math.isclose(w, 0.2)

    def test_cap_binds(self) -> None:
        w = fractional_kelly_weight(win_rate=0.9, win_loss_ratio=5.0, fraction=1.0, cap=0.25)
        assert w == 0.25

    def test_accepts_percentage_win_rate(self) -> None:
        frac = fractional_kelly_weight(win_rate=0.6, win_loss_ratio=2.0)
        pct = fractional_kelly_weight(win_rate=60.0, win_loss_ratio=2.0)
        assert math.isclose(frac, pct)

    def test_no_edge_no_allocation(self) -> None:
        assert fractional_kelly_weight(win_rate=0.4, win_loss_ratio=1.0) == 0.0
        assert fractional_kelly_weight(win_rate=0.6, win_loss_ratio=0.0) == 0.0


class TestPortfolioHeat:
    def test_heat_is_sum_of_risk_over_equity(self) -> None:
        assert math.isclose(portfolio_heat_pct([500, 500, 1000], 100_000), 2.0)

    def test_budget_and_fit(self) -> None:
        # cap 6% of 100k = $6000; used $4000 -> $2000 remaining.
        remaining = heat_budget_remaining_usd([2000, 2000], equity_usd=100_000, max_heat_pct=6.0)
        assert math.isclose(remaining, 2000.0)
        assert fits_within_heat(2000, [2000, 2000], equity_usd=100_000, max_heat_pct=6.0)
        assert not fits_within_heat(2001, [2000, 2000], equity_usd=100_000, max_heat_pct=6.0)


class TestDrawdownGovernor:
    def test_full_size_when_shallow(self) -> None:
        assert drawdown_governor_multiplier(drawdown_pct=3.0, soft_pct=5.0, hard_pct=10.0) == 1.0

    def test_linear_ramp_between_soft_and_hard(self) -> None:
        # Halfway from soft(5) to hard(10) with floor 0 -> 0.5 multiplier.
        m = drawdown_governor_multiplier(drawdown_pct=7.5, soft_pct=5.0, hard_pct=10.0, floor=0.0)
        assert math.isclose(m, 0.5)

    def test_floor_beyond_hard(self) -> None:
        assert drawdown_governor_multiplier(drawdown_pct=15.0, soft_pct=5.0, hard_pct=10.0, floor=0.1) == 0.1


class TestTimeStop:
    def test_fires_at_horizon(self) -> None:
        assert time_stop_hit(bars_held=10, max_bars=10)
        assert not time_stop_hit(bars_held=9, max_bars=10)

    def test_disabled_when_unset(self) -> None:
        assert not time_stop_hit(bars_held=100, max_bars=None)
        assert not time_stop_hit(bars_held=100, max_bars=0)
