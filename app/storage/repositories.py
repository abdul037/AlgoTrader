"""Repository layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.models.approval import ApprovalStatus, TradeProposal
from app.models.execution import ExecutionRecord
from app.models.execution_queue import ExecutionQueueRecord
from app.models.institutional import (
    BrokerAccountIdentity,
    BrokerCapability,
    BrokerComparison,
    BrokerReconciliationResult,
    PortfolioRiskSnapshot,
    PromotionDecision,
    RolloutGateEvidence,
    StrategyAudit,
    StrategyVersion,
)
from app.models.live_signal import LiveSignalSnapshot
from app.models.paper import PaperPerformanceSummary, PaperPositionRecord, PaperTradeRecord
from app.models.rl_policy import RLPolicyProposal, RLPolicyVersion
from app.models.screener import ScanDecisionRecord
from app.models.signal import Signal
from app.models.strategy_lab import (
    GeneratedStrategyRecord,
    StrategyLabBacktestRecord,
    StrategyLabDsl,
)
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

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    def get_by_broker_order_id(self, broker_order_id: str) -> ExecutionRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    def get_latest_by_proposal_id(self, proposal_id: str) -> ExecutionRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM executions
                WHERE proposal_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return None if row is None else self._row_to_model(row)

    def list(self, *, limit: int = 500) -> list[ExecutionRecord]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM executions ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

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
        target = (day or utc_now()).astimezone(UTC).date()
        events = [
            (closed_at, pnl)
            for closed_at, pnl in self._realized_events()
            if closed_at.astimezone(UTC).date() == target
        ]

        total_pnl = 0.0
        consecutive_losses = 0
        current_loss_streak = 0
        for _closed_at, pnl in events:
            total_pnl += pnl
            if pnl < 0:
                current_loss_streak += 1
                consecutive_losses = max(consecutive_losses, current_loss_streak)
            elif pnl > 0:
                current_loss_streak = 0
        return total_pnl, consecutive_losses

    def period_realized_pnl(self, *, days: int) -> float:
        since = utc_now() - timedelta(days=max(days, 1))
        return sum(pnl for closed_at, pnl in self._realized_events() if closed_at >= since)

    def consecutive_losses(self) -> int:
        streak = 0
        for _closed_at, pnl in self._realized_events():
            if pnl < 0:
                streak += 1
            elif pnl > 0:
                streak = 0
        return streak

    def _realized_events(self) -> list[tuple[datetime, float]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT realized_pnl_usd, response_json, updated_at
                FROM executions
                WHERE realized_pnl_usd != 0
                """,
            ).fetchall()
        events: list[tuple[datetime, float]] = []
        for row in rows:
            payload = json.loads(row["response_json"] or "{}")
            broker_execution = dict(payload.get("broker_execution") or {})
            filled_at = next(
                (
                    leg.get("filled_at")
                    for leg in list(broker_execution.get("legs") or [])
                    if str(leg.get("status") or "").lower() == "filled" and leg.get("filled_at")
                ),
                row["updated_at"],
            )
            parsed = datetime.fromisoformat(str(filled_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            events.append((parsed.astimezone(UTC), float(row["realized_pnl_usd"] or 0.0)))
        return sorted(events, key=lambda item: item[0])

    def count_since(self, since: datetime) -> int:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM executions
                WHERE created_at >= ?
                """,
                (since.astimezone(UTC).isoformat(),),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    @staticmethod
    def _row_to_model(row: Any) -> ExecutionRecord:
        return ExecutionRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            status=row["status"],
            mode=row["mode"],
            broker_order_id=row["broker_order_id"],
            request_payload=json.loads(row["request_json"] or "{}"),
            response_payload=json.loads(row["response_json"] or "{}"),
            error_message=row["error_message"],
            realized_pnl_usd=float(row["realized_pnl_usd"] or 0.0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


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
                    client_order_id, approval_required, ready_for_execution, requested_entry_price, latest_quote_price,
                    latest_quote_timestamp, validation_reason, payload_json, created_at, updated_at, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.client_order_id,
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

    def claim_for_processing(self, queue_id: str, *, stale_before: str) -> bool:
        """Atomically claim a queued item or recover an abandoned processing item."""

        with self.db.connect() as connection:
            result = connection.execute(
                """
                UPDATE execution_queue
                SET status = 'processing', updated_at = ?
                WHERE id = ?
                  AND (
                    status = 'queued'
                    OR status = 'blocked'
                    OR (status = 'processing' AND updated_at < ?)
                  )
                """,
                (utc_now().isoformat(), queue_id, stale_before),
            )
        return result.rowcount == 1

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
            client_order_id=row["client_order_id"],
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

    def get_latest_summary(
        self,
        symbol: str,
        strategy_name: str | None = None,
        timeframe: str | None = None,
    ) -> dict[str, Any] | None:
        query = """
            SELECT symbol, strategy_name, file_path, started_at, completed_at, metrics_json, trades_json
            FROM backtests
            WHERE symbol = ?
        """
        params: list[Any] = [symbol.upper()]
        if strategy_name:
            query += " AND strategy_name = ?"
            params.append(strategy_name)
        if timeframe:
            query += " AND file_path LIKE ?"
            params.append(f"%:{timeframe.lower()}:%")
        query += " ORDER BY completed_at DESC LIMIT 25"
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        if not rows:
            return None
        parsed: list[dict[str, Any]] = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            parsed.append(
                {
                    "symbol": row["symbol"],
                    "strategy_name": row["strategy_name"],
                    "file_path": row["file_path"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "metrics": metrics,
                    "trades": json.loads(row["trades_json"]),
                    "out_of_sample": bool(metrics.get("out_of_sample", False)),
                }
            )
        for item in parsed:
            if item["out_of_sample"]:
                return item
        return parsed[0]


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

    def get_many(self, state_keys: list[str] | tuple[str, ...]) -> dict[str, str | None]:
        keys = list(dict.fromkeys(str(key) for key in state_keys if str(key)))
        if not keys:
            return {}
        placeholders = ", ".join("?" for _ in keys)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT state_key, state_value
                FROM runtime_state
                WHERE state_key IN ({placeholders})
                """,
                tuple(keys),
            ).fetchall()
        values = {str(row["state_key"]): str(row["state_value"]) for row in rows}
        return {key: values.get(key) for key in keys}

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


class StrategyGovernanceRepository:
    """Persist strategy versions, qualification audits, and promotion decisions."""

    def __init__(self, db: Database):
        self.db = db

    def create_version(self, version: StrategyVersion) -> StrategyVersion:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_versions (
                    id, strategy_name, code_version, parameters_json, dataset_version,
                    timeframe, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.id,
                    version.strategy_name,
                    version.code_version,
                    json.dumps(version.parameters),
                    version.dataset_version,
                    version.timeframe,
                    version.status,
                    version.created_at,
                ),
            )
        return version

    def get_version(self, version_id: str) -> StrategyVersion:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Strategy version {version_id} not found")
        return self._version(row)

    def list_versions(self, *, limit: int = 200) -> list[StrategyVersion]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_versions ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._version(row) for row in rows]

    def update_version_status(self, version_id: str, status: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE strategy_versions SET status = ? WHERE id = ?",
                (status, version_id),
            )

    def record_audit(self, audit: StrategyAudit) -> StrategyAudit:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_audits (
                    id, strategy_version_id, dataset_version, timeframe, out_of_sample_trades,
                    deflated_sharpe, rolling_sharpe, profit_factor, expectancy_after_costs,
                    max_drawdown_pct, strategy_drawdown_pct, unexplained_errors,
                    protected_exit_coverage_pct, metrics_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit.id,
                    audit.strategy_version_id,
                    audit.dataset_version,
                    audit.timeframe,
                    audit.out_of_sample_trades,
                    audit.deflated_sharpe,
                    audit.rolling_sharpe,
                    audit.profit_factor,
                    audit.expectancy_after_costs,
                    audit.max_drawdown_pct,
                    audit.strategy_drawdown_pct,
                    audit.unexplained_errors,
                    audit.protected_exit_coverage_pct,
                    json.dumps(audit.metrics),
                    audit.created_at,
                ),
            )
        return audit

    def latest_audit(self, version_id: str) -> StrategyAudit | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM strategy_audits
                WHERE strategy_version_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (version_id,),
            ).fetchone()
        return None if row is None else self._audit(row)

    def list_audits(self, *, limit: int = 200) -> list[StrategyAudit]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_audits ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._audit(row) for row in rows]

    def record_decision(self, decision: PromotionDecision) -> PromotionDecision:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO promotion_decisions (
                    id, strategy_version_id, strategy_audit_id, target_stage, approved,
                    blockers_json, evidence_json, decided_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.strategy_version_id,
                    decision.strategy_audit_id,
                    decision.target_stage,
                    1 if decision.approved else 0,
                    json.dumps(decision.blockers),
                    json.dumps(decision.evidence),
                    decision.decided_by,
                    decision.created_at,
                ),
            )
        return decision

    def list_decisions(self, *, limit: int = 200) -> list[PromotionDecision]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM promotion_decisions ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._decision(row) for row in rows]

    def approved_production_versions(self) -> list[str]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT decision.strategy_version_id
                FROM promotion_decisions AS decision
                JOIN strategy_versions AS version
                  ON version.id = decision.strategy_version_id
                WHERE decision.approved = 1
                  AND decision.target_stage = 'production_candidate'
                  AND version.status = 'production_candidate'
                """
            ).fetchall()
        return [str(row["strategy_version_id"]) for row in rows]

    def approved_paper_exploration_strategies(self) -> list[str]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT version.strategy_name
                FROM promotion_decisions AS decision
                JOIN strategy_versions AS version
                  ON version.id = decision.strategy_version_id
                WHERE decision.approved = 1
                  AND decision.target_stage = 'paper_exploration'
                  AND version.status = 'paper_exploration'
                ORDER BY version.strategy_name
                """
            ).fetchall()
        return [str(row["strategy_name"]) for row in rows]

    def strategy_production_approved(self, strategy_name: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM promotion_decisions AS decision
                JOIN strategy_versions AS version
                  ON version.id = decision.strategy_version_id
                WHERE decision.approved = 1
                  AND decision.target_stage = 'production_candidate'
                  AND version.strategy_name = ?
                  AND version.status = 'production_candidate'
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        return row is not None

    def strategy_paper_exploration_approved(self, strategy_name: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM promotion_decisions AS decision
                JOIN strategy_versions AS version
                  ON version.id = decision.strategy_version_id
                WHERE decision.approved = 1
                  AND decision.target_stage = 'paper_exploration'
                  AND version.strategy_name = ?
                  AND version.status = 'paper_exploration'
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _version(row: Any) -> StrategyVersion:
        payload = dict(row)
        payload["parameters"] = json.loads(payload.pop("parameters_json") or "{}")
        return StrategyVersion.model_validate(payload)

    @staticmethod
    def _audit(row: Any) -> StrategyAudit:
        payload = dict(row)
        payload["metrics"] = json.loads(payload.pop("metrics_json") or "{}")
        return StrategyAudit.model_validate(payload)

    @staticmethod
    def _decision(row: Any) -> PromotionDecision:
        payload = dict(row)
        payload["approved"] = bool(payload["approved"])
        payload["blockers"] = json.loads(payload.pop("blockers_json") or "[]")
        payload["evidence"] = json.loads(payload.pop("evidence_json") or "{}")
        return PromotionDecision.model_validate(payload)


class StrategyLabRepository:
    """Persist generated strategy DSL specs and validation evidence."""

    def __init__(self, db: Database):
        self.db = db

    def create_generated(self, item: GeneratedStrategyRecord) -> GeneratedStrategyRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_lab_generated_strategies (
                    id, name, status, dsl_json, source, latest_backtest_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.name,
                    item.status,
                    item.dsl.model_dump_json(),
                    item.source,
                    item.latest_backtest_id,
                    item.created_at,
                    item.updated_at,
                ),
            )
        return item

    def get_generated(self, generated_id: str) -> GeneratedStrategyRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_lab_generated_strategies WHERE id = ?",
                (generated_id,),
            ).fetchone()
        return None if row is None else self._generated(row)

    def get_generated_by_name(self, name: str) -> GeneratedStrategyRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_lab_generated_strategies WHERE name = ?",
                (name,),
            ).fetchone()
        return None if row is None else self._generated(row)

    def list_generated(self, *, status: str | None = None, limit: int = 200) -> list[GeneratedStrategyRecord]:
        query = "SELECT * FROM strategy_lab_generated_strategies"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._generated(row) for row in rows]

    def update_generated_status(
        self,
        generated_id: str,
        *,
        status: str,
        latest_backtest_id: str | None = None,
    ) -> GeneratedStrategyRecord:
        updated_at = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE strategy_lab_generated_strategies
                SET status = ?, latest_backtest_id = COALESCE(?, latest_backtest_id), updated_at = ?
                WHERE id = ?
                """,
                (status, latest_backtest_id, updated_at, generated_id),
            )
        item = self.get_generated(generated_id)
        if item is None:
            raise KeyError(f"Generated strategy {generated_id} not found")
        return item

    def record_backtest(self, item: StrategyLabBacktestRecord) -> StrategyLabBacktestRecord:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_lab_backtests (
                    id, generated_strategy_id, status, metrics_json, blockers_json, results_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.generated_strategy_id,
                    item.status,
                    json.dumps(item.metrics),
                    json.dumps(item.blockers),
                    json.dumps(item.results),
                    item.created_at,
                ),
            )
        return item

    def latest_backtest(self, generated_strategy_id: str) -> StrategyLabBacktestRecord | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM strategy_lab_backtests
                WHERE generated_strategy_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (generated_strategy_id,),
            ).fetchone()
        return None if row is None else self._backtest(row)

    @staticmethod
    def _generated(row: Any) -> GeneratedStrategyRecord:
        return GeneratedStrategyRecord(
            id=row["id"],
            name=row["name"],
            status=row["status"],
            dsl=StrategyLabDsl.model_validate_json(row["dsl_json"]),
            source=row["source"],
            latest_backtest_id=row["latest_backtest_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _backtest(row: Any) -> StrategyLabBacktestRecord:
        return StrategyLabBacktestRecord(
            id=row["id"],
            generated_strategy_id=row["generated_strategy_id"],
            status=row["status"],
            metrics=json.loads(row["metrics_json"] or "{}"),
            blockers=json.loads(row["blockers_json"] or "[]"),
            results=json.loads(row["results_json"] or "[]"),
            created_at=row["created_at"],
        )


class RLPolicyRepository:
    """Persist offline RL policies and proposal decisions."""

    def __init__(self, db: Database):
        self.db = db

    def record_version(self, item: RLPolicyVersion) -> RLPolicyVersion:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO rl_policy_versions (
                    id, status, dataset_version, reward_model_version, row_count,
                    accepted_rows, metrics_json, policy_json, blockers_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.status,
                    item.dataset_version,
                    item.reward_model_version,
                    item.row_count,
                    item.accepted_rows,
                    json.dumps(item.metrics, sort_keys=True),
                    json.dumps(item.policy, sort_keys=True),
                    json.dumps(item.blockers),
                    item.created_at,
                ),
            )
        return item

    def latest_version(self, *, eligible_only: bool = False) -> RLPolicyVersion | None:
        query = "SELECT * FROM rl_policy_versions"
        params: tuple[Any, ...] = ()
        if eligible_only:
            query += " WHERE status IN ('paper_candidate', 'shadow')"
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.db.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return None if row is None else self._version(row)

    def list_versions(self, *, limit: int = 100) -> list[RLPolicyVersion]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rl_policy_versions ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._version(row) for row in rows]

    def record_proposal(self, item: RLPolicyProposal) -> RLPolicyProposal:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO rl_policy_proposals (
                    id, decision_key, policy_version_id, scan_decision_id, proposal_id,
                    symbol, strategy_name, timeframe, status, score, blockers_json,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_key) DO NOTHING
                """,
                (
                    item.id,
                    item.decision_key,
                    item.policy_version_id,
                    item.scan_decision_id,
                    item.proposal_id,
                    item.symbol.upper(),
                    item.strategy_name,
                    item.timeframe,
                    item.status,
                    item.score,
                    json.dumps(item.blockers),
                    json.dumps(item.metadata, sort_keys=True),
                    item.created_at,
                ),
            )
        return self.get_proposal_by_key(item.decision_key) or item

    def get_proposal_by_key(self, decision_key: str) -> RLPolicyProposal | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM rl_policy_proposals WHERE decision_key = ?",
                (decision_key,),
            ).fetchone()
        return None if row is None else self._proposal(row)

    def update_proposal_status(
        self,
        decision_key: str,
        *,
        status: str,
        proposal_id: str | None = None,
        blockers: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RLPolicyProposal | None:
        existing = self.get_proposal_by_key(decision_key)
        if existing is None:
            return None
        merged_metadata = {**existing.metadata, **(metadata or {})}
        blockers_payload = existing.blockers if blockers is None else blockers
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE rl_policy_proposals
                SET status = ?,
                    proposal_id = COALESCE(?, proposal_id),
                    blockers_json = ?,
                    metadata_json = ?
                WHERE decision_key = ?
                """,
                (
                    status,
                    proposal_id,
                    json.dumps(blockers_payload),
                    json.dumps(merged_metadata, sort_keys=True),
                    decision_key,
                ),
            )
        return self.get_proposal_by_key(decision_key)

    def list_proposals(self, *, limit: int = 200, status: str | None = None) -> list[RLPolicyProposal]:
        query = "SELECT * FROM rl_policy_proposals"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self.db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._proposal(row) for row in rows]

    def proposal_count_since(self, since_iso: str, *, statuses: set[str] | None = None) -> int:
        query = "SELECT COUNT(*) FROM rl_policy_proposals WHERE created_at >= ?"
        params: list[Any] = [since_iso]
        if statuses:
            placeholders = ", ".join(["?"] * len(statuses))
            query += f" AND status IN ({placeholders})"
            params.extend(sorted(statuses))
        with self.db.connect() as connection:
            return int(connection.execute(query, tuple(params)).fetchone()[0])

    def counts(self) -> dict[str, int]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM rl_policy_proposals GROUP BY status"
            ).fetchall()
            versions = connection.execute("SELECT COUNT(*) FROM rl_policy_versions").fetchone()[0]
        result = {str(row["status"]): int(row["count"]) for row in rows}
        result["versions"] = int(versions)
        return result

    @staticmethod
    def _version(row: Any) -> RLPolicyVersion:
        return RLPolicyVersion(
            id=row["id"],
            status=row["status"],
            dataset_version=row["dataset_version"],
            reward_model_version=row["reward_model_version"],
            row_count=int(row["row_count"]),
            accepted_rows=int(row["accepted_rows"]),
            metrics=json.loads(row["metrics_json"] or "{}"),
            policy=json.loads(row["policy_json"] or "{}"),
            blockers=json.loads(row["blockers_json"] or "[]"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _proposal(row: Any) -> RLPolicyProposal:
        return RLPolicyProposal(
            id=row["id"],
            decision_key=row["decision_key"],
            policy_version_id=row["policy_version_id"],
            scan_decision_id=row["scan_decision_id"],
            proposal_id=row["proposal_id"],
            symbol=row["symbol"],
            strategy_name=row["strategy_name"],
            timeframe=row["timeframe"],
            status=row["status"],
            score=float(row["score"] or 0.0),
            blockers=json.loads(row["blockers_json"] or "[]"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
        )


class BrokerGovernanceRepository:
    """Persist normalized multi-broker capability, identity, and comparison evidence."""

    def __init__(self, db: Database):
        self.db = db

    def upsert_capability(self, capability: BrokerCapability) -> BrokerCapability:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_capabilities (
                    id, broker, account_mode, supports_equities, supports_native_protection,
                    supports_client_idempotency, supports_shorting, supports_borrow_checks,
                    supports_financing_costs, verified, details_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker, account_mode)
                DO UPDATE SET
                    id = excluded.id,
                    supports_equities = excluded.supports_equities,
                    supports_native_protection = excluded.supports_native_protection,
                    supports_client_idempotency = excluded.supports_client_idempotency,
                    supports_shorting = excluded.supports_shorting,
                    supports_borrow_checks = excluded.supports_borrow_checks,
                    supports_financing_costs = excluded.supports_financing_costs,
                    verified = excluded.verified,
                    details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (
                    capability.id,
                    capability.broker,
                    capability.account_mode,
                    int(capability.supports_equities),
                    int(capability.supports_native_protection),
                    int(capability.supports_client_idempotency),
                    int(capability.supports_shorting),
                    int(capability.supports_borrow_checks),
                    int(capability.supports_financing_costs),
                    int(capability.verified),
                    json.dumps(capability.details),
                    capability.updated_at,
                ),
            )
        return capability

    def upsert_identity(self, identity: BrokerAccountIdentity) -> BrokerAccountIdentity:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_account_identities (
                    id, broker, account_mode, account_id, account_number,
                    expected_account_number, verified, status, details_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker, account_mode)
                DO UPDATE SET
                    id = excluded.id,
                    account_id = excluded.account_id,
                    account_number = excluded.account_number,
                    expected_account_number = excluded.expected_account_number,
                    verified = excluded.verified,
                    status = excluded.status,
                    details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.id,
                    identity.broker,
                    identity.account_mode,
                    identity.account_id,
                    identity.account_number,
                    identity.expected_account_number,
                    int(identity.verified),
                    identity.status,
                    json.dumps(identity.details),
                    identity.updated_at,
                ),
            )
        return identity

    def record_reconciliation(
        self, result: BrokerReconciliationResult
    ) -> BrokerReconciliationResult:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_reconciliation_results (
                    id, broker, account_id, status, orders_seen, positions_seen,
                    unknown_positions, unprotected_positions, issues_json, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.id,
                    result.broker,
                    result.account_id,
                    result.status,
                    result.orders_seen,
                    result.positions_seen,
                    result.unknown_positions,
                    result.unprotected_positions,
                    json.dumps(result.issues),
                    json.dumps(result.details),
                    result.created_at,
                ),
            )
        return result

    def record_comparison(self, comparison: BrokerComparison) -> BrokerComparison:
        payload = comparison.model_dump()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_comparisons (
                    id, signal_id, symbol, strategy_name, primary_broker, comparison_broker,
                    primary_order_id, comparison_order_id, status, primary_fill_price,
                    comparison_fill_price, primary_cost_usd, comparison_cost_usd,
                    primary_slippage_bps, comparison_slippage_bps, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison.id,
                    comparison.signal_id,
                    comparison.symbol.upper(),
                    comparison.strategy_name,
                    comparison.primary_broker,
                    comparison.comparison_broker,
                    comparison.primary_order_id,
                    comparison.comparison_order_id,
                    comparison.status,
                    comparison.primary_fill_price,
                    comparison.comparison_fill_price,
                    comparison.primary_cost_usd,
                    comparison.comparison_cost_usd,
                    comparison.primary_slippage_bps,
                    comparison.comparison_slippage_bps,
                    json.dumps(payload["details"]),
                    comparison.created_at,
                ),
            )
        return comparison

    def update_comparison_fill(
        self,
        *,
        broker: str,
        broker_order_id: str,
        fill_price: float | None,
        cost_usd: float = 0.0,
        slippage_bps: float | None = None,
    ) -> None:
        if broker == "alpaca":
            match_column = "primary_order_id"
            fill_column = "primary_fill_price"
            cost_column = "primary_cost_usd"
            slippage_column = "primary_slippage_bps"
            completed_column = "comparison_fill_price"
        elif broker == "etoro":
            match_column = "comparison_order_id"
            fill_column = "comparison_fill_price"
            cost_column = "comparison_cost_usd"
            slippage_column = "comparison_slippage_bps"
            completed_column = "primary_fill_price"
        else:
            raise ValueError(f"Unsupported comparison broker {broker}")
        status_expression = (
            f"""
                    CASE
                        WHEN {completed_column} IS NOT NULL
                        THEN 'completed'
                        ELSE status
                    END
            """
            if fill_price is not None
            else "status"
        )
        with self.db.connect() as connection:
            connection.execute(
                f"""
                UPDATE broker_comparisons
                SET {fill_column} = ?,
                    {cost_column} = ?,
                    {slippage_column} = ?,
                    status = {status_expression}
                WHERE {match_column} = ?
                """,
                (fill_price, cost_usd, slippage_bps, broker_order_id),
            )

    def list_capabilities(self) -> list[dict[str, Any]]:
        return self._list_json(
            "SELECT * FROM broker_capabilities ORDER BY broker, account_mode",
            bool_fields={
                "supports_equities",
                "supports_native_protection",
                "supports_client_idempotency",
                "supports_shorting",
                "supports_borrow_checks",
                "supports_financing_costs",
                "verified",
            },
        )

    def list_identities(self) -> list[dict[str, Any]]:
        return self._list_json(
            "SELECT * FROM broker_account_identities ORDER BY broker, account_mode",
            bool_fields={"verified"},
        )

    def list_reconciliations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_json(
            "SELECT * FROM broker_reconciliation_results ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        )

    def list_comparisons(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_json(
            "SELECT * FROM broker_comparisons ORDER BY created_at DESC LIMIT ?",
            (max(1, limit),),
        )

    def _list_json(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        bool_fields: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        parsed = [dict(row) for row in rows]
        for item in parsed:
            for key in list(item):
                if key.endswith("_json"):
                    item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
            for key in bool_fields or set():
                item[key] = bool(item[key])
        return parsed


class EToroDemoIdempotencyRepository:
    """Durably reserve and complete eToro Demo mutation requests."""

    def __init__(self, db: Database):
        self.db = db

    def reserve(
        self,
        *,
        client_order_id: str,
        request_id: str,
        request_hash: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO etoro_demo_order_requests (
                    client_order_id, request_id, request_hash, request_json,
                    broker_order_id, status, response_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, 'reserved', NULL, ?, ?)
                ON CONFLICT(client_order_id) DO NOTHING
                """,
                (
                    client_order_id,
                    request_id,
                    request_hash,
                    json.dumps(request_payload, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM etoro_demo_order_requests WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("eToro Demo idempotency reservation was not persisted")
        result = dict(row)
        result["is_new"] = cursor.rowcount > 0
        result["request_payload"] = json.loads(result.pop("request_json") or "{}")
        result["response"] = json.loads(result.pop("response_json") or "null")
        return result

    def complete(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        status: str,
        response: dict[str, Any],
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE etoro_demo_order_requests
                SET broker_order_id = ?, status = ?, response_json = ?, updated_at = ?
                WHERE client_order_id = ?
                """,
                (
                    broker_order_id,
                    status,
                    json.dumps(response),
                    utc_now().isoformat(),
                    client_order_id,
                ),
            )

    def list(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM etoro_demo_order_requests
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["request_payload"] = json.loads(item.pop("request_json") or "{}")
            item["response"] = json.loads(item.pop("response_json") or "null")
            results.append(item)
        return results


class ExtendedHoursExperimentRepository:
    """Persist supervised extended-hours experiment evidence."""

    OPEN_STATUSES = {
        "submitted",
        "accepted",
        "new",
        "partially_filled",
        "filled",
        "pending_exit",
        "exit_submitted",
    }
    EXPIRABLE_STATUSES = {"submitted", "accepted", "new"}

    def __init__(self, db: Database):
        self.db = db

    def create_order(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        payload = {
            "id": item["id"],
            "broker": item.get("broker", "alpaca"),
            "symbol": str(item["symbol"]).upper(),
            "side": item.get("side", "buy"),
            "qty": float(item.get("qty") or 0.0),
            "limit_price": float(item.get("limit_price") or 0.0),
            "notional_usd": float(item.get("notional_usd") or 0.0),
            "status": item.get("status", "created"),
            "client_order_id": item.get("client_order_id"),
            "broker_order_id": item.get("broker_order_id"),
            "quote_json": json.dumps(item.get("quote") or {}),
            "spread_bps": item.get("spread_bps"),
            "quote_age_seconds": item.get("quote_age_seconds"),
            "fill_price": item.get("fill_price"),
            "filled_qty": float(item.get("filled_qty") or 0.0),
            "exit_client_order_id": item.get("exit_client_order_id"),
            "exit_broker_order_id": item.get("exit_broker_order_id"),
            "exit_limit_price": item.get("exit_limit_price"),
            "exit_fill_price": item.get("exit_fill_price"),
            "realized_pnl_usd": item.get("realized_pnl_usd"),
            "operator": item.get("operator"),
            "failure_reason": item.get("failure_reason"),
            "created_at": item.get("created_at") or now,
            "updated_at": item.get("updated_at") or now,
            "submitted_at": item.get("submitted_at"),
            "filled_at": item.get("filled_at"),
            "canceled_at": item.get("canceled_at"),
            "closed_at": item.get("closed_at"),
            "expires_at": item.get("expires_at"),
        }
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO extended_hours_experiment_orders (
                    id, broker, symbol, side, qty, limit_price, notional_usd,
                    status, client_order_id, broker_order_id, quote_json, spread_bps,
                    quote_age_seconds, fill_price, filled_qty, exit_client_order_id,
                    exit_broker_order_id, exit_limit_price, exit_fill_price,
                    realized_pnl_usd, operator, failure_reason, created_at, updated_at,
                    submitted_at, filled_at, canceled_at, closed_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(payload[key] for key in payload),
            )
        return self.get_order(payload["id"]) or payload

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM extended_hours_experiment_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        return self._order(row) if row is not None else None

    def get_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM extended_hours_experiment_orders
                WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
        return self._order(row) if row is not None else None

    def update_order(self, order_id: str, **updates: Any) -> dict[str, Any]:
        if not updates:
            existing = self.get_order(order_id)
            if existing is None:
                raise LookupError(order_id)
            return existing
        allowed = {
            "status",
            "broker_order_id",
            "fill_price",
            "filled_qty",
            "exit_client_order_id",
            "exit_broker_order_id",
            "exit_limit_price",
            "exit_fill_price",
            "realized_pnl_usd",
            "failure_reason",
            "submitted_at",
            "filled_at",
            "canceled_at",
            "closed_at",
            "expires_at",
        }
        invalid = sorted(set(updates) - allowed)
        if invalid:
            raise ValueError(f"Unsupported extended-hours order update fields: {invalid}")
        updates["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [order_id]
        with self.db.connect() as connection:
            connection.execute(
                f"UPDATE extended_hours_experiment_orders SET {assignments} WHERE id = ?",
                values,
            )
        updated = self.get_order(order_id)
        if updated is None:
            raise LookupError(order_id)
        return updated

    def list_orders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM extended_hours_experiment_orders
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [self._order(row) for row in rows]

    def list_expired_open_orders(self, *, now_iso: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in self.EXPIRABLE_STATUSES)
        params = [*sorted(self.EXPIRABLE_STATUSES), now_iso]
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM extended_hours_experiment_orders
                WHERE status IN ({placeholders})
                AND broker_order_id IS NOT NULL
                AND expires_at IS NOT NULL
                AND expires_at <= ?
                """,
                params,
            ).fetchall()
        return [self._order(row) for row in rows]

    def open_order_count(self) -> int:
        placeholders = ",".join("?" for _ in self.OPEN_STATUSES)
        with self.db.connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM extended_hours_experiment_orders
                WHERE status IN ({placeholders})
                """,
                tuple(sorted(self.OPEN_STATUSES)),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def record_etoro_probe(
        self,
        *,
        probe_id: str,
        status: str,
        classification: str,
        account_verified: bool,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO extended_hours_etoro_probes (
                    id, status, classification, account_verified, evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    probe_id,
                    status,
                    classification,
                    1 if account_verified else 0,
                    json.dumps(evidence),
                    created_at,
                ),
            )
        return {
            "id": probe_id,
            "status": status,
            "classification": classification,
            "account_verified": account_verified,
            "evidence": evidence,
            "created_at": created_at,
        }

    def list_etoro_probes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM extended_hours_etoro_probes
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [self._etoro_probe(row) for row in rows]

    @staticmethod
    def _order(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["quote"] = json.loads(item.pop("quote_json") or "{}")
        return item

    @staticmethod
    def _etoro_probe(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["account_verified"] = bool(item.get("account_verified"))
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        return item


class PortfolioRiskRepository:
    """Persist portfolio-level risk snapshots."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, snapshot: PortfolioRiskSnapshot) -> PortfolioRiskSnapshot:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO portfolio_risk_snapshots (
                    id, broker, equity_usd, peak_equity_usd, drawdown_pct,
                    gross_exposure_pct, largest_symbol_exposure_pct,
                    largest_sector_exposure_pct, largest_correlated_exposure_pct,
                    open_positions, status, blockers_json, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.broker,
                    snapshot.equity_usd,
                    snapshot.peak_equity_usd,
                    snapshot.drawdown_pct,
                    snapshot.gross_exposure_pct,
                    snapshot.largest_symbol_exposure_pct,
                    snapshot.largest_sector_exposure_pct,
                    snapshot.largest_correlated_exposure_pct,
                    snapshot.open_positions,
                    snapshot.status,
                    json.dumps(snapshot.blockers),
                    json.dumps(snapshot.details),
                    snapshot.created_at,
                ),
            )
        return snapshot

    def latest(self) -> PortfolioRiskSnapshot | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_risk_snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["blockers"] = json.loads(payload.pop("blockers_json") or "[]")
        payload["details"] = json.loads(payload.pop("details_json") or "{}")
        return PortfolioRiskSnapshot.model_validate(payload)


class RolloutGateRepository:
    """Persist signed rollout-gate evidence."""

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, gate: RolloutGateEvidence) -> RolloutGateEvidence:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO rollout_gate_evidence (
                    id, stage, gate_name, status, evidence_json, signed_by, observed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage, gate_name)
                DO UPDATE SET
                    id = excluded.id,
                    status = excluded.status,
                    evidence_json = excluded.evidence_json,
                    signed_by = excluded.signed_by,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    gate.id,
                    gate.stage,
                    gate.gate_name,
                    gate.status,
                    json.dumps(gate.evidence),
                    gate.signed_by,
                    gate.observed_at,
                    gate.updated_at,
                ),
            )
        return gate

    def list(self, *, stage: str | None = None) -> list[RolloutGateEvidence]:
        query = "SELECT * FROM rollout_gate_evidence"
        params: tuple[Any, ...] = ()
        if stage:
            query += " WHERE stage = ?"
            params = (stage,)
        query += " ORDER BY stage, gate_name"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        gates = []
        for row in rows:
            payload = dict(row)
            payload["evidence"] = json.loads(payload.pop("evidence_json") or "{}")
            gates.append(RolloutGateEvidence.model_validate(payload))
        return gates


