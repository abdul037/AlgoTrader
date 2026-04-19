"""Execution interfaces for future approval-based broker automation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.live_signal_schema import LiveSignalSnapshot
from app.models.approval import TradeProposal, TradeProposalCreate
from app.models.trade import AssetClass, OrderSide


class ExecutionGateway(ABC):
    """Abstract broker execution contract."""

    @abstractmethod
    def submit_approved_trade(self, proposal: TradeProposal) -> object:
        """Submit an approved trade to the broker."""


class SignalApprovalAdapter:
    """Translate screener snapshots into manual approval proposals."""

    def build_proposal_request(
        self,
        snapshot: LiveSignalSnapshot,
        *,
        amount_usd: float,
        notes: str = "",
    ) -> TradeProposalCreate:
        side = OrderSide.SELL if snapshot.signal_role == "entry_short" else OrderSide.BUY
        asset_class = AssetClass.GOLD if snapshot.asset_class == "commodity" else AssetClass.EQUITY
        return TradeProposalCreate(
            symbol=snapshot.symbol,
            side=side,
            amount_usd=amount_usd,
            leverage=1,
            proposed_price=float(snapshot.entry_price or snapshot.current_price or 0.0),
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit or (snapshot.targets[0] if snapshot.targets else None),
            strategy_name=snapshot.strategy_name,
            rationale=snapshot.rationale,
            notes=notes,
            asset_class=asset_class,
        )
