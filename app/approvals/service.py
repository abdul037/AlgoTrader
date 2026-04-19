"""Approval workflow service."""

from __future__ import annotations

from datetime import datetime

from app.broker.etoro_client import BrokerClient
from app.config import AppSettings
from app.models.approval import ApprovalDecisionRequest, ApprovalStatus, TradeProposal, TradeProposalCreate
from app.models.trade import TradeOrder
from app.risk.guardrails import RiskContext, RiskManager
from app.storage.repositories import ExecutionRepository, ProposalRepository, RunLogRepository, SignalRepository
from app.utils.time import add_minutes, utc_now


class ProposalService:
    """Create and manage trade proposals that require approval."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        proposal_repository: ProposalRepository,
        signal_repository: SignalRepository,
        execution_repository: ExecutionRepository,
        run_log_repository: RunLogRepository,
        broker: BrokerClient,
        risk_manager: RiskManager,
    ):
        self.settings = settings
        self.proposals = proposal_repository
        self.signals = signal_repository
        self.executions = execution_repository
        self.logs = run_log_repository
        self.broker = broker
        self.risk_manager = risk_manager

    def create_proposal(self, request: TradeProposalCreate) -> TradeProposal:
        """Validate a proposal request and persist a pending proposal."""

        order = self._prepare_order(request.to_order())
        risk_context = self._risk_context()
        risk = self.risk_manager.validate_order(order, risk_context)
        if not risk.passed:
            raise ValueError("; ".join(risk.reasons))

        signal = request.signal
        if signal is not None:
            self.signals.create(signal)

        proposal = TradeProposal(
            order=order,
            signal=signal,
            notes=request.notes,
            expires_at=add_minutes(utc_now(), self.settings.proposal_expiry_minutes).isoformat(),
        )
        self.proposals.create(proposal)
        self.logs.log(
            "proposal_created",
            {
                "proposal_id": proposal.id,
                "symbol": proposal.order.symbol,
                "risk_pct_of_balance": risk.risk_pct_of_balance,
                "risk_amount_usd": risk.risk_amount_usd,
            },
        )
        return proposal

    def list_proposals(self, status: ApprovalStatus | None = None) -> list[TradeProposal]:
        """List proposals, expiring stale ones on read."""

        proposals = self.proposals.list(status=status)
        return [self._expire_if_needed(proposal) for proposal in proposals]

    def get_proposal(self, proposal_id: str) -> TradeProposal:
        """Fetch a single proposal."""

        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise LookupError(f"Proposal {proposal_id} was not found")
        return self._expire_if_needed(proposal)

    def approve_proposal(self, proposal_id: str, decision: ApprovalDecisionRequest) -> TradeProposal:
        """Approve a pending proposal."""

        proposal = self.get_proposal(proposal_id)
        if proposal.status != ApprovalStatus.PENDING:
            raise ValueError("Only pending proposals can be approved")
        proposal.status = ApprovalStatus.APPROVED
        proposal.approved_by = decision.reviewer
        proposal.decision_notes = decision.notes
        proposal.updated_at = utc_now().isoformat()
        self.proposals.update(proposal)
        self.logs.log(
            "proposal_approved",
            {"proposal_id": proposal.id, "reviewer": decision.reviewer, "notes": decision.notes},
        )
        return proposal

    def reject_proposal(self, proposal_id: str, decision: ApprovalDecisionRequest) -> TradeProposal:
        """Reject a pending proposal."""

        proposal = self.get_proposal(proposal_id)
        if proposal.status != ApprovalStatus.PENDING:
            raise ValueError("Only pending proposals can be rejected")
        proposal.status = ApprovalStatus.REJECTED
        proposal.approved_by = decision.reviewer
        proposal.decision_notes = decision.notes
        proposal.updated_at = utc_now().isoformat()
        self.proposals.update(proposal)
        self.logs.log(
            "proposal_rejected",
            {"proposal_id": proposal.id, "reviewer": decision.reviewer, "notes": decision.notes},
        )
        return proposal

    def mark_executed(self, proposal_id: str, execution_id: str) -> TradeProposal:
        """Mark an approved proposal as executed."""

        proposal = self.get_proposal(proposal_id)
        proposal.status = ApprovalStatus.EXECUTED
        proposal.execution_id = execution_id
        proposal.executed_at = utc_now().isoformat()
        proposal.updated_at = proposal.executed_at
        self.proposals.update(proposal)
        self.logs.log("proposal_executed", {"proposal_id": proposal.id, "execution_id": execution_id})
        return proposal

    def _prepare_order(self, order: TradeOrder) -> TradeOrder:
        instrument = self.risk_manager.resolver.resolve(order.symbol)
        order.symbol = instrument.symbol
        order.broker_symbol = instrument.broker_symbol
        order.asset_class = instrument.asset_class
        if order.leverage < 1:
            order.leverage = 1
        return order

    def _risk_context(self) -> RiskContext:
        if self.settings.execution_mode == "paper":
            daily_pnl, consecutive_losses = self.executions.daily_loss_stats()
            weekly_pnl = self.executions.period_realized_pnl(days=7)
            return RiskContext(
                account_balance=max(float(self.settings.paper_account_balance_usd), 1.0),
                daily_realized_pnl_usd=daily_pnl,
                weekly_realized_pnl_usd=weekly_pnl,
                open_positions=0,
                positions_by_symbol={},
                consecutive_losses_today=consecutive_losses,
                mode="paper",
            )

        portfolio = self.broker.get_portfolio()
        daily_pnl, consecutive_losses = self.executions.daily_loss_stats()
        weekly_pnl = self.executions.period_realized_pnl(days=7)
        account_balance = max(portfolio.account.equity, portfolio.account.cash_balance, 1.0)
        positions_by_symbol: dict[str, int] = {}
        for position in portfolio.positions:
            symbol = str(position.symbol or "").upper()
            if not symbol:
                continue
            positions_by_symbol[symbol] = positions_by_symbol.get(symbol, 0) + 1
        return RiskContext(
            account_balance=account_balance,
            daily_realized_pnl_usd=daily_pnl,
            weekly_realized_pnl_usd=weekly_pnl,
            open_positions=len(portfolio.positions),
            positions_by_symbol=positions_by_symbol,
            consecutive_losses_today=consecutive_losses,
            mode=self.settings.etoro_account_mode,
        )

    def _expire_if_needed(self, proposal: TradeProposal) -> TradeProposal:
        if proposal.status != ApprovalStatus.PENDING:
            return proposal

        if datetime.fromisoformat(proposal.expires_at) <= utc_now():
            proposal.status = ApprovalStatus.EXPIRED
            proposal.updated_at = utc_now().isoformat()
            self.proposals.update(proposal)
            self.logs.log("proposal_expired", {"proposal_id": proposal.id})
        return proposal
