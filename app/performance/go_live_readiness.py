"""Phase 3 go-live readiness: the operator-agreed bar before real money (2026-10-03).

Measured from the start of Phase 3 (``go_live_phase3_start_date``, or the first
refresh after ``require_strategy_oos_evidence`` was switched on):

* at least ``go_live_min_closed_trades`` closed trades (default 50);
* profit factor (gross wins / gross losses) >= ``go_live_min_profit_factor`` (1.3);
* max drawdown from the equity peak < ``go_live_max_drawdown_pct`` (3%), tracked
  incrementally from broker reconciliation equity;
* ``go_live_min_clean_weeks`` (4) consecutive completed weeks with no safety
  incident (emergency stop, breaker pause, failed reconciliation, failed flatten).

It also keeps a per-strategy scorecard in R (P&L / planned risk). The report is
refreshed in maintenance, stored in ``runtime_state`` and logged; the Phase 4
live guard (``AutomationService``) refuses live trading until ``ready`` is true.

Paper-only trades (operator option B, 2026-10-21; app/performance/paper_only.py) count for none
of it: they are left out of the trade count, profit factor and scorecard
(``paper_only_trades_excluded`` says how many), and the drawdown reads each reconciled equity
reading NET of their P&L (the account's equity includes their fills: a paper-only win before a
dip raises the peak, one during it hides part of the dip, a loss deepens it). A paper-only
position counts from the first reading after it opened, at its booked P&L once closed and at
its marked P&L while open (the paper ledger's ``paper_positions``), so a reading taken while
one is open is netted with that position's mark at the refresh (30 minutes apart), and a fill
the ledger has not synced yet is netted from the refresh after it is.
``paper_only_pnl_netted_usd`` is the paper-only P&L taken out of the latest reading.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from itertools import accumulate
from typing import Any

from app.performance.paper_only import PAPER_ONLY_FIELD, is_paper_only, request_is_paper_only
from app.utils.time import utc_now

READINESS_KEY = "go_live:readiness"
PHASE3_START_KEY = "go_live:phase3_started_at"
EQUITY_STATE_KEY = "go_live:equity_watermark"
DRAWDOWN_BASIS = "net_of_paper_only"  # the watermark's equity: account equity minus paper-only P&L
INCIDENT_EVENTS = (
    "kill_switch_emergency_stop",
    "workflow_scheduler_paused",
    "alpaca_reconciliation_failed",
    "unprotected_position_flatten_failed",
)
_EQUITY_RE = re.compile(r'"equity":\s*([0-9.]+)')


def _phase3_start(settings: Any, runtime_state: Any) -> datetime | None:
    configured = str(getattr(settings, "go_live_phase3_start_date", "") or "").strip()
    if configured:
        return (
            datetime.fromisoformat(configured).replace(tzinfo=UTC)
            if "T" not in configured
            else datetime.fromisoformat(configured)
        )
    stored = runtime_state.get(PHASE3_START_KEY)
    if stored:
        return datetime.fromisoformat(stored)
    if bool(getattr(settings, "require_strategy_oos_evidence", False)):
        started = utc_now()
        runtime_state.set(PHASE3_START_KEY, started.isoformat())
        return started
    return None


def _closed_trades(connection: Any, since: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT created_at, realized_pnl_usd, request_json, response_json FROM executions "
        "WHERE realized_pnl_usd != 0 AND created_at >= ? ORDER BY created_at",
        (since,),
    ).fetchall()
    trades = []
    for row in rows:
        try:  # one malformed row must not stop every later readiness refresh
            request = json.loads(row["request_json"] or "{}")
            broker = dict(json.loads(row["response_json"] or "{}").get("broker_execution") or {})
        except (TypeError, ValueError, AttributeError):
            continue
        entry = float(broker.get("filled_avg_price") or request.get("proposed_price") or 0.0)
        qty = float(broker.get("filled_qty") or 0.0)
        stop = float(request.get("stop_loss") or 0.0)
        risk = abs(entry - stop) * qty if entry > 0 and stop > 0 and qty > 0 else 0.0
        pnl = float(row["realized_pnl_usd"] or 0.0)
        trades.append(
            {
                "strategy": str(request.get("strategy_name") or "unknown"),
                "timeframe": str(dict(request.get("metadata") or {}).get("timeframe") or ""),
                "pnl": pnl,
                "r": pnl / risk if risk > 0 else None,
                "paper_only": request_is_paper_only(request),
            }
        )
    return trades


def _when(value: Any) -> datetime:
    """A stored time as an aware datetime; naive is UTC, unreadable is the earliest time."""

    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _paper_only_marks(connection: Any) -> tuple[list[datetime], list[float]]:
    """(open times, running P&L) of the paper-only positions, oldest first: entry i of the
    running P&L sums every paper-only position opened at or before open time i (booked P&L
    once closed, marked P&L while open)."""

    rows = connection.execute(
        "SELECT opened_at, status, realized_pnl_usd, unrealized_pnl_usd, payload_json "
        "FROM paper_positions WHERE payload_json LIKE ?",
        (f"%{PAPER_ONLY_FIELD}%",),
    ).fetchall()
    marks: list[tuple[datetime, float]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {PAPER_ONLY_FIELD: True}  # it matched the tag; unreadable: net it anyway
        if not is_paper_only(payload):
            continue
        is_open = str(row["status"] or "").lower() == "open"
        pnl = float((row["unrealized_pnl_usd"] if is_open else row["realized_pnl_usd"]) or 0.0)
        marks.append((_when(row["opened_at"]), pnl))
    marks.sort(key=lambda mark: mark[0])
    return [when for when, _ in marks], list(accumulate(pnl for _, pnl in marks))


def _update_drawdown(connection: Any, runtime_state: Any, since: str) -> tuple[float, float]:
    """(max drawdown %, paper-only P&L netted out of the latest reading), tracked incrementally
    on the reconciled equity net of the paper-only P&L."""

    try:
        state = json.loads(runtime_state.get(EQUITY_STATE_KEY) or "{}")
    except (TypeError, ValueError):
        state = {}
    opened, running = _paper_only_marks(connection)
    if state.get("since") != since:
        state = {"since": since, "last_ts": "", "peak": 0.0, "max_dd_pct": 0.0}
    if state.get("basis") != DRAWDOWN_BASIS:
        # A watermark from before paper-only P&L was netted is kept only when no paper-only
        # position had opened by its last reading (it is then the same number); else rebuilt.
        if state["last_ts"] and opened and opened[0] <= _when(state["last_ts"]):
            state = {"since": since, "last_ts": "", "peak": 0.0, "max_dd_pct": 0.0}
        state["basis"] = DRAWDOWN_BASIS
    rows = connection.execute(
        "SELECT created_at, payload_json FROM run_logs WHERE event_type = ? AND created_at >= ? AND created_at > ? "
        "ORDER BY created_at LIMIT 20000",
        ("alpaca_reconciliation_ok", since, state["last_ts"]),
    ).fetchall()
    netted = float(state.get("paper_only_pnl_netted_usd") or 0.0)
    for row in rows:
        match = _EQUITY_RE.search(str(row["payload_json"] or ""))
        if match:
            index = bisect_right(opened, _when(row["created_at"]))
            netted = running[index - 1] if index else 0.0
            equity = float(match.group(1)) - netted
            state["peak"] = max(float(state["peak"]), equity)
            if state["peak"] > 0:
                state["max_dd_pct"] = max(
                    float(state["max_dd_pct"]), (state["peak"] - equity) / state["peak"] * 100.0
                )
        state["last_ts"] = str(row["created_at"])
    state["paper_only_pnl_netted_usd"] = round(netted, 2)
    runtime_state.set(EQUITY_STATE_KEY, json.dumps(state))
    return round(float(state["max_dd_pct"]), 4), round(netted, 2)


def _clean_weeks(connection: Any, start: datetime, now: datetime) -> int:
    """Consecutive completed ISO weeks (Mon-Sun), newest first, with no incident."""

    placeholders = ",".join("?" for _ in INCIDENT_EVENTS)
    rows = connection.execute(
        f"SELECT created_at FROM run_logs WHERE event_type IN ({placeholders}) AND created_at >= ?",
        (*INCIDENT_EVENTS, start.isoformat()),
    ).fetchall()
    incident_weeks = {
        datetime.fromisoformat(str(row["created_at"])).date().isocalendar()[:2] for row in rows
    }
    # Monday of the last completed week; only whole weeks inside Phase 3 count.
    week_start = now.date() - timedelta(days=now.date().weekday() + 7)
    clean = 0
    while week_start >= start.date() and week_start.isocalendar()[:2] not in incident_weeks:
        clean += 1
        week_start -= timedelta(days=7)
    return clean


def compute_readiness(
    db: Any, settings: Any, runtime_state: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utc_now()
    start = _phase3_start(settings, runtime_state)
    min_trades = int(getattr(settings, "go_live_min_closed_trades", 50) or 50)
    min_pf = float(getattr(settings, "go_live_min_profit_factor", 1.3) or 1.3)
    max_dd = float(getattr(settings, "go_live_max_drawdown_pct", 3.0) or 3.0)
    min_weeks = int(getattr(settings, "go_live_min_clean_weeks", 4) or 4)
    if start is None:
        return {
            "ready": False,
            "phase3_started_at": None,
            "blockers": ["phase3_not_started"],
            "computed_at": now.isoformat(),
        }

    since = start.isoformat()
    with db.connect() as connection:
        trades = _closed_trades(connection, since)
        drawdown, paper_only_netted = _update_drawdown(connection, runtime_state, since)
        clean_weeks = _clean_weeks(connection, start, now)
    # Paper-only trades (option B, 2026-10-21) never count toward the bar or the scorecard.
    paper_only = sum(1 for t in trades if t["paper_only"])
    trades = [t for t in trades if not t["paper_only"]]
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    profit_factor = (
        gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    )

    scorecard: dict[str, dict[str, Any]] = {}
    for trade in trades:
        card = scorecard.setdefault(
            f"{trade['strategy']}:{trade['timeframe']}",
            {"trades": 0, "wins": 0, "pnl": 0.0, "r": []},
        )
        card["trades"] += 1
        card["wins"] += 1 if trade["pnl"] > 0 else 0
        card["pnl"] = round(card["pnl"] + trade["pnl"], 2)
        if trade["r"] is not None:
            card["r"].append(trade["r"])
    for card in scorecard.values():
        rs = card.pop("r")
        card["avg_r"] = round(sum(rs) / len(rs), 3) if rs else None

    blockers = []
    if len(trades) < min_trades:
        blockers.append(f"closed_trades_{len(trades)}_below_{min_trades}")
    if profit_factor < min_pf:
        blockers.append(f"profit_factor_{profit_factor:.2f}_below_{min_pf}")
    if drawdown >= max_dd:
        blockers.append(f"max_drawdown_{drawdown:.2f}pct_not_below_{max_dd}")
    if clean_weeks < min_weeks:
        blockers.append(f"clean_weeks_{clean_weeks}_below_{min_weeks}")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "phase3_started_at": since,
        "closed_trades": len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "net_pnl_usd": round(sum(t["pnl"] for t in trades), 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "max_drawdown_pct": drawdown,
        "clean_weeks": clean_weeks,
        "scorecard": scorecard,
        "paper_only_trades_excluded": paper_only,
        "paper_only_pnl_netted_usd": paper_only_netted,
        "computed_at": now.isoformat(),
    }


def refresh_readiness(
    db: Any, settings: Any, runtime_state: Any, run_logs: Any | None = None
) -> dict[str, Any]:
    report = compute_readiness(db, settings, runtime_state)
    runtime_state.set(READINESS_KEY, json.dumps(report))
    if run_logs is not None:
        run_logs.log("go_live_readiness", {k: v for k, v in report.items() if k != "scorecard"})
    return report


READINESS_MAX_AGE_HOURS = 2.0  # refreshed every 30 min; an older "ready" is not trusted


def readiness_ready(runtime_state: Any) -> bool:
    """Ready only if the cached report says so AND is fresh (review 2026-10-05: a past
    ``ready: true`` stayed in force even if later trades failed the bar)."""

    from datetime import datetime, timedelta

    from app.utils.time import utc_now

    try:
        report = json.loads(runtime_state.get(READINESS_KEY) or "{}")
        computed = datetime.fromisoformat(str(report.get("computed_at") or ""))
    except (TypeError, ValueError):
        return False
    if utc_now() - computed > timedelta(hours=READINESS_MAX_AGE_HOURS):
        return False
    return bool(report.get("ready"))


__all__ = ["compute_readiness", "readiness_ready", "refresh_readiness"]
