from __future__ import annotations

from app.models.signal import SignalAction
from app.strategies.base import BaseStrategy, valid_trade_plan


class Strategy(BaseStrategy):
    def generate_signal(self, data, symbol):
        return None


def test_strategy_builder_rejects_negative_short_target():
    signal = Strategy()._build_signal(
        symbol="TEST",
        strategy_name="test",
        action=SignalAction.SELL,
        rationale="invalid short geometry",
        price=1.0,
        stop_loss=2.0,
        take_profit=-1.0,
    )

    assert signal is None


def test_trade_plan_geometry_is_direction_aware():
    assert valid_trade_plan(action="buy", price=100, stop_loss=95, take_profit=110) is True
    assert valid_trade_plan(action="buy", price=100, stop_loss=105, take_profit=110) is False
    assert valid_trade_plan(action="sell", price=100, stop_loss=105, take_profit=90) is True
    assert valid_trade_plan(action="sell", price=100, stop_loss=105, take_profit=110) is False
