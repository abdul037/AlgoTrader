"""Repository layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.models.approval import ApprovalStatus, TradeProposal
from app.models.execution_queue import ExecutionQueueRecord
from app.models.execution import ExecutionRecord
from app.models.live_signal import LiveSignalSnapshot
from app.models.paper import PaperPerformanceSummary, PaperPositionRecord, PaperTradeRecord
from app.models.signal import Signal
from app.models.screener import ScanDecisionRecord
from app.models.workflow import AlertHistoryRecord, TrackedSignalRecord
from app.storage.db import Database
from app.utils.time import utc_now


def _dump_json(model: BaseModel | dict[str, Any] | None) -> str | None:
    if model is None:
        return None
    if isinstance(model, BaseModel):
        return model.model_dump_json()
    return json.dumps(model)


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return json.loads(raw)


class ProposalRepository:
    """Persist and query trade proposals."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, proposal: TradeProposal) -> TradeProposal:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    id, symbol, strategy_name, status, order_json, signal_json, notes,
                    decision_notes, approved_by, execution_id, created_at, updated_at,
                    expires_at, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.id,
                    proposal.order.symbol,
                    proposal.order.strategy_name,
                    proposal.status.value,
                    proposal.order.model_dump_json(),
                    _dump_json(proposal.signal),
                    proposal.notes,
                    proposal.decision_notes,
                    proposal.approved_by,
                    proposal.execution_id,
                    proposal.created_at,
                    proposal.updated_at,
                    proposal.expires_at,
                    proposal.executed_at,
                ),
            )
        return proposal

    def list(self, status: ApprovalStatus | None = None) -> list[TradeProposal]:
        query = "SELECT * FROM approvals"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_model(row) for row in rows]

    def get(self, proposal_id: str) -> TradeProposal | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    def update(self, proposal: TradeProposal) -> TradeProposal:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, order_json = ?, signal_json = ?, notes = ?, decision_notes = ?,
                    approved_by = ?, execution_id = ?, updated_at = ?, expires_at = ?, executed_at = ?
                WHERE id = ?
                """,
                (
                    proposal.status.value,
                    proposal.order.model_dump_json(),
                    _dump_json(proposal.signal),
                    proposal.notes,
                    proposal.decision_notes,
                    proposal.approved_by,
                    proposal.execution_id,
                    proposal.updated_at,
                    proposal.expires_at,
                    proposal.executed_at,
                    proposal.id,
                ),
            )
        return proposal

    @staticmethod
    def _row_to_model(row: Any) -> TradeProposal:
        signal_payload = _load_json(row["signal_json"])
        signal = Signal.model_validate(signal_payload) if signal_payload else None
        return TradeProposal(
            id=row["id"],
            status=ApprovalStatus(row["status"]),
            order=json.loads(row["order_json"]),
            signal=signal,
            notes=row["notes"] or "",
            decision_notes=row["decision_notes"] or "",
            approved_by=row["approved_by"],
            execution_id=row["execution_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            executed_at=row["executed_at"],
        )


class SignalRepository:
    """Persist generated signals."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, signal: Signal) -> Signal:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO signals (id, symbol, strategy_name, action, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.id,
                    signal.symbol,
                    signal.strategy_name,
                    signal.action.value,
                    signal.timestamp,
                    signal.model_dump_json(),
                ),
            )
        return signal


