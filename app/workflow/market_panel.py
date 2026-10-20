"""Market panel for the pre-market deep scan -- SHADOW ONLY (operator-approved 2026-10-18).

Post-mortem item 8: the pre-market scan carried no market context (no rates, oil, sector trend
or calendar) while AI and chip stocks turned from 6 Oct. Once per New York date this records,
from Yahoo Finance (yfinance, already a dependency), one daily history per symbol:

* overnight Asia: Kospi ``^KS11`` and Nikkei ``^N225``, the last session's % change (today's,
  closed by 02:30 New York time: Yahoo's daily bar for it has no close yet, so its close is
  Yahoo's quote of that session; with neither, the symbol is missing, see
  ``yahoo_daily_closes``);
* Nasdaq-100 futures ``NQ=F``: the latest price against the prior daily close (about the
  settle; Yahoo's quotes are delayed ~10 min);
* US 10-year yield ``^TNX``: the level and the 1- and 5-session changes in bp, on completed
  sessions (today's live level, if Yahoo has one yet, is shown apart);
* WTI oil ``CL=F``: the latest price and its % change against the prior daily close;
* chips against staples: ``SMH`` minus ``XLP``, 5- and 20-session returns (dividend-adjusted,
  completed sessions only);
* the day's economic events: the US CPI, PPI, jobs-report and FOMC days the in-code table
  (``macro_calendar``, the event-risk shadow's ``macro`` part) lists for that date, and the
  kinds it cannot vouch for then (``unknown_kinds``): no fetch, never in the label.

The regime label is LOGGED ONLY (``regime_label``). Nothing reads it to decide anything: it
never blocks, creates, changes, ranks or reorders a proposal, order, approval, scan decision,
watchlist ranking or ``tradeable`` flag, alert or Telegram message. Thresholds, from Yahoo's
daily closes around the post-mortem's 6-16 Oct 2026 episode (a morning reads the closes
before it; the counts are the 106 weekday mornings from 19 May to 19 Oct):

* ``rates_shock``: the 10y rose >= ``RATES_SHOCK_BP`` (15 bp) over 5 sessions. The run to the
  24-year high (4.97% on 22 Sep to 5.31% on 5 Oct, closes) read +17.9 to +28.7 bp on the 25 Sep
  - 1 Oct mornings; 15 bp sits just under that, and 14 of the 106 mornings reach it.
* ``risk_off``: chips trail staples by >= 5 pp over 5 sessions (``CHIPS_LAG_PP``) while the 10y
  fell >= 5 bp (``BONDS_BID_BP``): money out of chips into staples and bonds. 5 pp is the turn's
  first reading (+4.3 pp on 7 Oct, +1.3 on 8 Oct, -5.6 on 9 Oct, then -7.9 to -15.0); -5 bp
  catches the fall from 5.31% to 5.09% (-7.1 bp on 13 Oct, -5.2 on 14 Oct, -8.7 on 15 Oct). It
  labels 13-16 Oct (MSFT and SPY were copied on 14 Oct), 19 Oct and 29-30 Jun: 7 mornings.
* ``chips_weak``: chips trail staples by >= 5 pp with the 10y in between (9 and 12 Oct; 14).
* ``neutral`` otherwise (71); ``unknown`` when an input is missing.

The obvious rule, "the 10y rose AND chips trail", never fired in the episode: the 10y FELL on
every morning of it (5-session change -0.6 to -14.6 bp from 8 Oct). On 7 Oct, AMD's entry
day, every reading here was ``neutral``: this panel would not have flagged that trade.
``asia_selloff`` (Kospi or Nikkei <= -2% on today's session) and ``futures_down`` (NQ <= -1%
on today's session, as after 15 Oct's 08:30 producer prices) are recorded beside the label,
not in it. On Yahoo's complete daily closes Kospi read -2.6 to -7.6% on 8, 12, 13 and 15 Oct;
a morning reads that session from Yahoo's quote instead (on 19 Oct each index's closing
print), and what the quote said on those mornings can no longer be read back.

Where it runs: last in a ``workflow_cadence`` tick (through ``open_shadow.run_open_shadow``,
before the Phase 3 shadow, whose window starts at the open), only on weekdays once today's
deep scan is final (``workflow:last_premarket_scan_at`` dated today), never while a bucket was
deferred and only with the open shadow's margin of the tick's scan budget left; at most
``RUN_SECONDS`` per tick, never starting a fetch without its full ``FETCH_TIMEOUT_SECONDS``
left, never past 09:30 (or the market-open scan, if earlier). So it never takes time from the
deep scan (it starts only once the scan's list is final, at most later in that same tick with
the margin left): the scan's time-outs of 5-9 Oct (the old 08:30 scan inside the pre-fix
cadence budget, both replaced before 12 Oct) cannot come back through it, and the 09:35 bucket
never waits for it. Each symbol is fetched once a day: a failure, a timeout or no data is
recorded as missing and never retried that day; a panel not finished by 09:30 stays
``partial`` with what it has. It writes only
``runtime_state['premarket:market_panel:<NY date>']`` and, once the day is done, one
``premarket_market_panel_completed`` run log; it never raises (a failure is logged once a day).
Each shadow-ledger setup keeps its morning's label (``regime_tag``) and
``GET /performance/live-scorecard`` shows ``market_panel``: the latest panel, the recent labels
and the shadow ledger grouped by label. ``premarket_market_panel_enabled`` (default on) and the
deep scan's flag switch it.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.utils.time import utc_now
from app.workflow.cadence_budget import (
    MIN_USEFUL_SCAN_SECONDS,
    POST_SCAN_RESERVE_SECONDS,
    SCAN_SETUP_SECONDS,
)

PANEL_KEY = "premarket:market_panel:{date}"
SCAN_DONE_KEY = "workflow:last_premarket_scan_at"  # set once today's deep scan is final
SCHEMA_VERSION = 1
EVENT = "premarket_market_panel_completed"
SOURCE = "yfinance"
# symbol -> how it is read: "session" (latest vs prior close), "yield", "fund" (completed bars)
SYMBOLS = {
    "^KS11": "session",
    "^N225": "session",
    "NQ=F": "session",
    "^TNX": "yield",
    "CL=F": "session",
    "SMH": "fund",
    "XLP": "fund",
}
HISTORY_PERIOD = "3mo"  # >= 21 completed sessions for the 20-session returns
FETCH_TIMEOUT_SECONDS = 8.0  # one symbol's fetch; a slower answer is a missing symbol
RUN_SECONDS = 30.0  # per tick at most; the rest on the next tick
WINDOW_ENDS = (9, 30)  # never runs into the session (nor past the market-open scan)
# Of the tick's scan budget, or this tick is skipped: the open shadow's margin (a tick whose
# backstop skipped a bucket never has this much left, so it never adds to that bucket's wait).
MIN_START_SECONDS = SCAN_SETUP_SECONDS + MIN_USEFUL_SCAN_SECONDS
FINAL = frozenset({"complete", "complete_with_gaps"})
RECENT_DAYS = 14  # calendar days of panels in the scorecard
RATES_SHOCK_BP = 15.0  # 10y 5-session change at or above: a rates shock
BONDS_BID_BP = -5.0  # 10y 5-session change at or below: money into bonds
CHIPS_LAG_PP = -5.0  # SMH minus XLP 5-session return at or below: chips trail staples
ASIA_SELLOFF_PCT = -2.0  # Kospi or Nikkei last session at or below
FUTURES_DOWN_PCT = -1.0  # NQ=F against the prior close at or below
LABELS = ("risk_off", "rates_shock", "chips_weak", "neutral", "unknown")
REGIME_RULE = (
    "logged only, never used: rates_shock = 10y +15 bp or more over 5 sessions; risk_off = "
    "SMH trails XLP by 5 pp or more over 5 sessions and the 10y fell 5 bp or more; chips_weak "
    "= the same chip lag otherwise; neutral = none; unknown = an input missing"
)
EVENTS_NOTE = (
    "the day's US CPI, PPI, jobs-report and FOMC days from the in-code macro calendar "
    "(app/workflow/macro_calendar.py); unknown_kinds: kinds it cannot vouch for that day"
)


def enabled(settings: Any) -> bool:
    """Its own switch and the deep scan's. A settings object without them reads as off."""

    return bool(getattr(settings, "premarket_market_panel_enabled", False)) and bool(
        getattr(settings, "premarket_deep_scan_enabled", False)
    )


