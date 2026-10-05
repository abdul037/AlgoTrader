"""The reconciliation sweep writes an order/execution row only when the broker state
changed (2026-10-05: unconditional rewrites of ~500 orders took ~140 s per sweep and
pushed maintenance past its 240 s limit). A full rewrite still happens periodically."""

from __future__ import annotations

from types import SimpleNamespace

import app.automation.reconciliation as recon
from app.automation.reconciliation import AlpacaReconciliationService
from app.models.execution import ExecutionStatus


class _State(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, payload))


class _BrokerOrders:
    def __init__(self, fail_once=False):
        self.upserts = []
        self.fail_once = fail_once

    def upsert(self, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("db hiccup")
        self.upserts.append(kwargs["broker_order_id"])


class _Executions:
    def __init__(self, record):
        self.record = record
        self.updated = 0

    def list(self, limit=2000):
        return [self.record]

    def update(self, execution):
        self.updated += 1


def _order(status="filled", leg_status="new"):
    payload = {
        "broker_order_id": "o1",
        "symbol": "NVDA",
        "side": "buy",
        "order_class": "bracket",
        "status": status,
        "filled_qty": 10,
        "filled_avg_price": 101.0,
        "legs": [
            {
                "broker_order_id": "l1",
                "symbol": "NVDA",
                "side": "sell",
                "type": "stop",
                "status": leg_status,
            },
            {
                "broker_order_id": "l2",
                "symbol": "NVDA",
                "side": "sell",
                "type": "limit",
                "status": leg_status,
            },
        ],
    }
    return SimpleNamespace(broker_order_id="o1", response_payload=payload)


class _Alpaca:
    paper = True

    def __init__(self):
        self.orders = [_order()]

    def get_account_identity(self):
        return {"account_number": "PA1", "id": "a1", "status": "ACTIVE", "trading_blocked": False}

    def get_all_orders(self):
        return self.orders

    def get_portfolio(self):
        return SimpleNamespace(
            positions=[SimpleNamespace(symbol="NVDA", quantity=10.0, side="long")]
        )


def _service(broker_orders=None):
    execution = SimpleNamespace(
        id="e1",
        broker_order_id="o1",
        status=ExecutionStatus.SUBMITTED,
        response_payload={},
        request_payload={"proposed_price": 100.0},
        realized_pnl_usd=None,
        updated_at=None,
    )
    service = AlpacaReconciliationService(
        settings=SimpleNamespace(alpaca_reconciliation_enabled=True),
        alpaca_client=_Alpaca(),
        executions=_Executions(execution),
        broker_orders=broker_orders or _BrokerOrders(),
        broker_positions=SimpleNamespace(replace_active=lambda **kw: None),
        safety_state=SimpleNamespace(record_reconciliation=lambda **kw: None),
        runtime_state=_State(),
        run_logs=_Logs(),
        automation=SimpleNamespace(set_account_verified=lambda verified: None),
    )
    return service


def test_unchanged_sweep_writes_nothing() -> None:
    service = _service()
    assert service.reconcile()["status"] == "ok"
    assert service.broker_orders.upserts == ["o1", "l1", "l2"] and service.executions.updated == 1
    service.reconcile()
    assert service.broker_orders.upserts == ["o1", "l1", "l2"] and service.executions.updated == 1


def test_changed_leg_is_written_again() -> None:
    service = _service()
    service.reconcile()
    service.alpaca.orders = [_order(leg_status="accepted")]  # the legs changed state at the broker
    service.reconcile()
    assert service.broker_orders.upserts[3:] == ["o1", "l1", "l2"]  # parent payload embeds the legs
    assert service.executions.updated == 2


def test_periodic_full_rewrite(monkeypatch) -> None:
    service = _service()
    service.reconcile()
    service._full_write_due = 0.0  # refresh window elapsed
    service.reconcile()
    assert (
        service.broker_orders.upserts == ["o1", "l1", "l2"] * 2 and service.executions.updated == 2
    )
    assert recon.FULL_WRITE_REFRESH_SECONDS >= 60


def test_failed_write_is_retried_next_sweep() -> None:
    service = _service(_BrokerOrders(fail_once=True))
    service.reconcile()  # the first upsert raises -> sweep attempt fails and retries
    service.reconcile()
    assert "o1" in service.broker_orders.upserts
