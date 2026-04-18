"""Paper trading simulation service."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.models.approval import TradeProposal
from app.models.paper import PaperPositionRecord, PaperTradeRecord
from app.utils.time import utc_now


class PaperTradingService:
    """Simulate approved trades before any live execution is enabled."""

    def __init__(
        self,
        *,
        settings: Any,
        positions: Any,
        trades: Any,
        run_logs: Any,
        scan_decisions: Any | None = None,
    ):
        self.settings = settings
        self.positions = positions
        self.trades = trades
        self.logs = run_logs
        self.scan_decisions = scan_decisions

    def open_from_approved_proposal(
        self,
        proposal: TradeProposal,
        *,
        live_quote: Any,
        signal_snapshot: Any | None = None,
    ) -> PaperPositionRecord:
        order = proposal.order
        side = order.side.value
        quote_price = float(live_quote.last_execution or live_quote.ask or live_quote.bid or order.proposed_price)
        fill_price = self._apply_slippage(quote_price, side=side)
        quantity = round(float(order.amount_usd) / max(fill_price, 0.01), 6)
        metadata = dict(signal_snapshot.metadata or {}) if signal_snapshot is not None else {}
        trade_plan = dict(metadata.get("trade_plan") or {})
        targets = list(signal_snapshot.targets or []) if signal_snapshot is not None else []
        record = PaperPositionRecord(
            proposal_id=proposal.id,
            signal_id=getattr(proposal.signal, "id", None),
            symbol=order.symbol.upper(),
            strategy_name=order.strategy_name or metadata.get("strategy_name") or "manual",
            timeframe=str(metadata.get("timeframe") or getattr(signal_snapshot, "timeframe", "1d")),
            side=side,
            regime_label=str(metadata.get("market_regime_label") or ""),
            hold_style=str(trade_plan.get("hold_style") or "swing"),
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            stop_loss=order.stop_loss,
            target_1=targets[0] if len(targets) > 0 else order.take_profit,
            target_2=targets[1] if len(targets) > 1 else None,
            target_3=targets[2] if len(targets) > 2 else None,
            payload={
                "risk_reward_ratio": metadata.get("risk_reward_ratio") or metadata.get("estimated_reward_to_risk"),
                "signal_snapshot": signal_snapshot.model_dump() if signal_snapshot is not None else None,
                "quote_price": quote_price,
                "quote_timestamp": getattr(live_quote, "timestamp", None),
            },
        )
        self.positions.create(record)
        self.logs.log(
            "paper_position_opened",
            {
                "proposal_id": proposal.id,
                "symbol": record.symbol,
                "entry_price": record.entry_price,
                "quantity": record.quantity,
                "timeframe": record.timeframe,
            },
        )
        return record

    def refresh_open_positions(self, *, market_data_engine: Any, force_refresh: bool = True) -> dict[str, int]:
        open_positions = self.positions.list(status="open", limit=500)
        closed = 0
        checked = 0
        for position in open_positions:
            checked += 1
            quote = market_data_engine.get_quote(position.symbol, timeframe=position.timeframe, force_refresh=force_refresh)
            current_price = float(quote.last_execution or quote.ask or quote.bid or position.current_price)
            position.current_price = current_price
            position.updated_at = utc_now().isoformat()
            position.unrealized_pnl_usd = round(self._pnl_usd(position, current_price), 2)
            outcome = self._close_outcome(position, current_price)
            if outcome is None:
                self.positions.update(position)
                continue
            closed += 1
            self._close_position(position, exit_price=current_price, outcome=outcome)
        return {"checked": checked, "closed": closed}

    def summary(self) -> Any:
        open_positions = self.positions.list(status="open", limit=500)
        rejection_counts = Counter()
        if self.scan_decisions is not None:
            for item in self.scan_decisions.list(limit=500):
                for reason in item.rejection_reasons[:3]:
                    rejection_counts[reason] += 1
        return self.trades.summary(
            open_positions=open_positions,
            rejection_reason_counts=dict(rejection_counts),
        )

    def _close_position(self, position: PaperPositionRecord, *, exit_price: float, outcome: str) -> PaperTradeRecord:
        position.status = "closed"
        position.closed_at = utc_now().isoformat()
        position.updated_at = position.closed_at
        position.current_price = exit_price
        realized = round(self._pnl_usd(position, exit_price), 2)
        position.realized_pnl_usd = realized
        position.unrealized_pnl_usd = 0.0
        self.positions.update(position)
        trade = PaperTradeRecord(
            position_id=position.id,
            proposal_id=position.proposal_id,
            signal_id=position.signal_id,
            symbol=position.symbol,
            strategy_name=position.strategy_name,
            timeframe=position.timeframe,
            side=position.side,
            regime_label=position.regime_label,
            hold_style=position.hold_style,
            outcome=outcome,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            realized_pnl_usd=realized,
            realized_pnl_pct=round((realized / max(position.entry_price * position.quantity, 0.01)) * 100.0, 2),
            opened_at=position.opened_at,
            payload=dict(position.payload),
        )
        self.trades.create(trade)
        self.logs.log(
            "paper_position_closed",
            {
                "position_id": position.id,
                "symbol": position.symbol,
                "outcome": outcome,
                "realized_pnl_usd": trade.realized_pnl_usd,
            },
        )
        return trade

    def _close_outcome(self, position: PaperPositionRecord, current_price: float) -> str | None:
        is_short = position.side == "sell"
        stop = position.stop_loss
        if stop is not None:
            if is_short and current_price >= stop:
                return "stop_loss"
            if not is_short and current_price <= stop:
                return "stop_loss"
        for index, target in enumerate([position.target_1, position.target_2, position.target_3], start=1):
            if target is None:
                continue
            if is_short and current_price <= target:
                return f"target_{index}"
            if not is_short and current_price >= target:
                return f"target_{index}"

        opened_at = datetime.fromisoformat(position.opened_at)
        if position.timeframe == "1m" and utc_now() >= opened_at + timedelta(minutes=self.settings.paper_max_hold_minutes_scalp):
            return "timed_exit"
        if position.timeframe in {"5m", "15m"} and utc_now() >= opened_at + timedelta(minutes=self.settings.paper_max_hold_minutes_intraday):
            return "timed_exit"
        if position.timeframe in {"1h", "1d"} and utc_now() >= opened_at + timedelta(days=self.settings.paper_max_hold_days_swing):
            return "timed_exit"
        return None

    def _apply_slippage(self, price: float, *, side: str) -> float:
        slippage = price * (float(self.settings.paper_slippage_bps) / 10_000.0)
        return round(price + slippage if side == "buy" else price - slippage, 4)

    @staticmethod
    def _pnl_usd(position: PaperPositionRecord, price: float) -> float:
        if position.side == "sell":
            return (position.entry_price - price) * position.quantity
        return (price - position.entry_price) * position.quantity
