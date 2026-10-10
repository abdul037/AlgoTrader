"""Lifecycle evidence is read once per lifecycles() call (timeouts review 2026-10-09).

Per execution, the old path re-read the latest reconciliation, the trade review and two
1000-row lists, so candidate_blockers took 90-111 s at 24-31 executions. The batched path
must produce exactly the same flags -- it feeds the auto-approval circuit-breaker.
"""

from __future__ import annotations

from app.main import create_app
from app.models.execution import ExecutionRecord
from tests.conftest import MockBroker, make_settings


def _execution(index: int, *, client_order_id: str, filled: bool) -> ExecutionRecord:
    broker = {
        "broker_order_id": f"order-{index}",
        "client_order_id": client_order_id,
        "symbol": "NVDA",
        "side": "buy",
        "qty": 1.0,
        "filled_qty": 1.0 if filled else 0.0,
        "filled_avg_price": 100.0 if filled else None,
        "order_class": "bracket",
        "status": "filled" if filled else "canceled",
        "legs": [
            {
                "id": f"stop-{index}",
                "side": "sell",
                "order_type": "stop",
                "stop_price": 95.0,
                "status": "new",
            },
            {
                "id": f"tp-{index}",
                "side": "sell",
                "order_type": "limit",
                "limit_price": 110.0,
                "status": "new",
            },
        ],
    }
    return ExecutionRecord(
        id=f"exec_{index}",
        proposal_id=f"prop_{index}",
        status="filled" if filled else "canceled",
        mode="alpaca_paper",
        broker_order_id=f"order-{index}",
        request_payload={
            "symbol": "NVDA",
            "side": "buy",
            "strategy_name": "s1",
            "client_order_id": client_order_id,
        },
        response_payload={"broker": "alpaca", "broker_execution": broker},
        created_at=f"2026-10-0{1 + index % 8}T14:00:{index:02d}+00:00",
        updated_at=f"2026-10-0{1 + index % 8}T14:00:{index:02d}+00:00",
    )


def _seeded_service(tmp_path, count: int):
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    for index in range(count):
        # Three rows share one client_order_id -> duplicate_order_absent must be False for them.
        client_order_id = "dup-client" if index < 3 else f"client-{index}"
        service.executions.create(
            _execution(index, client_order_id=client_order_id, filled=index % 2 == 0)
        )
    service.safety_state.record_reconciliation(
        status="ok",
        account_number="PA0",
        orders_seen=count,
        positions_seen=1,
        issues=[],
        account={"equity": 1},
    )
    return service


def test_batched_lifecycle_flags_match_the_per_execution_reads(tmp_path) -> None:
    service = _seeded_service(tmp_path, 8)
    reviewed = service.broker_executions(limit=1000)[4].execution_id
    reviews = service.learning_repository
    original_get = reviews.get_review
    reviews.get_review = lambda execution_id: (
        object() if execution_id == reviewed else original_get(execution_id)
    )
    reviews.reviewed_execution_ids = lambda ids: {reviewed} & set(ids)

    batched = {
        item.execution_id: item.flags.model_dump() for item in service.lifecycles(limit=1000)
    }
    per_execution = {
        record.execution_id: service._lifecycle_from_execution(record).flags.model_dump()
        for record in service.broker_executions(limit=1000)
    }

    assert batched == per_execution
    assert [batched[f"exec_{i}"]["duplicate_order_absent"] for i in range(4)] == [
        False,
        False,
        False,
        True,
    ]
    assert batched[reviewed]["review_created"] is True


def test_lifecycle_reads_do_not_grow_with_the_number_of_executions(tmp_path) -> None:
    service = _seeded_service(tmp_path, 30)
    calls = {"executions.list": 0, "latest_reconciliation": 0, "get_review": 0}

    def counting(name, func):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return func(*args, **kwargs)

        return wrapper

    service.executions.list = counting("executions.list", service.executions.list)
    service.safety_state.latest_reconciliation = counting(
        "latest_reconciliation", service.safety_state.latest_reconciliation
    )
    service.learning_repository.get_review = counting(
        "get_review", service.learning_repository.get_review
    )

    assert len(service.lifecycles(limit=1000)) == 30
    assert calls == {"executions.list": 2, "latest_reconciliation": 1, "get_review": 0}


def test_review_batch_failure_falls_back_without_raising(tmp_path) -> None:
    # A raise out of lifecycles() would leave the near-miss path with no breaker blockers.
    service = _seeded_service(tmp_path, 6)
    expected = {i.execution_id: i.flags.model_dump() for i in service.lifecycles(limit=1000)}

    def broken(ids):
        raise RuntimeError("db hiccup")

    service.learning_repository.reviewed_execution_ids = broken
    got = {i.execution_id: i.flags.model_dump() for i in service.lifecycles(limit=1000)}
    assert got == expected


def test_reviewed_execution_ids_is_exact_and_chunked(tmp_path) -> None:
    service = _seeded_service(tmp_path, 1)
    repo = service.learning_repository
    connects = {"n": 0}
    real_connect = repo.db.connect

    def counting_connect(*a, **kw):
        connects["n"] += 1
        return real_connect(*a, **kw)

    repo.db.connect = counting_connect
    assert repo.reviewed_execution_ids([]) == set() and connects["n"] == 0  # no query
    assert repo.reviewed_execution_ids([f"missing-{i}" for i in range(1200)]) == set()
    assert connects["n"] == 3  # 500 + 500 + 200
