"""A losing exit on a symbol blocks re-entering it for a cooldown window.

Seen 2026-09-28: NVDA stopped out at 14:46 UTC; a different strategy re-bought it
at 16:29 and stopped out again (-$187).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.risk.context import recently_stopped_symbols
from app.utils.time import utc_now


def _execution(symbol: str, pnl: float, minutes_ago: int, *, via_flatten: bool = False):
    filled_at = (utc_now() - timedelta(minutes=minutes_ago)).isoformat()
    leg = {"status": "filled", "filled_at": filled_at}
    response = {"exit_fill": leg} if via_flatten else {"broker_execution": {"legs": [{"status": "canceled"}, leg]}}
    return SimpleNamespace(request_payload={"symbol": symbol}, realized_pnl_usd=pnl, response_payload=response)


class _Repo:
    def __init__(self, items):
        self.items = items

    def list(self, *, limit: int = 500):
        return self.items[:limit]


def test_recent_losing_exit_is_in_cooldown() -> None:
    settings = SimpleNamespace(reentry_cooldown_minutes_after_loss=240)
    repo = _Repo(
        [
            _execution("NVDA", -165.66, minutes_ago=103),
            _execution("AAPL", -84.98, minutes_ago=300),  # outside the window
            _execution("INTC", 545.71, minutes_ago=10),  # a win never blocks
            _execution("COST", -124.42, minutes_ago=30, via_flatten=True),
        ]
    )

    assert recently_stopped_symbols(settings, repo) == ["COST", "NVDA"]


def test_a_later_win_clears_the_cooldown() -> None:
    settings = SimpleNamespace(reentry_cooldown_minutes_after_loss=240)
    repo = _Repo([_execution("NVDA", -165.66, minutes_ago=120), _execution("NVDA", 80.0, minutes_ago=20)])

    assert recently_stopped_symbols(settings, repo) == []


def test_cooldown_off_when_zero() -> None:
    settings = SimpleNamespace(reentry_cooldown_minutes_after_loss=0)

    assert recently_stopped_symbols(settings, _Repo([_execution("NVDA", -1.0, minutes_ago=1)])) == []


def test_guardrail_blocks_reentry(tmp_path) -> None:
    from app.models.trade import TradeOrder
    from app.risk.guardrails import RiskContext, RiskManager
    from tests.conftest import make_settings

    manager = RiskManager(make_settings(tmp_path))
    order = TradeOrder(symbol="NVDA", amount_usd=1000, leverage=1, proposed_price=232.0, stop_loss=229.0, take_profit=238.0)
    context = RiskContext(account_balance=100_000.0, recently_stopped_symbols=["NVDA"])

    result = manager.validate_order(order, context)

    assert any("Re-entry cooldown" in reason for reason in result.reasons)