def yahoo_daily_closes(symbol: str) -> list[tuple[str, float]]:
    """(session date, close) of Yahoo's daily bars, oldest first (network; the caller bounds the
    time). Dates are the exchange's own (Seoul, Tokyo, New York, Chicago); closes are adjusted
    for dividends, so a fund's ex-dividend day is not read as a drop.

    Yahoo's daily bar can stay without a close for the rest of its day: on 19 Oct 2026 at noon
    in New York, nine hours after both had closed, Kospi's and Nikkei's bars for that date had
    none (yfinance 0.2.66 kept Kospi's with an empty close and dropped Nikkei's, which reports
    no volume, unless ``keepna``). The same response carries the regular-market quote (that
    day, each index's closing print), so the session the quote is dated in the exchange's zone
    takes it as its close: it fills an empty last close or adds a session the bars left out,
    and never replaces a close Yahoo has. Without such a quote the empty close stays, and
    ``read_rows`` records the session as missing rather than reading the one before."""

    import yfinance as yf

    ticker = yf.Ticker(symbol)
    frame = ticker.history(period=HISTORY_PERIOD, interval="1d", auto_adjust=True, keepna=True)
    if frame is None or len(frame) == 0 or "Close" not in frame:
        return []
    rows = [
        (stamp.date().isoformat(), math.nan if _number(close) is None else float(close))
        for stamp, close in zip(frame.index, frame["Close"], strict=True)
    ]
    quote = _session_quote(ticker, getattr(frame.index, "tz", None))
    if quote is not None:
        if quote[0] == rows[-1][0] and _number(rows[-1][1]) is None:
            rows[-1] = quote
        elif quote[0] > rows[-1][0]:
            rows.append(quote)
    return rows


