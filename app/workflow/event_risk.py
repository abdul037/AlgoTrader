"""Event-risk (earnings and US macro) check -- SHADOW ONLY (operator-approved 2026-10-18).

It records which entries an earnings rule, or a macro-calendar rule, WOULD have blocked; nothing
reads the flags to decide anything. It never blocks, creates, changes or reorders a proposal,
order, approval, scan decision, alert or Telegram message. Setting ``event_risk_shadow_enabled``
(default on) stops all of it: the earnings refresh, the watchlist hints and the proposal logs;
``event_risk_macro_shadow_enabled`` (default on) stops the macro part alone.

The rule: a known earnings date (``earnings_calendar``) inside the entry's window flags it. The
window runs from the signal bar's session (a report after that close, or before the next open,
gaps the entry's fill) through the ``HOLD_SESSIONS``-th session counted from the entry session.

``HOLD_SESSIONS`` = 10 for daily and longer strategies: the evidence gate's walk-forward fold is
14 calendar days (about 10 sessions); 56-85% of pooled OOS trades are still open at that
boundary (the rest end earlier at a stop or a target), the shadow ledger's ``time_14d`` leg uses
the same horizon, and the strategy-lab daily templates cap holds at 12-20 bars. The live rule
has no time exit, so a trade can run longer; 10 sessions covers the horizon the backtest priced.
Intraday strategies (5m/15m/... 1h) are flattened before the close, so their window is the entry
session alone. Sessions are weekdays, as everywhere else in the bot (no holiday calendar), so
around a holiday the window ends a day early. Days and the 16:00 close are New York's, whatever
``schedule_timezone`` is: the sessions and the earnings dates are US ones.

The macro rule (``assess_macro``, the post-mortem's calendar item): a scheduled CPI, PPI, jobs
report (Employment Situation) or FOMC statement day from the entry session through the same
window end flags the entry -- an entry made on an event day, or held across one. A release
before the entry session is in the entry's price (all four come out before the close), so the
macro window starts at the entry, not the signal bar. The dates come from the static table in
``macro_calendar`` (no network, no database); a window the table cannot vouch for reads
``calendar_unknown``, never clear. Every instrument is assessed, funds and crypto too: a
release moves the whole market. The flag rides as a ``macro`` block on the same hint and the
same log row, so it costs no extra call or database round trip.

Where it is recorded:

* (a) the pre-market deep scan's long watchlist items: an ``event_risk`` hint, never in
  ``rank_key`` or ``tradeable`` (absent when the switch is off, or when the macro part is off
  too and no earnings were ever cached; with no earnings cached its earnings reason is
  ``no_cache``);
* (b) each auto-created proposal: one ``event_risk_shadow`` run log, written after the scan's
  whole proposal loop (after every approval and order), from the in-memory cache only -- no
  network and no database read on that path;
* (c) ``GET /performance/live-scorecard`` -> ``event_risk_shadow``: the shadow ledger's setups
  and the eToro live trades, flagged against unflagged -- by earnings (the top level), by the
  macro calendar (``macro``; ``macro.entry_day`` splits off the entries made on an event day)
  and by either rule (``any_event``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

EVENT = "event_risk_shadow"
HOLD_SESSIONS = 10  # daily and longer: the walk-forward fold, about 10 sessions
INTRADAY_HOLD_SESSIONS = 1  # flattened before the close: the entry session alone
MAX_AGE_DAYS = 7  # earnings dates fetched longer ago than this are not trusted
NEW_YORK = ZoneInfo("America/New_York")  # the US session's clock and day, not schedule_timezone
SESSION_CLOSE = (16, 0)  # New York: a proposal after the close enters on the next session
# Reasons that clear an entry: no report in its window, or an instrument that never reports.
CLEAR_REASONS = frozenset({"no_earnings_in_window", "not_applicable"})
MACRO_CLEAR_REASONS = frozenset({"no_macro_in_window"})  # never calendar_unknown
COHORTS = ("flagged", "unflagged", "no_data", "not_assessed")
RULE = (
    "would block: a known earnings date from the signal bar's session through the entry's "
    f"hold window ({HOLD_SESSIONS} sessions from the entry for daily strategies, the entry "
    "session for intraday ones); shadow only, nothing is blocked"
)
MACRO_RULE = (
    "would block: a scheduled US CPI, PPI, jobs report (Employment Situation) or FOMC "
    "statement day from the entry session through the same hold window; a window the in-code "
    "calendar cannot vouch for is calendar_unknown; shadow only, nothing is blocked"
)
ANY_RULE = "flagged by either rule; unflagged only when both are clear; shadow only"


def enabled(settings: Any) -> bool:
    """The switch. A settings object without the field (test fakes) reads as off."""

    return bool(getattr(settings, "event_risk_shadow_enabled", False))


def macro_enabled(settings: Any) -> bool:
    """The macro sub-switch (default on), only ever on with the event-risk switch."""

    return enabled(settings) and bool(getattr(settings, "event_risk_macro_shadow_enabled", True))


def hold_sessions(timeframe: Any, settings: Any) -> int:
    from app.automation.intraday_exit import INTRADAY_TIMEFRAMES

    flatten = float(getattr(settings, "intraday_flatten_minutes_before_close", 0) or 0)
    if str(timeframe or "").lower() in INTRADAY_TIMEFRAMES and flatten > 0:
        return INTRADAY_HOLD_SESSIONS
    return HOLD_SESSIONS


def next_session(day: date) -> date:
    """``day`` itself when it is a weekday, else the following Monday."""

    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def add_sessions(day: date, count: int) -> date:
    """The session ``count`` weekdays after ``day`` (``day`` itself for 0)."""

    day = next_session(day)
    for _ in range(max(int(count), 0)):
        day = next_session(day + timedelta(days=1))
    return day


def assess(
    record: dict[str, Any] | None,
    *,
    signal_day: date,
    entry_day: date,
    hold: int,
    today: date,
) -> dict[str, Any]:
    """``would_block``, the ``earnings_date`` it is about and the ``reason``, for one entry.

    Pure: the record is one symbol's entry in the earnings cache (None when it has none)."""

    signal_day = min(signal_day, entry_day)
    window_end = add_sessions(entry_day, max(int(hold), 1) - 1)
    out: dict[str, Any] = {
        "would_block": False,
        "earnings_date": None,
        "reason": "no_earnings_data",
        "window_end": window_end.isoformat(),
    }
    if not isinstance(record, dict):
        return out
    if record.get("status") == "not_applicable":
        return {**out, "reason": "not_applicable"}
    fetched = _day(record.get("fetched_on"))
    if fetched is None:
        return out  # never fetched successfully (e.g. every try failed)
    if (today - fetched).days > MAX_AGE_DAYS:
        return {**out, "reason": "stale_earnings_data"}
    known = [d for d in (_day(v) for v in [*(record.get("upcoming") or []), record.get("last")]) if d]
    inside = sorted(d for d in known if signal_day <= d <= window_end)
    if inside:
        first = inside[0]
        if first < entry_day:
            reason = "earnings_before_entry"
        elif first == entry_day:
            reason = "earnings_on_entry_day"
        else:
            reason = "earnings_within_hold"
        return {**out, "would_block": True, "earnings_date": first.isoformat(), "reason": reason}
    if not known:
        return out
    later = sorted(d for d in known if d > window_end)
    return {
        **out,
        "earnings_date": later[0].isoformat() if later else None,
        "reason": "no_earnings_in_window",
    }


