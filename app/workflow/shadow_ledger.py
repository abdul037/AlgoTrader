"""Shadow forward ledger for the pre-market watchlist (phase 2, shadow only).

Every signal in each morning's deep-scan watchlist (``premarket:watchlist:<date>``) is
followed on the completed daily bars after it, with the backtest engine's fill rules
(``app/backtesting/engine.py``): entry at the next session's open; stop before target on the
same bar; a stop that gaps fills at the worse of the stop and the open; a target fills at the
target; the default cost model's spread on both fills. Two exits are reported:

* ``stop_target`` -- the live rule: held until the stop or the target (no time exit);
* ``time_14d`` -- the walk-forward fold length (``WalkForwardSplitter.test_days``): a trade
  still open 14 calendar days after its entry closes at the last close before then, the way
  a backtest fold force-closes it (56-85% of pooled OOS trades end like this).

As in the backtest, one setup per symbol and strategy at a time: a signal while the same
symbol+strategy trade is still open (by its stop/target exit) is ``skipped_open``. Strategy
sell signals, financing and max-hold metadata are not replayed. R = (exit - entry) /
|entry - stop| from the actual fill, as the backtest sizes a trade.

It reads the watchlists and daily bars (the deep scan's request, so usually the cache) and
writes only ``premarket:shadow_ledger`` (+ its small state key) and one run log. It never
creates proposals or orders and never touches the eToro mirror.

Shorts (operator 2026-10-17, step 2): the watchlists' ``short_signals`` are followed in their
own ledger, ``premarket:shadow_ledger:short`` (+ its own state key), by
``refresh_short_shadow_ledger``. It never shares a run, a key or a budget with the long ledger
above: it runs off-hours only (never on weekdays 08:00-16:30 New York time, never while crypto
trades around the clock) on a small budget that bounds its fetches too. The engine's short
rules: entry at the next open's bid; the stop is above (stop first; a gap fills at the worse
of the stop and the open), the target below; the cover pays the ask; R = (entry - exit) /
|entry - stop|. Nothing ever trades a short.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from typing import Any

from app.utils.time import utc_now

LEDGER_KEY = "premarket:shadow_ledger"
STATE_KEY = "premarket:shadow_ledger:state"
TIME_EXIT_DAYS = 14  # WalkForwardSplitter.test_days
KEEP_DAYS = 60  # entries are dropped this many days after their signal
FIRST_LOOKBACK_DAYS = 10  # watchlists read on the very first run
RUN_SECONDS = 60.0  # per maintenance run; symbols not reached are scored on the next one
RUN_AFTER = (9, 35)  # New York time: after the deep scan's latest cutoff (09:30)
_FINAL = ("closed", "skipped_open", "no_risk")
SHORT_LEDGER_KEY = "premarket:shadow_ledger:short"
SHORT_STATE_KEY = "premarket:shadow_ledger:short:state"
SHORT_RUN_SECONDS = 20.0  # per off-hours run, its fetches included
SHORT_QUIET = ((8, 0), (16, 30))  # New York, Mon-Fri: from before the pre-market scan to the close
LONG_RULES = (
    "next-open entry; stop first, gap-through; target at target; spread both ways; "
    "stop_target = no time exit, time_14d = fold-length exit"
)
SHORT_RULES = (
    "short, never traded: next-open entry at the bid; stop above, first, gap-through at the "
    "open; target below, at target; cover at the ask; R = (entry - exit) / risk; "
    "stop_target = no time exit, time_14d = fold-length exit"
)


def refresh_shadow_ledger(service: Any, *, budget_seconds: float = RUN_SECONDS) -> dict | None:
    """Once a day after the deep scan: add finished watchlists, re-score unfinished entries.

    None when nothing was due. A run that hits its budget leaves the rest to the next one.
    """

    from app.workflow.premarket_deep_scan import _zone

    settings = service.settings
    if not bool(getattr(settings, "premarket_deep_scan_enabled", False)):
        return None
    now_ny = utc_now().astimezone(_zone(settings))
    today = now_ny.date()
    if (now_ny.hour, now_ny.minute) < RUN_AFTER:
        return None
    state = _load(service.runtime_state, STATE_KEY)
    if state.get("done_on") == today.isoformat():
        return None
    ledger = _load(service.runtime_state, LEDGER_KEY)
    entries: dict[str, dict[str, Any]] = ledger.setdefault("entries", {})
    added = _ingest(service.runtime_state, entries, state, today)
    deadline = time.monotonic() + budget_seconds
    done_today = (
        set(state.get("scored_symbols") or []) if state.get("scored_day") == str(today) else set()
    )
    symbols = sorted({e["symbol"] for e in entries.values() if e.get("status") not in _FINAL})
    scored, failed = 0, []
    for symbol in symbols:
        if symbol in done_today:
            continue
        if time.monotonic() >= deadline:
            break
        bars = _bars(service, symbol, today)
        if bars is None:
            failed.append(symbol)
            continue
        own = sorted(
            (e for e in entries.values() if e["symbol"] == symbol),
            key=lambda e: (e["spec_key"], e["signal_bar"], e["signal_date"]),
        )
        _score_symbol(own, bars, today)
        done_today.add(symbol)
        scored += 1
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for key in [k for k, e in entries.items() if str(e.get("signal_date")) < cutoff]:
        entries.pop(key)
    ledger["updated_at"] = utc_now().isoformat()
    _save(service.runtime_state, LEDGER_KEY, ledger)
    state.update(scored_day=str(today), scored_symbols=sorted(done_today))
    if done_today >= set(symbols):
        state["done_on"] = str(today)
    _save(service.runtime_state, STATE_KEY, state)
    summary = {
        "added": added,
        "scored_symbols": scored,
        "failed_symbols": failed,
        "entries": len(entries),
        "open": sum(1 for e in entries.values() if e.get("status") == "open"),
        "done_for_today": state.get("done_on") == str(today),
        "shadow_only": True,
    }
    service.run_logs.log("premarket_shadow_ledger_updated", summary)
    return summary


def refresh_short_shadow_ledger(
    service: Any, *, budget_seconds: float = SHORT_RUN_SECONDS, clock: Any = time.monotonic
) -> dict | None:
    """Once a day, OFF-HOURS: add the finished watchlists' short setups, re-score open shorts.

    None when nothing was due; every check before the first database call. A symbol whose
    bars cannot be had is tried again the next day (each run re-scores from all the bars)."""

    from app.workflow.premarket_deep_scan import _zone

    settings = service.settings
    if not (
        bool(getattr(settings, "premarket_deep_scan_enabled", False))
        and bool(getattr(settings, "premarket_short_shadow_enabled", True))
    ):
        return None
    if bool(getattr(settings, "crypto_trading_enabled", False)):
        return None  # crypto trades around the clock: no tick is off-hours then
    now = utc_now()
    if _short_quiet(now, settings):
        return None
    deadline = clock() + budget_seconds
    today = now.astimezone(_zone(settings)).date()
    state = _load(service.runtime_state, SHORT_STATE_KEY)
    if state.get("done_on") == today.isoformat():
        return None
    ledger = _load(service.runtime_state, SHORT_LEDGER_KEY)
    entries: dict[str, dict[str, Any]] = ledger.setdefault("entries", {})
    added = _ingest(service.runtime_state, entries, state, today, short=True)
    tried = set(state.get("tried_symbols") or []) if state.get("tried_day") == str(today) else set()
    symbols = sorted({e["symbol"] for e in entries.values() if e.get("status") not in _FINAL})
    limit = float(getattr(settings, "screener_market_data_timeout_seconds", 20.0) or 0.0)
    scored, failed = 0, []
    for symbol in symbols:
        if symbol in tried:
            continue
        remaining = deadline - clock()
        if remaining < 1.0:
            break  # the rest on the next run
        tried.add(symbol)
        bars = _bars(service, symbol, today, timeout=min(limit, remaining) if limit else remaining)
        own = sorted(
            (e for e in entries.values() if e["symbol"] == symbol),
            key=lambda e: (e["spec_key"], e["signal_bar"], e["signal_date"]),
        )
        try:
            if bars is None:
                failed.append(symbol)
                continue
            _score_symbol(own, bars, today)
            scored += 1
        except Exception:  # noqa: BLE001 - one symbol never stops the others
            failed.append(symbol)
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for key in [k for k, e in entries.items() if str(e.get("signal_date")) < cutoff]:
        entries.pop(key)
    ledger["updated_at"] = utc_now().isoformat()
    _save(service.runtime_state, SHORT_LEDGER_KEY, ledger)
    state.update(tried_day=str(today), tried_symbols=sorted(tried))
    if tried >= set(symbols):
        state["done_on"] = str(today)
    _save(service.runtime_state, SHORT_STATE_KEY, state)
    summary = {
        "added": added,
        "scored_symbols": scored,
        "failed_symbols": failed,
        "entries": len(entries),
        "open": sum(1 for e in entries.values() if e.get("status") == "open"),
        "done_for_today": state.get("done_on") == str(today),
        "side": "short",
        "shadow_only": True,
    }
    service.run_logs.log("premarket_short_shadow_ledger_updated", summary)
    return summary


def _short_quiet(now: Any, settings: Any) -> bool:
    """Weekdays 08:00 (or the pre-market scan, if earlier) to 16:30, both in New York time
    (fixed: the US session) and in ``schedule_timezone`` (where the session buckets run)."""

    from zoneinfo import ZoneInfo

    from app.workflow.premarket_deep_scan import _zone

    start, end = SHORT_QUIET
    try:
        scan = tuple(int(p) for p in str(settings.premarket_scan_time_local).split(":")[:2])
    except (AttributeError, TypeError, ValueError):
        scan = start
    for zone in (ZoneInfo("America/New_York"), _zone(settings)):
        local = now.astimezone(zone)
        if local.weekday() < 5 and min(start, scan) <= (local.hour, local.minute) < end:
            return True
    return False


def shadow_ledger_report(runtime_state: Any, *, recent: int = 40) -> dict[str, Any]:
    """Shadow R per strategy and cohort next to the backtest's expectation.

    The top level is the long ledger; ``short`` is the short ledger's, apart."""

    entries = list((_load(runtime_state, LEDGER_KEY).get("entries") or {}).values())
    report = _side_report(entries, recent, LONG_RULES)
    shorts = list((_load(runtime_state, SHORT_LEDGER_KEY).get("entries") or {}).values())
    report["short"] = _side_report(shorts, recent, SHORT_RULES)
    return report