def _session_quote(ticker: Any, zone: Any) -> tuple[str, float] | None:
    """(session date, price) of the regular-market quote in the chart response ``history`` has
    just read, dated in the exchange's ``zone``; None when it is not there. It reads the
    metadata already fetched: the public ``history_metadata`` would send a request of its own
    for the trading hours a daily chart does not carry."""

    meta = getattr(getattr(ticker, "_price_history", None), "_history_metadata", None)
    price = _number(meta.get("regularMarketPrice")) if isinstance(meta, dict) else None
    if zone is None or price is None or price <= 0:
        return None
    stamp = meta.get("regularMarketTime")
    seconds = None if isinstance(stamp, datetime) else _number(stamp)  # Yahoo sends epoch s
    try:
        if isinstance(stamp, datetime) and stamp.tzinfo is not None:  # yfinance's formatted one
            moment = stamp.astimezone(zone)
        elif seconds is not None:
            moment = datetime.fromtimestamp(seconds, tz=zone)
        else:
            return None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.date().isoformat(), price


def run_market_panel(
    service: Any,
    *,
    started_at: float,
    deferred: Any = (),
    clock: Any = time.monotonic,
    provider: Any = None,
) -> dict[str, Any] | None:
    """One tick of the panel, last in a cadence tick. None when nothing was done.

    Never raises: a failure is logged once a day and the scheduler carries on."""

    try:
        return _tick(service, started_at, list(deferred or []), clock, provider)
    except Exception as exc:  # noqa: BLE001 - shadow only: never into the scheduler or a scan
        try:
            day = utc_now().astimezone(_zone(service.settings)).date().isoformat()
            if getattr(service, "_market_panel_error_day", None) != day:
                service._market_panel_error_day = day
                service.run_logs.log("premarket_market_panel_error", {"error": str(exc)[:300]})
        except Exception:  # noqa: BLE001
            pass
        return None


