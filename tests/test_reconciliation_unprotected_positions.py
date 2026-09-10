"""An owned position whose bracket legs are no longer live must not be treated
as protected, and in paper mode reconciliation closes it instead of tripping
the circuit breaker.

Seen 2026-09-09: GOOGL's take-profit expired and its stop was cancelled at the
close (day-TIF bracket), yet reconciliation reported no issues because the legs
still *existed* in the order payload.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.automation.reconciliation import AlpacaReconciliationService
from app.automation.service import AutomationService
from app.models.execution import ExecutionRecord, PortfolioPosition
from app.storage.db import Database
from app.storage.repositories import (
    BrokerOrderSnapshotRepository,
    BrokerPositionSnapshotRepository,
    ExecutionRepository,
    RunLogRepository,
    RuntimeStateRepository,
    SafetyStateRepository,
)
from tests.conftest import make_settings


class BrokerWithDeadBracket:
    def __init__(self, *, leg_statuses: tuple[str, str]):
        self.account_number = "PAPER-1"
        self.closed: list[str] = []
        self.leg_statuses = leg_statuses

    def get_account_identity(self):
        return {"account_number": self.account_number, "trading_blocked": False, "equity": 100000.0, "cash": 99660.0}

    def get_all_orders(self):
        tp_status, stop_status = self.leg_statuses
        return [
            SimpleNamespace(
                broker_order_id="parent-googl",
                response_payload={
                    "broker_order_id": "parent-googl",
                    "symbol": "GOOGL",
                    "side": "buy",
                    "qty": 1.0,
                    "filled_qty": 1.0,
                    "filled_avg_price": 338.75,
                    "order_class": "bracket",
                    "status": "filled",
                    "legs": [
                        {"broker_order_id": "leg-tp", "side": "sell", "type": "limit", "status": tp_status, "limit_price": 349.39},
                        {"broker_order_id": "leg-stop", "side": "sell", "type": "stop", "status": stop_status, "stop_price": 333.24},
                    ],
                },
            )
        ]

    def get_portfolio(self):
        return SimpleNamespace(
            positions=[PortfolioPosition(symbol="GOOGL", quantity=1.0, entry_price=338.75, current_price=330.65)]
        )

    def close_position(self, symbol: str):
        self.closed.append(symbol)
        return SimpleNamespace(order_id="close-1", status="submitted")

    def cancel_all_orders(self):
        return 0

    def close_all_positions(self):
        return 0


class Router:
    def __init__(self, client):
        self.client = client

    def all_clients(self):
        return [self.client]


def _service(tmp_path, broker, **overrides):
    settings = make_settings(
        tmp_path,
        alpaca_expected_account_number="PAPER-1",
        alpaca_reconciliation_enabled=True,
        alpaca_reconciliation_retry_backoff_seconds=0,
        **overrides,
    )
    db = Database(settings)
    db.initialize()
    executions = ExecutionRepository(db)
    executions.create(
        ExecutionRecord(id="exec_googl", proposal_id="prop_googl", status="filled", mode="alpaca_paper", broker_order_id="parent-googl")
    )
    state = RuntimeStateRepository(db)
    logs = RunLogRepository(db)
    automation = AutomationService(settings=settings, runtime_state=state, run_logs=logs, broker_router=Router(broker))
    service = AlpacaReconciliationService(
        settings=settings,
        alpaca_client=broker,
        executions=executions,
        broker_orders=BrokerOrderSnapshotRepository(db),
        broker_positions=BrokerPositionSnapshotRepository(db),
        safety_state=SafetyStateRepository(db),
        runtime_state=state,
        run_logs=logs,
        automation=automation,
    )
    return service, automation, logs


def _events(logs) -> list[str]:
    with logs.db.connect() as connection:
        rows = connection.execute("SELECT event_type FROM run_logs ORDER BY created_at").fetchall()
    return [row["event_type"] for row in rows]


def test_live_bracket_legs_count_as_protection(tmp_path) -> None:
    broker = BrokerWithDeadBracket(leg_statuses=("new", "held"))
    service, automation, _logs = _service(tmp_path, broker)

    result = service.reconcile()

    assert result["status"] == "ok"
    assert result["issues"] == []
    assert broker.closed == []
    assert automation.status().kill_switch_enabled is False


def test_dead_bracket_legs_flatten_the_position_in_paper_mode(tmp_path) -> None:
    broker = BrokerWithDeadBracket(leg_statuses=("expired", "canceled"))
    service, automation, logs = _service(tmp_path, broker)

    result = service.reconcile()

    assert broker.closed == ["GOOGL"]
    assert result["status"] == "ok"
    assert "missing_bracket_protection:GOOGL" not in result["issues"]
    assert "unprotected_position_flattened" in _events(logs)
    assert automation.status().kill_switch_enabled is False


def test_flatten_is_off_when_disabled_and_the_breaker_still_trips(tmp_path) -> None:
    broker = BrokerWithDeadBracket(leg_statuses=("expired", "canceled"))
    service, automation, _logs = _service(
        tmp_path, broker, reconciliation_flatten_unprotected_positions=False
    )

    result = service.reconcile()

    assert broker.closed == []
    assert "missing_bracket_protection:GOOGL" in result["issues"]
    assert automation.status().kill_switch_enabled is True


def test_flatten_never_runs_with_real_trading_enabled(tmp_path) -> None:
    broker = BrokerWithDeadBracket(leg_statuses=("expired", "canceled"))
    service, _automation, _logs = _service(tmp_path, broker, enable_real_trading=True)

    result = service.reconcile()

    assert broker.closed == []
    assert "missing_bracket_protection:GOOGL" in result["issues"]
