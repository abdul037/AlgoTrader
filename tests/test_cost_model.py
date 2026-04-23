"""Cost model tests — spread, financing, weekend triple, FX, min-position."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtesting.cost_model import CostModel, is_extended_hours, summarize_costs


def test_half_spread_applied_symmetrically() -> None:
    cm = CostModel(spread_bps=20.0)
    long_buy = cm.entry_fill_price(100.0, side="buy")
    long_sell = cm.exit_fill_price(100.0, side="buy")
    short_sell = cm.entry_fill_price(100.0, side="sell")
    short_cover = cm.exit_fill_price(100.0, side="sell")
    assert long_buy == pytest.approx(100.0 * 1.001, rel=1e-6)
    assert long_sell == pytest.approx(100.0 * 0.999, rel=1e-6)
    assert short_sell == pytest.approx(100.0 * 0.999, rel=1e-6)
    assert short_cover == pytest.approx(100.0 * 1.001, rel=1e-6)


def test_holding_cost_scales_with_days() -> None:
    cm = CostModel(overnight_fee_daily_pct=0.00015, include_weekend_financing=False)
    entry = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday
    exit_ = entry + timedelta(days=3)
    cost = cm.holding_cost_usd(notional_usd=10_000.0, entry_time=entry, exit_time=exit_)
    assert cost == pytest.approx(10_000.0 * 0.00015 * 3, rel=1e-6)


def test_weekend_triple_charge_when_holding_over_friday() -> None:
    cm = CostModel(overnight_fee_daily_pct=0.00015, weekend_multiplier=3.0)
    # Thursday -> Monday: crosses Friday's close once.
    entry = datetime(2026, 1, 8, tzinfo=timezone.utc)  # Thursday
    exit_ = datetime(2026, 1, 12, tzinfo=timezone.utc)  # Monday
    cost = cm.holding_cost_usd(notional_usd=10_000.0, entry_time=entry, exit_time=exit_)
    # 4 calendar days held + 2 extra from weekend multiplier (3x on Friday close).
    expected = 10_000.0 * 0.00015 * (4 + 2)
    assert cost == pytest.approx(expected, rel=1e-6)


def test_min_position_rejection_below_threshold() -> None:
    cm = CostModel(min_position_usd=50.0)
    assert cm.accepts_position(notional_usd=49.99) is False
    assert cm.accepts_position(notional_usd=50.0) is True


def test_naive_timestamp_is_rejected() -> None:
    cm = CostModel()
    naive = datetime(2026, 1, 5)
    with pytest.raises(ValueError):
        cm.holding_cost_usd(notional_usd=1000.0, entry_time=naive, exit_time=naive + timedelta(days=1))


def test_fx_cost_applied_when_spread_nonzero() -> None:
    cm = CostModel(fx_spread_bps=5.0)
    cost = cm.fx_round_trip_cost_usd(notional_usd=10_000.0)
    assert cost == pytest.approx(10_000.0 * 5.0 / 10_000.0, rel=1e-6)


def test_is_extended_hours_flags_premarket_and_aftermarket() -> None:
    # 08:00 ET is premarket.
    premarket = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)  # 08:00 ET in winter
    # 10:00 ET is RTH.
    rth = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
    # 17:00 ET is aftermarket.
    after = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)  # 17:00 ET
    assert is_extended_hours(premarket) is True
    assert is_extended_hours(rth) is False
    assert is_extended_hours(after) is True


def test_summarize_costs_sums_fields() -> None:
    events = [
        {"spread_usd": 1.0, "financing_usd": 0.5, "fx_usd": 0.0},
        {"spread_usd": 2.0, "financing_usd": 1.0, "fx_usd": 0.5},
    ]
    summary = summarize_costs(events)
    assert summary["total_cost_usd"] == pytest.approx(5.0, rel=1e-6)
    assert summary["trades_costed"] == 2