def _side_report(entries: list[dict[str, Any]], recent: int, rules: str) -> dict[str, Any]:
    taken = [e for e in entries if e.get("status") in {"open", "closed"}]
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in taken:
        groups.setdefault(str(entry.get("spec_key")), []).append(entry)
    cohorts = {
        "tradeable": [e for e in taken if e.get("tradeable")],
        "not_tradeable": [e for e in taken if not e.get("tradeable")],
        "strict": [e for e in taken if e.get("kind") == "strict"],
        "weak": [e for e in taken if e.get("kind") != "strict"],
    }
    newest = sorted(entries, key=lambda e: (str(e.get("signal_date")), str(e.get("symbol"))))
    return {
        "signals": len(entries),
        "taken": len(taken),
        "skipped_open": sum(1 for e in entries if e.get("status") == "skipped_open"),
        "waiting_for_entry": sum(1 for e in entries if e.get("status") == "pending"),
        "overall": _summary(taken),
        "strategies": {name: _summary(rows) for name, rows in sorted(groups.items())},
        "cohorts": {name: _summary(rows) for name, rows in cohorts.items()},
        "recent": newest[-recent:],
        "rules": rules,
    }


def score_entry(
    entry: dict[str, Any], bars: list[tuple[str, float, float, float, float]], today: date
) -> None:
    """Score one entry on (session date, open, high, low, close) bars, oldest first.

    A short (``side`` "short") mirrors it: the bid in, the stop above, the target below,
    the ask out, R = (entry - exit) / risk."""

    from app.backtesting.cost_model import CostModel

    cost = CostModel()
    side = "sell" if entry.get("side") == "short" else "buy"
    after = [i for i, bar in enumerate(bars) if bar[0] > entry["signal_bar"]]
    if not after:
        entry["status"] = "pending"  # the next session has not completed yet
        return
    first = after[0]
    fill = cost.entry_fill_price(bars[first][1], side=side, extended_hours=False)
    stop, target = float(entry["stop"]), float(entry["target"])
    risk = abs(fill - stop)
    if not risk > 0:
        entry["status"] = "no_risk"
        return
    entry_day = date.fromisoformat(bars[first][0])
    horizon = (entry_day + timedelta(days=TIME_EXIT_DAYS)).isoformat()
    entry.update(entry_date=bars[first][0], entry_price=round(fill, 4))

    def leg(index: int, raw: float, reason: str) -> dict[str, Any]:
        price = cost.exit_fill_price(raw, side=side, extended_hours=False)
        held = index - first + 1
        return {
            "status": "closed",
            "reason": reason,
            "exit_date": bars[index][0],
            "exit_price": round(price, 4),
            "r": round(((fill - price) if side == "sell" else (price - fill)) / risk, 3),
            "bars": held,
        }

    hit: dict[str, Any] | None = None
    timed: dict[str, Any] | None = None
    for index in range(first, len(bars)):
        day, bar_open, high, low, _close = bars[index]
        if timed is None and day >= horizon:  # the fold ended before this session
            timed = leg(index - 1, bars[index - 1][4], "time_exit")
        if side == "sell":  # a short: the stop is above, the target below
            if high >= stop:
                hit = leg(index, max(stop, bar_open), "stop")
            elif low <= target:
                hit = leg(index, target, "target")
        elif low <= stop:
            hit = leg(index, min(stop, bar_open), "stop")
        elif high >= target:
            hit = leg(index, target, "target")
        if hit is not None:
            timed = timed or hit
            break
    if timed is None and today.isoformat() >= horizon:  # every bar before the horizon is in
        last = max(i for i in range(first, len(bars)) if bars[i][0] < horizon)
        timed = leg(last, bars[last][4], "time_exit")
    if hit is None:
        hit = leg(len(bars) - 1, bars[-1][4], "marked")
        hit["status"] = "open"
    entry["stop_target"] = hit
    entry["time_14d"] = timed or {"status": "open"}
    entry["status"] = "closed" if hit["status"] == "closed" and timed is not None else "open"


