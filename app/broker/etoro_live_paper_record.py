"""eToro copies a strategy only after paper has shown it making money (operator 2026-11-01).

The eToro live copy (app/broker/etoro_live_mirror.py) refuses a strategy until its closed trades
on the main paper account number at least ``MIN_CLOSED_PAPER_TRADES`` with a total realized P&L
above zero ("3 or more closed paper trades with a positive total"). The record is kept per
strategy AND timeframe, the key the evidence gate decides on: pooled by name, September's
15-minute trades would have let ema_trend_stack's daily entries qualify while those were net
-$62 (one win, one loss). Paper-only trades (option B) do not count, as in every other decision
judged by what eToro-copyable entries earn (app/performance/paper_only.py).

Why (2026-11-01): etf_mega_cap_relative_strength_rotation was copied on 10-05, 10-07 and 10-14
before it had one closed paper trade. Its four paper trades then all lost (-$1,557) and its three
copies were eToro's three losing copies (-$280).

None of these reasons is a room reason (app/broker/etoro_live_room.py), so paper's own entry
goes ahead and builds the record. An unreadable ledger, or a trade whose P&L is not a finite
number, refuses the copy (``UNKNOWN``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

MIN_CLOSED_PAPER_TRADES = 3
RECORD_ROWS = 2000  # the latest closed trades, as the paper ledger's own summary reads them
TOO_FEW = "etoro_live_paper_record_too_few"
NOT_POSITIVE = "etoro_live_paper_record_not_positive"
UNKNOWN = "etoro_live_paper_record_unknown"

Records = dict[str, tuple[int, float]]


def record_key(strategy: Any, timeframe: Any) -> str:
    """``strategy:timeframe`` in lower case, as the evidence verdicts are keyed."""

    return f"{str(strategy or '').strip().lower()}:{str(timeframe or '').strip().lower()}"


def paper_records(trades: Iterable[Any]) -> Records:
    """(closed trades, total realized P&L) per ``record_key``, paper-only trades left out. A P&L
    that is not a finite number makes that key's total NaN (unknown)."""

    from app.performance.paper_only import without_paper_only

    records: Records = {}
    for trade in without_paper_only(trades):
        if not str(getattr(trade, "strategy_name", "") or "").strip():
            continue
        key = record_key(trade.strategy_name, getattr(trade, "timeframe", None))
        pnl = getattr(trade, "realized_pnl_usd", None)
        pnl = float(pnl) if isinstance(pnl, (int, float)) and not isinstance(pnl, bool) else math.nan
        count, total = records.get(key, (0, 0.0))
        records[key] = (count + 1, total + pnl)
    return records


def read_paper_records(repository: Any) -> Records | None:
    """The paper ledger's records, or None when there is no ledger or it cannot be read."""

    if repository is None:
        return None
    try:
        return paper_records(repository.list(limit=RECORD_ROWS))
    except Exception:  # noqa: BLE001 - an unreadable ledger refuses the copy
        return None


def paper_record_blocker(records: Records | None, strategy: Any, timeframe: Any) -> str | None:
    """None when ``strategy`` on ``timeframe`` has the record a copy needs, else why not."""

    if records is None:
        return UNKNOWN
    count, total = records.get(record_key(strategy, timeframe), (0, 0.0))
    if not math.isfinite(total):
        return UNKNOWN
    if count < MIN_CLOSED_PAPER_TRADES:
        return TOO_FEW
    if not total > 0:
        return NOT_POSITIVE
    return None
