from __future__ import annotations

from app.models.trade import OrderSide, TradeOrder
from app.risk.allocation import AllocationCandidate, allocate_candidates
from app.risk.guardrails import RiskContext, RiskManager
from tests.conftest import make_settings


def test_short_entry_requires_capital_borrow_cost_and_margin(tmp_path):
    settings = make_settings(tmp_path, short_trading_enabled=True)
    manager = RiskManager(settings)
    order = TradeOrder(
        symbol="NVDA",
        side=OrderSide.SELL,
        amount_usd=1000,
        proposed_price=100,
        stop_loss=105,
        metadata={"opens_short": True},
    )

    result = manager.validate_order(order, RiskContext(account_balance=20_000, mode="paper"))

    assert result.passed is False
    assert "short_minimum_account_equity_not_met" in result.reasons
    assert "short_not_easy_to_borrow" in result.reasons
    assert "short_borrow_cost_missing" in result.reasons
    assert "short_margin_requirement_missing" in result.reasons


def test_portfolio_drawdown_and_projected_exposure_block_order(tmp_path):
    settings = make_settings(tmp_path, institutional_portfolio_controls_enabled=True)
    manager = RiskManager(settings)
    order = TradeOrder(
        symbol="NVDA",
        amount_usd=1000,
        proposed_price=100,
        stop_loss=99,
        metadata={"sector": "technology"},
    )

    result = manager.validate_order(
        order,
        RiskContext(
            account_balance=10_000,
            portfolio_drawdown_pct=10,
            gross_exposure_pct=25,
            exposure_by_symbol_pct={"NVDA": 10},
            exposure_by_sector_pct={"technology": 20},
            correlated_exposure_pct=25,
            mode="paper",
        ),
    )

    assert result.passed is False
    assert "Portfolio hard drawdown limit reached" in result.reasons
    assert "Projected gross exposure exceeds the portfolio limit" in result.reasons
    assert "Projected sector exposure exceeds the portfolio limit" in result.reasons


def test_allocator_enforces_trade_sector_and_correlation_caps():
    decisions = allocate_candidates(
        [
            AllocationCandidate(
                symbol="AAPL",
                strategy_name="trend",
                sector="technology",
                score=90,
                annualized_volatility_pct=20,
                correlation_bucket="mega_cap_tech",
                requested_amount_usd=5000,
            ),
            AllocationCandidate(
                symbol="MSFT",
                strategy_name="momentum",
                sector="technology",
                score=80,
                annualized_volatility_pct=22,
                correlation_bucket="mega_cap_tech",
                requested_amount_usd=5000,
            ),
        ],
        equity_usd=10_000,
        gross_exposure_limit_pct=30,
        symbol_limit_pct=15,
        sector_limit_pct=20,
        correlation_limit_pct=20,
        per_trade_cap_usd=1000,
    )

    assert decisions[0].amount_usd == 1000
    assert decisions[1].amount_usd == 1000
    assert decisions[0].weight_pct == 10


def test_live_risk_uses_micro_live_cap(tmp_path):
    settings = make_settings(
        tmp_path,
        execution_mode="live",
        max_risk_per_trade_pct=1.0,
        portfolio_micro_live_max_risk_per_trade_pct=0.1,
        institutional_portfolio_controls_enabled=True,
    )
    manager = RiskManager(settings)
    order = TradeOrder(
        symbol="NVDA",
        amount_usd=1000,
        proposed_price=100,
        stop_loss=98,
    )

    result = manager.validate_order(order, RiskContext(account_balance=10_000, mode="real"))

    assert result.passed is False
    assert any("0.10% cap" in reason for reason in result.reasons)