def _score_symbol(entries: list[dict[str, Any]], bars: list[tuple], today: date) -> None:
    """Score a symbol's entries; a signal while the same strategy's trade is open is skipped."""

    busy_until: dict[str, str | None] = {}  # spec_key -> exit session of the trade in force
    for entry in entries:
        spec = entry["spec_key"]
        if spec in busy_until:
            exit_day = busy_until[spec]
            if exit_day is None or exit_day > entry["signal_bar"]:
                for stale in ("entry_date", "entry_price", "stop_target", "time_14d"):
                    entry.pop(stale, None)
                entry["status"] = "skipped_open"
                continue
        score_entry(entry, bars, today)
        if entry["status"] in {"open", "closed"}:
            leg = entry["stop_target"]
            busy_until[spec] = leg["exit_date"] if leg["status"] == "closed" else None
        elif entry["status"] == "pending":
            busy_until[spec] = None


def _ingest(
    runtime_state: Any,
    entries: dict[str, Any],
    state: dict[str, Any],
    today: date,
    *,
    short: bool = False,
) -> int:
    """Add the finished watchlists' setups: ``signals``, or ``short_signals`` for the shorts."""

    from app.workflow.premarket_deep_scan import FINAL_STATUSES, load_watchlist

    last = state.get("ingested_through")
    day = (
        date.fromisoformat(last) + timedelta(days=1)
        if last
        else today - timedelta(days=FIRST_LOOKBACK_DAYS)
    )
    added = 0
    while day <= today:
        watchlist = load_watchlist(runtime_state, day.isoformat()) or {}
        final = watchlist.get("status") in FINAL_STATUSES
        if day == today and not final:
            break  # today's scan may still finish: look again next run
        items = watchlist.get("short_signals" if short else "signals") or []
        for item in items if final else []:
            entry = (_short_entry if short else _entry)(item, day.isoformat())
            if entry is not None and entry["key"] not in entries:
                entries[entry.pop("key")] = entry
                added += 1
        state["ingested_through"] = day.isoformat()
        day += timedelta(days=1)
    return added


