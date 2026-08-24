from __future__ import annotations

from types import SimpleNamespace

from app.models.trade import TradeOrder
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskContext, RiskManager
from tests.conftest import make_settings


def _order() -> TradeOrder:
    return TradeOrder(symbol="NVDA", amount_usd=100, proposed_price=100, stop_loss=99)


def test_open_loss_trips_daily_limit(tmp_path) -> None:
    settings = make_settings(tmp_path, max_daily_loss_usd=50.0)
    manager = RiskManager(settings)
    # No realized loss yet, but a -60 open position exceeds the 50 daily cap.
    ctx = RiskContext(
        account_balance=10_000,
        daily_realized_pnl_usd=0.0,
        open_unrealized_pnl_usd=-60.0,
        mode="paper",
    )
    result = manager.validate_order(_order(), ctx)
    assert "Daily loss limit has already been reached" in result.reasons


def test_realized_plus_open_loss_combine(tmp_path) -> None:
    settings = make_settings(tmp_path, max_daily_loss_usd=50.0)
    manager = RiskManager(settings)
    # -30 realized + -25 open = -55 <= -50 cap.
    ctx = RiskContext(
        account_balance=10_000,
        daily_realized_pnl_usd=-30.0,
        open_unrealized_pnl_usd=-25.0,
        mode="paper",
    )
    result = manager.validate_order(_order(), ctx)
    assert "Daily loss limit has already been reached" in result.reasons


def test_open_gain_does_not_mask_realized_loss(tmp_path) -> None:
    settings = make_settings(tmp_path, max_daily_loss_usd=50.0)
    manager = RiskManager(settings)
    # -60 realized already breaches; a +100 open gain must NOT rescue it.
    ctx = RiskContext(
        account_balance=10_000,
        daily_realized_pnl_usd=-60.0,
        open_unrealized_pnl_usd=100.0,
        mode="paper",
    )
    result = manager.validate_order(_order(), ctx)
    assert "Daily loss limit has already been reached" in result.reasons


def test_toggle_off_restores_realized_only(tmp_path) -> None:
    settings = make_settings(
        tmp_path, max_daily_loss_usd=50.0, loss_limit_includes_unrealized=False
    )
    manager = RiskManager(settings)
    ctx = RiskContext(
        account_balance=10_000,
        daily_realized_pnl_usd=0.0,
        open_unrealized_pnl_usd=-500.0,
        mode="paper",
    )
    result = manager.validate_order(_order(), ctx)
    assert "Daily loss limit has already been reached" not in result.reasons


class _Executions:
    def count_since(self, _since):
        return 0

    def daily_loss_stats(self):
        return 0.0, 0

    def consecutive_losses(self):
        return 0

    def period_realized_pnl(self, *, days):
        return 0.0


def test_build_risk_context_sums_unrealized_pnl() -> None:
    settings = SimpleNamespace(execution_mode="paper", paper_broker="alpaca", etoro_account_mode="demo")
    account = SimpleNamespace(equity=10_000.0, cash_balance=10_000.0)
    positions = [
        SimpleNamespace(symbol="NVDA", market_value=1000.0, unrealized_pnl=-40.0),
        SimpleNamespace(symbol="AMD", market_value=1000.0, unrealized_pnl=15.0),
    ]
    broker = SimpleNamespace(get_portfolio=lambda: SimpleNamespace(account=account, positions=positions))
    ctx = build_risk_context(settings, broker, _Executions())
    assert ctx.open_unrealized_pnl_usd == -25.0