class BrokerOrderSnapshotRepository:
    """Persist broker order and bracket-leg snapshots."""

    def __init__(self, db: Database):
        self.db = db

    def upsert(
        self,
        *,
        broker_order_id: str,
        execution_id: str | None,
        client_order_id: str | None,
        symbol: str,
        side: str,
        order_class: str,
        status: str,
        filled_qty: float,
        filled_avg_price: float | None,
        parent_order_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_order_snapshots (
                    broker_order_id, execution_id, client_order_id, symbol, side,
                    order_class, status, filled_qty, filled_avg_price, parent_order_id,
                    payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker_order_id)
                DO UPDATE SET
                    execution_id = excluded.execution_id,
                    client_order_id = excluded.client_order_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    order_class = excluded.order_class,
                    status = excluded.status,
                    filled_qty = excluded.filled_qty,
                    filled_avg_price = excluded.filled_avg_price,
                    parent_order_id = excluded.parent_order_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    broker_order_id,
                    execution_id,
                    client_order_id,
                    symbol.upper(),
                    side,
                    order_class,
                    status,
                    filled_qty,
                    filled_avg_price,
                    parent_order_id,
                    json.dumps(payload),
                    now,
                    now,
                ),
            )

    def list(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM broker_order_snapshots ORDER BY updated_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]


