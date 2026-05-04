"""Paper trading simulation service."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.models.approval import TradeProposal
from app.models.paper import BotPerformanceDashboard, PaperPositionRecord, PaperTradeRecord
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
        status_counts = Counter()
        if self.scan_decisions is not None:
            for item in self.scan_decisions.list(limit=500):
                status_counts[str(getattr(item, "status", "") or "unknown")] += 1
                payload = dict(getattr(item, "payload", {}) or {})
                metadata = dict(payload.get("metadata") or {})
                classification = str(metadata.get("signal_classification") or "")
                if classification:
                    status_counts[classification] += 1
                for reason in item.rejection_reasons[:3]:
                    rejection_counts[reason] += 1
        rejection_counts.update(status_counts)
        return self.trades.summary(
            open_positions=open_positions,
            rejection_reason_counts=dict(rejection_counts),
        )

    def dashboard(self) -> BotPerformanceDashboard:
        summary = self.summary()
        open_positions = self.positions.list(status="open", limit=50)
        recent_trades = self.trades.list(limit=50)
        recent_decisions = self._recent_scan_decisions(limit=50)
        return BotPerformanceDashboard(
            paper=summary,
            open_positions=open_positions,
            recent_trades=recent_trades,
            recent_scan_decisions=recent_decisions,
            provider_health=self._provider_health(recent_decisions),
            calibration_suggestions=self._calibration_suggestions(summary.rejection_reason_counts),
            risk_controls=self._risk_controls(),
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
            payload={
                **dict(position.payload),
                "realized_r_multiple": self._realized_r_multiple(position, exit_price),
            },
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
        if position.timeframe in {"10m", "1h", "1d", "1w"} and utc_now() >= opened_at + timedelta(days=self.settings.paper_max_hold_days_swing):
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

    @staticmethod
    def _realized_r_multiple(position: PaperPositionRecord, price: float) -> float:
        if position.stop_loss is None:
            return 0.0
        unit_risk = abs(float(position.entry_price) - float(position.stop_loss))
        if unit_risk <= 0:
            return 0.0
        if position.side == "sell":
            return round((float(position.entry_price) - price) / unit_risk, 3)
        return round((price - float(position.entry_price)) / unit_risk, 3)

    def _recent_scan_decisions(self, *, limit: int) -> list[dict[str, Any]]:
        if self.scan_decisions is None:
            return []
        decisions = []
        for item in self.scan_decisions.list(limit=limit):
            payload = dict(getattr(item, "payload", {}) or {})
            measurements = dict(payload.get("measurements") or {})
            market_data = dict(payload.get("market_data_status") or {})
            metadata = dict(payload.get("metadata") or {})
            decisions.append(
                {
                    "id": getattr(item, "id", None),
                    "scan_task": getattr(item, "scan_task", None),
                    "symbol": getattr(item, "symbol", None),
                    "strategy_name": getattr(item, "strategy_name", None),
                    "timeframe": getattr(item, "timeframe", None),
                    "status": getattr(item, "status", None),
                    "final_score": getattr(item, "final_score", None),
                    "alert_eligible": getattr(item, "alert_eligible", False),
                    "rejection_reasons": list(getattr(item, "rejection_reasons", []) or [])[:5],
                    "provider": (
                        market_data.get("quote_provider")
                        or measurements.get("quote_provider")
                        or metadata.get("data_source_quote")
                        or metadata.get("data_source")
                    ),
                    "freshness_status": (
                        market_data.get("freshness_status")
                        or measurements.get("freshness_status")
                        or metadata.get("freshness_status")
                    ),
                    "created_at": getattr(item, "created_at", None),
                }
            )
        return decisions

    @staticmethod
    def _provider_health(decisions: list[dict[str, Any]]) -> dict[str, Any]:
        for item in decisions:
            provider = item.get("provider")
            freshness = item.get("freshness_status")
            if provider or freshness:
                return {
                    "history_provider": provider or "unknown",
                    "quote_provider": provider or "unknown",
                    "freshness_status": freshness or "unknown",
                    "last_decision_at": item.get("created_at"),
                }
        return {}

    @staticmethod
    def _calibration_suggestions(rejection_counts: dict[str, int]) -> list[str]:
        suggestions: list[str] = []
        if rejection_counts.get("market_data_error", 0) or rejection_counts.get("provider_request_failed", 0):
            suggestions.append("Fix provider reliability before relaxing any trading filters.")
        if rejection_counts.get("relative_volume_too_low", 0):
            suggestions.append("Review near-miss outcomes before lowering relative-volume thresholds.")
        if rejection_counts.get("confluence_score_too_low", 0):
            suggestions.append("Compare rejected confluence setups against later trigger/target outcomes.")
        if rejection_counts.get("breakout_level_not_cleared", 0) or rejection_counts.get("breakdown_level_not_cleared", 0):
            suggestions.append("Keep these as watchlist setups until price confirms the trigger.")
        if not suggestions:
            suggestions.append("Collect more paper/watchlist outcomes before changing thresholds.")
        return suggestions[:5]

    def _risk_controls(self) -> dict[str, Any]:
        return {
            "execution_mode": getattr(self.settings, "execution_mode", "paper"),
            "paper_trading_enabled": bool(getattr(self.settings, "paper_trading_enabled", True)),
            "enable_real_trading": bool(getattr(self.settings, "enable_real_trading", False)),
            "require_approval": bool(getattr(self.settings, "require_approval", True)),
            "max_risk_per_trade_pct": getattr(self.settings, "max_risk_per_trade_pct", None),
            "max_daily_loss_usd": getattr(self.settings, "max_daily_loss_usd", None),
            "max_weekly_loss_usd": getattr(self.settings, "max_weekly_loss_usd", None),
            "max_open_positions": getattr(self.settings, "max_open_positions", None),
            "max_trades_per_day": getattr(self.settings, "max_trades_per_day", None),
            "kill_switch_enabled": bool(getattr(self.settings, "kill_switch_enabled", False)),
        }
