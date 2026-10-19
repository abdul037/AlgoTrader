"""Overnight adverse-move alert for eToro LIVE positions (operator 2026-10-18; alert only).

AMD 2026-10-15: before the open, at 12:11 UTC (16:11 Dubai), the bot's quote was 551.34
(bid 547.49 / ask 595.77, a thin pre-market quote), 3.4% under the 570.82 close and 1.8%
above the 541.53 stop, and three tracked AMD signals had hit their own stops. eToro's stop
filled at 13:27 UTC and nothing told the operator. The guard (``run_live_checks``, every
60 s) now Telegrams the operator when, outside US regular hours, a live eToro position:

* ``gap_down``: its fresh eToro rate is at least ``ALERT_DROP_PCT`` % below the previous
  regular-session close (the newest completed daily bar);
* ``near_stop``: the rate is within ``ALERT_STOP_ATR_MULT`` average daily moves (ATR over
  ``ATR_PERIOD`` completed daily bars) of the strategy stop, or already below it;
* ``signal_stop``: a tracked long signal on the same symbol closed ``stop_hit`` since the
  last regular-session close and since midnight New York, so a hit is not re-sent when the
  date changes (the ``tracked_signals`` table, read at most every
  ``SIGNAL_READ_SECONDS`` through ``app/storage/tracked_signal_stops.py``: only the stop hits
  on the live symbols since that close, never the whole table).

"Outside regular hours" means weekdays outside 09:30-16:00 New York wall-clock time (zoneinfo,
so it follows US daylight saving: regular hours are 13:30-20:00 UTC in summer time and
14:30-21:00 UTC in winter time, from 1 Nov 2026); crypto (the one-off eToro test order) is
also checked all weekend. The message gives the symbol, entry, stop, eToro's bid/ask and
spread (``THIN_SPREAD_PCT`` % or wider is flagged as a thin quote), the % vs entry and vs
stop, and the time in UTC and Dubai.

* Alert only: it never opens, closes, blocks or resizes anything and writes no mirror state
  (halt flag, room, copy rules, stops); paper is untouched.
* At most one alert per symbol per kind per New York date, deduped in runtime_state under
  ``ALERT_STATE_KEY`` plus an in-process memo, so a failed state write cannot cause a resend.
  A failed send is retried on later ticks, at most ``MAX_SEND_ATTEMPTS`` per kind per day.
* Inputs and their cost: the rate the backup stop saved this tick (``MARKS_KEY``, used only
  when at most ``MAX_RATE_AGE_SECONDS`` old) or the crypto test's ``last_rate``; entry and
  stop from the scorecard (or the test order); the previous close and ATR from the paper
  Alpaca daily bars, read once per symbol per session and kept in memory (at most
  ``MAX_BAR_READS_PER_TICK`` reads a tick; a failed read is retried after
  ``BAR_RETRY_SECONDS``). The only extra eToro call is one ``/market-data/instruments/rates``
  read for the symbols about to be alerted: at most one a tick and ``MAX_QUOTE_READS_PER_DAY``
  a day, never while the account's eToro rate-limit cooldown runs.
* A missing input skips that kind (no alert; logged once a day); every failure is logged
  and swallowed. Setting ``etoro_live_overnight_alert_enabled`` (default on); with Telegram
  off or no live client nothing runs and nothing is read.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

logger = logging.getLogger(__name__)

ALERT_STATE_KEY = "etoro_live:overnight_alerts"
ALERT_DROP_PCT = 3.0  # (a) fresh rate this far below the previous close
ALERT_STOP_ATR_MULT = 1.0  # (b) within this many average daily moves (ATR) of the stop
ATR_PERIOD = 14
BARS_LOOKBACK_DAYS = 45  # calendar days of daily bars: ~30 sessions for a 14-bar ATR
THIN_SPREAD_PCT = 1.0  # a bid/ask spread this wide (% of mid) is flagged as a thin quote
MAX_RATE_AGE_SECONDS = 300  # older eToro rates are not used (5 guard ticks)
SIGNAL_READ_SECONDS = 300  # the tracked-signal store is read at most every 5 minutes
SIGNAL_READ_LIMIT = 20  # newest stop hits on the live symbols since the last close
MAX_SEND_ATTEMPTS = 3  # per symbol per kind per day
MAX_BAR_READS_PER_TICK = 3
BAR_RETRY_SECONDS = 900
MAX_QUOTE_READS_PER_DAY = 10
KINDS = ("gap_down", "near_stop", "signal_stop")

NEW_YORK = ZoneInfo("America/New_York")
DUBAI = ZoneInfo("Asia/Dubai")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
_EPS = 1e-9


def in_alert_window(now: datetime, *, crypto: bool) -> bool:
    """Weekdays outside 09:30-16:00 New York; crypto also all weekend."""

    ny = now.astimezone(NEW_YORK)
    if ny.weekday() >= 5:
        return crypto
    return not (REGULAR_OPEN <= ny.time() < REGULAR_CLOSE)


def last_session_date(now: datetime) -> date:
    """New York date of the newest weekday regular session that has closed by ``now``."""

    ny = now.astimezone(NEW_YORK)
    if ny.weekday() < 5 and ny.time() >= REGULAR_CLOSE:
        return ny.date()
    day = ny.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def signal_window_start(now: datetime) -> datetime:
    """Tracked-signal stop hits count from the close (16:00 New York) that began the current
    off-hours window, but not before midnight New York (the dedupe day)."""

    close = datetime.combine(last_session_date(now), REGULAR_CLOSE, tzinfo=NEW_YORK)
    midnight = datetime.combine(now.astimezone(NEW_YORK).date(), time(0), tzinfo=NEW_YORK)
    return max(close, midnight)


def reference_from_bars(frame: Any, *, crypto: bool, now: datetime) -> dict[str, Any] | None:
    """Previous close and ATR from completed daily bars, or None when there are none.

    Stocks: bars dated (New York) up to the last closed session, so a partial bar of today's
    pre-market is ignored. Crypto: bars that started at least a day ago (complete)."""

    import pandas as pd

    from app.signals.evaluation import atr_from_candles

    if frame is None or len(frame) == 0:
        return None
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    if crypto:
        keep = stamps <= pd.Timestamp(now - timedelta(days=1))
    else:
        keep = stamps.dt.tz_convert(NEW_YORK).dt.date <= last_session_date(now)
    done = frame.assign(_ts=stamps)[keep.to_numpy()].sort_values("_ts").reset_index(drop=True)
    if done.empty:
        return None
    prev_close = _num(done["close"].iloc[-1])
    if prev_close is None:
        return None
    atr = atr_from_candles(done, period=ATR_PERIOD)
    return {
        "prev_close": prev_close,
        "atr": atr if atr > 0 else None,
        "bar_date": done["_ts"].iloc[-1].tz_convert(NEW_YORK).date().isoformat(),
    }


def stop_hit_reader(tracked_signals: Any | None) -> Any | None:
    """The alert's ``stop_hits`` reader over a TrackedSignalRepository's database, or None."""

    db = getattr(tracked_signals, "db", None)
    if db is None:
        return None

    def read(symbols: list[str], since_iso: str) -> list[dict[str, Any]]:
        from app.storage.tracked_signal_stops import recent_stop_hits

        return recent_stop_hits(db, symbols=symbols, since_iso=since_iso, limit=SIGNAL_READ_LIMIT)

    return read


class OvernightAdverseMoveAlert:
    """Telegram the operator about adverse off-hours moves in live eToro positions."""

    def __init__(self, *, mirror: Any, bars: Any | None = None, stop_hits: Any | None = None):
        self.mirror = mirror
        self.bars = bars  # callable(symbol, *, timeframe, start, end) -> OHLC frame
        self.stop_hits = stop_hits  # callable(symbols, since_iso) -> tracked-signal stop hits
        self._lock = threading.Lock()
        self._refs: dict[tuple[str, str], dict[str, Any]] = {}
        self._signals: list[dict[str, Any]] | None = None
        self._signals_read_at: datetime | None = None
        self._done: set[tuple[str, str, str]] = set()  # (ny_date, symbol, kind): no more today
        self._noted: set[tuple[str, str]] = set()  # (ny_date, step) logged to run_logs
        self._quote_reads: dict[str, int] = {}
        self._bar_reads = 0

    def run(self, now: datetime | None = None) -> int:
        """Check once; returns how many alerts went out. Never raises."""

        now = now or utc_now()
        if not self._lock.acquire(blocking=False):
            return 0  # the other caller (guard or maintenance fallback) is mid-check
        try:
            if not self._active():
                return 0
            return self._run(now)
        except Exception as exc:  # noqa: BLE001 - an alert must never disturb the guard
            self._note(now, "run", exc)
            return 0
        finally:
            self._lock.release()

    # -- checks ----------------------------------------------------------------
    def _active(self) -> bool:
        from app.broker.etoro_live_mirror import client_problem

        mirror = self.mirror
        if not bool(getattr(mirror.settings, "etoro_live_overnight_alert_enabled", True)):
            return False
        notifier = getattr(mirror, "notifier", None)
        if notifier is None or getattr(notifier, "enabled", True) is False:
            return False
        return client_problem(getattr(mirror, "client", None)) is None

    def _run(self, now: datetime) -> int:
        ny = now.astimezone(NEW_YORK)
        weekend = ny.weekday() >= 5
        if not weekend and REGULAR_OPEN <= ny.time() < REGULAR_CLOSE:
            return 0  # regular hours: nothing is read
        positions = self._positions(now)
        if not positions:
            return 0
        ny_date = ny.date().isoformat()
        self._bar_reads = 0
        self._refresh_signal_stops([p["symbol"] for p in positions], now)
        candidates: list[tuple[dict[str, Any], dict[str, str]]] = []
        for position in positions:
            symbol = position["symbol"]
            try:
                found = self._kinds(position, now)
            except Exception as exc:  # noqa: BLE001 - the other positions are still checked
                self._note(now, f"check:{symbol}", exc)
                continue
            found = {k: v for k, v in found.items() if (ny_date, symbol, k) not in self._done}
            if found:
                candidates.append((position, found))
        if not candidates:
            return 0
        return self._alert(candidates, now, ny_date)

    def _kinds(self, position: dict[str, Any], now: datetime) -> dict[str, str]:
        symbol, rate, stop = position["symbol"], position.get("rate"), position.get("stop")
        if rate is None:
            self._note(now, f"rate_unavailable:{symbol}", "no fresh eToro rate", gap=True)
        found: dict[str, str] = {}
        ref = self._reference(position, now) if rate is not None else None
        position["ref"] = ref
        if rate is not None and ref is not None:
            drop = (1.0 - rate / ref["prev_close"]) * 100.0
            if drop >= ALERT_DROP_PCT - _EPS:
                found["gap_down"] = (
                    f"down {drop:.1f}% from the last close {ref['prev_close']:,.2f}"
                    f" ({ref['bar_date']})"
                )
            atr = ref.get("atr")
            if atr is None:
                self._note(now, f"atr_unavailable:{symbol}", "too few daily bars", gap=True)
            elif stop is None:
                self._note(now, f"stop_unavailable:{symbol}", "no stop recorded", gap=True)
            elif rate - stop <= ALERT_STOP_ATR_MULT * atr + _EPS:
                found["near_stop"] = (
                    f"at or below the stop (ATR {atr:,.2f}): the backup stop should close it"
                    if rate <= stop
                    else f"{(rate - stop) / atr:.2f} average daily moves above the stop"
                    f" (ATR {atr:,.2f})"
                )
        hits = self._signal_stops(symbol, now)
        position["signal_hits"] = hits
        if hits:
            found["signal_stop"] = "; ".join(_signal_line(h) for h in hits[:3]) + (
                f"; +{len(hits) - 3} more" if len(hits) > 3 else ""
            )
        return found

    def _reference(self, position: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        symbol, crypto = position["symbol"], position["crypto"]
        session = (
            now.astimezone(UTC).date().isoformat() if crypto else last_session_date(now).isoformat()
        )
        key = (symbol, session)
        cached = self._refs.get(key)
        if cached is not None and (
            cached["ok"] or now - cached["at"] < timedelta(seconds=BAR_RETRY_SECONDS)
        ):
            return cached if cached["ok"] else None
        if self.bars is None:
            self._note(now, "no_bar_source", "no daily-bar source wired", gap=True)
            return None
        if self._bar_reads >= MAX_BAR_READS_PER_TICK:
            return None  # read on a later tick
        self._bar_reads += 1
        ref: dict[str, Any] | None = None
        try:
            frame = self.bars(
                f"{symbol}/USD" if crypto else symbol,
                timeframe="1d",
                start=now - timedelta(days=BARS_LOOKBACK_DAYS),
                end=now,
            )
            ref = reference_from_bars(frame, crypto=crypto, now=now)
            if ref is None:
                self._note(now, f"no_bars:{symbol}", "no completed daily bar", gap=True)
        except Exception as exc:  # noqa: BLE001 - (a) and (b) wait for the retry
            self._note(now, f"bars:{symbol}", exc)
        cutoff = now - timedelta(days=4)
        self._refs = {k: v for k, v in self._refs.items() if v["at"] >= cutoff}
        self._refs[key] = {"ok": ref is not None, "at": now, **(ref or {})}
        return ref

    def _refresh_signal_stops(self, symbols: list[str], now: datetime) -> None:
        """Re-read the tracked-signal stop hits at most every ``SIGNAL_READ_SECONDS``."""

        if self.stop_hits is None:
            return
        fresh = self._signals_read_at is not None and now - self._signals_read_at < timedelta(
            seconds=SIGNAL_READ_SECONDS
        )
        if fresh:
            return
        self._signals_read_at = now  # also throttles retries after a failed read
        names = {s for s in symbols} | {f"{s}/USD" for s in symbols if _is_crypto(s)}
        try:
            since = signal_window_start(now).astimezone(UTC).isoformat()
            rows = self.stop_hits(sorted(names), since)
            self._signals = [row for row in map(_signal_row, rows or []) if row]
        except Exception as exc:  # noqa: BLE001 - (c) is skipped until the next read
            self._note(now, "tracked_signals", exc)
            self._signals = None

    def _signal_stops(self, symbol: str, now: datetime) -> list[dict[str, Any]]:
        start = signal_window_start(now)
        return [
            s
            for s in self._signals or []
            if s["symbol"] == symbol and s["role"] != "entry_short" and s["closed_at"] >= start
        ]

    # -- alerting --------------------------------------------------------------
    def _alert(
        self, candidates: list[tuple[dict[str, Any], dict[str, str]]], now: datetime, ny_date: str
    ) -> int:
        raw = self.mirror.state.get(ALERT_STATE_KEY)  # unreadable: skip the tick (run() logs)
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("ny_date") != ny_date:
            data = {"ny_date": ny_date, "sent": {}, "failed": {}}
        sent, failed = data.setdefault("sent", {}), data.setdefault("failed", {})
        due: list[tuple[dict[str, Any], dict[str, str]]] = []
        for position, found in candidates:
            symbol = position["symbol"]
            for kind in list(found):
                if kind in (sent.get(symbol) or {}):
                    self._done.add((ny_date, symbol, kind))  # sent by another process
                    found.pop(kind)
                elif int(failed.get(f"{symbol}:{kind}") or 0) >= MAX_SEND_ATTEMPTS:
                    self._done.add((ny_date, symbol, kind))  # gave up for today
                    found.pop(kind)
            if found:
                due.append((position, found))
        if not due:
            return 0
        quotes = self._quotes([p for p, _ in due], now, ny_date)
        count = 0
        for position, found in due:
            symbol = position["symbol"]
            quote = quotes.get(str(position.get("instrument_id") or ""))
            text = format_alert(position, found, now=now, quote=quote)
            ok = self._send(text, now, symbol)
            for kind in found:
                if ok:
                    self._done.add((ny_date, symbol, kind))
                    sent.setdefault(symbol, {})[kind] = now.isoformat()
                else:
                    failed[f"{symbol}:{kind}"] = int(failed.get(f"{symbol}:{kind}") or 0) + 1
            count += 1 if ok else 0
            self._log(
                "etoro_live_overnight_alert_sent"
                if ok
                else "etoro_live_overnight_alert_send_failed",
                _log_payload(position, found, quote, ny_date),
            )
        try:
            self.mirror.state.set(ALERT_STATE_KEY, json.dumps(data))
        except Exception as exc:  # noqa: BLE001 - the in-process memo still stops a resend
            self._note(now, "state_write", exc)
        return count

    def _quotes(
        self, positions: list[dict[str, Any]], now: datetime, ny_date: str
    ) -> dict[str, dict[str, Any]]:
        """eToro bid/ask for these instruments: one bounded read, never in a cooldown."""

        from app.broker.etoro_rate_limit import etoro_cooldown_remaining

        ids = sorted({str(int(p["instrument_id"])) for p in positions if p.get("instrument_id")})
        client = getattr(self.mirror, "client", None)
        if not ids or client is None:
            return {}
        try:
            if etoro_cooldown_remaining(getattr(client, "settings", None)) > 0:
                self._note(now, "quotes_cooldown", "eToro cooldown: no bid/ask read", gap=True)
                return {}
            if self._quote_reads.get(ny_date, 0) >= MAX_QUOTE_READS_PER_DAY:
                return {}
            self._quote_reads = {ny_date: self._quote_reads.get(ny_date, 0) + 1}
            payload = client._request(
                "GET", "/market-data/instruments/rates", params={"instrumentIds": ",".join(ids)}
            )
            rows = (payload.get("rates") or []) if isinstance(payload, dict) else []
            return {
                str(int(r["instrumentID"])): r
                for r in rows
                if isinstance(r, dict) and r.get("instrumentID") is not None
            }
        except Exception as exc:  # noqa: BLE001 - the alert goes out without bid/ask
            self._note(now, "quotes", exc)
            return {}

    def _send(self, text: str, now: datetime, symbol: str) -> bool:
        send = getattr(getattr(self.mirror, "notifier", None), "send_text", None)
        if send is None:
            return False
        try:
            return send(text) is not False
        except Exception as exc:  # noqa: BLE001 - retried on a later tick
            self._note(now, f"send:{symbol}", exc)
            return False

    # -- inputs ----------------------------------------------------------------
    def _positions(self, now: datetime) -> list[dict[str, Any]]:
        """Open live positions inside the alert window: mirrored trades from the scorecard
        plus the crypto test. Marks are read only when a mirrored position is in the window."""

        from app.broker.etoro_live_backup_stop import MARKS_KEY
        from app.broker.etoro_live_scorecard import SCORECARD_KEY
        from app.broker.etoro_live_test_order import TEST_STATE_KEY

        state = self.mirror.state
        out: dict[str, dict[str, Any]] = {}
        for card in _load(state, SCORECARD_KEY).values():
            if not isinstance(card, dict) or card.get("status") != "open":
                continue
            symbol = _base(card.get("symbol"))
            if symbol and symbol not in out and in_alert_window(now, crypto=_is_crypto(symbol)):
                out[symbol] = {
                    "symbol": symbol,
                    "crypto": _is_crypto(symbol),
                    "entry": _num(card.get("open_rate")) or _num(card.get("signal_price")),
                    "stop": _num(card.get("stop_loss")) or _num(card.get("broker_stop")),
                    "broker_stop": _num(card.get("broker_stop")),
                    "instrument_id": card.get("instrument_id"),
                    "rate": None,
                    "rate_at": None,
                }
        if out:
            marks = _load(state, MARKS_KEY)
            for symbol, position in out.items():
                mark = marks.get(symbol) if isinstance(marks.get(symbol), dict) else {}
                position["rate"], position["rate_at"] = mark.get("rate"), mark.get("at")
        test = _load(state, TEST_STATE_KEY)
        symbol = _base(test.get("symbol"))
        if (
            test.get("status") == "open"
            and symbol
            and test.get("position_id") is not None
            and in_alert_window(now, crypto=_is_crypto(symbol))
        ):
            out.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "crypto": _is_crypto(symbol),
                    "entry": _num(test.get("open_rate")) or _num(test.get("entry_rate_ref")),
                    "stop": _num(test.get("stop_loss")),
                    "broker_stop": _num(test.get("stop_loss_at_broker")),
                    "instrument_id": test.get("instrument_id"),
                    "rate": test.get("last_rate"),
                    "rate_at": test.get("last_rate_at"),
                },
            )
        for position in out.values():
            position["rate"] = _fresh_rate(position["rate"], position["rate_at"], now)
        return list(out.values())

    # -- logging ---------------------------------------------------------------
    def _note(self, now: datetime, step: str, error: Any, *, gap: bool = False) -> None:
        """Log a failure (app log every time) or a data gap (app log once a day); both go to
        run_logs once per step per New York date, so a lasting problem cannot flood it."""

        key = (now.astimezone(NEW_YORK).date().isoformat(), step)
        first = key not in self._noted
        if not gap:
            logger.warning("eToro overnight alert %s failed: %s", step, error)
        elif first:
            logger.info("eToro overnight alert %s: %s", step, error)
        if not first:
            return
        self._noted = {k for k in self._noted if k[0] == key[0]} | {key}
        self._log("etoro_live_overnight_alert_error", {"step": step, "error": str(error)[:300]})

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self.mirror.logs.log(event, payload)
        except Exception:  # noqa: BLE001 - logging must never raise into the guard
            logger.exception("eToro overnight alert: run log write failed")


def format_alert(
    position: dict[str, Any],
    found: dict[str, str],
    *,
    now: datetime,
    quote: dict[str, Any] | None = None,
) -> str:
    """The Telegram text for one symbol's new alert kinds."""

    symbol, rate = position["symbol"], position.get("rate")
    entry, stop = position.get("entry"), position.get("stop")
    ref = position.get("ref") or {}
    lines = [f"eToro LIVE overnight alert: {symbol} ({_session_label(now)})"]
    lines.extend(f"- {found[kind]}" for kind in KINDS if kind in found)
    seen = f"Now {_px(rate)} (eToro rate)" if rate is not None else "Now n/a (no fresh eToro rate)"
    if rate is not None and ref.get("prev_close"):
        seen += f", last close {_px(ref['prev_close'])} ({_pct(rate, ref['prev_close'])})"
    lines.append(f"{seen} | {_bid_ask(quote)}")
    stops = f"stop {_px(stop)} ({_pct(rate, stop)} vs stop)"
    broker = position.get("broker_stop")
    if broker and stop and abs(broker / stop - 1.0) > 0.005:
        stops += f", eToro's own stop {_px(broker)}"
    lines.append(f"Entry {_px(entry)} ({_pct(rate, entry)} vs entry) | {stops}")
    dubai = now.astimezone(DUBAI)
    lines.append(f"{now.astimezone(UTC):%H:%M} UTC / {dubai:%H:%M} Dubai, {dubai:%a %d %b}")
    lines.append(
        "Alert only: nothing was closed or changed; eToro's stop and the backup stop stay."
    )
    return "\n".join(lines)