class SignalStateRepository:
    """Persist and query the latest signal state per symbol/strategy/timeframe."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, symbol: str, strategy_name: str, timeframe: str) -> LiveSignalSnapshot | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM signal_states
                WHERE symbol = ? AND strategy_name = ? AND timeframe = ?
                """,
                (symbol.upper(), strategy_name, timeframe),
            ).fetchone()
        if row is None:
            return None
        return LiveSignalSnapshot.model_validate_json(row["payload_json"])

    def upsert(self, snapshot: LiveSignalSnapshot) -> LiveSignalSnapshot:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO signal_states (
                    symbol, strategy_name, timeframe, state, generated_at,
                    candle_timestamp, rate_timestamp, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, strategy_name, timeframe)
                DO UPDATE SET
                    state = excluded.state,
                    generated_at = excluded.generated_at,
                    candle_timestamp = excluded.candle_timestamp,
                    rate_timestamp = excluded.rate_timestamp,
                    payload_json = excluded.payload_json
                """,
                (
                    snapshot.symbol,
                    snapshot.strategy_name,
                    snapshot.timeframe,
                    snapshot.state.value,
                    snapshot.generated_at,
                    snapshot.candle_timestamp,
                    snapshot.rate_timestamp,
                    snapshot.model_dump_json(),
                ),
            )
        return snapshot


class ExecutionRepository:
    """Persist execution attempts and provide loss stats."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, execution: ExecutionRecord) -> ExecutionRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO executions (
                    id, proposal_id, status, mode, broker_order_id, request_json,
                    response_json, error_message, realized_pnl_usd, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.id,
                    execution.proposal_id,
                    execution.status,
                    execution.mode,
                    execution.broker_order_id,
                    _dump_json(execution.request_payload) or "{}",
                    _dump_json(execution.response_payload) or "{}",
                    execution.error_message,
                    execution.realized_pnl_usd,
                    execution.created_at,
                    execution.updated_at,
                ),
            )
        return execution

    def update(self, execution: ExecutionRecord) -> ExecutionRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE executions
                SET status = ?, broker_order_id = ?, request_json = ?, response_json = ?,
                    error_message = ?, realized_pnl_usd = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    execution.status,
                    execution.broker_order_id,
                    _dump_json(execution.request_payload) or "{}",
                    _dump_json(execution.response_payload) or "{}",
                    execution.error_message,
                    execution.realized_pnl_usd,
                    execution.updated_at,
                    execution.id,
                ),
            )
        return execution

    def daily_loss_stats(self, day: datetime | None = None) -> tuple[float, int]:
        target = (day or utc_now()).astimezone(UTC).date().isoformat()
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT realized_pnl_usd
                FROM executions
                WHERE substr(created_at, 1, 10) = ?
                ORDER BY created_at ASC
                """,
                (target,),
            ).fetchall()

        total_pnl = 0.0
        consecutive_losses = 0
        current_loss_streak = 0
        for row in rows:
            pnl = float(row["realized_pnl_usd"] or 0.0)
            total_pnl += pnl
            if pnl < 0:
                current_loss_streak += 1
                consecutive_losses = max(consecutive_losses, current_loss_streak)
            elif pnl > 0:
                current_loss_streak = 0
        return total_pnl, consecutive_losses

    def period_realized_pnl(self, *, days: int) -> float:
        since = (utc_now() - timedelta(days=max(days, 1))).isoformat()
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS total_pnl
                FROM executions
                WHERE created_at >= ?
                """,
                (since,),
            ).fetchone()
        return float(row["total_pnl"] if row is not None else 0.0)


class ExecutionQueueRepository:
    """Persist approval-gated execution queue items."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, record: ExecutionQueueRecord) -> ExecutionQueueRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_queue (
                    id, proposal_id, signal_id, symbol, strategy_name, timeframe, mode, status,
                    approval_required, ready_for_execution, requested_entry_price, latest_quote_price,
                    latest_quote_timestamp, validation_reason, payload_json, created_at, updated_at, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.proposal_id,
                    record.signal_id,
                    record.symbol,
                    record.strategy_name,
                    record.timeframe,
                    record.mode,
                    record.status,
                    1 if record.approval_required else 0,
                    1 if record.ready_for_execution else 0,
                    record.requested_entry_price,
                    record.latest_quote_price,
                    record.latest_quote_timestamp,
                    record.validation_reason,
                    json.dumps(record.payload),
                    record.created_at,
                    record.updated_at,
                    record.executed_at,
                ),
            )
        return record

    def update(self, record: ExecutionQueueRecord) -> ExecutionQueueRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE execution_queue
                SET status = ?, ready_for_execution = ?, latest_quote_price = ?, latest_quote_timestamp = ?,
                    validation_reason = ?, payload_json = ?, updated_at = ?, executed_at = ?
                WHERE id = ?
                """,
                (
                    record.status,
                    1 if record.ready_for_execution else 0,
                    record.latest_quote_price,
                    record.latest_quote_timestamp,
                    record.validation_reason,
                    json.dumps(record.payload),
                    record.updated_at,
                    record.executed_at,
                    record.id,
                ),
            )
        return record

    def get(self, queue_id: str) -> ExecutionQueueRecord | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,)).fetchone()
        return None if row is None else self._row_to_model(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[ExecutionQueueRecord]:
        query = "SELECT * FROM execution_queue"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(limit, 1))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    def latest_open_for_symbol(self, symbol: str) -> ExecutionQueueRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM execution_queue
                WHERE symbol = ? AND status IN ('queued', 'processing')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    @staticmethod
    def _row_to_model(row: Any) -> ExecutionQueueRecord:
        return ExecutionQueueRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            mode=row["mode"],
            status=row["status"],
            approval_required=bool(row["approval_required"]),
            ready_for_execution=bool(row["ready_for_execution"]),
            requested_entry_price=row["requested_entry_price"],
            latest_quote_price=row["latest_quote_price"],
            latest_quote_timestamp=row["latest_quote_timestamp"],
            validation_reason=row["validation_reason"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            executed_at=row["executed_at"],
        )


