from __future__ import annotations

from types import SimpleNamespace

from app.broker.comparison import ParallelBrokerComparisonService
from app.models.execution import BrokerOrderResponse
from app.models.trade import AssetClass, TradeOrder
from app.storage.db import Database
from app.storage.repositories import BrokerGovernanceRepository, RunLogRepository
from tests.conftest import make_settings


class Automation:
    def __init__(self):
        self.reasons = []

    def trip_circuit_breaker(self, *, reason, emergency_stop):
        self.reasons.append((reason, emergency_stop))


class EToroDemo:
    def __init__(self, verified=True):
        self.verified = verified
        self.orders = []

    def get_account_identity(self):
        return {"verified": self.verified}

    def open_market_order_by_amount(self, order, *, client_order_id):
        self.orders.append((order, client_order_id))
        return BrokerOrderResponse(
            order_id="etoro-1",
            status="submitted",
            mode="etoro_demo",
        )


def _proposal():
    return SimpleNamespace(
        id="proposal-1",
        signal=None,
        order=TradeOrder(
            symbol="AAPL",
            amount_usd=1000,
            proposed_price=200,
            stop_loss=190,
            take_profit=220,
            asset_class=AssetClass.EQUITY,
            strategy_name="swing_trend",
        ),
    )


def _service(tmp_path, *, verified=True):
    settings = make_settings(
        tmp_path,
        etoro_demo_v2_enabled=True,
        etoro_parallel_comparison_enabled=True,
    )
    db = Database(settings)
    db.initialize()
    automation = Automation()
    etoro = EToroDemo(verified=verified)
    brokers = BrokerGovernanceRepository(db)
    service = ParallelBrokerComparisonService(
        settings=settings,
        etoro_demo_client=etoro,
        broker_governance=brokers,
        automation=automation,
        run_logs=RunLogRepository(db),
    )
    return service, etoro, automation, brokers


def test_parallel_comparison_mirrors_eligible_alpaca_paper_signal(tmp_path):
    service, etoro, automation, brokers = _service(tmp_path)

    result = service.mirror(
        proposal=_proposal(),
        primary_execution=SimpleNamespace(broker_order_id="alpaca-1"),
        primary_broker="alpaca",
    )

    assert result.status == "submitted"
    assert etoro.orders[0][1] == "mirror:proposal-1"
    assert automation.reasons == []
    brokers.update_comparison_fill(
        broker="alpaca",
        broker_order_id="alpaca-1",
        fill_price=200,
    )
    brokers.update_comparison_fill(
        broker="etoro",
        broker_order_id="etoro-1",
        fill_price=200.1,
        cost_usd=1.5,
    )
    completed = brokers.list_comparisons()[0]
    assert completed["status"] == "completed"
    assert completed["comparison_cost_usd"] == 1.5


def test_comparison_fill_update_allows_missing_fill_price(tmp_path):
    service, _etoro, _automation, brokers = _service(tmp_path)
    service.mirror(
        proposal=_proposal(),
        primary_execution=SimpleNamespace(broker_order_id="alpaca-1"),
        primary_broker="alpaca",
    )

    brokers.update_comparison_fill(
        broker="alpaca",
        broker_order_id="alpaca-1",
        fill_price=None,
    )

    comparison = brokers.list_comparisons()[0]
    assert comparison["status"] == "submitted"
    assert comparison["primary_fill_price"] is None


def test_parallel_comparison_trips_circuit_breaker_for_unverified_account(tmp_path):
    service, _etoro, automation, _brokers = _service(tmp_path, verified=False)

    result = service.mirror(
        proposal=_proposal(),
        primary_execution=SimpleNamespace(broker_order_id="alpaca-1"),
        primary_broker="alpaca",
    )

    assert result.status == "blocked"
    assert automation.reasons
