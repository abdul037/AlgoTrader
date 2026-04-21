"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

from app.config import AppSettings
from app.models.execution import AccountSummary, BrokerOrderResponse, PortfolioSummary


class MockBroker:
    """Simple broker double used by tests."""

    def __init__(self) -> None:
        self.orders = []

    def get_portfolio(self) -> PortfolioSummary:
        return PortfolioSummary(
            mode="demo",
            account=AccountSummary(cash_balance=10000.0, equity=10000.0, daily_pnl=0.0),
            positions=[],
        )

    def get_balance(self) -> AccountSummary:
        return self.get_portfolio().account

    def open_market_order_by_amount(self, order):
        self.orders.append(order)
        return BrokerOrderResponse(
            order_id=f"mock-order-{len(self.orders)}",
            status="submitted",
            mode="demo",
            message="mock broker accepted order",
            raw_response={"symbol": order.symbol},
        )

    def close_position(self, symbol: str) -> BrokerOrderResponse:
        return BrokerOrderResponse(
            order_id="mock-close",
            status="submitted",
            mode="demo",
            raw_response={"symbol": symbol},
        )

    def list_supported_instruments(self):
        return []


def make_settings(tmp_path: Path, **overrides) -> AppSettings:
    """Create isolated test settings."""

    defaults = {
        "database_url": f"sqlite:///{(tmp_path / 'test.db').resolve()}",
        "etoro_account_mode": "demo",
        "enable_real_trading": False,
        "require_approval": True,
        "etoro_api_key": "",
        "etoro_user_key": "",
        "etoro_base_url": "https://api.etoro.example",
        "allowed_instruments": ["NVDA", "GOOG", "GOOGL", "AMD", "MU", "GOLD"],
        "blocked_instruments": ["OIL", "NATGAS", "SILVER"],
        "screener_active_strategy_names": ["all"],
        "require_backtest_validation_for_alerts": False,
        "screener_min_final_score_to_alert": 65.0,
        "screener_min_final_score_to_keep": 55.0,
        "screener_min_accuracy_score": 0.52,
        "screener_min_confirmation_score": 0.45,
        "screener_max_false_positive_risk": 0.68,
    }
    defaults.update(overrides)
    return AppSettings(**defaults)
