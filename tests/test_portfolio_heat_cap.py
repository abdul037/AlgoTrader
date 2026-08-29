"""The portfolio-heat cap must block a new trade when aggregate open risk plus
the new trade's risk exceeds the budget, even if the single trade is within its
own per-trade risk cap."""

from __future__ import annotations

from app.models.trade import TradeOrder
from app.risk.guardrails import RiskContext, RiskManager
from tests.conftest import make_settings


def _order() -> TradeOrder:
    # entry 100, stop 99 => 1% stop distance; $1000 notional => ~$10 risk.
    return TradeOrder(symbol="NVDA", amount_usd=1000, proposed_price=100, stop_loss=99)


def test_heat_cap_blocks_when_budget_exhausted(tmp_path) -> None:
    settings = make_settings(tmp_path, portfolio_max_heat_pct=6.0, max_risk_per_trade_pct=5.0)
    manager = RiskManager(settings)
    # Account 10k, cap 6% => $600 heat budget. Existing open risk already $595,
    # so only $5 remains; the new order's ~$10 risk cannot fit.
    context = RiskContext(
        account_balance=10_000,
        open_trade_risks_usd=[300.0, 295.0],
        mode="paper",
    )
    result = manager.validate_order(_order(), context)
    assert result.passed is False
    assert any("Portfolio heat" in r for r in result.reasons)


def test_heat_cap_allows_when_budget_available(tmp_path) -> None:
    settings = make_settings(tmp_path, portfolio_max_heat_pct=6.0, max_risk_per_trade_pct=5.0)
    manager = RiskManager(settings)
    context = RiskContext(
        account_balance=10_000,
        open_trade_risks_usd=[100.0],  # only $100 used of the $600 budget
        mode="paper",
    )
    result = manager.validate_order(_order(), context)
    assert result.passed is True, result.reasons


def test_heat_cap_disabled_when_zero(tmp_path) -> None:
    settings = make_settings(tmp_path, portfolio_max_heat_pct=0.0, max_risk_per_trade_pct=5.0)
    manager = RiskManager(settings)
    context = RiskContext(
        account_balance=10_000,
        open_trade_risks_usd=[5000.0, 5000.0],  # way over any budget
        mode="paper",
    )
    result = manager.validate_order(_order(), context)
    # Heat check is off, and nothing else here blocks the order.
    assert result.passed is True, result.reasons
