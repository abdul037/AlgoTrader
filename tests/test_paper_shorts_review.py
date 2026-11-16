"""Shorts step 3, review fixes of fa9b7d3 (docs/notes/paper_shorts.md, section 9).

Each fix only tightens or corrects what a short meets; every long path is shown unchanged.
"""

from __future__ import annotations

from app.models.execution import AccountSummary, PortfolioPosition, PortfolioSummary
from app.models.trade import TradeOrder
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskManager
from tests.conftest import make_settings

CAP = "Open-position cap of 2 reached for the information technology sector"


class _Book:
    """Alpaca paper's portfolio: (symbol, signed quantity) positions."""

    def __init__(self, positions, *, equity=100_000.0, cash=100_000.0):
        self.positions, self.equity, self.cash = positions, equity, cash

    def get_portfolio(self):
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=self.cash, equity=self.equity),
            positions=[
                PortfolioPosition(symbol=s, quantity=q, is_buy=q >= 0, market_value=q * 100.0)
                for s, q in self.positions
            ],
        )


class _Executions:
    def count_since(self, _since):
        return 0

    def daily_loss_stats(self):
        return 0.0, 0

    def period_realized_pnl(self, *, days):
        return 0.0

    def list(self, limit=200):
        return []


def _settings(tmp_path, **values):
    values.setdefault("allowed_instruments", ["NVDA", "AAPL", "MSFT", "CRM", "WMT"])
    return make_settings(tmp_path, paper_broker="alpaca", broker_for_equities="alpaca", max_open_positions=20, **values)


def _order(symbol, side="buy"):
    stop, target = (102.0, 96.0) if side == "sell" else (98.0, 104.0)
    return TradeOrder(symbol=symbol, side=side, amount_usd=1_000, proposed_price=100.0, stop_loss=stop, take_profit=target)


def _capped(manager, order, book):
    context = build_risk_context(manager.settings, book, _Executions())
    return [r for r in manager.validate_order(order, context).reasons if r.startswith("Open-position cap")]


# --- 1. the sector cap counts shorts ---------------------------------------------------------


def test_open_shorts_count_toward_a_sector_s_open_positions(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    two_shorts = _Book([("AAPL", -10.0), ("CRM", -5.0)])
    assert _capped(manager, _order("NVDA"), two_shorts) == [CAP]  # a new long
    assert _capped(manager, _order("NVDA", "sell"), two_shorts) == [CAP]  # a new short
    mixed = _Book([("AAPL", 10.0), ("CRM", -5.0)])
    assert _capped(manager, _order("NVDA"), mixed) == [CAP]
    assert _capped(manager, _order("NVDA", "sell"), _Book([("AAPL", -10.0), ("WMT", 3.0)])) == []


def test_a_long_meets_the_sector_cap_exactly_as_before_with_no_short_open(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    assert _capped(manager, _order("NVDA"), _Book([("AAPL", 10.0), ("CRM", 5.0)])) == [CAP]
    assert _capped(manager, _order("NVDA"), _Book([("AAPL", 10.0), ("WMT", 5.0)])) == []
    assert _capped(manager, _order("AAPL"), _Book([("AAPL", 10.0), ("CRM", 5.0)])) == []  # own symbol
    off = RiskManager(_settings(tmp_path / "off", max_open_positions_per_sector=0))
    assert _capped(off, _order("NVDA", "sell"), _Book([("AAPL", -1.0), ("CRM", -1.0)])) == []


def test_while_etoro_decides_the_room_the_cap_is_not_paper_s(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    context = build_risk_context(manager.settings, _Book([("AAPL", -10.0), ("CRM", -5.0)]), _Executions())
    reasons = manager.validate_order(_order("NVDA"), context, etoro_room=[]).reasons
    assert not [r for r in reasons if r.startswith("Open-position cap")]


