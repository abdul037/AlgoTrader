from __future__ import annotations

from app.main import create_app
from app.models.approval import ApprovalDecisionRequest
from app.models.execution import ExecutionStatus
from app.storage.repositories import ExecutionRepository
from tests.conftest import MockBroker, make_settings
from tests.test_execution_coordinator_alpaca_routing import (
    FakeAlpacaClient,
    FakeEtoroMarketData,
    log_events,
    proposal_payload,
)


def test_alpaca_paper_end_to_end_with_idempotent_retry(tmp_path) -> None:
    alpaca = FakeAlpacaClient()
    app = create_app(
        make_settings(
            tmp_path,
            alpaca_enabled=True,
            broker_for_equities="alpaca",
            broker_for_non_equities="etoro",
            paper_broker="alpaca",
            max_trade_amount_usd=1000,
        ),
        broker=MockBroker(),
        alpaca_client=alpaca,
        market_data_client=FakeEtoroMarketData(),
        enable_background_jobs=False,
    )

    proposal = app.state.proposal_service.create_proposal(proposal_payload("NVDA"))
    approved = app.state.proposal_service.approve_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="approved"),
    )
    queued = app.state.execution_coordinator.enqueue_approved_proposal(approved.id)

    first = app.state.execution_coordinator.process_queue_item(queued.id)
    second = app.state.execution_coordinator.process_queue_item(queued.id)

    assert first.payload["execution_id"] == second.payload["execution_id"]
    assert len(alpaca.submitted_orders) == 1
    submitted = alpaca.submitted_orders[0]
    assert submitted["symbol"] == "NVDA"
    assert submitted["side"] == "buy"
    assert submitted["client_order_id"] == queued.client_order_id
    assert submitted["qty"] == 8

    execution = ExecutionRepository(app.state.db).get(str(first.payload["execution_id"]))
    assert execution is not None
    assert execution.proposal_id == proposal.id
    assert execution.status == ExecutionStatus.SUBMITTED
    assert execution.response_payload["broker"] == "alpaca"

    stored = app.state.proposal_service.get_proposal(proposal.id)
    assert stored.status.value == "executed"
    assert stored.execution_id == execution.id

    events = log_events(app)
    expected = [
        "proposal_created",
        "proposal_approved",
        "execution_queue_enqueued",
        "proposal_executed",
        "order_submitted",
        "execution_queue_executed",
    ]
    positions = [events.index(event) for event in expected]
    assert positions == sorted(positions)
