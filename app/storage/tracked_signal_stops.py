"""Light read of recent tracked-signal stop hits for the overnight eToro LIVE alert
(``app/broker/etoro_live_overnight_alert.py``, operator 2026-10-18). Read-only.

``TrackedSignalRepository.list`` returns whole rows with the snapshot JSON (several KB each).
The alert, which reads at most every 5 minutes outside US hours, needs only the stop hits on
its few live symbols since the last close, so this selects just those rows.
"""

from __future__ import annotations

import json
from typing import Any


def recent_stop_hits(
    db: Any, *, symbols: list[str], since_iso: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Tracked signals closed ``stop_hit`` at or after ``since_iso`` on these symbols, newest
    first, with the snapshot's signal role and bid/ask at the close.

    ``since_iso`` is a UTC ISO timestamp, the format ``closed_at`` is written in
    (``utc_now().isoformat()``), so the text comparison orders by time on SQLite and Postgres.
    """

    wanted = sorted({str(s).strip().upper() for s in symbols if str(s or "").strip()})
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    query = (
        "SELECT symbol, strategy_name, timeframe, closed_at, stop_loss, last_price, payload_json"
        " FROM tracked_signals"
        f" WHERE status = 'stop_hit' AND closed_at >= ? AND symbol IN ({marks})"
        " ORDER BY closed_at DESC LIMIT ?"
    )
    with db.connect() as connection:
        rows = connection.execute(query, (since_iso, *wanted, max(1, int(limit)))).fetchall()
    return [_row(row) for row in rows]


def _row(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "symbol": row["symbol"],
        "strategy_name": row["strategy_name"],
        "timeframe": row["timeframe"],
        "closed_at": row["closed_at"],
        "stop_loss": row["stop_loss"],
        "last_price": row["last_price"],
        "signal_role": payload.get("signal_role") or metadata.get("signal_role") or "entry_long",
        "current_bid": payload.get("current_bid"),
        "current_ask": payload.get("current_ask"),
    }
