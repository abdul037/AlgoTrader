"""Approval-gated execution queue coordinator."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from app.broker.router import BrokerRouter, NoBrokerForAssetClass
from app.models.approval import ApprovalStatus
from app.models.execution import ExecutionRecord, ExecutionStatus
from app.models.execution_queue import ExecutionQueueRecord, ExecutionQueueStatus
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskManager
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
        broker_router: BrokerRouter | None = None,
        risk_manager: RiskManager | None = None,
        risk_context_factory: Callable[[Any, Any, Any], Any] | None = None,
        parallel_broker_service: Any | None = None,
        learning_service: Any | None = None,
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
        self.broker_router = broker_router
        self.risk_manager = risk_manager or RiskManager(settings)
        self.risk_context_factory = risk_context_factory or build_risk_context
        self.parallel_broker = parallel_broker_service
        self.learning = learning_service

    def enqueue_approved_proposal(self, proposal_id: str) -> ExecutionQueueRecord:
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal.status.value != "approved":
            raise PermissionError("Proposal must be approved before it can be queued")
        # Best-effort fast path; the partial unique index is the source of truth.
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
        record.client_order_id = self._client_order_id(proposal.id, record.id)
        try:
            self.queue.create(record)
        except (sqlite3.IntegrityError, SQLAlchemyIntegrityError) as exc:
            raise ValueError(f"Duplicate execution queue item already exists for {proposal.order.symbol}") from exc
        self.logs.log("execution_queue_enqueued", {"queue_id": record.id, "proposal_id": proposal.id, "symbol": record.symbol})
        return record

    def process_queue_item(self, queue_id: str) -> ExecutionQueueRecord:
        record = self.queue.get(queue_id)
        if record is None:
            raise LookupError(f"Execution queue item {queue_id} was not found")
        if record.payload.get("execution_id"):
            existing_execution = self.executions.get(str(record.payload["execution_id"]))
            if existing_execution is not None:
                return record
        existing_execution = self.executions.get_latest_by_proposal_id(record.proposal_id)
        if existing_execution is not None and existing_execution.status not in {
            ExecutionStatus.FAILED,
            ExecutionStatus.BLOCKED,
        }:
            record.status = ExecutionQueueStatus.EXECUTED
            record.executed_at = record.executed_at or existing_execution.updated_at
            record.updated_at = utc_now().isoformat()
            record.payload["execution_id"] = existing_execution.id
            self.queue.update(record)
            return record
        if record.status in {
            ExecutionQueueStatus.EXECUTED,
            ExecutionQueueStatus.CANCELLED,
            ExecutionQueueStatus.FAILED,
        }:
            return record

        stale_before = (
            utc_now()
            - timedelta(minutes=max(int(getattr(self.settings, "workflow_lock_timeout_minutes", 45)), 1))
        ).isoformat()
        if not self.queue.claim_for_processing(record.id, stale_before=stale_before):
            return self.queue.get(record.id) or record
        record = self.queue.get(record.id) or record

        proposal = self.proposals.get_proposal(record.proposal_id)
        if proposal.status != ApprovalStatus.APPROVED:
            record.status = ExecutionQueueStatus.BLOCKED
            record.validation_reason = "proposal_status_" + proposal.status.value
            record.updated_at = utc_now().isoformat()
            self.queue.update(record)
            self.logs.log(
                "execution_queue_proposal_not_approved",
                {
                    "queue_id": record.id,
                    "proposal_id": proposal.id,
                    "status": proposal.status.value,
                },
            )
            return record

        try:
            broker, broker_name = self._select_broker(proposal)
        except NoBrokerForAssetClass as exc:
            record.status = ExecutionQueueStatus.BLOCKED
            record.ready_for_execution = False
            record.validation_reason = str(exc)
            record.updated_at = utc_now().isoformat()
            self.queue.update(record)
            self.logs.log(
                "execution_queue_no_broker",
                {
                    "queue_id": record.id,
                    "proposal_id": proposal.id,
                    "symbol": proposal.order.symbol,
                    "reason": str(exc),
                },
            )
            return record

        automation_blockers = self._automation_blockers()

        timeframe = record.timeframe or (proposal.signal.metadata.get("timeframe") if proposal.signal is not None else "1d") or "1d"
        quote_provider = broker_name if broker_name in {"alpaca", "etoro"} else None
        quote = self.market_data.get_quote(proposal.order.symbol, timeframe=timeframe, provider=quote_provider, force_refresh=True)
        quote_price = float(quote.last_execution or quote.ask or quote.bid or proposal.order.proposed_price)
        quote_drift_bps = abs((quote_price - float(proposal.order.proposed_price)) / max(float(proposal.order.proposed_price), 0.01)) * 10_000.0
        validation_reasons = [
            *automation_blockers,
            *self._account_validation_reasons(
                broker,
                broker_name=broker_name,
                symbol=proposal.order.symbol,
            ),
            *self._quote_validation_reasons(quote, expected_broker=broker_name),
        ]
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        if self.executions.count_since(start_of_day) >= int(getattr(self.settings, "max_trades_per_day", 999999)):
            validation_reasons.append("max_trades_per_day_reached")
        if quote_drift_bps > float(self.settings.execution_max_entry_drift_bps):
            if self._can_bypass_entry_drift_for_smoke(proposal):
                self.logs.log(
                    "execution_queue_entry_drift_bypassed",
                    {
                        "queue_id": record.id,
                        "proposal_id": proposal.id,
                        "symbol": proposal.order.symbol,
                        "strategy_name": proposal.order.strategy_name,
                        "quote_drift_bps": quote_drift_bps,
                    },
                )
            else:
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

        risk_context = self.risk_context_factory(self.settings, broker, self.executions)
        risk = self.risk_manager.validate_order(proposal.order, risk_context)
        if not risk.passed:
            record.status = ExecutionQueueStatus.BLOCKED
            record.ready_for_execution = False
            record.validation_reason = "risk_failed:" + ",".join(risk.reasons)
            record.updated_at = utc_now().isoformat()
            self.queue.update(record)
            self.logs.log(
                "execution_queue_risk_blocked",
                {
                    "queue_id": record.id,
                    "proposal_id": proposal.id,
                    "symbol": proposal.order.symbol,
                    "reasons": risk.reasons,
                },
            )
            return record

        if broker_name == "alpaca" and bool(getattr(self.settings, "alpaca_require_bracket_orders", True)):
            bracket_reasons = self._alpaca_bracket_reasons(proposal, quote_price)
            if bracket_reasons:
                record.status = ExecutionQueueStatus.BLOCKED
                record.ready_for_execution = False
                record.validation_reason = "bracket_failed:" + ",".join(bracket_reasons)
                record.updated_at = utc_now().isoformat()
                self.queue.update(record)
                self.logs.log(
                    "execution_queue_bracket_blocked",
                    {
                        "queue_id": record.id,
                        "proposal_id": proposal.id,
                        "symbol": proposal.order.symbol,
                        "reasons": bracket_reasons,
                    },
                )
                return record

        if self.settings.execution_mode == "paper" and self.settings.paper_broker == "self_simulated":
            paper_position = self.paper.open_from_approved_proposal(proposal, live_quote=quote)
            execution = ExecutionRecord(
                proposal_id=proposal.id,
                status=ExecutionStatus.SUBMITTED,
                mode="paper",
                request_payload={**proposal.order.model_dump(), "client_order_id": record.client_order_id},
                response_payload={
                    "broker": "self_simulated",
                    "paper_position_id": paper_position.id,
                    "fill_price": paper_position.entry_price,
                },
            )
            self._create_execution(execution)
            self.proposals.mark_executed(proposal.id, execution.id)
        elif self.settings.execution_mode in {"paper", "live"}:
            if record.payload.get("execution_id"):
                existing_execution = self.executions.get(str(record.payload["execution_id"]))
                if existing_execution is not None:
                    return record
            try:
                execution = self._execute_with_broker(
                    proposal=proposal,
                    broker=broker,
                    broker_name=broker_name,
                    quote_price=quote_price,
                    client_order_id=record.client_order_id,
                )
            except Exception as exc:
                if self.settings.execution_mode == "paper" and bool(getattr(self.settings, "paper_simulated_fallback_enabled", False)):
                    paper_position = self.paper.open_from_approved_proposal(proposal, live_quote=quote)
                    execution = ExecutionRecord(
                        proposal_id=proposal.id,
                        status=ExecutionStatus.SUBMITTED,
                        mode="paper",
                        request_payload={**proposal.order.model_dump(), "client_order_id": record.client_order_id},
                        response_payload={
                            "broker": "self_simulated_fallback",
                            "paper_position_id": paper_position.id,
                            "fill_price": paper_position.entry_price,
                            "broker_error": str(exc),
                        },
                    )
                    self._create_execution(execution)
                    self.proposals.mark_executed(proposal.id, execution.id)
                else:
                    execution = ExecutionRecord(
                        proposal_id=proposal.id,
                        status=ExecutionStatus.FAILED,
                        mode=self.settings.execution_mode,
                        request_payload={**proposal.order.model_dump(), "client_order_id": record.client_order_id},
                        response_payload={"broker": broker_name},
                        error_message=str(exc),
                    )
                    self._create_execution(execution)
                    record.status = ExecutionQueueStatus.BLOCKED
                    record.ready_for_execution = False
                    record.validation_reason = "broker_submission_failed"
                    record.updated_at = utc_now().isoformat()
                    record.payload["execution_id"] = execution.id
                    self.queue.update(record)
                    self.logs.log(
                        "execution_queue_broker_failed",
                        {
                            "queue_id": record.id,
                            "proposal_id": proposal.id,
                            "symbol": proposal.order.symbol,
                            "broker": broker_name,
                            "error": str(exc),
                        },
                    )
                    return record
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

    @staticmethod
    def _client_order_id(proposal_id: str, queue_id: str) -> str:
        return hashlib.sha256(f"{proposal_id}:{queue_id}:v1".encode()).hexdigest()[:32]

    def _automation_blockers(self) -> list[str]:
        if self.automation is not None:
            blockers = list(self.automation.execution_blockers())
            if bool(getattr(self.settings, "kill_switch_enabled", False)):
                blockers = [item for item in blockers if item != "automation_kill_switch_enabled"]
            return blockers
        reasons: list[str] = []
        if getattr(self.settings, "execution_mode", "paper") == "live":
            if not bool(getattr(self.settings, "require_approval", True)):
                reasons.append("approval_required_must_remain_enabled")
            if not bool(getattr(self.settings, "enable_real_trading", False)):
                reasons.append("enable_real_trading_false")
            if bool(getattr(self.settings, "paper_trading_enabled", True)):
                reasons.append("paper_trading_enabled_in_live_mode")
        return reasons

    def _quote_validation_reasons(self, quote: Any, *, expected_broker: str = "etoro") -> list[str]:
        reasons: list[str] = []
        quote_source = str(getattr(quote, "source", "")).lower()
        if expected_broker == "alpaca":
            if not quote_source.startswith("alpaca"):
                reasons.append("quote_provider_not_alpaca")
        elif expected_broker == "etoro" and not quote_source.startswith("etoro"):
            reasons.append("quote_provider_not_etoro")
        if bool(getattr(quote, "quote_derived_from_history", False)):
            reasons.append("quote_not_direct")
        age = getattr(quote, "data_age_seconds", None)
        if age is not None and float(age) > float(self.settings.max_market_data_age_seconds):
            reasons.append("quote_too_old")
        if getattr(quote, "last_execution", None) is None and getattr(quote, "ask", None) is None:
            reasons.append("quote_missing")
        return reasons

    def _account_validation_reasons(
        self,
        broker: Any,
        *,
        broker_name: str,
        symbol: str,
    ) -> list[str]:
        if broker_name != "alpaca":
            return []
        expected = str(
            getattr(
                self.settings,
                "alpaca_effective_expected_account_number",
                getattr(self.settings, "alpaca_expected_account_number", ""),
            )
            or ""
        ).strip()
        reasons: list[str] = []
        if expected and hasattr(broker, "get_account_identity"):
            identity = broker.get_account_identity()
            actual = str(identity.get("account_number") or "")
            if actual != expected:
                reasons.append("alpaca_account_mismatch")
                if self.automation is not None:
                    self.automation.set_account_verified(False)
                    self.automation.trip_circuit_breaker(
                        reason=f"account_mismatch:expected={expected}:actual={actual}",
                        emergency_stop=False,
                    )
            if bool(identity.get("trading_blocked")):
                reasons.append("alpaca_trading_blocked")
        if hasattr(broker, "is_supported_equity"):
            try:
                if not broker.is_supported_equity(symbol):
                    reasons.append("symbol_not_supported_by_alpaca")
            except Exception:
                reasons.append("alpaca_asset_check_failed")
        return reasons

    def _can_bypass_entry_drift_for_smoke(self, proposal: Any) -> bool:
        """Allow the explicit manual smoke command to reach broker routing in paper mode."""

        return (
            str(getattr(self.settings, "execution_mode", "paper")).lower() == "paper"
            and not bool(getattr(self.settings, "enable_real_trading", False))
            and str(getattr(proposal.order, "strategy_name", "") or "").lower() == "manual_smoke"
        )

    def _select_broker(self, proposal: Any) -> tuple[Any, str]:
        if self.broker_router is None:
            return self.trader.broker, "etoro"
        broker = self.broker_router.select_broker_for(proposal)
        broker_name = self.broker_router.selected_broker_name_for(proposal)
        return broker, broker_name

    def _execute_with_broker(
        self,
        *,
        proposal: Any,
        broker: Any,
        broker_name: str,
        quote_price: float,
        client_order_id: str | None,
    ) -> ExecutionRecord:
        if broker_name == "etoro" and broker is self.trader.broker:
            return self.trader.execute_proposal(proposal.id, client_order_id=client_order_id)

        request_payload = {
            **proposal.order.model_dump(),
            "client_order_id": client_order_id,
            "broker": broker_name,
        }
        if self.learning is not None:
            self.learning.record_proposal_event(
                proposal,
                event_type="pre_submit_validation",
                payload={
                    "broker": broker_name,
                    "quote_price": quote_price,
                    "request": request_payload,
                    "risk_rechecked": True,
                },
            )
        if hasattr(broker, "submit_order"):
            capped_amount = min(
                float(proposal.order.amount_usd),
                float(getattr(self.settings, "max_trade_amount_usd", 1000.0)),
            )
            if broker_name == "alpaca":
                qty = math.floor(capped_amount / max(float(quote_price), 0.01))
                if qty < 1:
                    raise ValueError("one_share_exceeds_max_trade_amount")
            else:
                qty = capped_amount / max(float(quote_price), 0.01)
            if (
                broker_name == "alpaca"
                and bool(getattr(self.settings, "alpaca_require_bracket_orders", True))
                and hasattr(broker, "submit_bracket_order")
            ):
                broker_execution = broker.submit_bracket_order(
                    symbol=proposal.order.symbol,
                    side=proposal.order.side.value,
                    qty=int(qty),
                    take_profit_price=float(proposal.order.take_profit),
                    stop_loss_price=float(proposal.order.stop_loss),
                    time_in_force="day",
                    client_order_id=client_order_id,
                )
            else:
                broker_execution = broker.submit_order(
                    symbol=proposal.order.symbol,
                    side=proposal.order.side.value,
                    qty=qty,
                    order_type="market",
                    time_in_force="day",
                    client_order_id=client_order_id,
                )
            broker_order_id = getattr(broker_execution, "broker_order_id", None)
            if broker_order_id:
                existing_execution = self.executions.get_by_broker_order_id(str(broker_order_id))
                if existing_execution is not None:
                    self.proposals.mark_executed(proposal.id, existing_execution.id)
                    return existing_execution
            execution = ExecutionRecord(
                proposal_id=proposal.id,
                status=self._map_broker_execution_status(getattr(broker_execution, "status", "")),
                mode=str(getattr(broker_execution, "mode", self.settings.execution_mode)),
                broker_order_id=broker_order_id,
                request_payload=request_payload,
                response_payload={
                    "broker": broker_name,
                    "broker_execution": broker_execution.model_dump()
                    if hasattr(broker_execution, "model_dump")
                    else dict(getattr(broker_execution, "response_payload", {}) or {}),
                },
            )
            self._create_execution(execution)
            self.proposals.mark_executed(proposal.id, execution.id)
            self.logs.log(
                "order_submitted",
                {
                    "proposal_id": proposal.id,
                    "execution_id": execution.id,
                    "broker_order_id": execution.broker_order_id,
                    "broker": broker_name,
                    "status": execution.status,
                },
            )
            self._mirror_parallel(proposal, execution, broker_name)
            return execution

        if hasattr(broker, "open_market_order_by_amount"):
            response = broker.open_market_order_by_amount(proposal.order, client_order_id=client_order_id)
            execution = ExecutionRecord(
                proposal_id=proposal.id,
                status=ExecutionStatus.SUBMITTED
                if str(response.status) not in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED}
                else response.status,
                mode=str(getattr(response, "mode", self.settings.execution_mode)),
                broker_order_id=response.order_id,
                request_payload=request_payload,
                response_payload={"broker": broker_name, "broker_response": response.model_dump()},
            )
            self._create_execution(execution)
            self.proposals.mark_executed(proposal.id, execution.id)
            self.logs.log(
                "order_submitted",
                {
                    "proposal_id": proposal.id,
                    "execution_id": execution.id,
                    "broker_order_id": execution.broker_order_id,
                    "broker": broker_name,
                    "status": execution.status,
                },
            )
            self._mirror_parallel(proposal, execution, broker_name)
            return execution

        raise TypeError(f"Selected broker {broker_name!r} does not expose a supported order submission method")

    def _mirror_parallel(self, proposal: Any, execution: ExecutionRecord, broker_name: str) -> None:
        if self.parallel_broker is None:
            return
        self.parallel_broker.mirror(
            proposal=proposal,
            primary_execution=execution,
            primary_broker=broker_name,
        )

    def _create_execution(self, execution: ExecutionRecord) -> ExecutionRecord:
        persisted = self.executions.create(execution)
        if self.learning is not None:
            self.learning.record_execution_event(persisted, event_type="submitted")
        return persisted

    @staticmethod
    def _alpaca_bracket_reasons(proposal: Any, quote_price: float) -> list[str]:
        order = proposal.order
        reasons: list[str] = []
        if str(getattr(order.side, "value", order.side)).lower() != "buy":
            reasons.append("long_entries_only")
        if order.stop_loss is None:
            reasons.append("stop_loss_missing")
        if order.take_profit is None:
            reasons.append("take_profit_missing")
        if order.stop_loss is not None and float(order.stop_loss) >= float(quote_price):
            reasons.append("stop_loss_not_below_entry")
        if order.take_profit is not None and float(order.take_profit) <= float(quote_price):
            reasons.append("take_profit_not_above_entry")
        return reasons

    @staticmethod
    def _map_broker_execution_status(status: str) -> str:
        normalized = str(status or "").lower()
        if normalized in {ExecutionStatus.FAILED, "rejected", "canceled", "cancelled"}:
            return ExecutionStatus.FAILED
        if normalized == ExecutionStatus.BLOCKED:
            return ExecutionStatus.BLOCKED
        return ExecutionStatus.SUBMITTED
