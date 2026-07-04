"""Paper trading simulation service."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.models.approval import TradeProposal
from app.models.paper import (
    BotPerformanceDashboard,
    PaperBrokerExecutionRecord,
    PaperBrokerOrderLeg,
    PaperPositionRecord,
    PaperTradeRecord,
)
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
        executions: Any | None = None,
        broker_orders: Any | None = None,
        execution_queue: Any | None = None,
    ):
        self.settings = settings
        self.positions = positions
        self.trades = trades
        self.logs = run_logs
        self.scan_decisions = scan_decisions
        self.executions = executions
        self.broker_orders = broker_orders
        self.execution_queue = execution_queue

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
        recent_broker_executions = self.broker_executions(limit=50)
        recent_decisions = self._recent_scan_decisions(limit=50)
        return BotPerformanceDashboard(
            paper=summary,
            open_positions=open_positions,
            recent_trades=recent_trades,
            recent_broker_executions=recent_broker_executions,
            recent_scan_decisions=recent_decisions,
            provider_health=self._provider_health(recent_decisions),
            calibration_suggestions=self._calibration_suggestions(summary.rejection_reason_counts),
            risk_controls=self._risk_controls(),
        )

    def broker_executions(self, *, limit: int = 100) -> list[PaperBrokerExecutionRecord]:
        """Return real Alpaca Paper broker executions distinct from simulated paper trades."""

        if self.executions is None:
            return []
        executions = self.executions.list(limit=max(limit, 1))
        queue_by_proposal = self._queue_by_proposal()
        snapshots = self._broker_order_snapshots(limit=max(1000, limit * 20))
        snapshots_by_execution: dict[str, list[dict[str, Any]]] = {}
        snapshots_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for snapshot in snapshots:
            execution_id = str(snapshot.get("execution_id") or "")
            if execution_id:
                snapshots_by_execution.setdefault(execution_id, []).append(snapshot)
            symbol = str(snapshot.get("symbol") or "").upper()
            if symbol:
                snapshots_by_symbol.setdefault(symbol, []).append(snapshot)
        records = [
            self._broker_execution_record(
                execution,
                queue=queue_by_proposal.get(execution.proposal_id),
                snapshots=snapshots_by_execution.get(execution.id, []),
                symbol_snapshots=snapshots_by_symbol,
            )
            for execution in executions
            if self._is_broker_paper_execution(execution)
        ]
        return records[: max(limit, 1)]

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

    def _broker_execution_record(
        self,
        execution: Any,
        *,
        queue: Any | None,
        snapshots: list[dict[str, Any]],
        symbol_snapshots: dict[str, list[dict[str, Any]]],
    ) -> PaperBrokerExecutionRecord:
        broker_execution = dict((execution.response_payload or {}).get("broker_execution") or {})
        parent = self._parent_snapshot(execution, snapshots) or {}
        parent_payload = dict(parent.get("payload") or {})
        payload = {**broker_execution, **{key: value for key, value in parent_payload.items() if value not in (None, "")}}
        request_payload = dict(execution.request_payload or {})
        symbol = str(payload.get("symbol") or request_payload.get("symbol") or getattr(queue, "symbol", "") or "").upper()
        strategy_name = (
            str(getattr(queue, "strategy_name", "") or "")
            or str(request_payload.get("strategy_name") or "")
            or self._payload_strategy_name(getattr(queue, "payload", None))
        )
        side = str(payload.get("side") or request_payload.get("side") or "")
        entry_price = self._optional_float(payload.get("filled_avg_price") or parent.get("filled_avg_price"))
        filled_qty = float(payload.get("filled_qty") or parent.get("filled_qty") or 0.0)
        legs = self._broker_legs(payload=payload, snapshots=snapshots, parent_order_id=execution.broker_order_id)
        exit_leg = next(
            (
                leg
                for leg in legs
                if str(leg.status or "").lower() == "filled"
                and str(leg.side or "").lower() == "sell"
                and leg.filled_avg_price is not None
            ),
            None,
        )
        exit_snapshot = None
        if exit_leg is None:
            exit_snapshot = self._nearest_exit_snapshot(
                symbol=symbol,
                parent_payload=payload,
                symbol_snapshots=symbol_snapshots,
            )
        exit_price = (
            exit_leg.filled_avg_price
            if exit_leg is not None
            else self._optional_float((exit_snapshot or {}).get("filled_avg_price"))
        )
        realized = float(getattr(execution, "realized_pnl_usd", 0.0) or 0.0)
        if realized == 0.0 and entry_price is not None and exit_price is not None and filled_qty > 0:
            realized = round((exit_price - entry_price) * filled_qty, 2)
        return PaperBrokerExecutionRecord(
            execution_id=execution.id,
            proposal_id=execution.proposal_id,
            queue_id=getattr(queue, "id", None),
            symbol=symbol,
            strategy_name=strategy_name or None,
            source=self._execution_source(strategy_name, request_payload),
            mode=execution.mode,
            status=execution.status,
            broker_order_id=execution.broker_order_id,
            client_order_id=str(payload.get("client_order_id") or request_payload.get("client_order_id") or "") or None,
            side=side or None,
            order_class=str(payload.get("order_class") or parent.get("order_class") or "") or None,
            quantity=float(payload.get("qty") or parent.get("qty") or filled_qty or 0.0),
            filled_qty=filled_qty,
            entry_fill_price=entry_price,
            exit_order_id=(
                exit_leg.broker_order_id
                if exit_leg is not None
                else str((exit_snapshot or {}).get("broker_order_id") or "") or None
            ),
            exit_fill_price=exit_price,
            realized_pnl_usd=realized,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            submitted_at=str(payload.get("submitted_at") or "") or None,
            filled_at=str(payload.get("filled_at") or parent_payload.get("filled_at") or "") or None,
            canceled_at=str(payload.get("canceled_at") or parent_payload.get("canceled_at") or "") or None,
            legs=legs,
            payload={
                "broker": (execution.response_payload or {}).get("broker"),
                "request": request_payload,
                "exit_source": "bracket_leg" if exit_leg is not None else ("separate_close_order" if exit_snapshot else None),
            },
        )

    def _queue_by_proposal(self) -> dict[str, Any]:
        if self.execution_queue is None:
            return {}
        return {
            item.proposal_id: item
            for item in self.execution_queue.list(limit=1000)
            if getattr(item, "proposal_id", None)
        }

    def _broker_order_snapshots(self, *, limit: int) -> list[dict[str, Any]]:
        if self.broker_orders is None:
            return []
        snapshots = []
        for row in self.broker_orders.list(limit=limit):
            item = dict(row)
            item["payload"] = self._json_or_empty(item.get("payload_json"), {})
            snapshots.append(item)
        return snapshots

    @staticmethod
    def _is_broker_paper_execution(execution: Any) -> bool:
        payload = dict(getattr(execution, "response_payload", {}) or {})
        return str(payload.get("broker") or "").lower() == "alpaca" or str(getattr(execution, "mode", "")).startswith("alpaca_")

    @staticmethod
    def _parent_snapshot(execution: Any, snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
        broker_order_id = str(getattr(execution, "broker_order_id", "") or "")
        for snapshot in snapshots:
            if str(snapshot.get("broker_order_id") or "") == broker_order_id:
                return snapshot
        for snapshot in snapshots:
            if not snapshot.get("parent_order_id"):
                return snapshot
        return None

    def _broker_legs(
        self,
        *,
        payload: dict[str, Any],
        snapshots: list[dict[str, Any]],
        parent_order_id: str | None,
    ) -> list[PaperBrokerOrderLeg]:
        snapshot_legs = [
            item
            for item in snapshots
            if parent_order_id and str(item.get("parent_order_id") or "") == str(parent_order_id)
        ]
        if snapshot_legs:
            return [self._leg_from_snapshot(item) for item in sorted(snapshot_legs, key=lambda row: str(row.get("created_at") or ""))]
        return [self._leg_from_payload(item) for item in list(payload.get("legs") or [])]

    @staticmethod
    def _leg_from_snapshot(item: dict[str, Any]) -> PaperBrokerOrderLeg:
        payload = dict(item.get("payload") or {})
        return PaperBrokerOrderLeg(
            broker_order_id=str(item.get("broker_order_id") or "") or None,
            client_order_id=str(item.get("client_order_id") or payload.get("client_order_id") or "") or None,
            side=str(item.get("side") or payload.get("side") or "") or None,
            order_type=str(payload.get("type") or "") or None,
            status=str(item.get("status") or payload.get("status") or "") or None,
            quantity=float(payload.get("qty") or item.get("qty") or 0.0),
            filled_qty=float(item.get("filled_qty") or payload.get("filled_qty") or 0.0),
            filled_avg_price=PaperTradingService._optional_float(item.get("filled_avg_price") or payload.get("filled_avg_price")),
            limit_price=PaperTradingService._optional_float(payload.get("limit_price")),
            stop_price=PaperTradingService._optional_float(payload.get("stop_price")),
            created_at=str(payload.get("created_at") or item.get("created_at") or "") or None,
            filled_at=str(payload.get("filled_at") or "") or None,
            canceled_at=str(payload.get("canceled_at") or "") or None,
        )

    @staticmethod
    def _leg_from_payload(item: dict[str, Any]) -> PaperBrokerOrderLeg:
        return PaperBrokerOrderLeg(
            broker_order_id=str(item.get("broker_order_id") or "") or None,
            client_order_id=str(item.get("client_order_id") or "") or None,
            side=str(item.get("side") or "") or None,
            order_type=str(item.get("type") or "") or None,
            status=str(item.get("status") or "") or None,
            quantity=float(item.get("qty") or 0.0),
            filled_qty=float(item.get("filled_qty") or 0.0),
            filled_avg_price=PaperTradingService._optional_float(item.get("filled_avg_price")),
            limit_price=PaperTradingService._optional_float(item.get("limit_price")),
            stop_price=PaperTradingService._optional_float(item.get("stop_price")),
            created_at=str(item.get("created_at") or "") or None,
            filled_at=str(item.get("filled_at") or "") or None,
            canceled_at=str(item.get("canceled_at") or "") or None,
        )

    @staticmethod
    def _nearest_exit_snapshot(
        *,
        symbol: str,
        parent_payload: dict[str, Any],
        symbol_snapshots: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        parent_created = str(parent_payload.get("created_at") or parent_payload.get("filled_at") or "")
        candidates = [
            item
            for item in symbol_snapshots.get(symbol, [])
            if not item.get("execution_id")
            and str(item.get("side") or "").lower() == "sell"
            and str(item.get("status") or "").lower() == "filled"
            and str((item.get("payload") or {}).get("created_at") or item.get("created_at") or "") >= parent_created
        ]
        return (
            sorted(
                candidates,
                key=lambda row: str((row.get("payload") or {}).get("created_at") or row.get("created_at") or ""),
            )[0]
            if candidates
            else None
        )

    @staticmethod
    def _execution_source(strategy_name: str, request_payload: dict[str, Any]) -> str:
        if strategy_name == "manual_smoke":
            return "manual_smoke"
        metadata = dict(request_payload.get("metadata") or {})
        if metadata.get("strategy_lab_generated") or str(strategy_name).startswith("generated_"):
            return "generated_strategy"
        if metadata.get("source") == "rl_policy":
            return "rl_policy"
        return "scanner_strategy" if strategy_name else "unknown"

    @staticmethod
    def _payload_strategy_name(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        order = dict(payload.get("order") or {})
        return str(order.get("strategy_name") or "")

    @staticmethod
    def _json_or_empty(raw: Any, default: Any) -> Any:
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(str(raw))
        except Exception:
            return default

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
