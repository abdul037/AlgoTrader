"""Approval-gated execution queue coordinator."""

from __future__ import annotations

from typing import Any

from app.models.execution import ExecutionRecord, ExecutionStatus
from app.models.execution_queue import ExecutionQueueRecord, ExecutionQueueStatus
from app.utils.time import utc_now


class ExecutionCoordinator:
    """Queue approved proposals, revalidate quotes, then route to paper or broker execution."""

    def __init__(
        self,
        *,
        settings: Any,
        proposal_service: Any,
        queue_repository: Any,
        execution_repository: Any,
        trader_service: Any,
        paper_trading_service: Any,
        market_data_engine: Any,
        run_logs: Any,
        automation_service: Any | None = None,
    ):
        self.settings = settings
        self.proposals = proposal_service
        self.queue = queue_repository
        self.executions = execution_repository
        self.trader = trader_service
        self.paper = paper_trading_service
        self.market_data = market_data_engine
        self.logs = run_logs
        self.automation = automation_service

    def enqueue_approved_proposal(self, proposal_id: str) -> ExecutionQueueRecord:
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal.status.value != "approved":
            raise PermissionError("Proposal must be approved before it can be queued")
        existing = self.queue.latest_open_for_symbol(proposal.order.symbol)
        if existing is not None:
            raise ValueError(f"Duplicate execution queue item already exists for {proposal.order.symbol}")
        record = ExecutionQueueRecord(
            proposal_id=proposal.id,
            signal_id=getattr(proposal.signal, "id", None),
            symbol=proposal.order.symbol.upper(),
            strategy_name=proposal.order.strategy_name,
            timeframe=(proposal.signal.metadata.get("timeframe") if proposal.signal is not None else None),
            mode=self.settings.execution_mode,
            requested_entry_price=proposal.order.proposed_price,
            payload={"order": proposal.order.model_dump(), "signal": proposal.signal.model_dump() if proposal.signal else None},
        )
        self.queue.create(record)
        self.logs.log("execution_queue_enqueued", {"queue_id": record.id, "proposal_id": proposal.id, "symbol": record.symbol})
        return record

    def process_queue_item(self, queue_id: str) -> ExecutionQueueRecord:
        record = self.queue.get(queue_id)
        if record is None:
            raise LookupError(f"Execution queue item {queue_id} was not found")
        proposal = self.proposals.get_proposal(record.proposal_id)
        record.status = ExecutionQueueStatus.PROCESSING
        record.updated_at = utc_now().isoformat()
        self.queue.update(record)
        automation_blockers = self._automation_blockers()

        timeframe = record.timeframe or (proposal.signal.metadata.get("timeframe") if proposal.signal is not None else "1d") or "1d"
        quote = self.market_data.get_quote(proposal.order.symbol, timeframe=timeframe, force_refresh=True)
        quote_price = float(quote.last_execution or quote.ask or quote.bid or proposal.order.proposed_price)
        quote_drift_bps = abs((quote_price - float(proposal.order.proposed_price)) / max(float(proposal.order.proposed_price), 0.01)) * 10_000.0
        validation_reasons = [*automation_blockers, *self._quote_validation_reasons(quote)]
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        if self.executions.count_since(start_of_day) >= int(getattr(self.settings, "max_trades_per_day", 999999)):
            validation_reasons.append("max_trades_per_day_reached")
        if quote_drift_bps > float(self.settings.execution_max_entry_drift_bps):
            validation_reasons.append("entry_drift_too_large")

        record.latest_quote_price = quote_price
        record.latest_quote_timestamp = quote.timestamp
        record.ready_for_execution = not validation_reasons
        record.validation_reason = ",".join(validation_reasons) if validation_reasons else "ready"

        if validation_reasons:
            record.status = ExecutionQueueStatus.BLOCKED
            record.updated_at = utc_now().isoformat()
            self.queue.update(record)
            self.logs.log(
                "execution_queue_blocked",
                {"queue_id": record.id, "proposal_id": proposal.id, "symbol": proposal.order.symbol, "reason": record.validation_reason},
            )
            return record

        if self.settings.execution_mode == "paper":
            paper_position = self.paper.open_from_approved_proposal(proposal, live_quote=quote)
            execution = ExecutionRecord(
                proposal_id=proposal.id,
                status=ExecutionStatus.SUBMITTED,
                mode="paper",
                request_payload=proposal.order.model_dump(),
                response_payload={"paper_position_id": paper_position.id, "fill_price": paper_position.entry_price},
            )
            self.executions.create(execution)
            self.proposals.mark_executed(proposal.id, execution.id)
        elif self.settings.execution_mode == "live":
            execution = self.trader.execute_proposal(proposal.id)
        else:
            raise ValueError(f"Unsupported execution mode: {self.settings.execution_mode}")

        record.status = ExecutionQueueStatus.EXECUTED
        record.executed_at = utc_now().isoformat()
        record.updated_at = record.executed_at
        record.payload["execution_id"] = execution.id
        self.queue.update(record)
        self.logs.log(
            "execution_queue_executed",
            {"queue_id": record.id, "proposal_id": proposal.id, "symbol": proposal.order.symbol, "execution_id": execution.id},
        )
        return record

    def process_ready_queue(self, *, limit: int = 20) -> list[ExecutionQueueRecord]:
        results: list[ExecutionQueueRecord] = []
        for record in self.queue.list(status=ExecutionQueueStatus.QUEUED, limit=limit):
            results.append(self.process_queue_item(record.id))
        return results

    def _automation_blockers(self) -> list[str]:
        if self.automation is not None:
            return list(self.automation.execution_blockers())
        reasons: list[str] = []
        if bool(getattr(self.settings, "kill_switch_enabled", False)):
            reasons.append("automation_kill_switch_enabled")
        if getattr(self.settings, "execution_mode", "paper") == "live":
            if not bool(getattr(self.settings, "require_approval", True)):
                reasons.append("approval_required_must_remain_enabled")
            if not bool(getattr(self.settings, "enable_real_trading", False)):
                reasons.append("enable_real_trading_false")
            if bool(getattr(self.settings, "paper_trading_enabled", True)):
                reasons.append("paper_trading_enabled_in_live_mode")
        return reasons

    def _quote_validation_reasons(self, quote: Any) -> list[str]:
        reasons: list[str] = []
        if not str(getattr(quote, "source", "")).lower().startswith("etoro"):
            reasons.append("quote_provider_not_etoro")
        if bool(getattr(quote, "quote_derived_from_history", False)):
            reasons.append("quote_not_direct")
        age = getattr(quote, "data_age_seconds", None)
        if age is not None and float(age) > float(self.settings.max_market_data_age_seconds):
            reasons.append("quote_too_old")
        if getattr(quote, "last_execution", None) is None and getattr(quote, "ask", None) is None:
            reasons.append("quote_missing")
        return reasons