def _tick(service: Any, started_at: float, deferred: list, clock: Any, provider: Any) -> Any:
    from app.workflow.schedule import parse_time

    settings = service.settings
    if not enabled(settings):
        return None
    zone = _zone(settings)
    now = utc_now().astimezone(zone)
    day = now.date().isoformat()
    if now.weekday() >= 5 or getattr(service, "_market_panel_done", None) == day:
        return None
    opens = parse_time(str(getattr(settings, "market_open_scan_time_local", "09:35")))
    starts = parse_time(str(getattr(settings, "premarket_scan_time_local", "08:30")))
    ends = _at(now, min(WINDOW_ENDS, opens))
    if now < _at(now, starts) or now + timedelta(seconds=RUN_SECONDS) > ends:
        return None
    if deferred:
        return None  # live buckets wait for the next tick: never add to their delay
    job = float(getattr(settings, "scheduler_job_timeout_seconds", 240) or 240)
    left = started_at + job - POST_SCAN_RESERVE_SECONDS - clock()  # the cadence budget's rule
    if left < MIN_START_SECONDS:
        return None  # e.g. the deep scan used this tick: it always comes first
    stop_at = clock() + min(left, RUN_SECONDS)
    key = PANEL_KEY.format(date=day)
    values = get_many(service.runtime_state, [key, SCAN_DONE_KEY])
    record = _parse(values.get(key))
    if record.get("session_date") == day and record.get("status") in FINAL:
        service._market_panel_done = day
        return None
    if _local_date(values.get(SCAN_DONE_KEY), zone) != day:
        return None  # today's deep scan is not final yet: it keeps every tick it needs
    if not _resumable(record, day):
        record = _new_record(day)
    memory = getattr(service, "_market_panel_symbols", None)  # this process's fetches today
    if not (isinstance(memory, dict) and memory.get("day") == day):
        memory = service._market_panel_symbols = {"day": day, "symbols": {}}
    for symbol, result in memory["symbols"].items():  # a failed save never costs a refetch
        record["symbols"].setdefault(symbol, result)
    record["ticks"] = int(record.get("ticks") or 0) + 1
    fetch = provider or yahoo_daily_closes
    for symbol in [s for s in SYMBOLS if s not in record["symbols"]]:
        if stop_at - clock() < FETCH_TIMEOUT_SECONDS:
            break  # every fetch gets its full timeout: the rest on the next tick
        result = _fetch_one(fetch, symbol, now.date(), clock)
        record["symbols"][symbol] = memory["symbols"][symbol] = result
    _refresh(record)
    finished = all(symbol in record["symbols"] for symbol in SYMBOLS)
    if finished:
        record["status"] = "complete_with_gaps" if record["missing"] else "complete"
        record["computed_at"] = utc_now().isoformat()
    record["updated_at"] = utc_now().isoformat()
    service.runtime_state.set(key, json.dumps(record, allow_nan=False, default=str))
    if not finished:
        return {"status": record["status"], "fetched": len(record["symbols"])}
    service._market_panel_done = day
    regime = record["regime"]
    summary = {
        "session_date": day,
        "status": record["status"],
        "label": regime["label"],
        "inputs": regime["inputs"],
        "components": regime["components"],
        "missing": record["missing"],
        "ticks": record["ticks"],
        "economic_events": record["economic_events"],
        "shadow_only": True,
    }
    service.run_logs.log(EVENT, summary)
    return summary


def _fetch_one(fetch: Any, symbol: str, today: date, clock: Any) -> dict[str, Any]:
    """One symbol's only fetch of the day: its reading, or ``missing`` with the reason."""

    started = clock()
    try:
        rows = _bounded(lambda: fetch(symbol), FETCH_TIMEOUT_SECONDS)
        reading = read_rows(SYMBOLS[symbol], rows, today)
    except Exception as exc:  # noqa: BLE001 - a missing symbol never stops the panel or a scan
        return {
            "status": "missing",
            "error": f"{type(exc).__name__}: {exc}"[:160],
            "fetched_at": utc_now().isoformat(),
        }
    return {
        "status": "ok",
        "reading": reading,
        "seconds": round(max(clock() - started, 0.0), 2),
        "fetched_at": utc_now().isoformat(),
    }