def assess_macro(*, entry_day: date, window_end: date, calendar: Any = None) -> dict[str, Any]:
    """``would_block``, the ``reason`` and the scheduled ``events`` from the entry session
    through ``window_end``, for one entry; ``unknown_kinds`` are the kinds the table cannot
    vouch for over that span. Pure: it reads the in-code ``macro_calendar`` table only."""

    from app.workflow.macro_calendar import scheduled

    events, unknown = scheduled(entry_day, max(window_end, entry_day), calendar)
    if events:
        on_entry = events[0]["date"] == entry_day.isoformat()
        reason = "macro_on_entry_day" if on_entry else "macro_within_hold"
    else:
        reason = "calendar_unknown" if unknown else "no_macro_in_window"
    return {
        "would_block": bool(events),
        "reason": reason,
        "events": events,
        "unknown_kinds": unknown,
    }


def _macro_for(entry: date, hold: Any) -> dict[str, Any]:
    """The macro flag from ``entry`` through its ``hold``-th session (the earnings window's
    end), or an error row. Never raises."""

    try:
        return assess_macro(entry_day=entry, window_end=add_sessions(entry, max(int(hold), 1) - 1))
    except Exception as exc:  # noqa: BLE001 - shadow only
        flag = {"would_block": False, "reason": "error", "events": [], "unknown_kinds": []}
        return {**flag, "error": str(exc)[:160]}


