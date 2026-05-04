from __future__ import annotations

from datetime import timedelta

from app.live_signal_schema import MarketQuote
from app.main import create_app
from app.models.approval import ApprovalDecisionRequest, TradeProposalCreate
from app.models.execution import ExecutionRecord, ExecutionStatus
from app.models.execution_queue import ExecutionQueueStatus
from app.storage.repositories import ExecutionRepository
from app.utils.time import utc_now
from tests.conftest import MockBroker, make_settings


class FakeEtoroMarketData:
    def __init__(self, price: float = 120.0) -> None:
        self.price = price
        self.calls: list[tuple[list[str], str | None]] = []

    def get_rates(self, symbols: list[str], timeframe: str | None = None):
        self.calls.append((symbols, timeframe))
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


def approved_queue(tmp_path, **settings_overrides):
    market_data = FakeEtoroMarketData()
    app = create_app(
        make_settings(tmp_path, **settings_overrides),
        broker=MockBroker(),
        market_data_client=market_data,
        enable_background_jobs=False,
    )
    proposal = app.state.proposal_service.create_proposal(proposal_payload())
    approved = app.state.proposal_service.approve_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="approved"),
    )
    queued = app.state.execution_coordinator.enqueue_approved_proposal(approved.id)
    return app, market_data, approved, queued


def test_risk_revalidation_blocks_after_kill_switch_flip(tmp_path) -> None:
    app, market_data, proposal, queued = approved_queue(tmp_path)

    app.state.settings.kill_switch_enabled = True
    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.proposal_id == proposal.id
    assert result.latest_quote_price == 120.0
    assert result.validation_reason is not None
    assert result.validation_reason.startswith("risk_failed:")
    assert "Kill switch is enabled" in result.validation_reason
    assert len(market_data.calls) == 1


def test_risk_revalidation_blocks_after_daily_loss_breach(tmp_path) -> None:
    app, market_data, proposal, queued = approved_queue(tmp_path, max_daily_loss_usd=100.0)
    ExecutionRepository(app.state.db).create(
        ExecutionRecord(
            proposal_id="prior_loss",
            status=ExecutionStatus.SUBMITTED,
            mode="paper",
            realized_pnl_usd=-150.0,
        )
    )

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.proposal_id == proposal.id
    assert result.latest_quote_price == 120.0
    assert result.validation_reason is not None
    assert result.validation_reason.startswith("risk_failed:")
    assert "Daily loss limit has already been reached" in result.validation_reason
    assert len(market_data.calls) == 1


def test_status_guard_blocks_expired_proposal(tmp_path) -> None:
    app, market_data, proposal, queued = approved_queue(tmp_path)
    stored = app.state.proposal_service.proposals.get(proposal.id)
    assert stored is not None
    stored.expires_at = (utc_now() - timedelta(minutes=1)).isoformat()
    app.state.proposal_service.proposals.update(stored)

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.validation_reason == "proposal_status_expired"
    assert result.latest_quote_price is None
    assert app.state.proposal_service.get_proposal(proposal.id).status.value == "expired"
    assert market_data.calls == []


def test_status_guard_blocks_rejected_proposal(tmp_path) -> None:
    app, market_data, proposal, queued = approved_queue(tmp_path)
    rejected = app.state.proposal_service.reject_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="reject after approval"),
    )

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert rejected.status.value == "rejected"
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.validation_reason == "proposal_status_rejected"
    assert result.latest_quote_price is None
    assert result.proposal_id == proposal.id
    assert market_data.calls == []