class BacktestRepository:
    """Persist backtest summaries."""

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        backtest_id: str,
        symbol: str,
        strategy_name: str,
        file_path: str,
        started_at: str,
        completed_at: str,
        metrics: dict[str, Any],
        trades: list[dict[str, Any]],
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO backtests (
                    id, symbol, strategy_name, file_path, started_at,
                    completed_at, metrics_json, trades_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    backtest_id,
                    symbol,
                    strategy_name,
                    file_path,
                    started_at,
                    completed_at,
                    json.dumps(metrics),
                    json.dumps(trades),
                ),
            )

    def get_latest_summary(self, symbol: str, strategy_name: str | None = None) -> dict[str, Any] | None:
        query = """
            SELECT symbol, strategy_name, file_path, started_at, completed_at, metrics_json, trades_json
            FROM backtests
            WHERE symbol = ?
        """
        params: list[Any] = [symbol.upper()]
        if strategy_name:
            query += " AND strategy_name = ?"
            params.append(strategy_name)
        query += " ORDER BY completed_at DESC LIMIT 1"
        with self.db.connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return {
            "symbol": row["symbol"],
            "strategy_name": row["strategy_name"],
            "file_path": row["file_path"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "metrics": json.loads(row["metrics_json"]),
            "trades": json.loads(row["trades_json"]),
        }


class ScanDecisionRepository:
    """Persist per-candidate scan decisions for review and anti-spam logic."""

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        scan_task: str,
        symbol: str,
        strategy_name: str,
        timeframe: str,
        status: str,
        final_score: float | None,
        alert_eligible: bool,
        freshness: str | None,
        reason_codes: list[str],
        rejection_reasons: list[str],
        payload: dict[str, Any] | None = None,
    ) -> ScanDecisionRecord:
        created_at = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_decisions (
                    scan_task, symbol, strategy_name, timeframe, status, final_score,
                    alert_eligible, freshness, reason_codes_json, rejection_reasons_json,
                    payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_task,
                    symbol.upper(),
                    strategy_name,
                    timeframe,
                    status,
                    final_score,
                    1 if alert_eligible else 0,
                    freshness,
                    json.dumps(reason_codes),
                    json.dumps(rejection_reasons),
                    json.dumps(payload or {}),
                    created_at,
                ),
            )
            record_id = int(cursor.lastrowid)
        return self.get(record_id)

    def list(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        scan_task: str | None = None,
        symbol: str | None = None,
    ) -> list[ScanDecisionRecord]:
        query = "SELECT * FROM scan_decisions WHERE 1 = 1"
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if scan_task is not None:
            query += " AND scan_task = ?"
            params.append(scan_task)
        if symbol is not None:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    def get_latest(
        self,
        *,
        symbol: str,
        strategy_name: str,
        timeframe: str,
        since_minutes: int | None = None,
        statuses: list[str] | None = None,
    ) -> ScanDecisionRecord | None:
        query = """
            SELECT *
            FROM scan_decisions
            WHERE symbol = ? AND strategy_name = ? AND timeframe = ?
        """
        params: list[Any] = [symbol.upper(), strategy_name, timeframe]
        if since_minutes is not None:
            since_dt = utc_now() - timedelta(minutes=max(since_minutes, 1))
            query += " AND created_at >= ?"
            params.append(since_dt.isoformat())
        if statuses:
            placeholders = ", ".join(["?"] * len(statuses))
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.db.connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        return None if row is None else self._row_to_model(row)

    def get(self, record_id: int) -> ScanDecisionRecord:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_decisions WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Scan decision {record_id} not found")
        return self._row_to_model(row)

    @staticmethod
    def _row_to_model(row: Any) -> ScanDecisionRecord:
        return ScanDecisionRecord(
            id=int(row["id"]),
            scan_task=row["scan_task"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            status=row["status"],
            final_score=row["final_score"],
            alert_eligible=bool(row["alert_eligible"]),
            freshness=row["freshness"],
            reason_codes=json.loads(row["reason_codes_json"] or "[]"),
            rejection_reasons=json.loads(row["rejection_reasons_json"] or "[]"),
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )


class RunLogRepository:
    """Persist audit log events."""

    def __init__(self, db: Database):
        self.db = db

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_logs (event_type, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (event_type, json.dumps(payload), utc_now().isoformat()),
            )


class RuntimeStateRepository:
    """Persist lightweight key/value runtime state."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, state_key: str) -> str | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        return None if row is None else str(row["state_value"])

    def set(self, state_key: str, state_value: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state (state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key)
                DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (state_key, state_value, utc_now().isoformat()),
            )


class TrackedSignalRepository:
    """Persist and query tracked open signals derived from alerts."""

    def __init__(self, db: Database):
        self.db = db

    def list(self, *, status: str | None = None, limit: int = 100) -> list[TrackedSignalRecord]:
        query = "SELECT * FROM tracked_signals"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    def get_active(self, symbol: str, strategy_name: str, timeframe: str) -> TrackedSignalRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tracked_signals
                WHERE symbol = ? AND strategy_name = ? AND timeframe = ? AND status = 'open'
                ORDER BY id DESC LIMIT 1
                """,
                (symbol.upper(), strategy_name, timeframe),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    def upsert_open(self, snapshot: LiveSignalSnapshot, *, origin: str) -> TrackedSignalRecord:
        existing = self.get_active(snapshot.symbol, snapshot.strategy_name, snapshot.timeframe)
        now = utc_now().isoformat()
        if existing is None:
            with self.db.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO tracked_signals (
                        symbol, strategy_name, timeframe, status, origin, opened_at,
                        updated_at, closed_at, entry_price, stop_loss, take_profit,
                        last_price, payload_json
                    )
                    VALUES (?, ?, ?, 'open', ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.symbol.upper(),
                        snapshot.strategy_name,
                        snapshot.timeframe,
                        origin,
                        now,
                        now,
                        snapshot.entry_price,
                        snapshot.stop_loss,
                        snapshot.take_profit,
                        snapshot.current_price,
                        snapshot.model_dump_json(),
                    ),
                )
                record_id = int(cursor.lastrowid)
            return self.get_by_id(record_id)

        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tracked_signals
                SET updated_at = ?, origin = ?, entry_price = ?, stop_loss = ?, take_profit = ?,
                    last_price = ?, payload_json = ?
                WHERE id = ?
                """,
                (
                    now,
                    origin,
                    snapshot.entry_price,
                    snapshot.stop_loss,
                    snapshot.take_profit,
                    snapshot.current_price,
                    snapshot.model_dump_json(),
                    existing.id,
                ),
            )
        return self.get_by_id(existing.id)

    def update_price(self, record_id: int, *, last_price: float, snapshot: LiveSignalSnapshot | None = None) -> TrackedSignalRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tracked_signals
                SET updated_at = ?, last_price = ?, payload_json = COALESCE(?, payload_json)
                WHERE id = ?
                """,
                (
                    utc_now().isoformat(),
                    last_price,
                    snapshot.model_dump_json() if snapshot is not None else None,
                    record_id,
                ),
            )
        return self.get_by_id(record_id)

    def close(self, record_id: int, *, status: str, last_price: float, snapshot: LiveSignalSnapshot | None = None) -> TrackedSignalRecord:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tracked_signals
                SET status = ?, updated_at = ?, closed_at = ?, last_price = ?,
                    payload_json = COALESCE(?, payload_json)
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    now,
                    last_price,
                    snapshot.model_dump_json() if snapshot is not None else None,
                    record_id,
                ),
            )
        return self.get_by_id(record_id)

    def get_by_id(self, record_id: int) -> TrackedSignalRecord:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracked_signals WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Tracked signal {record_id} not found")
        return self._row_to_model(row)

    @staticmethod
    def _row_to_model(row: Any) -> TrackedSignalRecord:
        return TrackedSignalRecord(
            id=int(row["id"]),
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            status=row["status"],
            origin=row["origin"],
            opened_at=row["opened_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            last_price=row["last_price"],
            snapshot=LiveSignalSnapshot.model_validate_json(row["payload_json"]),
        )


class AlertHistoryRepository:
    """Persist historical alert messages and workflow events."""

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        category: str,
        status: str,
        message_text: str,
        symbol: str | None = None,
        strategy_name: str | None = None,
        timeframe: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AlertHistoryRecord:
        created_at = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO alert_history (
                    category, symbol, strategy_name, timeframe, status,
                    message_text, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    category,
                    symbol,
                    strategy_name,
                    timeframe,
                    status,
                    message_text,
                    json.dumps(payload or {}),
                    created_at,
                ),
            )
            record_id = int(cursor.lastrowid)
        return self.get(record_id)

    def list(self, *, limit: int = 50, category: str | None = None) -> list[AlertHistoryRecord]:
        query = "SELECT * FROM alert_history"
        params: list[Any] = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    def count(self) -> int:
        with self.db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM alert_history").fetchone()
        return int(row["count"] if row is not None else 0)

    def get(self, record_id: int) -> AlertHistoryRecord:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM alert_history WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Alert history {record_id} not found")
        return self._row_to_model(row)

    @staticmethod
    def _row_to_model(row: Any) -> AlertHistoryRecord:
        return AlertHistoryRecord(
            id=int(row["id"]),
            category=row["category"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            status=row["status"],
            message_text=row["message_text"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )


class PaperPositionRepository:
    """Persist open paper positions."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, record: PaperPositionRecord) -> PaperPositionRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_positions (
                    id, proposal_id, signal_id, symbol, strategy_name, timeframe, side, regime_label, hold_style,
                    status, quantity, entry_price, current_price, stop_loss, target_1, target_2, target_3,
                    opened_at, updated_at, closed_at, realized_pnl_usd, unrealized_pnl_usd, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.proposal_id,
                    record.signal_id,
                    record.symbol,
                    record.strategy_name,
                    record.timeframe,
                    record.side,
                    record.regime_label,
                    record.hold_style,
                    record.status,
                    record.quantity,
                    record.entry_price,
                    record.current_price,
                    record.stop_loss,
                    record.target_1,
                    record.target_2,
                    record.target_3,
                    record.opened_at,
                    record.updated_at,
                    record.closed_at,
                    record.realized_pnl_usd,
                    record.unrealized_pnl_usd,
                    json.dumps(record.payload),
                ),
            )
        return record

    def update(self, record: PaperPositionRecord) -> PaperPositionRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE paper_positions
                SET status = ?, current_price = ?, updated_at = ?, closed_at = ?, realized_pnl_usd = ?,
                    unrealized_pnl_usd = ?, payload_json = ?
                WHERE id = ?
                """,
                (
                    record.status,
                    record.current_price,
                    record.updated_at,
                    record.closed_at,
                    record.realized_pnl_usd,
                    record.unrealized_pnl_usd,
                    json.dumps(record.payload),
                    record.id,
                ),
            )
        return record

    def get(self, record_id: str) -> PaperPositionRecord | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM paper_positions WHERE id = ?", (record_id,)).fetchone()
        return None if row is None else self._row_to_model(row)

    def list(self, *, status: str | None = None, limit: int = 200) -> list[PaperPositionRecord]:
        query = "SELECT * FROM paper_positions"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(limit, 1))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_model(row) for row in rows]

    def open_for_symbol(self, symbol: str) -> list[PaperPositionRecord]:
        return [item for item in self.list(status="open", limit=500) if item.symbol == symbol.upper()]

    @staticmethod
    def _row_to_model(row: Any) -> PaperPositionRecord:
        return PaperPositionRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            side=row["side"],
            regime_label=row["regime_label"],
            hold_style=row["hold_style"],
            status=row["status"],
            quantity=float(row["quantity"] or 0.0),
            entry_price=float(row["entry_price"] or 0.0),
            current_price=float(row["current_price"] or 0.0),
            stop_loss=row["stop_loss"],
            target_1=row["target_1"],
            target_2=row["target_2"],
            target_3=row["target_3"],
            opened_at=row["opened_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            realized_pnl_usd=float(row["realized_pnl_usd"] or 0.0),
            unrealized_pnl_usd=float(row["unrealized_pnl_usd"] or 0.0),
            payload=json.loads(row["payload_json"] or "{}"),
        )


