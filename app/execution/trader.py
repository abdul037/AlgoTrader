"""Execution workflow orchestration."""

from __future__ import annotations

from app.approvals.service import ProposalService
from app.broker.etoro_client import BrokerClient
from app.runtime_settings import AppSettings
from app.models.approval import ApprovalStatus, TradeProposalCreate
from app.models.execution import BrokerOrderResponse, ExecutionRecord, ExecutionStatus
from app.models.signal import Signal, SignalAction
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskManager
from app.risk.position_sizing import calculate_position_size
from app.storage.repositories import ExecutionRepository, RunLogRepository
from app.utils.time import utc_now


class TraderService:
    """Drive proposal creation and order execution."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        proposal_service: ProposalService,
        execution_repository: ExecutionRepository,
        run_log_repository: RunLogRepository,
        broker: BrokerClient,
        risk_manager: RiskManager,
    ):
        self.settings = settings
        self.proposals = proposal_service
        self.executions = execution_repository
        self.logs = run_log_repository
        self.broker = broker
        self.risk_manager = risk_manager

    def propose_from_signal(
        self,
        signal: Signal,
        *,
        amount_usd: float | None = None,
        leverage: int = 1,
        notes: str = "",
    ):
        """Create a pending proposal from a buy signal."""

        if signal.action != SignalAction.BUY:
            raise ValueError("Only buy signals can be turned into entry proposals")

        balance = self.broker.get_balance()
        if amount_usd is None:
            if signal.price and signal.stop_loss:
                sizing = calculate_position_size(
                    account_balance=max(balance.equity, balance.cash_balance, 1.0),
                    risk_pct=self.settings.max_risk_per_trade_pct,
                    entry_price=signal.price,
                    stop_price=signal.stop_loss,
                    leverage=leverage,
                )
                amount_usd = min(sizing.amount_usd, self.settings.default_trade_amount_usd)
            else:
                amount_usd = self.settings.default_trade_amount_usd

        return self.proposals.create_proposal(
            TradeProposalCreate(
                symbol=signal.symbol,
                amount_usd=amount_usd,
                leverage=leverage,
                proposed_price=float(signal.price or 0),
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                strategy_name=signal.strategy_name,
                rationale=signal.rationale,
                notes=notes,
                signal=signal,
            )
        )

    def execute_proposal(self, proposal_id: str, *, client_order_id: str | None = None) -> ExecutionRecord:
        """Execute an approved proposal after re-checking risk rules."""

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal.status != ApprovalStatus.APPROVED:
            blocked = ExecutionRecord(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                mode=self.settings.etoro_account_mode,
                error_message="Proposal must be approved before execution",
            )
            self.executions.create(blocked)
            raise PermissionError("Proposal must be approved before execution")

        risk_context = build_risk_context(self.settings, self.broker, self.executions)
        risk = self.risk_manager.validate_order(proposal.order, risk_context)
        if not risk.passed:
            blocked = ExecutionRecord(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                mode=self.settings.etoro_account_mode,
                error_message="; ".join(risk.reasons),
                request_payload={**proposal.order.model_dump(), "client_order_id": client_order_id},
            )
            self.executions.create(blocked)
            raise ValueError("; ".join(risk.reasons))

        execution = ExecutionRecord(
            proposal_id=proposal_id,
            status=ExecutionStatus.VALIDATED,
            mode=self.settings.etoro_account_mode,
            request_payload={**proposal.order.model_dump(), "client_order_id": client_order_id},
        )
        self.executions.create(execution)

        if proposal.order.side.value == "sell":
            broker_response = self.broker.close_position(proposal.order.symbol)
        else:
            broker_response = self.broker.open_market_order_by_amount(
                proposal.order,
                client_order_id=client_order_id,
            )
        execution.status = self._map_broker_status(broker_response)
        execution.broker_order_id = broker_response.order_id
        execution.response_payload = broker_response.model_dump()
        execution.updated_at = utc_now().isoformat()
        self.executions.update(execution)

        self.proposals.mark_executed(proposal_id, execution.id)
        self.logs.log(
            "order_submitted",
            {
                "proposal_id": proposal_id,
                "execution_id": execution.id,
                "broker_order_id": broker_response.order_id,
                "status": execution.status,
            },
        )
        return execution

    @staticmethod
    def _map_broker_status(response: BrokerOrderResponse) -> str:
        if response.status.startswith("simulated"):
            return ExecutionStatus.SUBMITTED
        if response.status in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED}:
            return response.status
        return ExecutionStatus.SUBMITTED
