from __future__ import annotations

from app.live_signal_schema import MarketQuote
from app.main import create_app
from app.models.approval import ApprovalDecisionRequest, TradeProposalCreate
from app.models.execution import ExecutionRecord, ExecutionStatus, PortfolioSummary, AccountSummary
from app.models.execution_queue import ExecutionQueueStatus
from app.storage.repositories import ExecutionRepository
from app.utils.time import utc_now
from tests.conftest import MockBroker, make_settings


class FakeAlpacaClient:
    def __init__(self, *, price: float = 120.0, source: str = "alpaca", age: float = 0.0) -> None:
        self.price = price
        self.source = source
        self.age = age
        self.submitted_orders: list[dict] = []
        self.cancel_all_calls = 0
        self.close_all_calls = 0

    def get_quote(self, symbol: str, *, force_refresh: bool = False, timeframe: str = "1d") -> MarketQuote:
        return MarketQuote(
            symbol=symbol.upper(),
            bid=self.price - 0.01,
            ask=self.price + 0.01,
            last_execution=self.price,
            timestamp=utc_now().isoformat(),
            source=self.source,
            quote_derived_from_history=False,
            data_age_seconds=self.age,
        )

    def get_portfolio(self) -> PortfolioSummary:
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=100000.0, equity=100000.0, daily_pnl=0.0),
            positions=[],
        )

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price=None,
        stop_price=None,
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> ExecutionRecord:
        self.submitted_orders.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "order_type": order_type,
                "time_in_force": time_in_force,
                "client_order_id": client_order_id,
            }
        )
        return ExecutionRecord(
            proposal_id=f"alpaca:{client_order_id}",
            status=ExecutionStatus.SUBMITTED,
            mode="alpaca_paper",
            broker_order_id=f"alpaca-order-{len(self.submitted_orders)}",
            response_payload={"client_order_id": client_order_id},
        )

    def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        return 3

    def close_all_positions(self) -> int:
        self.close_all_calls += 1
        return 2


class FakeEtoroMarketData:
    def __init__(self, price: float = 120.0) -> None:
        self.price = price

    def get_rates(self, symbols: list[str], timeframe: str | None = None):
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


def proposal_payload(symbol: str = "NVDA", *, strategy_name: str = "ma_crossover") -> TradeProposalCreate:
    return TradeProposalCreate(
        symbol=symbol,
        amount_usd=1000,
        leverage=1,
        proposed_price=120.0,
        stop_loss=114.0,
        take_profit=132.0,
        strategy_name=strategy_name,
        rationale="test setup",
    )


def queued_app(
    tmp_path,
    *,
    alpaca: FakeAlpacaClient | None = None,
    strategy_name: str = "ma_crossover",
    **settings_overrides,
):
    alpaca = alpaca or FakeAlpacaClient()
    settings_values = {
        "alpaca_enabled": True,
        "broker_for_equities": "alpaca",
        "broker_for_non_equities": "etoro",
        "paper_broker": "alpaca",
    }
    settings_values.update(settings_overrides)
    app = create_app(
        make_settings(tmp_path, **settings_values),
        broker=MockBroker(),
        alpaca_client=alpaca,
        market_data_client=FakeEtoroMarketData(price=alpaca.price),
        enable_background_jobs=False,
    )
    proposal = app.state.proposal_service.create_proposal(proposal_payload(strategy_name=strategy_name))
    approved = app.state.proposal_service.approve_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="approved"),
    )
    queued = app.state.execution_coordinator.enqueue_approved_proposal(approved.id)
    return app, alpaca, approved, queued


def execution_for(app, queue_record):
    return ExecutionRepository(app.state.db).get(str(queue_record.payload["execution_id"]))


def log_events(app) -> list[str]:
    with app.state.db.connect() as connection:
        rows = connection.execute("SELECT event_type FROM run_logs ORDER BY created_at").fetchall()
    return [row["event_type"] for row in rows]


