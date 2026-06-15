from __future__ import annotations

from types import SimpleNamespace

from app.automation.etoro_reconciliation import EToroDemoReconciliationService
from app.storage.db import Database
from app.storage.repositories import (
    BrokerGovernanceRepository,
    RunLogRepository,
    RuntimeStateRepository,
)
from tests.conftest import make_settings


class Idempotency:
    def __init__(self):
        self.completed = []

    def list(self, limit=1000):
        return [{"client_order_id": "proposal-1", "request_id": "request-1"}]

    def complete(self, **values):
        self.completed.append(values)


class Automation:
    def __init__(self):
        self.reasons = []

    def trip_circuit_breaker(self, *, reason, emergency_stop):
        self.reasons.append((reason, emergency_stop))


def _service(tmp_path, client):
    settings = make_settings(
        tmp_path,
        etoro_demo_v2_enabled=True,
        etoro_demo_expected_account_id="77",
    )
    db = Database(settings)
    db.initialize()
    automation = Automation()
    service = EToroDemoReconciliationService(
        settings=settings,
        client=client,
        idempotency=Idempotency(),
        broker_governance=BrokerGovernanceRepository(db),
        runtime_state=RuntimeStateRepository(db),
        run_logs=RunLogRepository(db),
        automation=automation,
    )
    return service, automation


def test_etoro_demo_reconciliation_accepts_owned_protected_position(tmp_path):
    client = SimpleNamespace(
        get_demo_portfolio=lambda: {
            "clientPortfolio": {
                "positions": [
                    {
                        "positionID": 9001,
                        "CID": 77,
                        "stopLossRate": 90,
                        "takeProfitRate": 120,
                    }
                ]
            }
        },
        get_order=lambda **_kwargs: {
            "orderId": 123,
            "accountId": 77,
            "status": {"name": "Executed"},
            "positionExecutions": [{"positionId": 9001}],
        },
    )
    service, automation = _service(tmp_path, client)

    result = service.reconcile()

    assert result["status"] == "ok"
    assert result["unknown_positions"] == 0
    assert result["unprotected_positions"] == 0
    assert automation.reasons == []


def test_etoro_demo_reconciliation_trips_on_unknown_unprotected_position(tmp_path):
    client = SimpleNamespace(
        get_demo_portfolio=lambda: {
            "clientPortfolio": {
                "positions": [
                    {
                        "positionID": 9999,
                        "CID": 77,
                        "isNoStopLoss": True,
                        "isNoTakeProfit": True,
                    }
                ]
            }
        },
        get_order=lambda **_kwargs: {
            "orderId": 123,
            "accountId": 77,
            "status": {"name": "Executed"},
            "positionExecutions": [],
        },
    )
    service, automation = _service(tmp_path, client)

    result = service.reconcile()

    assert result["status"] == "error"
    assert "unknown_etoro_demo_position:9999" in result["issues"]
    assert "missing_etoro_demo_protection:9999" in result["issues"]
    assert automation.reasons
