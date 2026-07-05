from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.execution import ExecutionRecord
from app.models.execution_queue import ExecutionQueueRecord
from tests.conftest import MockBroker, make_settings


def test_paper_dashboard_exposes_performance_and_controls(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    client = TestClient(app)

    response = client.get("/paper/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper"]["mode"] == "paper"
    assert payload["paper"]["total_trades"] == 0
    assert payload["risk_controls"]["execution_mode"] == "paper"
    assert payload["risk_controls"]["enable_real_trading"] is False
    assert "calibration_suggestions" in payload


def test_paper_broker_executions_exposes_real_alpaca_lifecycle(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(
        ExecutionRecord(
            id="exec_canceled",
            proposal_id="prop_canceled",
            status="canceled",
            mode="alpaca_paper",
            broker_order_id="old-canceled-order",
            request_payload={
                "symbol": "NVDA",
                "side": "buy",
                "strategy_name": "manual_smoke",
                "client_order_id": "old-client",
            },
            response_payload={
                "broker": "alpaca",
                "broker_execution": {
                    "broker_order_id": "old-canceled-order",
                    "client_order_id": "old-client",
                    "symbol": "NVDA",
                    "side": "buy",
                    "qty": 0.1,
                    "filled_qty": 0.0,
                    "order_class": "simple",
                    "status": "canceled",
                    "created_at": "2026-05-17T21:18:49+00:00",
                    "canceled_at": "2026-05-17T21:20:40+00:00",
                },
            },
            created_at="2026-05-17T21:17:44+00:00",
            updated_at="2026-05-17T21:20:40+00:00",
        )
    )
    service.executions.create(
        ExecutionRecord(
            id="exec_nvda",
            proposal_id="prop_nvda",
            status="filled",
            mode="alpaca_paper",
            broker_order_id="parent-order",
            request_payload={
                "symbol": "NVDA",
                "side": "buy",
                "strategy_name": "manual_smoke",
                "client_order_id": "client-parent",
            },
            response_payload={
                "broker": "alpaca",
                "broker_execution": {
                    "broker_order_id": "parent-order",
                    "client_order_id": "client-parent",
                    "symbol": "NVDA",
                    "side": "buy",
                    "qty": 1.0,
                    "filled_qty": 1.0,
                    "filled_avg_price": 208.64,
                    "order_class": "bracket",
                    "status": "filled",
                    "created_at": "2026-06-22T18:57:07+00:00",
                    "filled_at": "2026-06-22T18:57:08+00:00",
                    "legs": [],
                },
            },
        )
    )
    service.execution_queue.create(
        ExecutionQueueRecord(
            id="queue_nvda",
            proposal_id="prop_nvda",
            symbol="NVDA",
            strategy_name="manual_smoke",
            status="executed",
            client_order_id="client-parent",
            payload={"order": {"strategy_name": "manual_smoke"}},
        )
    )
    service.broker_orders.upsert(
        broker_order_id="parent-order",
        execution_id="exec_nvda",
        client_order_id="client-parent",
        symbol="NVDA",
        side="buy",
        order_class="bracket",
        status="filled",
        filled_qty=1.0,
        filled_avg_price=208.64,
        parent_order_id=None,
        payload={
            "broker_order_id": "parent-order",
            "client_order_id": "client-parent",
            "symbol": "NVDA",
            "side": "buy",
            "qty": 1.0,
            "filled_qty": 1.0,
            "filled_avg_price": 208.64,
            "order_class": "bracket",
            "status": "filled",
            "created_at": "2026-06-22T18:57:07+00:00",
            "filled_at": "2026-06-22T18:57:08+00:00",
        },
    )
    service.broker_orders.upsert(
        broker_order_id="take-profit",
        execution_id="exec_nvda",
        client_order_id="tp-client",
        symbol="NVDA",
        side="sell",
        order_class="bracket_leg",
        status="canceled",
        filled_qty=0.0,
        filled_avg_price=None,
        parent_order_id="parent-order",
        payload={
            "broker_order_id": "take-profit",
            "client_order_id": "tp-client",
            "symbol": "NVDA",
            "side": "sell",
            "type": "limit",
            "status": "canceled",
            "qty": 1.0,
            "limit_price": 215.13,
            "created_at": "2026-06-22T18:57:07+00:00",
            "canceled_at": "2026-06-22T19:00:50+00:00",
        },
    )
    service.broker_orders.upsert(
        broker_order_id="stop-loss",
        execution_id="exec_nvda",
        client_order_id="sl-client",
        symbol="NVDA",
        side="sell",
        order_class="bracket_leg",
        status="canceled",
        filled_qty=0.0,
        filled_avg_price=None,
        parent_order_id="parent-order",
        payload={
            "broker_order_id": "stop-loss",
            "client_order_id": "sl-client",
            "symbol": "NVDA",
            "side": "sell",
            "type": "stop",
            "status": "canceled",
            "qty": 1.0,
            "stop_price": 204.68,
            "created_at": "2026-06-22T18:57:07+00:00",
            "canceled_at": "2026-06-22T19:00:50+00:00",
        },
    )
    service.broker_orders.upsert(
        broker_order_id="close-order",
        execution_id=None,
        client_order_id="close-client",
        symbol="NVDA",
        side="sell",
        order_class="simple",
        status="filled",
        filled_qty=1.0,
        filled_avg_price=208.52,
        parent_order_id=None,
        payload={
            "broker_order_id": "close-order",
            "client_order_id": "close-client",
            "symbol": "NVDA",
            "side": "sell",
            "qty": 1.0,
            "filled_qty": 1.0,
            "filled_avg_price": 208.52,
            "order_class": "simple",
            "status": "filled",
            "created_at": "2026-06-22T19:00:52+00:00",
            "filled_at": "2026-06-22T19:00:53+00:00",
        },
    )
    service.safety_state.record_reconciliation(
        status="ok",
        account_number="PA3B287XBZYU",
        orders_seen=4,
        positions_seen=0,
        issues=[],
        account={"equity": 100000},
    )
    client = TestClient(app)

    trades = client.get("/paper/trades").json()
    response = client.get("/paper/broker-executions")
    dashboard = client.get("/paper/dashboard").json()
    lifecycles = client.get("/paper/lifecycles").json()
    autonomous_lifecycles = client.get("/paper/lifecycles?autonomous_only=true").json()

    assert trades == []
    assert response.status_code == 200
    records = {item["execution_id"]: item for item in response.json()}
    record = records["exec_nvda"]
    assert record["execution_id"] == "exec_nvda"
    assert record["queue_id"] == "queue_nvda"
    assert record["source"] == "manual_smoke"
    assert record["entry_fill_price"] == 208.64
    assert record["exit_order_id"] == "close-order"
    assert record["exit_fill_price"] == 208.52
    assert record["realized_pnl_usd"] == -0.12
    assert len(record["legs"]) == 2
    assert records["exec_canceled"]["exit_order_id"] is None
    assert records["exec_canceled"]["exit_fill_price"] is None
    assert records["exec_canceled"]["realized_pnl_usd"] == 0.0
    assert dashboard["recent_broker_executions"][0]["execution_id"] == "exec_nvda"
    lifecycle = {item["execution_id"]: item for item in lifecycles}["exec_nvda"]
    assert lifecycle["source"] == "manual_smoke"
    assert lifecycle["autonomous"] is False
    assert lifecycle["flags"]["entry_submitted"] is True
    assert lifecycle["flags"]["entry_filled"] is True
    assert lifecycle["flags"]["bracket_legs_verified"] is True
    assert lifecycle["flags"]["exit_filled_or_position_flat"] is True
    assert lifecycle["flags"]["reconciled"] is True
    assert lifecycle["flags"]["duplicate_order_absent"] is True
    assert "manual_or_unknown_source" in lifecycle["blockers"]
    assert autonomous_lifecycles == []