class PaperTradeRepository:
    """Persist closed paper trades and summarize performance."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, record: PaperTradeRecord) -> PaperTradeRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_trades (
                    id, position_id, proposal_id, signal_id, symbol, strategy_name, timeframe, side,
                    regime_label, hold_style, outcome, entry_price, exit_price, quantity,
                    realized_pnl_usd, realized_pnl_pct, opened_at, closed_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.position_id,
                    record.proposal_id,
                    record.signal_id,
                    record.symbol,
                    record.strategy_name,
                    record.timeframe,
                    record.side,
                    record.regime_label,
                    record.hold_style,
                    record.outcome,
                    record.entry_price,
                    record.exit_price,
                    record.quantity,
                    record.realized_pnl_usd,
                    record.realized_pnl_pct,
                    record.opened_at,
                    record.closed_at,
                    json.dumps(record.payload),
                ),
            )
        return record

    def list(self, *, limit: int = 500) -> list[PaperTradeRecord]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at DESC LIMIT ?",
                (max(limit, 1),),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def summary(
        self,
        *,
        open_positions: list[PaperPositionRecord],
        rejection_reason_counts: dict[str, int] | None = None,
    ) -> PaperPerformanceSummary:
        trades = self.list(limit=2000)
        total_trades = len(trades)
        winners = [trade for trade in trades if trade.realized_pnl_usd > 0]
        realized_pnl = round(sum(trade.realized_pnl_usd for trade in trades), 2)
        unrealized_pnl = round(sum(position.unrealized_pnl_usd for position in open_positions), 2)
        expectancy = round(realized_pnl / total_trades, 2) if total_trades else 0.0
        average_rr = [
            float((trade.payload or {}).get("risk_reward_ratio") or 0.0)
            for trade in trades
            if float((trade.payload or {}).get("risk_reward_ratio") or 0.0) > 0.0
        ]
        return PaperPerformanceSummary(
            total_trades=total_trades,
            open_positions=len(open_positions),
            win_rate=round((len(winners) / total_trades) * 100.0, 2) if total_trades else 0.0,
            realized_pnl_usd=realized_pnl,
            unrealized_pnl_usd=unrealized_pnl,
            expectancy_usd=expectancy,
            average_reward_to_risk=round(sum(average_rr) / len(average_rr), 2) if average_rr else 0.0,
            pnl_by_timeframe=_sum_by_key(trades, "timeframe"),
            pnl_by_strategy=_sum_by_key(trades, "strategy_name"),
            pnl_by_symbol=_sum_by_key(trades, "symbol"),
            pnl_by_regime=_sum_by_key(trades, "regime_label"),
            rejection_reason_counts=rejection_reason_counts or {},
        )

    @staticmethod
    def _row_to_model(row: Any) -> PaperTradeRecord:
        return PaperTradeRecord(
            id=row["id"],
            position_id=row["position_id"],
            proposal_id=row["proposal_id"],
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            side=row["side"],
            regime_label=row["regime_label"],
            hold_style=row["hold_style"],
            outcome=row["outcome"],
            entry_price=float(row["entry_price"] or 0.0),
            exit_price=float(row["exit_price"] or 0.0),
            quantity=float(row["quantity"] or 0.0),
            realized_pnl_usd=float(row["realized_pnl_usd"] or 0.0),
            realized_pnl_pct=float(row["realized_pnl_pct"] or 0.0),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            payload=json.loads(row["payload_json"] or "{}"),
        )


def _sum_by_key(trades: list[PaperTradeRecord], key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for trade in trades:
        label = str(getattr(trade, key) or "unknown")
        totals[label] = round(totals.get(label, 0.0) + float(trade.realized_pnl_usd), 2)
    return totals
