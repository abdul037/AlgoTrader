from __future__ import annotations

from app.main import create_app
from app.models.execution import ExecutionRecord
from tests.conftest import MockBroker, make_settings


def _execution(*, closed: bool) -> ExecutionRecord:
    leg = {
        "status": "filled",
        "side": "sell" if closed else "buy",
        "filled_avg_price": "110",
        "filled_qty": "1",
    }
    return ExecutionRecord(
        proposal_id="proposal_1",
        mode="paper",
        broker_order_id="order_1",
        request_payload={
            "symbol": "NVDA",
            "strategy_name": "swing",
            "proposed_price": 100,
            "stop_loss": 95,
            "take_profit": 110,
            "amount_usd": 1000,
        },
        response_payload={"broker_execution": {"legs": [leg]}},
        realized_pnl_usd=10,
    )


def test_duplicate_close_event_creates_one_real_label_and_review_job(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    execution = app.state.learning_service.executions.create(_execution(closed=True))

    app.state.learning_service.record_execution_event(execution, event_type="reconciled")
    app.state.learning_service.record_execution_event(execution, event_type="reconciled")

    status = app.state.learning_service.status()
    assert status["decisions"] == 1
    assert status["labels"] == 1
    assert status["pending_jobs"] == 1


def test_entry_fill_does_not_create_closed_trade_label(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    execution = app.state.learning_service.executions.create(_execution(closed=False))

    app.state.learning_service.record_execution_event(execution, event_type="reconciled")

    status = app.state.learning_service.status()
    assert status["labels"] == 0
    assert status["pending_jobs"] == 0


def test_openai_failure_falls_back_to_deterministic_review(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, learning_reviews_enabled=True),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    execution = app.state.learning_service.executions.create(_execution(closed=True))

    review = app.state.learning_service.review_execution(execution.id)

    assert review.reviewer == "deterministic"
    assert "critic_error" in review.details
