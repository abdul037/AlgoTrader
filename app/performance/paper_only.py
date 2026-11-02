"""Paper-only entries on the main paper account (operator option B, 2026-10-21): what they are.

An entry the main paper gate admits ONLY through the paper OOS bar
(``strategy_evidence.paper_oos_only``: its pooled OOS evidence passes, its holdout does not) is
paper-only: the eToro live mirror refuses it (``etoro_live_paper_only``). Its proposal and
order carry ``PAPER_ONLY_FIELD`` and ``PAPER_ONLY_REASON_FIELD`` (app/workflow/paper_only.py);
the order's metadata rides into the execution's request payload, and the paper ledger copies
the tags into its position and closed trade (``paper_only_tags``).

Paper-only entries are automatic only: a person cannot approve one (``AUTOMATIC_ONLY``,
refused by ``ProposalService.approve_proposal`` for any reviewer but ``AUTO_REVIEWER``).

Paper-only results stay in paper's trades and positions and in the learning/outcome tables
(the learning meta-model trains on them; it can only lower a candidate's score or block it,
never make one eligible), but feed none of the decisions keyed or judged by what
eToro-copyable entries earn:

* the strategy-health deactivation and the Phase 3 auto-demotion, both keyed by strategy NAME
  across timeframes (paper-only losses on X:1d must not switch off X's eToro-copyable entries,
  nor paper-only wins keep a losing X in the scan), and the tier-3 qualification, also by name;
* the Phase 3 go-live readiness: its trade count, profit factor and R scorecard leave them out
  and its drawdown is read net of their P&L, so the Phase 4 live lock reading it is unmoved;
  and the Stage 3 real-capital record and gates (app/performance/stage3.py).

They are also left out of the auto-approval clean-lifecycle bootstrap (``reliability``: it
unlocks auto-approval of eToro-copyable entries) and ``GET /performance/weekly-target-readiness``.

``paper_only_report`` shows them on their own in ``GET /performance/live-scorecard`` (open
positions, and those opened in the last ``REPORT_WINDOW_DAYS`` days).

The paper ledger stores a position's payload with ``json.dumps``, so a tagged one holds exactly
``"paper_only": true``; ``TAGGED_PAYLOAD_SQL`` matches that fragment (``_`` escaped) as a
prefilter, and every reader parses the payload before it counts a row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

PAPER_ONLY_FIELD = "paper_only"
PAPER_ONLY_REASON_FIELD = "paper_only_reason"
OOS_ONLY = "oos_only"  # admitted on the pooled OOS evidence alone; the holdout failed
TAGGED_PAYLOAD_SQL = "payload_json LIKE ? ESCAPE '!'"  # with TAGGED_PAYLOAD_PATTERN
TAGGED_PAYLOAD_PATTERN = '%"paper!_only": true%'  # the tag as json.dumps writes it
REPORT_WINDOW_DAYS = 90
AUTO_REVIEWER = "paper_auto"  # the unattended path's approvals (app/automation/unattended.py)
AUTOMATIC_ONLY = "Paper-only entries are automatic only: they cannot be approved by hand."


def is_paper_only(metadata: Any) -> bool:
    """True for order, request or ledger metadata tagged paper-only."""

    return isinstance(metadata, dict) and bool(metadata.get(PAPER_ONLY_FIELD))


def request_is_paper_only(request: Any) -> bool:
    """``is_paper_only`` for an execution's request payload (the order dump with ``metadata``)."""

    return isinstance(request, dict) and is_paper_only(request.get("metadata"))


def lifecycle_is_paper_only(lifecycle: Any) -> bool:
    """``is_paper_only`` for a paper trade lifecycle (its execution's request payload)."""

    payload = getattr(getattr(lifecycle, "execution", None), "payload", None)
    return isinstance(payload, dict) and request_is_paper_only(payload.get("request"))


def without_paper_only(trades: Iterable[Any]) -> list[Any]:
    """The paper ledger trades or positions that are not paper-only (their ``payload``)."""

    return [trade for trade in trades if not is_paper_only(getattr(trade, "payload", None))]


def paper_only_tags(metadata: Any) -> dict[str, Any]:
    """The tags a paper ledger position copies from its order metadata (they ride into its
    closed trade's payload); {} for an entry that is not paper-only."""

    if not is_paper_only(metadata):
        return {}
    return {PAPER_ONLY_FIELD: True, PAPER_ONLY_REASON_FIELD: metadata.get(PAPER_ONLY_REASON_FIELD)}


def paper_only_report(db: Any, *, now: Any = None) -> dict[str, Any]:
    """Paper-only positions from the paper ledger, open or opened in the last
    ``REPORT_WINDOW_DAYS`` days: entries, open, closed and the closed ones' mean R per
    ``strategy:timeframe`` (R = the move from entry over the entry-to-stop risk)."""

    since = ((now or utc_now()) - timedelta(days=REPORT_WINDOW_DAYS)).isoformat()
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT strategy_name, timeframe, side, status, entry_price, current_price, stop_loss, "
            f"payload_json FROM paper_positions WHERE {TAGGED_PAYLOAD_SQL} "
            "AND (status = 'open' OR opened_at >= ?)",
            (TAGGED_PAYLOAD_PATTERN, since),
        ).fetchall()
    cards: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not is_paper_only(payload):
            continue
        card = cards.setdefault(
            f"{row['strategy_name']}:{row['timeframe']}",
            {"entries": 0, "open": 0, "closed": 0, "r": []},
        )
        card["entries"] += 1
        if str(row["status"] or "").lower() == "open":
            card["open"] += 1
            continue
        card["closed"] += 1
        risk = abs(float(row["entry_price"] or 0.0) - float(row["stop_loss"] or 0.0))
        if row["stop_loss"] is not None and risk > 0:
            move = float(row["current_price"] or 0.0) - float(row["entry_price"] or 0.0)
            card["r"].append(-move / risk if str(row["side"]).lower() == "sell" else move / risk)
    for card in cards.values():
        rs = card.pop("r")
        card["mean_r"] = round(sum(rs) / len(rs), 3) if rs else None
    return {
        "window_days": REPORT_WINDOW_DAYS,
        "entries": sum(card["entries"] for card in cards.values()),
        "open": sum(card["open"] for card in cards.values()),
        "closed": sum(card["closed"] for card in cards.values()),
        "by_strategy": dict(sorted(cards.items())),
    }