def _entry(item: dict[str, Any], signal_date: str) -> dict[str, Any] | None:
    try:
        stop, target, price = (float(item[k]) for k in ("stop", "target", "entry"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (stop, target, price)) or not 0 < stop < price < target:
        return None
    signal_bar = str(item.get("signal_bar_ts") or "")[:10]
    symbol, spec_key = str(item.get("symbol") or "").upper(), str(item.get("spec_key") or "")
    if len(signal_bar) != 10 or not symbol or not spec_key:
        return None
    return {
        "key": f"{signal_date}|{symbol}|{spec_key}",
        "signal_date": signal_date,
        "signal_bar": signal_bar,
        "symbol": symbol,
        "spec_key": spec_key,
        "kind": item.get("kind"),
        "tradeable": bool(item.get("tradeable")),
        "evidence_passing": bool(item.get("evidence_passing")),
        "expected_r": item.get("oos_expectancy_r"),
        "signal_price": price,
        "stop": stop,
        "target": target,
        "status": "pending",
    }


def _short_entry(item: dict[str, Any], signal_date: str) -> dict[str, Any] | None:
    """A ``short_signals`` setup: 0 < target < entry < stop, never tradeable."""

    try:
        stop, target, price = (float(item[k]) for k in ("stop", "target", "entry"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (stop, target, price)) or not 0 < target < price < stop:
        return None
    signal_bar = str(item.get("signal_bar_ts") or "")[:10]
    symbol, spec_key = str(item.get("symbol") or "").upper(), str(item.get("spec_key") or "")
    if item.get("side") != "short" or len(signal_bar) != 10 or not symbol or not spec_key:
        return None
    expected = item.get("oos_expectancy_r")  # the short research verdict, if any
    return {
        "key": f"{signal_date}|{symbol}|{spec_key}|short",
        "signal_date": signal_date,
        "signal_bar": signal_bar,
        "symbol": symbol,
        "spec_key": spec_key,
        "side": "short",
        "kind": item.get("kind"),
        "tradeable": False,
        "expected_r": float(expected) if _finite(expected) else None,
        "signal_price": price,
        "stop": stop,
        "target": target,
        "status": "pending",
    }


def _bars(
    service: Any, symbol: str, today: date, timeout: float | None = None
) -> list[tuple] | None:
    """(session date, open, high, low, close) of completed daily bars, oldest first."""

    from datetime import datetime

    from app.workflow.premarket_deep_scan import _fetch_completed, _zone

    now_ny = datetime.combine(today, datetime.min.time(), tzinfo=_zone(service.settings))
    try:
        frame = _fetch_completed(service, symbol, now_ny, False, timeout)
    except Exception:  # noqa: BLE001 - no data this run: scored on the next one
        return None
    if frame is None or len(frame) == 0:
        return None
    import pandas as pd

    days = pd.to_datetime(frame["timestamp"], utc=True).dt.date.astype(str).tolist()
    rows = []
    for day, o, h, low, c in zip(
        days, frame["open"], frame["high"], frame["low"], frame["close"], strict=True
    ):
        values = [float(v) for v in (o, h, low, c)]
        if all(math.isfinite(v) and v > 0 for v in values):
            rows.append((day, *values))
    return rows or None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"taken": len(rows)}
    for leg in ("stop_target", "time_14d"):
        closed = [r[leg]["r"] for r in rows if (r.get(leg) or {}).get("status") == "closed"]
        out[leg] = {
            "closed": len(closed),
            "mean_r": round(sum(closed) / len(closed), 3) if closed else None,
            "win_rate": round(sum(1 for r in closed if r > 0) / len(closed), 3) if closed else None,
        }
    marked = [
        r["stop_target"]["r"] for r in rows if (r.get("stop_target") or {}).get("status") == "open"
    ]
    out["open"] = len(marked)
    out["open_mark_r"] = round(sum(marked) / len(marked), 3) if marked else None
    expected = [float(r["expected_r"]) for r in rows if _finite(r.get("expected_r"))]
    out["backtest_expected_r"] = round(sum(expected) / len(expected), 4) if expected else None
    return out


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _load(runtime_state: Any, key: str) -> dict[str, Any]:
    try:
        value = json.loads(runtime_state.get(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(runtime_state: Any, key: str, value: dict[str, Any]) -> None:
    runtime_state.set(key, json.dumps(value, allow_nan=False, default=str))
