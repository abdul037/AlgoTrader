"""Alpaca-specific cost-model factory tests."""

from __future__ import annotations

from app.backtesting.cost_model import CostModel


def test_alpaca_equities_has_zero_financing() -> None:
    cm = CostModel.alpaca_equities()

    assert cm.overnight_fee_daily_pct == 0.0
    assert cm.include_weekend_financing is False


def test_alpaca_equities_has_tight_spread_2_bps() -> None:
    cm = CostModel.alpaca_equities()

    assert cm.spread_bps == 2.0
    assert cm.extended_hours_spread_bps == 10.0


def test_alpaca_equities_no_weekend_multiplier() -> None:
    cm = CostModel.alpaca_equities()

    assert cm.weekend_multiplier == 1.0


def test_alpaca_equities_supports_fractional_position_floor_1_usd() -> None:
    cm = CostModel.alpaca_equities()

    assert cm.min_position_usd == 1.0
    assert cm.accepts_position(notional_usd=0.99) is False
    assert cm.accepts_position(notional_usd=1.0) is True


def test_etoro_factory_unchanged() -> None:
    cm = CostModel()

    assert cm.spread_bps == 10.0
    assert cm.extended_hours_spread_bps == 30.0
    assert cm.overnight_fee_daily_pct == 0.00015
    assert cm.weekend_multiplier == 3.0
    assert cm.min_position_usd == 50.0
    assert cm.include_weekend_financing is True
