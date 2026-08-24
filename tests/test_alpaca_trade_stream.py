from __future__ import annotations

from types import SimpleNamespace

from app.automation.reconciliation import AlpacaReconciliationService
from app.broker.alpaca_trade_stream import (
    AlpacaTradeStream,
    extract_order_update,
)
from app.models.execution import ExecutionStatus


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class _Logs:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class _BrokerOrders:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)


class _Executions:
    def __init__(self, record) -> None:
        self._record = record
        self.updated: list = []

    def get_by_broker_order_id(self, broker_order_id: str):
        if self._record is not None and self._record.broker_order_id == broker_order_id:
            return self._record
        return None

    def update(self, execution) -> None:
        self.updated.append(execution)


class _Alpaca:
    def __init__(self, order) -> None:
        self._order = order
        self.paper = True

    def get_order(self, broker_order_id: str):
        return self._order


def _execution(broker_order_id: str = "abc123"):
    return SimpleNamespace(
        id="exec-1",
        broker_order_id=broker_order_id,
        status=ExecutionStatus.SUBMITTED,
        response_payload={},
        request_payload={"proposed_price": 100.0},
        realized_pnl_usd=None,
        updated_at=None,
    )


def _order_record(broker_order_id: str = "abc123"):
    payload = {
        "broker_order_id": broker_order_id,
        "client_order_id": "cli-1",
        "symbol": "NVDA",
        "side": "buy",
        "order_class": "bracket",
        "status": "filled",
        "filled_qty": 10,
        "filled_avg_price": 101.0,
        "legs": [
            {
                "broker_order_id": "leg-1",
                "symbol": "NVDA",
                "side": "sell",
                "status": "new",
                "order_class": "limit",
            }
        ],
    }
    return SimpleNamespace(broker_order_id=broker_order_id, response_payload=payload)


def _service(alpaca, executions, broker_orders, state, logs):
    return AlpacaReconciliationService(
        settings=SimpleNamespace(alpaca_reconciliation_enabled=True),
        alpaca_client=alpaca,
        executions=executions,
        broker_orders=broker_orders,
        broker_positions=None,
        safety_state=None,
        runtime_state=state,
        run_logs=logs,
        automation=None,
    )


# -- extract_order_update ----------------------------------------------------


def test_extract_from_object() -> None:
    update = SimpleNamespace(event="fill", order=SimpleNamespace(id="o1", status="filled"))
    info = extract_order_update(update)
    assert info.order_id == "o1"
    assert info.event == "fill"
    assert info.status == "filled"
    assert info.is_relevant is True


def test_extract_from_dict() -> None:
    update = {"event": "PARTIAL_FILL", "order": {"id": "o2", "status": "partially_filled"}}
    info = extract_order_update(update)
    assert info.order_id == "o2"
    assert info.event == "partial_fill"
    assert info.is_relevant is True


def test_non_terminal_event_is_not_relevant() -> None:
    info = extract_order_update({"event": "new", "order": {"id": "o3", "status": "new"}})
    assert info.is_relevant is False


def test_missing_order_id_is_not_relevant() -> None:
    info = extract_order_update({"event": "fill", "order": {"status": "filled"}})
    assert info.is_relevant is False


# -- handle_update dispatch --------------------------------------------------


def test_handle_update_calls_back_only_for_relevant_events() -> None:
    seen: list[str] = []
    stream = AlpacaTradeStream(
        api_key="k",
        secret_key="s",
        paper=True,
        on_order_update=seen.append,
    )
    assert stream.handle_update({"event": "fill", "order": {"id": "o1", "status": "filled"}}) is True
    assert stream.handle_update({"event": "new", "order": {"id": "o1", "status": "new"}}) is False
    assert seen == ["o1"]


def test_handle_update_swallows_callback_errors() -> None:
    def boom(_order_id: str) -> None:
        raise RuntimeError("nope")

    logs = _Logs()
    stream = AlpacaTradeStream(
        api_key="k", secret_key="s", paper=True, on_order_update=boom, run_logs=logs
    )
    # Must not raise even though the callback does.
    assert stream.handle_update({"event": "fill", "order": {"id": "o1", "status": "filled"}}) is False
    assert any(evt == "alpaca_trade_stream_error" for evt, _ in logs.events)


# -- ingest_order_update (reconciliation reuse) ------------------------------


def test_ingest_updates_execution_to_filled() -> None:
    execution = _execution()
    state, logs, broker_orders = _State(), _Logs(), _BrokerOrders()
    service = _service(_Alpaca(_order_record()), _Executions(execution), broker_orders, state, logs)

    result = service.ingest_order_update("abc123")

    assert result["status"] == "updated"
    assert execution.status == ExecutionStatus.FILLED
    assert state.get("trade_stream:last_update_at") is not None
    # Parent order + one leg upserted.
    assert {u["broker_order_id"] for u in broker_orders.upserts} == {"abc123", "leg-1"}


def test_ingest_ignores_unknown_order() -> None:
    state, logs, broker_orders = _State(), _Logs(), _BrokerOrders()
    service = _service(_Alpaca(_order_record()), _Executions(None), broker_orders, state, logs)

    result = service.ingest_order_update("does-not-exist")

    assert result["status"] == "unknown_order"
    assert broker_orders.upserts == []


def test_ingest_skips_blank_order_id() -> None:
    state, logs, broker_orders = _State(), _Logs(), _BrokerOrders()
    service = _service(_Alpaca(_order_record()), _Executions(_execution()), broker_orders, state, logs)
    assert service.ingest_order_update("")["status"] == "skipped"