def _bid_ask(quote: dict[str, Any] | None) -> str:
    bid, ask = _num((quote or {}).get("bid")), _num((quote or {}).get("ask"))
    if bid is None or ask is None or ask < bid:
        return "bid/ask n/a"
    spread = (ask - bid) / ((ask + bid) / 2.0) * 100.0
    text = f"bid {_px(bid)} / ask {_px(ask)}, spread {spread:.1f}%"
    return text + (" THIN QUOTE" if spread >= THIN_SPREAD_PCT - _EPS else "")


def _session_label(now: datetime) -> str:
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() >= 5:
        return "weekend"
    if time(4, 0) <= ny.time() < REGULAR_OPEN:
        return "pre-market"
    if REGULAR_CLOSE <= ny.time() < time(20, 0):
        return "after hours"
    return "overnight"


def _signal_row(row: Any) -> dict[str, Any] | None:
    """A ``recent_stop_hits`` row in the shape the alert uses (None when unusable)."""

    if not isinstance(row, dict):
        return None
    closed, symbol = _when(row.get("closed_at")), _base(row.get("symbol"))
    if closed is None or not symbol:
        return None
    return {
        "symbol": symbol,
        "closed_at": closed,
        "role": str(row.get("signal_role") or "entry_long"),
        "strategy": str(row.get("strategy_name") or ""),
        "timeframe": str(row.get("timeframe") or ""),
        "stop": _num(row.get("stop_loss")),
        "price": _num(row.get("last_price")),
        "bid": _num(row.get("current_bid")),
        "ask": _num(row.get("current_ask")),
    }


