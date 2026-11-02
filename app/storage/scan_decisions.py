"""Scan-decision persistence (re-exported by ``app.storage.repositories``)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
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
_MIN_SQLITE_VERSION = (3, 35, 0)  # INSERT ... RETURNING


class ScanDecisionRepository:
    """Persist per-candidate scan decisions for review and anti-spam logic.

    A scan writes one row per symbol/strategy/timeframe it evaluates. Its no_signal
    diagnostics (whose stored record no caller reads back) go through
    ``create_deferred``: inside ``deferred_writes()``, which the screener opens around
    each scan, they are queued and written by ``flush`` as one multi-row INSERT.

    The queue belongs to the scan's thread and only that thread writes it: before
    each of its own reads and immediate inserts, when it queues a row for another
    symbol, after each symbol, and when the scan ends. So a failed write raises in
    the scan that made the rows, as each one-row insert did, and every read in the
    scan sees the rows (and ids) it saw before. A reader or scan in another thread
    never writes, waits for, fails on or drops another scan's rows; it sees them
    once that scan writes them, at most one symbol later. A failed write keeps the
    rows queued, so the scan's next write retries them and raises again if it also
    fails; the rows still queued when the scan's last write fails are dropped with
    that error raised (a scan that had already failed keeps its own error). A crash
    loses at most the symbol being scanned. Outside ``deferred_writes()``
    ``create_deferred`` writes at once.
    """

    def __init__(self, db: Database):
        if getattr(db, "is_sqlite", False) and sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
            raise RuntimeError(f"scan_decisions needs SQLite 3.35+ (INSERT ... RETURNING); found {sqlite3.sqlite_version}")
        self.db = db
        self._thread = threading.local()  # .pending: this thread's scan queue (None outside a scan)

    @contextmanager
    def deferred_writes(self) -> Iterator[None]:
        """Queue this thread's ``create_deferred`` rows for one scan and write them as it ends."""

        outermost = self._pending() is None
        if outermost:
            self._thread.pending = []
        try:
            yield
        except BaseException:
            with suppress(Exception):  # never replace the scan's own error
                self.flush()
            raise
        else:
            self.flush()
        finally:
            if outermost:
                self._thread.pending = None  # rows whose last write failed, which raised

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
        """Insert one decision and return the row as stored (``RETURNING *``, no re-read)."""

        params = _row_params(
            scan_task=scan_task, symbol=symbol, strategy_name=strategy_name, timeframe=timeframe,
            status=status, final_score=final_score, alert_eligible=alert_eligible, freshness=freshness,
            reason_codes=reason_codes, rejection_reasons=rejection_reasons, payload=payload,
        )
        self.flush()
        return self._insert(params)

    def create_deferred(
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
    ) -> None:
        """Queue a decision for this thread's scan; same fields and stored row as ``create``."""

        params = _row_params(
            scan_task=scan_task, symbol=symbol, strategy_name=strategy_name, timeframe=timeframe,
            status=status, final_score=final_score, alert_eligible=alert_eligible, freshness=freshness,
            reason_codes=reason_codes, rejection_reasons=rejection_reasons, payload=payload,
        )
        pending = self._pending()
        if pending is None:  # not inside a scan: write it now
            self._insert(params)
            return
        if pending and pending[-1][1] != params[1]:
            self.flush()  # the previous symbol is done: write its rows now
        pending.append(params)

    def flush(self) -> None:
        """Write this thread's queued decisions, in queue order (so ids keep that order).

        One transaction: if it fails the rows stay queued and the error is raised here,
        in the scan that queued them. Once committed, each stored row is parsed into a
        ScanDecisionRecord, as the old per-row re-read did, so a value the record
        rejects still raises in the scan with the row stored.
        """

        pending = self._pending()
        if not pending:
            return
        stored: list[Any] = []
        with self.db.connect() as connection:
            for start in range(0, len(pending), _FLUSH_CHUNK_ROWS):
                chunk = pending[start : start + _FLUSH_CHUNK_ROWS]
                values = ", ".join([_ROW_PLACEHOLDERS] * len(chunk))
                # Read to the end: SQLite cannot commit a half-read RETURNING.
                stored.extend(
                    connection.execute(
                        f"INSERT INTO scan_decisions ({_COLUMNS}) VALUES {values} RETURNING *",
                        tuple(value for row in chunk for value in row),
                    ).fetchall()
                )
        pending.clear()
        for row in stored:
            self._row_to_model(row)

    def _pending(self) -> list[tuple[Any, ...]] | None:
        return getattr(self._thread, "pending", None)

    def _insert(self, params: tuple[Any, ...]) -> ScanDecisionRecord:
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
    payload: dict[str, Any] | None,
) -> tuple[Any, ...]:
    # Serialised now, at the call, so later changes to the caller's dicts never reach
    # a queued row (and an unserialisable payload raises before anything is written).
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