def read_rows(kind: str, rows: Any, today: date) -> dict[str, Any]:
    """A symbol's figures from its (session date, close) rows. Pure; raises on no usable data.

    "session": the latest row (a forming futures session included) against the one before;
    ``current`` when it is dated ``today`` (Asia's session of the same date has closed by the
    New York morning; an older one is a holiday or a late feed). A row for today without a
    close raises ``today_close_unavailable``: the session before is never read as if today
    were a holiday.
    "yield": completed sessions (before ``today``) for the level and its 1- and 5-session
    changes in bp; a row for today, if any, is shown as ``today_level_pct``.
    "fund": completed sessions only, 5- and 20-session returns in %.
    A date listed twice keeps its last row; a row dated after ``today`` is never read."""

    rows = list(rows or [])
    by_day = {
        str(day)[:10]: float(close)
        for day, close in rows
        if _number(close) is not None and float(close) > 0
    }
    listed_today = any(str(day)[:10] == today.isoformat() for day, _ in rows)
    if kind == "session" and listed_today and today.isoformat() not in by_day:
        raise ValueError("today_close_unavailable")
    clean = sorted(item for item in by_day.items() if item[0] <= today.isoformat())
    if not clean:
        raise ValueError("no_data")
    if kind == "session":
        if len(clean) < 2:
            raise ValueError("too_few_sessions")
        (day, last), (prior_day, prior) = clean[-1], clean[-2]
        return {
            "as_of": day,
            "current": day == today.isoformat(),
            "last": round(last, 4),
            "prior_close": round(prior, 4),
            "prior_date": prior_day,
            "change_pct": round((last / prior - 1.0) * 100.0, 2),
        }
    done = [(day, value) for day, value in clean if day < today.isoformat()]
    if kind == "yield":
        scale = 10.0 if done and done[-1][1] > 20.0 else 1.0  # an index quoted at 10x the yield
        levels = [(day, value / scale) for day, value in done]
        if len(levels) < 2:
            raise ValueError("too_few_sessions")
        out: dict[str, Any] = {
            "as_of": levels[-1][0],
            "level_pct": round(levels[-1][1], 3),
            "change_1d_bp": round((levels[-1][1] - levels[-2][1]) * 100.0, 1),
            "change_5d_bp": (
                round((levels[-1][1] - levels[-6][1]) * 100.0, 1) if len(levels) >= 6 else None
            ),
        }
        if today.isoformat() in by_day:
            out["today_level_pct"] = round(by_day[today.isoformat()] / scale, 3)
        return out
    if len(done) < 6:
        raise ValueError("too_few_sessions")
    closes = [value for _, value in done]
    return {
        "as_of": done[-1][0],
        "close": round(closes[-1], 4),
        "return_5d_pct": round((closes[-1] / closes[-6] - 1.0) * 100.0, 2),
        "return_20d_pct": (
            round((closes[-1] / closes[-21] - 1.0) * 100.0, 2) if len(closes) >= 21 else None
        ),
    }


def build_panel(symbols: dict[str, Any]) -> dict[str, Any]:
    """The panel from the per-symbol records (a missing symbol reads as None). Pure."""

    def reading(symbol: str) -> dict[str, Any] | None:
        record = symbols.get(symbol)
        if isinstance(record, dict) and record.get("status") == "ok":
            value = record.get("reading")
            return value if isinstance(value, dict) else None
        return None

    smh, xlp = reading("SMH"), reading("XLP")

    def relative(field: str) -> float | None:
        a, b = (_number((r or {}).get(field)) for r in (smh, xlp))
        return round(a - b, 2) if a is not None and b is not None else None

    return {
        "asia": {"kospi": reading("^KS11"), "nikkei": reading("^N225")},
        "nasdaq_100_futures": reading("NQ=F"),
        "us_10y_yield": reading("^TNX"),
        "wti_oil": reading("CL=F"),
        "chips_vs_staples": {
            "smh": smh,
            "xlp": xlp,
            "relative_5d_pp": relative("return_5d_pct"),
            "relative_20d_pp": relative("return_20d_pct"),
        },
    }