def _signal_line(hit: dict[str, Any]) -> str:
    quote = f"its quote {_px(hit['price'])}"
    if hit.get("bid") and hit.get("ask"):
        quote += f", bid {_px(hit['bid'])} / ask {_px(hit['ask'])}"
    return (
        f"tracked signal {hit['strategy']} {hit['timeframe']} hit its own stop {_px(hit['stop'])}"
        f" at {hit['closed_at'].astimezone(UTC):%H:%M} UTC ({quote})"
    )


def _log_payload(
    position: dict[str, Any], found: dict[str, str], quote: dict[str, Any] | None, ny_date: str
) -> dict[str, Any]:
    ref = position.get("ref") or {}
    return {
        "symbol": position["symbol"],
        "kinds": [kind for kind in KINDS if kind in found],
        "ny_date": ny_date,
        "rate": position.get("rate"),
        "entry": position.get("entry"),
        "stop": position.get("stop"),
        "prev_close": ref.get("prev_close"),
        "atr": ref.get("atr"),
        "bid": _num((quote or {}).get("bid")),
        "ask": _num((quote or {}).get("ask")),
        "signal_stops": len(position.get("signal_hits") or []),
    }


def _fresh_rate(rate: Any, at: Any, now: datetime) -> float | None:
    value, seen = _num(rate), _when(at)
    if value is None or seen is None:
        return None
    return value if now - seen <= timedelta(seconds=MAX_RATE_AGE_SECONDS) else None


def _base(symbol: Any) -> str:
    from app.broker import crypto

    raw = str(symbol or "").strip().upper()
    return crypto.canonical(raw).split("/")[0] if crypto.is_crypto_symbol(raw) else raw


def _is_crypto(symbol: str) -> bool:
    from app.broker import crypto

    return symbol in crypto.KNOWN_CRYPTO_BASES or crypto.is_crypto_symbol(symbol)


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=UTC) if moment and moment.tzinfo is None else moment


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _px(value: Any) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:,.2f}"


def _pct(rate: Any, level: Any) -> str:
    rate, level = _num(rate), _num(level)
    return "n/a" if rate is None or level is None else f"{(rate / level - 1.0) * 100.0:+.2f}%"


def _load(state: Any, key: str) -> dict[str, Any]:
    try:
        value = json.loads(state.get(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
