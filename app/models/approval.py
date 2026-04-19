"""Approval models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.models.signal import Signal
from app.models.trade import AssetClass, OrderSide, TradeOrder
from app.utils.ids import generate_id
from app.utils.time import add_minutes, utc_now


class ApprovalStatus(str, Enum):
    """Proposal approval states."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


class TradeProposalCreate(BaseModel):
    """Request body for a new trade proposal."""

    symbol: str
    side: OrderSide = OrderSide.BUY
    amount_usd: float = Field(gt=0)
    leverage: int = Field(default=1, ge=1)
    proposed_price: float = Field(gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    strategy_name: str | None = None
    rationale: str = ""
    notes: str = ""
    asset_class: AssetClass = AssetClass.UNKNOWN
    signal: Signal | None = None

    def to_order(self) -> TradeOrder:
        """Convert the proposal request to a trade order."""

        return TradeOrder(
            symbol=self.symbol.upper(),
            side=self.side,
            amount_usd=self.amount_usd,
            leverage=self.leverage,
            proposed_price=self.proposed_price,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            strategy_name=self.strategy_name,
            rationale=self.rationale,
            asset_class=self.asset_class,
        )


class ApprovalDecisionRequest(BaseModel):
    """Human approval or rejection metadata."""

    reviewer: str = "human"
    notes: str = ""


class TradeProposal(BaseModel):
    """A persisted trade proposal waiting for human approval."""

    id: str = Field(default_factory=lambda: generate_id("prop"))
    status: ApprovalStatus = ApprovalStatus.PENDING
    order: TradeOrder
    signal: Signal | None = None
    notes: str = ""
    decision_notes: str = ""
    approved_by: str | None = None
    execution_id: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
    expires_at: str = Field(
        default_factory=lambda: add_minutes(utc_now(), 240).isoformat()
    )
    executed_at: str | None = None