def regime_label(panel: dict[str, Any]) -> dict[str, Any]:
    """The logged-only label, its inputs and the component flags (None = input missing). Pure."""

    rates = _number((panel.get("us_10y_yield") or {}).get("change_5d_bp"))
    chips = _number((panel.get("chips_vs_staples") or {}).get("relative_5d_pp"))
    overnight = panel.get("asia") or {}
    asia = [_current_change(overnight.get(name)) for name in ("kospi", "nikkei")]
    asia = [value for value in asia if value is not None]
    futures = _current_change(panel.get("nasdaq_100_futures"))
    if rates is not None and rates >= RATES_SHOCK_BP:
        label = "rates_shock"  # decided without the chips: risk_off needs a falling 10y
    elif rates is None or chips is None:
        label = "unknown"
    elif chips <= CHIPS_LAG_PP and rates <= BONDS_BID_BP:
        label = "risk_off"
    elif chips <= CHIPS_LAG_PP:
        label = "chips_weak"
    else:
        label = "neutral"
    return {
        "label": label,
        "risk_off": label == "risk_off",
        "inputs": {"us10y_change_5d_bp": rates, "chips_vs_staples_5d_pp": chips},
        "components": {
            "rates_shock": None if rates is None else rates >= RATES_SHOCK_BP,
            "bonds_bid": None if rates is None else rates <= BONDS_BID_BP,
            "chips_lag": None if chips is None else chips <= CHIPS_LAG_PP,
            "asia_selloff": any(v <= ASIA_SELLOFF_PCT for v in asia) if asia else None,
            "futures_down": None if futures is None else futures <= FUTURES_DOWN_PCT,
        },
        "rule": REGIME_RULE,
    }


def regime_tag(raw: Any) -> dict[str, Any]:
    """What a shadow-ledger setup keeps of its morning's panel (``no_panel`` without one or for
    an unreadable one). Never raises: an edited record must not stop the ledger."""

    return _tag(_parse(raw))


def _tag(record: dict[str, Any]) -> dict[str, Any]:
    regime = record.get("regime")
    if record.get("schema_version") != SCHEMA_VERSION or not isinstance(regime, dict):
        return {"label": "no_panel"}
    label = regime.get("label")
    inputs, components = regime.get("inputs"), regime.get("components")
    return {
        "label": label if label in LABELS else "unknown",
        "panel_status": record.get("status"),
        "inputs": dict(inputs) if isinstance(inputs, dict) else {},
        "components": dict(components) if isinstance(components, dict) else {},
    }


def market_panel_report(runtime_state: Any, *, settings: Any = None) -> dict[str, Any]:
    """The scorecard's section: the latest panel, the recent labels and the shadow ledger's
    taken setups grouped by their morning's label (two reads: the panels and the ledger)."""

    from app.workflow.shadow_ledger import LEDGER_KEY, _load, _summary

    today = utc_now().astimezone(_zone(settings)).date()
    keys = [
        PANEL_KEY.format(date=(today - timedelta(days=n)).isoformat()) for n in range(RECENT_DAYS)
    ]
    values = get_many(runtime_state, keys)
    records = [r for r in (_parse(values.get(k)) for k in keys) if r.get("session_date")]
    recent = [
        {
            "session_date": r.get("session_date"),
            "status": r.get("status"),
            **{k: v for k, v in _tag(r).items() if k != "components"},
            "missing": r.get("missing"),
        }
        for r in records
    ]
    entries = list((_load(runtime_state, LEDGER_KEY).get("entries") or {}).values())
    taken = [e for e in entries if isinstance(e, dict) and e.get("status") in {"open", "closed"}]
    groups = (*LABELS, "no_panel", "not_recorded")
    return {
        "latest": records[0] if records else None,
        "recent": recent,
        "shadow_ledger_by_regime": {
            name: _summary([e for e in taken if _ledger_label(e) == name]) for name in groups
        },
        "rule": REGIME_RULE,
        "economic_events": EVENTS_NOTE,
        "shadow_only": True,
    }


