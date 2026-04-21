"""SQLite persistence for portfolio snapshots and signal outcomes."""

from __future__ import annotations

from collections import defaultdict
import json
from datetime import timedelta
from typing import Any

from app.storage.db import Database
from app.utils.time import utc_now


class LedgerRepository:
    """Persistence layer for the outcome ledger."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def insert_snapshot(
        self,
        *,
        snapshot_ts: str,
        positions: list[dict[str, Any]],
        credit: float | None,
        unrealized_pnl_usd: float | None,
        raw_payload: dict[str, Any] | None = None,
    ) -> int:
        """Persist a single portfolio snapshot. Returns the new row id."""
        created_at = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO portfolio_snapshots (
                    snapshot_ts, position_count, credit, unrealized_pnl_usd,
                    positions_json, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_ts,
                    len(positions),
                    credit,
                    unrealized_pnl_usd,
                    json.dumps(positions, default=str),
                    json.dumps(raw_payload, default=str) if raw_payload is not None else None,
                    created_at,
                ),
            )
            return int(cursor.lastrowid or 0)

    def latest_snapshot(self) -> dict[str, Any] | None:
        """Return the most recent portfolio snapshot, or None."""
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY snapshot_ts DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self._row_to_snapshot(dict(row))

    def snapshot_before(self, ts_iso: str) -> dict[str, Any] | None:
        """Return the most recent snapshot with snapshot_ts strictly before ts_iso."""
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_snapshots WHERE snapshot_ts < ? ORDER BY snapshot_ts DESC LIMIT 1",
                (ts_iso,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_snapshot(dict(row))

    @staticmethod
    def _row_to_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        positions_raw = row.get("positions_json") or "[]"
        try:
            row["positions"] = json.loads(positions_raw)
        except Exception:
            row["positions"] = []
        return row

    # ------------------------------------------------------------------
    # Signal outcomes
    # ------------------------------------------------------------------

    def insert_outcome(
        self,
        *,
        alert_source: str,
        alert_id: str | None,
        symbol: str,
        strategy_name: str | None,
        timeframe: str | None,
        alert_created_at: str,
        alert_entry_price: float | None,
        alert_stop: float | None,
        alert_target: float | None,
        alert_score: float | None,
        alert_payload: dict[str, Any] | None = None,
    ) -> int:
        """Record a new alert as a pending_match outcome."""
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_outcomes (
                    alert_source, alert_id, symbol, strategy_name, timeframe,
                    alert_created_at, alert_entry_price, alert_stop, alert_target,
                    alert_score, alert_payload_json, outcome_status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_match', ?, ?)
                """,
                (
                    alert_source,
                    alert_id,
                    symbol.upper(),
                    strategy_name,
                    timeframe,
                    alert_created_at,
                    alert_entry_price,
                    alert_stop,
                    alert_target,
                    alert_score,
                    json.dumps(alert_payload, default=str) if alert_payload else None,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def get_by_alert_id(self, alert_id: str) -> dict[str, Any] | None:
        """Return an outcome by alert_id, if one was already recorded."""
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM signal_outcomes WHERE alert_id = ? ORDER BY created_at DESC LIMIT 1",
                (alert_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def list_pending_match(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """List outcomes still waiting to be linked to a position."""
        query = "SELECT * FROM signal_outcomes WHERE outcome_status = 'pending_match'"
        params: tuple[Any, ...] = ()
        if symbol is not None:
            query += " AND symbol = ?"
            params = (symbol.upper(),)
        query += " ORDER BY alert_created_at ASC"
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def pending_match_count(self) -> int:
        """Count outcomes still waiting for an eToro position match."""
        with self.db.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM signal_outcomes WHERE outcome_status = 'pending_match'"
                ).fetchone()[0]
            )

    def pending_match_older_than_count(self, *, hours: int) -> int:
        """Count pending outcomes older than the supplied age."""
        cutoff = (utc_now() - timedelta(hours=max(hours, 0))).isoformat()
        with self.db.connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM signal_outcomes
                    WHERE outcome_status = 'pending_match'
                      AND alert_created_at < ?
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )

    def list_open_outcomes(self) -> list[dict[str, Any]]:
        """Outcomes that are matched to a live position but not yet closed."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signal_outcomes WHERE outcome_status = 'open' "
                "ORDER BY matched_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def position_already_matched(self, position_id: int) -> bool:
        """True if the given eToro positionID is already linked to any outcome."""
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM signal_outcomes WHERE matched_position_id = ? LIMIT 1",
                (position_id,),
            ).fetchone()
        return row is not None

    def mark_matched(
        self,
        outcome_id: int,
        *,
        position_id: int,
        position_open_at: str | None,
        position_open_rate: float,
        position_amount_usd: float | None,
        position_units: float,
        position_is_buy: bool,
        position_leverage: int,
        position_stop_loss_rate: float | None,
        position_take_profit_rate: float | None,
    ) -> None:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE signal_outcomes
                SET matched_position_id = ?,
                    matched_at = ?,
                    position_open_at = ?,
                    position_open_rate = ?,
                    position_amount_usd = ?,
                    position_units = ?,
                    position_is_buy = ?,
                    position_leverage = ?,
                    position_stop_loss_rate = ?,
                    position_take_profit_rate = ?,
                    outcome_status = 'open',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    position_id,
                    now,
                    position_open_at,
                    position_open_rate,
                    position_amount_usd,
                    position_units,
                    1 if position_is_buy else 0,
                    position_leverage,
                    position_stop_loss_rate,
                    position_take_profit_rate,
                    now,
                    outcome_id,
                ),
            )

    def mark_closed(
        self,
        outcome_id: int,
        *,
        closed_at: str,
        close_rate: float,
        realized_pnl_usd: float,
        realized_r_multiple: float | None,
        outcome_status: str,
        notes: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE signal_outcomes
                SET closed_at = ?,
                    close_rate = ?,
                    realized_pnl_usd = ?,
                    realized_r_multiple = ?,
                    outcome_status = ?,
                    notes = COALESCE(?, notes),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    closed_at,
                    close_rate,
                    realized_pnl_usd,
                    realized_r_multiple,
                    outcome_status,
                    notes,
                    now,
                    outcome_id,
                ),
            )

    def expire_stale_pending(self, alert_ts_before: str) -> int:
        """Mark pending_match outcomes older than alert_ts_before as expired_unmatched."""
        now = utc_now().isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE signal_outcomes
                SET outcome_status = 'expired_unmatched', updated_at = ?
                WHERE outcome_status = 'pending_match' AND alert_created_at < ?
                """,
                (now, alert_ts_before),
            )
            return cursor.rowcount or 0

    def recent_outcomes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signal_outcomes ORDER BY alert_created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary_stats(self) -> dict[str, Any]:
        """Aggregate ledger statistics for reports."""
        with self.db.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM signal_outcomes"
            ).fetchone()[0]
            counts_rows = connection.execute(
                "SELECT outcome_status, COUNT(*) AS c FROM signal_outcomes GROUP BY outcome_status"
            ).fetchall()
            closed_rows = connection.execute(
                """
                SELECT realized_pnl_usd, realized_r_multiple, position_open_at, closed_at
                FROM signal_outcomes
                WHERE outcome_status IN ('target_hit', 'stop_hit', 'closed_manual')
                  AND realized_pnl_usd IS NOT NULL
                """
            ).fetchall()
            strategy_rows = connection.execute(
                """
                SELECT
                    COALESCE(strategy_name, '-') AS strategy_name,
                    COUNT(*) AS total,
                    SUM(CASE WHEN outcome_status = 'pending_match' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN outcome_status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN outcome_status IN ('target_hit', 'stop_hit', 'closed_manual') THEN 1 ELSE 0 END) AS closed,
                    SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN realized_pnl_usd <= 0 AND realized_pnl_usd IS NOT NULL THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN realized_pnl_usd > 0 THEN realized_pnl_usd ELSE 0 END) AS gross_wins,
                    SUM(CASE WHEN realized_pnl_usd < 0 THEN realized_pnl_usd ELSE 0 END) AS gross_losses,
                    AVG(realized_r_multiple) AS avg_r_multiple,
                    AVG(
                        CASE
                            WHEN position_open_at IS NOT NULL AND closed_at IS NOT NULL
                            THEN (julianday(closed_at) - julianday(position_open_at)) * 24.0
                            ELSE NULL
                        END
                    ) AS avg_hold_hours
                FROM signal_outcomes
                GROUP BY COALESCE(strategy_name, '-')
                ORDER BY closed DESC, total DESC
                """
            ).fetchall()

        counts = {row[0]: row[1] for row in counts_rows}
        closed = [dict(row) for row in closed_rows]
        wins = [r for r in closed if (r.get("realized_pnl_usd") or 0) > 0]
        losses = [r for r in closed if (r.get("realized_pnl_usd") or 0) <= 0]
        total_pnl = sum((r.get("realized_pnl_usd") or 0) for r in closed)
        gross_wins = sum((r.get("realized_pnl_usd") or 0) for r in wins)
        gross_losses = sum((r.get("realized_pnl_usd") or 0) for r in losses)
        profit_factor = (
            gross_wins / abs(gross_losses)
            if gross_wins > 0 and gross_losses < 0
            else None
        )
        avg_r = (
            sum(r.get("realized_r_multiple") or 0 for r in closed) / len(closed)
            if closed
            else None
        )
        win_rate = (len(wins) / len(closed)) if closed else None
        hold_hours = [
            (r.get("closed_at"), r.get("position_open_at"))
            for r in closed
            if r.get("closed_at") and r.get("position_open_at")
        ]
        avg_hold_hours = None
        if hold_hours:
            with self.db.connect() as connection:
                avg_hold_hours = connection.execute(
                    """
                    SELECT AVG((julianday(closed_at) - julianday(position_open_at)) * 24.0)
                    FROM signal_outcomes
                    WHERE outcome_status IN ('target_hit', 'stop_hit', 'closed_manual')
                      AND position_open_at IS NOT NULL
                      AND closed_at IS NOT NULL
                    """
                ).fetchone()[0]

        by_strategy = []
        for row in strategy_rows:
            item = dict(row)
            item_gross_wins = float(item.get("gross_wins") or 0.0)
            item_gross_losses = float(item.get("gross_losses") or 0.0)
            closed_count = int(item.get("closed") or 0)
            item["win_rate"] = (
                int(item.get("wins") or 0) / closed_count
                if closed_count
                else None
            )
            item["profit_factor"] = (
                item_gross_wins / abs(item_gross_losses)
                if item_gross_wins > 0 and item_gross_losses < 0
                else None
            )
            by_strategy.append(item)

        return {
            "total_outcomes": total,
            "by_status": counts,
            "closed_count": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_realized_pnl_usd": total_pnl,
            "avg_r_multiple": avg_r,
            "avg_hold_hours": avg_hold_hours,
            "by_strategy": by_strategy,
        }

    def strategy_audit(self, *, min_closed: int = 20) -> dict[str, Any]:
        """Return strategy/symbol/timeframe/score-bucket quality diagnostics."""
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM signal_outcomes
                ORDER BY alert_created_at ASC
                """
            ).fetchall()

        outcomes = [dict(row) for row in rows]
        groups: dict[str, dict[str, list[dict[str, Any]]]] = {
            "strategies": defaultdict(list),
            "symbols": defaultdict(list),
            "timeframes": defaultdict(list),
            "score_buckets": defaultdict(list),
        }
        for row in outcomes:
            groups["strategies"][str(row.get("strategy_name") or "-")].append(row)
            groups["symbols"][str(row.get("symbol") or "-").upper()].append(row)
            groups["timeframes"][str(row.get("timeframe") or "-")].append(row)
            groups["score_buckets"][_score_bucket(row.get("alert_score"))].append(row)

        return {
            "generated_at": utc_now().isoformat(),
            "min_closed_for_decision": max(int(min_closed), 1),
            "overall": _audit_metrics("all", outcomes, min_closed=min_closed),
            "by_strategy": [
                _audit_metrics(name, items, min_closed=min_closed)
                for name, items in sorted(groups["strategies"].items())
            ],
            "by_symbol": [
                _audit_metrics(name, items, min_closed=min_closed)
                for name, items in sorted(groups["symbols"].items())
            ],
            "by_timeframe": [
                _audit_metrics(name, items, min_closed=min_closed)
                for name, items in sorted(groups["timeframes"].items())
            ],
            "by_score_bucket": [
                _audit_metrics(name, items, min_closed=min_closed)
                for name, items in sorted(
                    groups["score_buckets"].items(),
                    key=lambda item: _score_bucket_sort_key(item[0]),
                )
            ],
        }


CLOSED_OUTCOME_STATUSES = {"target_hit", "stop_hit", "closed_manual"}


def _audit_metrics(name: str, rows: list[dict[str, Any]], *, min_closed: int) -> dict[str, Any]:
    total = len(rows)
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("outcome_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    closed = [
        row
        for row in rows
        if str(row.get("outcome_status") or "") in CLOSED_OUTCOME_STATUSES
        and row.get("realized_pnl_usd") is not None
    ]
    wins = [row for row in closed if float(row.get("realized_pnl_usd") or 0.0) > 0.0]
    losses = [row for row in closed if float(row.get("realized_pnl_usd") or 0.0) <= 0.0]
    gross_wins = sum(float(row.get("realized_pnl_usd") or 0.0) for row in wins)
    gross_losses = sum(float(row.get("realized_pnl_usd") or 0.0) for row in losses)
    r_values = [
        float(row["realized_r_multiple"])
        for row in closed
        if row.get("realized_r_multiple") is not None
    ]
    matched_count = sum(
        1
        for row in rows
        if row.get("matched_position_id") is not None
        or str(row.get("outcome_status") or "") in CLOSED_OUTCOME_STATUSES
    )
    expired_count = int(status_counts.get("expired_unmatched", 0))

    win_rate = len(wins) / len(closed) if closed else None
    profit_factor = (
        gross_wins / abs(gross_losses)
        if gross_wins > 0.0 and gross_losses < 0.0
        else None
    )
    avg_r = sum(r_values) / len(r_values) if r_values else None
    avg_score = _avg([row.get("alert_score") for row in rows])

    recommendation, reason = _recommendation(
        closed_count=len(closed),
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_r=avg_r,
        min_closed=max(int(min_closed), 1),
        expired_count=expired_count,
        total=total,
    )
    return {
        "name": name,
        "total_alerts": total,
        "matched_count": matched_count,
        "match_rate": matched_count / total if total else None,
        "expired_unmatched": expired_count,
        "open_count": int(status_counts.get("open", 0)),
        "pending_match": int(status_counts.get("pending_match", 0)),
        "closed_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_r_multiple": avg_r,
        "avg_score": avg_score,
        "status_counts": status_counts,
        "recommendation": recommendation,
        "recommendation_reason": reason,
    }


def _recommendation(
    *,
    closed_count: int,
    win_rate: float | None,
    profit_factor: float | None,
    avg_r: float | None,
    min_closed: int,
    expired_count: int,
    total: int,
) -> tuple[str, str]:
    if total == 0:
        return "needs_more_data", "no alerts recorded yet"
    if closed_count < min_closed:
        return "needs_more_data", f"only {closed_count}/{min_closed} closed outcomes"
    if profit_factor is None:
        return "needs_more_data", "no realized losses yet, PF not stable"
    if profit_factor >= 1.5 and (win_rate or 0.0) >= 0.55 and (avg_r or 0.0) >= 0.3:
        return "keep", "meets PF, win-rate, and avg-R targets"
    if profit_factor < 1.0 or (win_rate is not None and win_rate < 0.45) or (avg_r is not None and avg_r < 0.0):
        return "disable_or_reduce", "negative expectancy or weak hit rate"
    if profit_factor < 1.5 or (win_rate is not None and win_rate < 0.55):
        return "reduce", "positive but below target quality"
    if expired_count / total > 0.5:
        return "reduce", "more than half of alerts expired unmatched"
    return "keep", "acceptable live ledger metrics"


def _score_bucket(value: Any) -> str:
    if value in (None, ""):
        return "no_score"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "no_score"
    if score < 65:
        return "<65"
    if score < 70:
        return "65-70"
    if score < 80:
        return "70-80"
    if score < 90:
        return "80-90"
    return "90+"


def _score_bucket_sort_key(bucket: str) -> int:
    order = {"<65": 0, "65-70": 1, "70-80": 2, "80-90": 3, "90+": 4, "no_score": 5}
    return order.get(bucket, 99)


def _avg(values: list[Any]) -> float | None:
    numeric = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(numeric) / len(numeric) if numeric else None
