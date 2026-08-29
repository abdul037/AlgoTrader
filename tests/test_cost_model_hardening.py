"""Tests for the hardened cost model: commission, slippage, market impact,
regulatory sell-side fees, and their aggregation into the persisted metrics."""

from __future__ import annotations

import math

from app.backtesting.cost_model import CostModel, summarize_costs


def test_alpaca_preset_carries_regulatory_and_slippage_terms() -> None:
    model = CostModel.alpaca_equities()
    assert model.commission_per_share == 0.0  # Alpaca is commission-free
    assert model.slippage_bps > 0.0
    assert model.sec_fee_per_dollar > 0.0
    assert model.finra_taf_per_share > 0.0
    assert model.finra_taf_max_usd > 0.0
    assert model.impact_coefficient > 0.0


def test_commission_applies_per_share_with_floor() -> None:
    model = CostModel(commission_per_share=0.005, commission_min_usd=1.0)
    # 100 shares * $0.005 = $0.50, raised to the $1.00 floor.
    assert model.commission_usd(quantity=100) == 1.0
    # 1000 shares * $0.005 = $5.00, above the floor.
    assert model.commission_usd(quantity=1000) == 5.0
    # Zero commission model charges nothing.
    assert CostModel().commission_usd(quantity=1000) == 0.0


def test_slippage_scales_with_notional() -> None:
    model = CostModel(slippage_bps=2.0)
    # 2 bps of $10,000 == $2.00 per leg.
    assert math.isclose(model.slippage_usd(notional_usd=10_000), 2.0)
    assert CostModel().slippage_usd(notional_usd=10_000) == 0.0


def test_market_impact_is_square_root_of_participation() -> None:
    model = CostModel(impact_coefficient=0.1)
    # Trading 1% of ADV: impact fraction == 0.1 * sqrt(0.01) == 0.01 == 100 bps.
    impact = model.market_impact_usd(notional_usd=10_000, avg_daily_dollar_volume=1_000_000)
    assert math.isclose(impact, 10_000 * 0.1 * math.sqrt(0.01))
    # Unknown ADV disables impact (fall back to flat slippage instead).
    assert model.market_impact_usd(notional_usd=10_000, avg_daily_dollar_volume=None) == 0.0
    assert model.market_impact_usd(notional_usd=10_000, avg_daily_dollar_volume=0) == 0.0


def test_regulatory_fees_are_sell_side_and_capped() -> None:
    model = CostModel.alpaca_equities()
    # Big sale: TAF should hit its cap, SEC scales with notional.
    fee = model.regulatory_sell_fee_usd(notional_usd=1_000_000, quantity=10_000_000)
    sec = 1_000_000 * model.sec_fee_per_dollar
    assert math.isclose(fee, sec + model.finra_taf_max_usd)
    # No notional -> no fee.
    assert model.regulatory_sell_fee_usd(notional_usd=0, quantity=100) == 0.0


def test_summarize_costs_aggregates_all_terms() -> None:
    events = [
        {
            "spread_usd": 1.0,
            "financing_usd": 0.5,
            "fx_usd": 0.0,
            "commission_usd": 2.0,
            "slippage_usd": 3.0,
            "regulatory_usd": 0.25,
            "impact_usd": 4.0,
        }
    ]
    summary = summarize_costs(events)
    assert summary["commission_cost_usd"] == 2.0
    assert summary["slippage_cost_usd"] == 3.0
    assert summary["regulatory_cost_usd"] == 0.25
    assert summary["impact_cost_usd"] == 4.0
    assert math.isclose(summary["total_cost_usd"], 1.0 + 0.5 + 2.0 + 3.0 + 0.25 + 4.0)
