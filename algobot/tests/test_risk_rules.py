from __future__ import annotations

import pytest

from app.broker.etoro_client import EToroClient
from app.models.trade import OrderSide, TradeOrder
from app.risk.guardrails import RiskContext, RiskManager
from tests.conftest import make_settings


def test_blocked_instrument_is_rejected(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manager = RiskManager(settings)
    order = TradeOrder(
        symbol="OIL",
        side=OrderSide.BUY,
        amount_usd=1000,
        leverage=1,
        proposed_price=70,
        stop_loss=66,
    )
    result = manager.validate_order(
        order,
        RiskContext(account_balance=10000, mode="demo"),
    )
    assert not result.passed
    assert "explicitly blocked" in result.reasons[0]


def test_excessive_leverage_is_rejected(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manager = RiskManager(settings)
    order = TradeOrder(
        symbol="NVDA",
        side=OrderSide.BUY,
        amount_usd=1000,
        leverage=6,
        proposed_price=100,
        stop_loss=96,
    )
    result = manager.validate_order(
        order,
        RiskContext(account_balance=10000, mode="demo"),
    )
    assert not result.passed
    assert any("Leverage" in reason for reason in result.reasons)


def test_real_trading_is_blocked_by_default(tmp_path) -> None:
    settings = make_settings(tmp_path, etoro_account_mode="real", enable_real_trading=False)
    client = EToroClient(settings)
    order = TradeOrder(
        symbol="NVDA",
        side=OrderSide.BUY,
        amount_usd=1000,
        leverage=1,
        proposed_price=100,
        stop_loss=96,
    )
    with pytest.raises(PermissionError):
        client.open_market_order_by_amount(order)