def watchlist_hint(service: Any) -> Any:
    """For the deep scan: ``hint(symbol, signal_bar_ts) -> dict | None`` on one cache read, or
    None when the switch is off, or the macro part is off and no earnings were ever cached
    (or the stored copy cannot be read). The hint never raises."""

    from app.workflow import earnings_calendar

    if not enabled(service.settings):
        return None
    try:
        macro = macro_enabled(service.settings)
        today = utc_now().astimezone(NEW_YORK).date()
    except Exception:  # noqa: BLE001 - a hint only: the scan goes on without it
        return None
    try:
        snapshot = earnings_calendar.snapshot_for_scan(service)
    except Exception:  # noqa: BLE001 - a hint only: the macro part goes on without earnings
        snapshot = None
    if snapshot is None and not macro:
        return None
    records = (snapshot or {}).get("symbols") or {}
    entry = next_session(today)  # the deep scan runs before the session it enters on

    def hint(symbol: str, signal_bar_ts: Any) -> dict[str, Any] | None:
        try:
            if snapshot is None:  # no earnings cached (or unreadable): the macro part alone
                flag = {"would_block": False, "earnings_date": None, "reason": "no_cache"}
                flag["window_end"] = add_sessions(entry, HOLD_SESSIONS - 1).isoformat()
            else:
                flag = assess(
                    _record(records, symbol),
                    signal_day=_day(str(signal_bar_ts or "")[:10]) or entry,
                    entry_day=entry,
                    hold=HOLD_SESSIONS,
                    today=today,
                )
        except Exception:  # noqa: BLE001 - a hint only
            return None
        out = {k: flag[k] for k in ("would_block", "earnings_date", "reason", "window_end")}
        if macro:
            out["macro"] = _macro_for(entry, HOLD_SESSIONS)
        return out

    return hint


def log_proposal_event_risk(service: Any, created: list[tuple[Any, Any]]) -> None:
    """One ``event_risk_shadow`` run log per (proposal id, candidate), after the proposal loop.

    Reads the in-memory earnings cache only (no network, no database read; "no_cache" before
    the first refresh or load) and the in-code macro table (a ``macro`` block on the same
    row). Never raises; a failing log write stops the rest (a database outage must not cost
    one pool timeout per proposal)."""

    from app.workflow import earnings_calendar

    try:
        settings = service.settings
        if not created or not enabled(settings):
            return
        macro = macro_enabled(settings)
        snapshot = earnings_calendar.cached_snapshot()
        now_ny = utc_now().astimezone(NEW_YORK)
    except Exception:  # noqa: BLE001 - shadow only
        return
    today = now_ny.date()
    entry = next_session(
        today + timedelta(days=1) if (now_ny.hour, now_ny.minute) >= SESSION_CLOSE else today
    )
    for proposal_id, candidate in created:
        payload: dict[str, Any] = {"proposal_id": str(proposal_id)}
        hold: Any = HOLD_SESSIONS
        try:
            symbol = str(getattr(candidate, "symbol", "") or "").upper()
            timeframe = str(getattr(candidate, "timeframe", "") or "")
            hold = hold_sessions(timeframe, settings)
            signal_day = _day(str(getattr(candidate, "candle_timestamp", "") or "")[:10]) or entry
            payload.update(
                symbol=symbol,
                strategy=str(getattr(candidate, "strategy_name", "") or ""),
                timeframe=timeframe,
                signal_day=min(signal_day, entry).isoformat(),
                entry_day=entry.isoformat(),
                hold_sessions=hold,
            )
            records = (snapshot or {}).get("symbols") or {}
            if not records:  # before the first refresh, or its load after a restart
                flag = {"would_block": False, "earnings_date": None, "reason": "no_cache"}
                flag["window_end"] = add_sessions(entry, hold - 1).isoformat()
            else:
                record = _record(records, symbol)
                flag = assess(record, signal_day=signal_day, entry_day=entry, hold=hold, today=today)
            payload.update(flag, cache_day=(snapshot or {}).get("day"))
        except Exception as exc:  # noqa: BLE001 - recorded as an error row, never raised
            payload.update(would_block=False, earnings_date=None, reason="error", error=str(exc)[:160])
        if macro:
            payload["macro"] = _macro_for(entry, hold)
        payload["shadow_only"] = True
        try:
            service.run_logs.log(EVENT, payload)
        except Exception:  # noqa: BLE001 - shadow only
            return


