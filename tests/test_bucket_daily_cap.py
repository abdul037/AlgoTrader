"""At most N new entries per correlation bucket per day.

Seen 2026-09-28: NVDA, AAPL, NVDA and META (all tech_complex) opened on one down
day and all stopped out (-$601). The open-exposure caps never saw them together
because the first two had closed before the next two opened.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.models.trade import TradeOrder
from app.risk.context import entries_today_by_bucket
from app.risk.guardrails import RiskContext, RiskManager
from app.risk.sectors import correlation_bucket_for_symbol
from app.utils.time import utc_now
from tests.conftest import make_settings


def _execution(symbol: str, status: str = "filled", *, hours_ago: float = 1.0):
    return SimpleNamespace(
        request_payload={"symbol": symbol},
        status=status,
        created_at=(utc_now() - timedelta(hours=hours_ago)).isoformat(),
    )


class _Repo:
    def __init__(self, items):
        self.items = items

    def list(self, *, limit: int = 500):
        return self.items[:limit]


def test_qqq_and_csco_count_as_tech() -> None:
    assert correlation_bucket_for_symbol("QQQ") == "tech_complex"
    assert correlation_bucket_for_symbol("CSCO") == "tech_complex"
    assert correlation_bucket_for_symbol("IWM") == "broad_market"


def test_counts_only_todays_live_entries() -> None:
    start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    repo = _Repo(
        [
            _execution("NVDA", hours_ago=0),
            _execution("AAPL", hours_ago=0),
            _execution("META", status="failed", hours_ago=0),  # never placed
            _execution("MSFT", hours_ago=30),  # yesterday
            _execution("COST", hours_ago=0),
        ]
    )

    counts = entries_today_by_bucket(repo, start_of_day)

    assert counts == {"tech_complex": 2, "consumer_staples": 1}


def test_third_tech_entry_of_the_day_is_blocked(tmp_path) -> None:
    manager = RiskManager(
        make_settings(tmp_path, max_daily_entries_per_correlation_bucket=2, allowed_instruments=["META", "COST"])
    )
    meta = TradeOrder(symbol="META", amount_usd=1000, leverage=1, proposed_price=724.0, stop_loss=716.0, take_profit=737.0)
    cost = TradeOrder(symbol="COST", amount_usd=1000, leverage=1, proposed_price=922.0, stop_loss=913.0, take_profit=939.0)
    context = RiskContext(account_balance=100_000.0, entries_today_by_correlation_bucket={"tech_complex": 2})

    assert any("Daily entry cap" in reason for reason in manager.validate_order(meta, context).reasons)
    assert not any("Daily entry cap" in reason for reason in manager.validate_order(cost, context).reasons)
