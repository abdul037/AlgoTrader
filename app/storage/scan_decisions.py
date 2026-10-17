"""Scan-decision persistence (re-exported by ``app.storage.repositories``)."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

from app.models.screener import ScanDecisionRecord
from app.storage.db import Database
from app.utils.time import utc_now

_COLUMNS = """
    scan_task, symbol, strategy_name, timeframe, status, final_score,
    alert_eligible, freshness, reason_codes_json, rejection_reasons_json,
    payload_json, created_at
"""
_ROW_PLACEHOLDERS = "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
# 80 rows x 12 columns = 960 bound parameters, inside every SQLite and Postgres limit.
_FLUSH_CHUNK_ROWS = 80


class ScanDecisionRepository:
    """Persist per-candidate scan decisions for review and anti-spam logic.

    A scan writes one row per symbol/strategy/timeframe it evaluates. The
    no_signal diagnostics (whose stored record no caller reads back) are queued
    with ``create_deferred`` and written in one multi-row INSERT by ``flush``.
    The queue is flushed before every read and every immediate insert, from any
    thread, when a row for another symbol is queued, and at the end of each scan,
    so every read through this repository sees exactly the rows (and ids) it saw
    when each row was written on its own. Only a process that dies mid-symbol
    loses that symbol's queued rows. ``RETURNING`` needs SQLite 3.35+.
    """

    def __init__(self, db: Database):
        self.db = db
        self._pending: list[tuple[Any, ...]] = []
        self._lock = threading.RLock()

    def create(self, **fields: Any) -> ScanDecisionRecord:
        """Insert one decision and return the row as stored (``RETURNING *``, no re-read)."""

        params = self._row_params(**fields)
        self.flush()
        with self.db.connect() as connection:
            row = connection.execute(
                f"""
                INSERT INTO scan_decisions ({_COLUMNS})
                VALUES {_ROW_PLACEHOLDERS}
                RETURNING *
                """,
                params,
            ).fetchone()
        return self._row_to_model(row)

    def create_deferred(self, **fields: Any) -> None:
        """Queue a decision for the next ``flush``; same fields and stored row as ``create``."""

        params = self._row_params(**fields)
        with self._lock:
            if self._pending and self._pending[-1][1] != params[1]:
                self.flush()  # the previous symbol is done: write its rows now
            self._pending.append(params)

    def flush(self) -> None:
        """Write every queued decision, in queue order (so ids keep that order).

        The lock is held for the write, so a concurrent reader waits for it. A failed
        write drops the queued rows and raises, as a failed one-row insert did.
        """

        with self._lock:
            pending, self._pending = self._pending, []
            if not pending:
                return
            with self.db.connect() as connection:
                for start in range(0, len(pending), _FLUSH_CHUNK_ROWS):
                    chunk = pending[start : start + _FLUSH_CHUNK_ROWS]
                    values = ", ".join([_ROW_PLACEHOLDERS] * len(chunk))
                    # RETURNING id (which the Postgres wrapper would append anyway) is
                    # read to the end: SQLite cannot commit a half-read RETURNING.
                    connection.execute(
                        f"INSERT INTO scan_decisions ({_COLUMNS}) VALUES {values} RETURNING id",
                        tuple(value for row in chunk for value in row),
                    ).fetchall()

    @staticmethod
    def _row_params(
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
    ) -> tuple[Any, ...]:
        # Serialised now, at the call, so later changes to the caller's dicts never
        # reach a queued row.
        created_at = utc_now().isoformat()
        return (
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
        )

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
        self.flush()
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
        self.flush()
        with self.db.connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        return None if row is None else self._row_to_model(row)

    def get(self, record_id: int) -> ScanDecisionRecord:
        self.flush()
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