def test_paper_mode_alpaca_routes_through_alpaca_submit_order(tmp_path) -> None:
    app, alpaca, proposal, queued = queued_app(tmp_path)

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.EXECUTED
    assert len(alpaca.submitted_orders) == 1
    order = alpaca.submitted_orders[0]
    assert order["symbol"] == proposal.order.symbol
    assert order["side"] == "buy"
    assert order["order_type"] == "market"
    assert order["time_in_force"] == "day"
    assert order["client_order_id"] == queued.client_order_id
    assert order["qty"] == 1000 / 120.0
    execution = execution_for(app, result)
    assert execution is not None
    assert execution.proposal_id == proposal.id
    assert execution.response_payload["broker"] == "alpaca"


def test_paper_mode_self_simulated_uses_existing_paper_service(tmp_path) -> None:
    app, alpaca, _, queued = queued_app(
        tmp_path,
        alpaca=FakeAlpacaClient(source="etoro"),
        broker_for_equities="etoro",
        paper_broker="self_simulated",
    )

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.EXECUTED
    assert alpaca.submitted_orders == []
    execution = execution_for(app, result)
    assert execution is not None
    assert execution.response_payload["broker"] == "self_simulated"


def test_live_mode_routes_through_alpaca_for_equity_proposal(tmp_path) -> None:
    app, alpaca, _, queued = queued_app(
        tmp_path,
        execution_mode="live",
        enable_real_trading=True,
        paper_trading_enabled=False,
    )

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.EXECUTED
    assert len(alpaca.submitted_orders) == 1


def test_all_sprint1_gates_still_fire_with_alpaca_routing(tmp_path) -> None:
    app, alpaca, _, queued = queued_app(tmp_path / "kill")
    app.state.settings.kill_switch_enabled = True
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.validation_reason.startswith("risk_failed:")
    assert "Kill switch is enabled" in result.validation_reason
    assert alpaca.submitted_orders == []

    app, alpaca, proposal, queued = queued_app(tmp_path / "status")
    app.state.proposal_service.reject_proposal(
        proposal.id,
        ApprovalDecisionRequest(reviewer="qa", notes="reject after approval"),
    )
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.validation_reason == "proposal_status_rejected"
    assert alpaca.submitted_orders == []

    app, alpaca, _, queued = queued_app(tmp_path / "drift", alpaca=FakeAlpacaClient(price=130.0))
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert "entry_drift_too_large" in result.validation_reason
    assert alpaca.submitted_orders == []

    app, alpaca, _, queued = queued_app(tmp_path / "freshness", alpaca=FakeAlpacaClient(age=9999.0))
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert "quote_too_old" in result.validation_reason
    assert alpaca.submitted_orders == []

    app, alpaca, _, queued = queued_app(tmp_path / "idempotency")
    first = app.state.execution_coordinator.process_queue_item(queued.id)
    second = app.state.execution_coordinator.process_queue_item(queued.id)
    assert first.payload["execution_id"] == second.payload["execution_id"]
    assert len(alpaca.submitted_orders) == 1


def test_manual_smoke_bypasses_entry_drift_only_in_paper(tmp_path) -> None:
    app, alpaca, _, queued = queued_app(
        tmp_path / "manual_smoke",
        alpaca=FakeAlpacaClient(price=130.0),
        strategy_name="manual_smoke",
    )

    result = app.state.execution_coordinator.process_queue_item(queued.id)

    assert result.status == ExecutionQueueStatus.EXECUTED
    assert result.validation_reason == "ready"
    assert len(alpaca.submitted_orders) == 1
    assert "execution_queue_entry_drift_bypassed" in log_events(app)


def test_kill_switch_calls_alpaca_cancel_all_and_close_all_in_paper(tmp_path) -> None:
    app, alpaca, _, _ = queued_app(tmp_path)

    status = app.state.automation_service.enable_kill_switch(reason="test")

    assert status.kill_switch_enabled is True
    assert alpaca.cancel_all_calls == 1
    assert alpaca.close_all_calls == 1
    assert "kill_switch_emergency_stop" in log_events(app)


def test_kill_switch_does_not_auto_close_in_live_without_confirmation_flag(tmp_path) -> None:
    app, alpaca, _, _ = queued_app(
        tmp_path,
        execution_mode="live",
        enable_real_trading=True,
        paper_trading_enabled=False,
        kill_switch_auto_close_positions=False,
    )

    app.state.automation_service.enable_kill_switch(reason="test")

    assert alpaca.cancel_all_calls == 1
    assert alpaca.close_all_calls == 0
    assert "kill_switch_emergency_stop" in log_events(app)
