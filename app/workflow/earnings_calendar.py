"""Earnings dates for the event-risk shadow check (operator-approved 2026-10-18 -- SHADOW ONLY).

No earnings source was wired before this (``PEADStrategy.set_earnings_provider`` is still never
called; this module does not call it either). Once a day, OFF-HOURS, the 30-min phase-gates
maintenance step asks Yahoo Finance (yfinance, already a dependency) for the earnings dates of
every universe equity: at most one fetch per symbol per New York day (a failed fetch counts),
each on a daemon thread with its own timeout, the run bounded by a small budget and stopped by
a few failures in a row (Yahoo throttles cloud IPs; the next run tries the rest). Crypto and
ETFs are never fetched: they report no earnings.

The dates are stored in ``runtime_state['event_risk:earnings']`` and kept in memory. The
proposal path reads the memory copy only (``cached_snapshot``): no network, no database. A
fetch that fails keeps the symbol's previous dates, and a listed date that passes becomes the
symbol's ``last`` report, so a report between a signal bar and its entry is still seen after
Yahoo has moved on to the next quarter.

Yahoo's ``calendarEvents`` gives the next report (two dates while it is unconfirmed: the report
falls in that range), or for a few days after a report the one just made. Dates only, no time of
day; yfinance converts the timestamps in the server's zone (UTC on Railway), so an evening
report can read a day late.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.utils.time import utc_now

CACHE_KEY = "event_risk:earnings"
FETCH_TIMEOUT_SECONDS = 8.0  # one symbol's fetch; a slower answer is a failure
RUN_SECONDS = 30.0  # per maintenance run; a fetch starts only with its full timeout left
FAILURE_STREAK = 3  # failures in a row: Yahoo is throttling or down, the rest on the next run
KEEP_DAYS = 30  # a symbol out of the universe and unchecked this long is dropped
WARM_RETRY_SECONDS = 300.0  # a failed load of the stored copy is retried this often
# Funds report no earnings: never fetched (index, sector, bond, commodity, leveraged).
NON_EQUITY = frozenset(
    {
        "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP", "MDY", "IWF", "IWD",
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE",
        "SMH", "SOXX", "IGV", "HACK", "ARKK", "KRE", "KBE", "XBI", "IBB", "XOP", "XME",
        "XHB", "ITB", "GDX", "GDXJ", "GLD", "SLV", "USO", "UNG", "TLT", "IEF", "SHY",
        "HYG", "LQD", "AGG", "BND", "EEM", "EFA", "VWO", "FXI", "EWZ", "EWJ", "TQQQ",
        "SQQQ", "SOXL", "SOXS", "SPXL", "UVXY", "VXX", "VIXY",
    }
)

_LOCK = threading.Lock()
_MEMORY: dict[str, Any] = {"snapshot": None, "tried_at": None}


def yahoo_earnings_dates(symbol: str) -> list[str]:
    """The earnings dates Yahoo lists for ``symbol`` (network; the caller bounds the time)."""

    import yfinance as yf

    calendar = yf.Ticker(str(symbol).upper().replace(".", "-")).calendar  # BRK.B -> BRK-B
    return iso_dates(calendar.get("Earnings Date") if isinstance(calendar, dict) else None)


def iso_dates(raw: Any) -> list[str]:
    """Sorted unique ``YYYY-MM-DD`` strings from dates, datetimes, timestamps or strings."""

    if raw is None:
        return []
    values = [raw] if isinstance(raw, (str, date)) or not hasattr(raw, "__iter__") else raw
    out: set[str] = set()
    for value in values:
        try:
            if isinstance(value, datetime) or hasattr(value, "to_pydatetime"):
                value = value.date()
            text = value.isoformat() if isinstance(value, date) else str(value).strip()[:10]
            out.add(date.fromisoformat(text).isoformat())  # NaT and other odd values raise
        except (AttributeError, TypeError, ValueError):
            continue  # an odd entry is skipped, never the symbol
    return sorted(out)


def reports_earnings(symbol: str) -> bool:
    """False for crypto and funds: they are never fetched and never flagged."""

    from app.broker import crypto as crypto_symbols

    symbol = str(symbol or "").upper()
    return not (crypto_symbols.is_crypto_symbol(symbol) or symbol in NON_EQUITY)


def cached_snapshot() -> dict[str, Any] | None:
    """The in-memory cache (None until loaded or refreshed in this process). Never I/O."""

    return _MEMORY["snapshot"]


def snapshot_for_scan(service: Any) -> dict[str, Any] | None:
    """The memory copy, else one read of the stored copy (the pre-market scan, off the order
    path). None when nothing was ever stored or it cannot be read."""

    snapshot = cached_snapshot()
    if snapshot is None:
        stored = service.runtime_state.get(CACHE_KEY)
        if not stored:
            return None
        snapshot = _parse(stored)
        _publish(snapshot)
    return snapshot if snapshot.get("symbols") else None


def warm_memory(service: Any, *, clock: Any = time.monotonic) -> None:
    """After a restart: load the stored copy into memory once (a failed read is retried at most
    every 5 minutes). A no-op once loaded, so a maintenance tick costs nothing. Never raises."""

    from app.workflow.event_risk import enabled

    try:
        if not enabled(service.settings) or _MEMORY["snapshot"] is not None:
            return
        tried = _MEMORY["tried_at"]
        if tried is not None and clock() - tried < WARM_RETRY_SECONDS:
            return
        _MEMORY["tried_at"] = clock()
        _publish(_parse(service.runtime_state.get(CACHE_KEY)))
    except Exception:  # noqa: BLE001 - shadow only: readers see no cache until the next try
        return


def reset_memory() -> None:
    """Forget the memory copy (tests, or a process that must reload it)."""

    with _LOCK:
        _MEMORY.update(snapshot=None, tried_at=None)


def refresh_earnings_cache(
    service: Any,
    *,
    provider: Any = None,
    budget_seconds: float = RUN_SECONDS,
    clock: Any = time.monotonic,
) -> dict[str, Any] | None:
    """Off-hours, once a day: fetch the universe equities not yet checked today.

    None when nothing was due (no database call when the memory copy shows the day done).
    ``provider(symbol) -> dates`` defaults to Yahoo. Raises only if the stored copy cannot be
    read or written (the maintenance step records it); a fetch failure never raises."""

    from app.universe import resolve_universe
    from app.workflow.event_risk import enabled
    from app.workflow.premarket_deep_scan import _zone
    from app.workflow.shadow_ledger import _short_quiet as session_hours  # weekdays 08:00-16:30 NY

    settings = service.settings
    if not enabled(settings):
        return None
    now = utc_now()
    if session_hours(now, settings):
        return None  # off-hours only: never during the session or the pre-market scan
    today = now.astimezone(_zone(settings)).date().isoformat()
    memory = cached_snapshot()
    if memory is not None and memory.get("done_on") == today:
        return None
    started = clock()
    cache = _parse(service.runtime_state.get(CACHE_KEY))
    _publish(cache)  # e.g. the first run after a restart
    if cache.get("done_on") == today:
        return None
    cache = json.loads(json.dumps(cache))  # the published copy is never changed in place
    records: dict[str, dict[str, Any]] = cache.setdefault("symbols", {})
    universe = list(dict.fromkeys(str(s).upper() for s in resolve_universe(settings)))
    fetch = provider or yahoo_earnings_dates
    deadline = started + budget_seconds
    fetched, failed, funds, streak, stopped = 0, [], 0, 0, None
    for symbol in universe:
        record = records.get(symbol) or {}
        if record.get("checked_on") == today:
            continue
        if not reports_earnings(symbol):
            records[symbol] = {"status": "not_applicable", "checked_on": today}
            funds += 1
            continue
        if deadline - clock() < FETCH_TIMEOUT_SECONDS:
            stopped = "budget"  # every fetch gets its full timeout: the rest on the next run
            break
        try:
            dates = iso_dates(_bounded(functools.partial(fetch, symbol), FETCH_TIMEOUT_SECONDS))
            records[symbol] = _merged(record, dates, today)
            fetched, streak = fetched + 1, 0
        except Exception as exc:  # noqa: BLE001 - kept the old dates; tried again tomorrow
            error = f"{type(exc).__name__}: {exc}"[:160]
            records[symbol] = {**record, "status": "error", "error": error, "checked_on": today}
            failed.append(symbol)
            streak += 1
            if streak >= FAILURE_STREAK:
                stopped = "failure_streak"
                break
    keep = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    for symbol in [s for s, r in records.items() if s not in universe]:
        if str(records[symbol].get("checked_on") or "") < keep:
            records.pop(symbol)
    done = all((records.get(s) or {}).get("checked_on") == today for s in universe)
    cache.update(day=today, done_on=today if done else None, updated_at=utc_now().isoformat())
    _publish(cache)  # the proposal path's copy first, even if the save below fails
    service.runtime_state.set(CACHE_KEY, json.dumps(cache, allow_nan=False, default=str))
    summary = {
        "day": today,
        "fetched": fetched,
        "failed_symbols": failed[:20],
        "not_applicable": funds,
        "remaining": sum(1 for s in universe if (records.get(s) or {}).get("checked_on") != today),
        "stopped": stopped,
        "done_for_today": done,
        "seconds": round(clock() - started, 1),
        "shadow_only": True,
    }
    service.run_logs.log("event_risk_earnings_refreshed", summary)
    return summary


def _merged(record: dict[str, Any], dates: list[str], today: str) -> dict[str, Any]:
    """A fetched symbol's record: the upcoming dates, and its last report (Yahoo's past date,
    or a date this cache listed that has passed since).

    No dates for a symbol whose listed report is still ahead is no news (yfinance answers an
    HTTP error with an empty calendar): the old dates stay, with their own ``fetched_on``, so
    they age out as stale after ``event_risk.MAX_AGE_DAYS`` if Yahoo keeps answering empty."""

    ahead = [d for d in record.get("upcoming") or [] if isinstance(d, str) and d >= today]
    if not dates and ahead and record.get("fetched_on"):
        kept = {k: v for k, v in record.items() if k != "error"}
        return {**kept, "status": "empty", "checked_on": today}
    past = [d for d in dates if d < today]
    past += [d for d in record.get("upcoming") or [] if isinstance(d, str) and d < today]
    if isinstance(record.get("last"), str):
        past.append(record["last"])
    return {
        "status": "ok" if dates else "none",
        "upcoming": [d for d in dates if d >= today],
        "last": max(past) if past else None,
        "fetched_on": today,
        "checked_on": today,
    }


def _bounded(fn: Any, timeout: float) -> Any:
    """``fn()`` on a daemon thread; TimeoutError if it overruns (the thread is abandoned, as
    ``yfinance_loader._call_with_timeout`` does, so a stalled socket never holds the tick)."""

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=run, name="earnings-fetch", daemon=True)
    worker.start()
    worker.join(max(float(timeout), 0.0))
    if worker.is_alive():
        raise TimeoutError(f"earnings fetch timed out after {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _parse(raw: Any) -> dict[str, Any]:
    """The stored cache as a dict with a ``symbols`` dict of dict records (an unreadable value
    starts over; an odd record is dropped and fetched again)."""

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    symbols = value.get("symbols")
    symbols = symbols if isinstance(symbols, dict) else {}
    value["symbols"] = {str(k).upper(): v for k, v in symbols.items() if isinstance(v, dict)}
    return value


def _publish(snapshot: dict[str, Any]) -> None:
    with _LOCK:
        _MEMORY["snapshot"] = snapshot