def event_risk_report(
    runtime_state: Any,
    *,
    trades: list[dict[str, Any]] | None = None,
    proposal_logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Outcomes of flagged against unflagged setups: the shadow ledger's (from the watchlist
    hint) and the eToro live trades' (from their proposal's ``event_risk_shadow`` log), by
    earnings (the top level), by the macro calendar (``macro``) and by either (``any_event``)."""

    from app.workflow.macro_calendar import describe
    from app.workflow.shadow_ledger import LEDGER_KEY, _load, _summary

    entries = list((_load(runtime_state, LEDGER_KEY).get("entries") or {}).values())
    taken = [e for e in entries if isinstance(e, dict) and e.get("status") in {"open", "closed"}]
    flags: dict[str, dict[str, Any]] = {}
    for row in proposal_logs or []:  # newest first: the first row for a proposal wins
        payload = (row or {}).get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict) and payload.get("proposal_id"):
            flags.setdefault(str(payload["proposal_id"]), payload)

    def split(cohort: Any) -> dict[str, Any]:
        live: dict[str, list[dict[str, Any]]] = {name: [] for name in COHORTS}
        for trade in trades or []:
            live[cohort(flags.get(str(trade.get("proposal_id") or "")))].append(trade)
        return {
            "shadow_ledger": {
                name: _summary([e for e in taken if cohort(e.get("event_risk")) == name])
                for name in COHORTS
            },
            "live_trades": {name: _live_summary(rows) for name, rows in live.items()},
        }

    flagged = [p for p in flags.values() if p.get("would_block") is True]
    macro_flagged = [p for p in flags.values() if _macro_cohort(p) == "flagged"]
    return {
        "rule": RULE,
        **split(_cohort),
        "proposals_logged": len(flags),
        "proposals_flagged": len(flagged),
        "recent_flagged": [
            {k: p.get(k) for k in ("symbol", "strategy", "proposal_id", "earnings_date", "reason")}
            for p in flagged[:20]
        ],
        "macro": {
            "rule": MACRO_RULE,
            "calendar": describe(),
            **split(_macro_cohort),
            # A 10-session window holds an event most weeks: entries made ON an event day
            # against the other assessed ones (flagged within the hold, or clear).
            "entry_day": split(_entry_day_cohort),
            "proposals_flagged": len(macro_flagged),
            "recent_flagged": [
                {
                    **{k: p.get(k) for k in ("symbol", "strategy", "proposal_id", "entry_day")},
                    **{k: p["macro"].get(k) for k in ("reason", "events")},
                }
                for p in macro_flagged[:20]
            ],
        },
        "any_event": {
            "rule": ANY_RULE,
            **split(_any_cohort),
            "proposals_flagged": sum(1 for p in flags.values() if _any_cohort(p) == "flagged"),
        },
        "shadow_only": True,
    }


def _record(records: dict[str, Any], symbol: Any) -> Any:
    """The symbol's cache entry; crypto and funds never report, cached or not."""

    from app.workflow.earnings_calendar import reports_earnings

    symbol = str(symbol or "").upper()
    return records.get(symbol) if reports_earnings(symbol) else {"status": "not_applicable"}


def _cohort(flag: Any) -> str:
    if not isinstance(flag, dict):
        return "not_assessed"
    if flag.get("would_block") is True:
        return "flagged"
    return "unflagged" if flag.get("reason") in CLEAR_REASONS else "no_data"


def _macro_cohort(flag: Any) -> str:
    macro = flag.get("macro") if isinstance(flag, dict) else None
    if not isinstance(macro, dict):
        return "not_assessed"  # no macro block: recorded before it shipped, or its switch off
    if macro.get("would_block") is True:
        return "flagged"
    return "unflagged" if macro.get("reason") in MACRO_CLEAR_REASONS else "no_data"


def _entry_day_cohort(flag: Any) -> str:
    """Flagged only for an event on the entry session; any other flag or a clear window is
    unflagged here."""

    cohort = _macro_cohort(flag)
    if cohort != "flagged":
        return cohort
    on_entry = flag["macro"].get("reason") == "macro_on_entry_day"
    return "flagged" if on_entry else "unflagged"


def _any_cohort(flag: Any) -> str:
    """Flagged by either rule; unflagged only when both rules are clear."""

    earnings, macro = _cohort(flag), _macro_cohort(flag)
    if earnings == "not_assessed":
        return "not_assessed"
    if "flagged" in (earnings, macro):
        return "flagged"
    return "unflagged" if earnings == macro == "unflagged" else "no_data"


def _live_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        float(t["realized_r"])
        for t in rows
        if t.get("status") == "closed" and _finite(t.get("realized_r"))
    ]
    pnl = [float(t.get("pnl_usd") or 0.0) for t in rows if t.get("status") == "closed"]
    return {
        "trades": len(rows),
        "closed": len(closed),
        "avg_realized_r": round(sum(closed) / len(closed), 3) if closed else None,
        "win_rate": round(sum(1 for r in closed if r > 0) / len(closed), 3) if closed else None,
        "pnl_usd": round(sum(pnl), 2),
    }


def _finite(value: Any) -> bool:
    import math

    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
