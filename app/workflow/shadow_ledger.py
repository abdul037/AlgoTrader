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


def shadow_ledger_report(runtime_state: Any, *, recent: int = 40) -> dict[str, Any]:
    """Shadow R per strategy and cohort next to the backtest's expectation."""

    entries = list((_load(runtime_state, LEDGER_KEY).get("entries") or {}).values())
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
        "rules": "next-open entry; stop first, gap-through; target at target; spread both ways; "
        "stop_target = no time exit, time_14d = fold-length exit",
    }


def score_entry(
    entry: dict[str, Any], bars: list[tuple[str, float, float, float, float]], today: date
) -> None:
    """Score one entry on (session date, open, high, low, close) bars, oldest first."""

    from app.backtesting.cost_model import CostModel

    cost = CostModel()
    after = [i for i, bar in enumerate(bars) if bar[0] > entry["signal_bar"]]
    if not after:
        entry["status"] = "pending"  # the next session has not completed yet
        return
    first = after[0]
    fill = cost.entry_fill_price(bars[first][1], side="buy", extended_hours=False)
    stop, target = float(entry["stop"]), float(entry["target"])
    risk = abs(fill - stop)
    if not risk > 0:
        entry["status"] = "no_risk"
        return
    entry_day = date.fromisoformat(bars[first][0])
    horizon = (entry_day + timedelta(days=TIME_EXIT_DAYS)).isoformat()
    entry.update(entry_date=bars[first][0], entry_price=round(fill, 4))

    def leg(index: int, raw: float, reason: str) -> dict[str, Any]:
        price = cost.exit_fill_price(raw, side="buy", extended_hours=False)
        held = index - first + 1
        return {
            "status": "closed",
            "reason": reason,
            "exit_date": bars[index][0],
            "exit_price": round(price, 4),
            "r": round((price - fill) / risk, 3),
            "bars": held,
        }

    hit: dict[str, Any] | None = None
    timed: dict[str, Any] | None = None
    for index in range(first, len(bars)):
        day, bar_open, high, low, _close = bars[index]
        if timed is None and day >= horizon:  # the fold ended before this session
            timed = leg(index - 1, bars[index - 1][4], "time_exit")
        if low <= stop:
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


def _ingest(runtime_state: Any, entries: dict[str, Any], state: dict[str, Any], today: date) -> int:
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
        for item in (watchlist.get("signals") or []) if final else []:
            entry = _entry(item, day.isoformat())
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


def _bars(service: Any, symbol: str, today: date) -> list[tuple] | None:
    """(session date, open, high, low, close) of completed daily bars, oldest first."""

    from datetime import datetime

    from app.workflow.premarket_deep_scan import _fetch_completed, _zone

    now_ny = datetime.combine(today, datetime.min.time(), tzinfo=_zone(service.settings))
    try:
        frame = _fetch_completed(service, symbol, now_ny, False)
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