class BrokerPositionSnapshotRepository:
    """Persist the latest reconciled Alpaca position state."""

    def __init__(self, db: Database):
        self.db = db

    def replace_active(self, *, account_number: str, positions: list[Any]) -> None:
        now = utc_now().isoformat()
        active_symbols: list[str] = []
        with self.db.connect() as connection:
            for position in positions:
                payload = position.model_dump() if hasattr(position, "model_dump") else dict(position)
                symbol = str(payload.get("symbol") or "").upper()
                if not symbol:
                    continue
                active_symbols.append(symbol)
                connection.execute(
                    """
                    INSERT INTO broker_position_snapshots (
                        symbol, account_number, quantity, average_price, market_value,
                        unrealized_pnl, active, payload_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(symbol)
                    DO UPDATE SET
                        account_number = excluded.account_number,
                        quantity = excluded.quantity,
                        average_price = excluded.average_price,
                        market_value = excluded.market_value,
                        unrealized_pnl = excluded.unrealized_pnl,
                        active = 1,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        symbol,
                        account_number,
                        float(payload.get("quantity") or 0.0),
                        float(payload.get("average_price") or 0.0),
                        float(payload.get("market_value") or 0.0),
                        float(payload.get("unrealized_pnl") or 0.0),
                        json.dumps(payload),
                        now,
                        now,
                    ),
                )
            if active_symbols:
                placeholders = ",".join("?" for _ in active_symbols)
                connection.execute(
                    f"""
                    UPDATE broker_position_snapshots
                    SET active = 0, updated_at = ?
                    WHERE active = 1 AND symbol NOT IN ({placeholders})
                    """,
                    (now, *active_symbols),
                )
            else:
                connection.execute(
                    "UPDATE broker_position_snapshots SET active = 0, updated_at = ? WHERE active = 1",
                    (now,),
                )

    def list_active(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM broker_position_snapshots WHERE active = 1 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]


class SafetyStateRepository:
    """Persist reconciliation, blacklist, and strategy-health safety state."""

    def __init__(self, db: Database):
        self.db = db

    def record_reconciliation(
        self,
        *,
        status: str,
        account_number: str,
        orders_seen: int,
        positions_seen: int,
        issues: list[str],
        account: dict[str, Any],
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO reconciliation_runs (
                    status, account_number, orders_seen, positions_seen,
                    issues_json, account_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    status,
                    account_number,
                    orders_seen,
                    positions_seen,
                    json.dumps(issues),
                    json.dumps(account),
                    utc_now().isoformat(),
                ),
            )

    def latest_reconciliation(self) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else dict(row)

    def list_reconciliations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def blacklist(self, symbol: str, *, reason: str) -> None:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO instrument_blacklist (symbol, reason, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(symbol)
                DO UPDATE SET reason = excluded.reason, active = 1, updated_at = excluded.updated_at
                """,
                (symbol.upper(), reason, now, now),
            )

    def unblacklist(self, symbol: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE instrument_blacklist SET active = 0, updated_at = ? WHERE symbol = ?",
                (utc_now().isoformat(), symbol.upper()),
            )

    def is_blacklisted(self, symbol: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM instrument_blacklist WHERE symbol = ? AND active = 1",
                (symbol.upper(),),
            ).fetchone()
        return row is not None

    def list_blacklist(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instrument_blacklist WHERE active = 1 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_strategy_health(
        self,
        *,
        strategy_name: str,
        active: bool,
        closed_trades: int,
        expectancy_usd: float,
        profit_factor: float,
        reason: str,
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_health (
                    strategy_name, active, closed_trades, expectancy_usd,
                    profit_factor, reason, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name)
                DO UPDATE SET
                    active = excluded.active,
                    closed_trades = excluded.closed_trades,
                    expectancy_usd = excluded.expectancy_usd,
                    profit_factor = excluded.profit_factor,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    strategy_name,
                    1 if active else 0,
                    closed_trades,
                    expectancy_usd,
                    profit_factor,
                    reason,
                    utc_now().isoformat(),
                ),
            )

    def strategy_active(self, strategy_name: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT active FROM strategy_health WHERE strategy_name = ?",
                (strategy_name,),
            ).fetchone()
        return row is None or bool(row["active"])

    def list_strategy_health(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_health ORDER BY strategy_name"
            ).fetchall()
        return [dict(row) for row in rows]


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
        losers = [trade for trade in trades if trade.realized_pnl_usd < 0]
        realized_pnl = round(sum(trade.realized_pnl_usd for trade in trades), 2)
        unrealized_pnl = round(sum(position.unrealized_pnl_usd for position in open_positions), 2)
        expectancy = round(realized_pnl / total_trades, 2) if total_trades else 0.0
        gross_profit = sum(trade.realized_pnl_usd for trade in winners)
        gross_loss = abs(sum(trade.realized_pnl_usd for trade in losers))
        equity_curve = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade in sorted(trades, key=lambda item: item.closed_at):
            equity_curve += trade.realized_pnl_usd
            peak = max(peak, equity_curve)
            max_drawdown = min(max_drawdown, equity_curve - peak)
        average_rr = [
            float((trade.payload or {}).get("risk_reward_ratio") or 0.0)
            for trade in trades
            if float((trade.payload or {}).get("risk_reward_ratio") or 0.0) > 0.0
        ]
        r_values = [
            float((trade.payload or {}).get("realized_r_multiple") or 0.0)
            for trade in trades
            if (trade.payload or {}).get("realized_r_multiple") not in (None, "")
        ]
        rejection_counts = rejection_reason_counts or {}
        return PaperPerformanceSummary(
            total_trades=total_trades,
            open_positions=len(open_positions),
            win_rate=round((len(winners) / total_trades) * 100.0, 2) if total_trades else 0.0,
            profit_factor=round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit else 0.0),
            realized_pnl_usd=realized_pnl,
            unrealized_pnl_usd=unrealized_pnl,
            expectancy_usd=expectancy,
            average_reward_to_risk=round(sum(average_rr) / len(average_rr), 2) if average_rr else 0.0,
            average_r_multiple=round(sum(r_values) / len(r_values), 2) if r_values else 0.0,
            max_drawdown_usd=round(abs(max_drawdown), 2),
            watchlist_signals=int(rejection_counts.get("watchlist", 0) or 0),
            trigger_ready_signals=int(rejection_counts.get("trigger_ready", 0) or 0),
            execution_ready_signals=int(rejection_counts.get("execution_ready", 0) or 0),
            pnl_by_timeframe=_sum_by_key(trades, "timeframe"),
            pnl_by_strategy=_sum_by_key(trades, "strategy_name"),
            pnl_by_symbol=_sum_by_key(trades, "symbol"),
            pnl_by_regime=_sum_by_key(trades, "regime_label"),
            rejection_reason_counts=rejection_counts,
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
