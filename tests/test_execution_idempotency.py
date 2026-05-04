from __future__ import annotations

import pytest

from app.execution.coordinator import ExecutionCoordinator
from app.live_signal_schema import MarketQuote
from app.main import create_app
from app.models.approval import ApprovalDecisionRequest, TradeProposalCreate
from app.models.execution_queue import ExecutionQueueRecord, ExecutionQueueStatus
from app.storage.repositories import ExecutionRepository
from app.utils.time import utc_now
from tests.conftest import MockBroker, make_settings


class FakeEtoroMarketData:
    def __init__(self, price: float = 120.0) -> None:
        self.price = price

    def get_rates(self, symbols: list[str]):
        return {
            symbol.upper(): MarketQuote(
                symbol=symbol.upper(),
                bid=self.price - 0.01,
                ask=self.price + 0.01,
                last_execution=self.price,
                timestamp=utc_now().isoformat(),
                source="etoro",
                quote_derived_from_history=False,
                data_age_seconds=0.0,
            )
            for symbol in symbols
        }


def proposal_payload(symbol: str = "NVDA") -> TradeProposalCreate:
    return TradeProposalCreate(
        symbol=symbol,
        amount_usd=1000,
        leverage=1,
        proposed_price=120.0,
        stop_loss=114.0,
        take_profit=132.0,
        strategy_name="ma_crossover",
        rationale="test setup",
    )


def create_approved_proposal(app, symbol: str = "NVDA"):
    proposal = app.state.proposal_service.create_proposal(proposal_payload(symbol))
    return app.state.proposal_service.approve_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="approved"),
    )


def test_client_order_id_is_deterministic() -> None:
    first = ExecutionQueueRecord(id="queue_same", proposal_id="prop_same", symbol="AAPL")
    second = ExecutionQueueRecord(id="queue_same", proposal_id="prop_same", symbol="AAPL")

    first.client_order_id = ExecutionCoordinator._client_order_id(first.proposal_id, first.id)
    second.client_order_id = ExecutionCoordinator._client_order_id(second.proposal_id, second.id)

    assert first.client_order_id == second.client_order_id
    assert first.client_order_id == "151bf6303a88a9bf353010c4c666e625"
    assert len(first.client_order_id) == 32
    assert first.client_order_id.isalnum()


def test_db_unique_blocks_duplicate_queue(tmp_path, monkeypatch) -> None:
    app = create_app(
        make_settings(tmp_path),
        broker=MockBroker(),
        market_data_client=FakeEtoroMarketData(),
        enable_background_jobs=False,
    )
    proposal = create_approved_proposal(app, "NVDA")
    monkeypatch.setattr(app.state.execution_coordinator.queue, "latest_open_for_symbol", lambda symbol: None)

    first = app.state.execution_coordinator.enqueue_approved_proposal(proposal.id)
    with pytest.raises(ValueError, match="Duplicate execution queue item already exists for NVDA"):
        app.state.execution_coordinator.enqueue_approved_proposal(proposal.id)

    queued = app.state.execution_queue_repository.list(status=ExecutionQueueStatus.QUEUED, limit=10)
    assert first.status == ExecutionQueueStatus.QUEUED
    assert first.client_order_id is not None
    assert len(queued) == 1
    assert queued[0].symbol == "NVDA"


def test_broker_retry_with_same_client_order_id_is_idempotent(tmp_path) -> None:
    broker = MockBroker()
    app = create_app(
        make_settings(
            tmp_path,
            execution_mode="live",
            enable_real_trading=True,
            paper_trading_enabled=False,
            max_trades_per_day=10,
        ),
        broker=broker,
        market_data_client=FakeEtoroMarketData(),
        enable_background_jobs=False,
    )
    proposal = create_approved_proposal(app)
    queued = app.state.execution_coordinator.enqueue_approved_proposal(proposal.id)
    executions = ExecutionRepository(app.state.db)

    first = app.state.execution_coordinator.process_queue_item(queued.id)
    second = app.state.execution_coordinator.process_queue_item(queued.id)
    execution_id = first.payload["execution_id"]
    execution = executions.get(execution_id)

    assert first.status == ExecutionQueueStatus.EXECUTED
    assert second.status == ExecutionQueueStatus.EXECUTED
    assert second.payload["execution_id"] == execution_id
    assert execution is not None
    assert execution.request_payload["client_order_id"] == queued.client_order_id
    assert broker.orders[0]["client_order_id"] == queued.client_order_id
    assert len(broker.orders) == 1
    assert executions.count_since(utc_now().replace(hour=0, minute=0, second=0, microsecond=0)) == 1