def get_many(runtime_state: Any, keys: list[str]) -> dict[str, Any]:
    """One database round trip where the repository can (the fakes read key by key)."""

    getter = getattr(runtime_state, "get_many", None)
    if callable(getter):
        return dict(getter(keys))
    return {key: runtime_state.get(key) for key in keys}


def _current_change(reading: Any) -> float | None:
    """A session reading's % change when it is today's session (an older one was already read
    on its own morning, so a holiday never re-flags it)."""

    if not isinstance(reading, dict) or reading.get("current") is not True:
        return None
    return _number(reading.get("change_pct"))


def _ledger_label(entry: dict[str, Any]) -> str:
    tag = entry.get("market_regime")
    if not isinstance(tag, dict):
        return "not_recorded"  # ingested before the panel existed
    label = tag.get("label")
    return label if label in (*LABELS, "no_panel") else "unknown"


def _refresh(record: dict[str, Any]) -> None:
    symbols = record["symbols"]
    record["panel"] = build_panel(symbols)
    record["regime"] = regime_label(record["panel"])
    record["missing"] = [s for s in SYMBOLS if (symbols.get(s) or {}).get("status") == "missing"]
    record["not_fetched"] = [s for s in SYMBOLS if s not in symbols]
    record["economic_events"] = economic_events(record.get("session_date"))
    record["economic_events_note"] = EVENTS_NOTE


def economic_events(day: Any) -> dict[str, Any]:
    """The macro calendar's events on ``day`` (an ISO date) and the kinds it cannot vouch for
    then. Pure (the in-code table, no I/O) and never raises: logged only, read by nothing. A
    day it cannot read vouches for no kind, as the event-risk shadow reads the table."""

    from app.workflow.macro_calendar import KINDS, scheduled

    try:
        session = date.fromisoformat(str(day))
        events, unknown = scheduled(session, session)
    except Exception as exc:  # noqa: BLE001 - a logged field never stops the panel
        return {"events": [], "unknown_kinds": list(KINDS), "error": str(exc)[:160]}
    return {"events": events, "unknown_kinds": unknown}


def _new_record(day: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session_date": day,
        "status": "partial",
        "source": SOURCE,
        "started_at": utc_now().isoformat(),
        "ticks": 0,
        "symbols": {},
        "shadow_only": True,
    }


def _resumable(record: dict[str, Any], day: str) -> bool:
    """A record this code wrote today and can resume (anything else starts over)."""

    return (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("session_date") == day
        and record.get("status") == "partial"
        and isinstance(record.get("symbols"), dict)
        and all(isinstance(v, dict) for v in record["symbols"].values())
    )


def _bounded(fn: Any, timeout: float) -> Any:
    """``fn()`` on a daemon thread; TimeoutError if it overruns (the thread is abandoned, as
    ``yfinance_loader._call_with_timeout`` does, so a stalled socket never holds the tick)."""

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=run, name="market-panel-fetch", daemon=True)
    worker.start()
    worker.join(max(float(timeout), 0.0))
    if worker.is_alive():
        raise TimeoutError(f"market panel fetch timed out after {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _parse(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _local_date(value: Any, zone: Any) -> str | None:
    """An aware ISO timestamp's date in ``zone``; None when missing or unreadable."""

    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(zone).date().isoformat()


def _at(now: datetime, hour_minute: tuple[int, int]) -> datetime:
    return now.replace(hour=hour_minute[0], minute=hour_minute[1], second=0, microsecond=0)


def _zone(settings: Any) -> Any:
    from app.workflow.premarket_deep_scan import _zone as deep_scan_zone

    return deep_scan_zone(settings)
