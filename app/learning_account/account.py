"""The learning account's thread: offers in, verification, the lease, entries and reconcile.

``LearningAccount.offer`` is the only call the main path makes (the end of ``run_scan_task``):
it copies the scan's candidates into a bounded in-memory queue, stamped with the scan's time,
and wakes the thread; no database, no network, never a thread start. Everything else runs on
the learning thread, one ``tick`` at a time (``TICK_SECONDS`` apart, sooner after an offer):

1. the route from the settings (no I/O), then the account's identity, checked on this thread;
2. the lease (app/learning_account/store.py): no lease, no pass;
3. the book; unreadable: no entries and no reconcile;
4. reconcile when due (app/learning_account/reconcile.py) and the untracked-position sweep;
5. entries: fresh, prefiltered candidates; the cheap gates (kill switch or pause, the caps,
   the market clock); the learning verdicts; the main path's own proposal blockers, which
   must be exactly ``['strategy_lacks_oos_evidence']``; then per candidate, the identity
   again, the book and broker symbol checks, one quote, the main path's quote, bracket and
   market-direction checks, the size, the book write and the send.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from contextlib import suppress
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.learning_account import route as routes
from app.learning_account import store as records
from app.learning_account.verdicts import (
    FEE_BAND,
    classify,
    compute_learning_verdicts,
    rank_key,
    stale,
)
from app.utils.time import utc_now

TICK_SECONDS = 60.0
FIRST_PASS_DELAY_SECONDS = 30.0
QUEUE_LIMIT = 200
MAX_ENTRIES_PER_DAY = 3  # new entries a New York trading day (closed ones included)
MAX_ORDERS_PER_DAY = 6  # entry orders sent a New York trading day
MAX_OPEN = 4  # trades open (or whose outcome is not known yet)
RISK_FRACTION = 0.5  # of the normal paper risk size, on the learning account's own equity
INTRADAY_FRESH_SECONDS = 300.0  # an intraday candidate's age, counted from its scan
SWING_FRESH_SECONDS = 1800.0
VERDICTS_EVERY_SECONDS = 1800.0
VERDICTS_BACKOFF_SECONDS = 600.0
REVERIFY_SECONDS = 900.0  # while running, the identity is read again this often
ENTRY_CLOSE_BUFFER_MINUTES = 5.0  # no intraday entry this close to its flatten window
EVIDENCE = "strategy_lacks_oos_evidence"
TELEGRAM_PREFIX = "LEARNING (paper, separate account)"
LIVE = frozenset({"submitting", "submitted", "unknown", "open"})
FINAL = frozenset({"rejected", "closed", "closed_outside"})
UNREPORTED = "closed_unreported"
_NY = "America/New_York"


def ny_day(moment: datetime) -> str:
    from zoneinfo import ZoneInfo

    return moment.astimezone(ZoneInfo(_NY)).date().isoformat()


def scan_time(generated_at: Any, now: datetime) -> datetime | None:
    """The scan's time, capped at ``now``; None (offer nothing) when unreadable or naive."""

    try:
        stamp = datetime.fromisoformat(str(generated_at)) if generated_at else None
    except (TypeError, ValueError):
        return None
    if stamp is None or stamp.tzinfo is None:
        return None
    return min(stamp, now)


def _trade_hash(*parts: Any) -> str:
    import hashlib

    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def trade_id_for(candidate: Any, scanned_at: datetime) -> str:
    symbol = str(getattr(candidate, "symbol", "") or "").upper()
    key = f"{getattr(candidate, 'strategy_name', '')}:{str(getattr(candidate, 'timeframe', '') or '').lower()}"
    return _trade_hash(symbol, key, scanned_at.isoformat())[:20]


def entry_client_order_id(trade_id: str) -> str:
    return f"la-e-{trade_id}"


def close_client_order_id(trade_id: str, attempt: int) -> str:
    return f"la-x-{_trade_hash(trade_id, int(attempt))[:24]}"


def day_counts(trades: dict[str, dict[str, Any]], day: str) -> dict[str, int]:
    """Today's entries and entry orders, and the trades open, from the durable book."""

    today = [trade for trade in trades.values() if trade.get("day") == day]
    return {
        "entries": sum(1 for trade in today if trade.get("status") != "rejected"),
        "orders": sum(1 for trade in today if trade.get("submitted_at") or trade.get("status") in {"submitting", "unknown"}),
        "open": sum(1 for trade in trades.values() if trade.get("status") in LIVE),
    }


def cap_reason(trades: dict[str, dict[str, Any]], day: str) -> str | None:
    counts = day_counts(trades, day)
    if counts["entries"] >= MAX_ENTRIES_PER_DAY:
        return "daily_entry_cap"
    if counts["orders"] >= MAX_ORDERS_PER_DAY:
        return "daily_order_cap"
    if counts["open"] >= MAX_OPEN:
        return "open_position_cap"
    return None


def prefilter_reason(settings: Any, candidate: Any) -> str | None:
    """Only strict-valid, execution-ready, alert-eligible longs above the auto score floor with
    ``metadata.backtest_validated``, never crypto."""

    from app.automation.reliability import STRICT_VALID, proposal_quality_label
    from app.broker import crypto as crypto_symbols
    from app.screener.profiles import effective_auto_execution_min_score

    metadata = dict(getattr(candidate, "metadata", {}) or {})
    symbol = str(getattr(candidate, "symbol", "") or "").upper()
    if not symbol or crypto_symbols.is_crypto_symbol(symbol):
        return "crypto_or_no_symbol"
    if str(getattr(candidate, "signal_role", "") or "").lower() == "entry_short":
        return "not_long"
    if not bool(getattr(candidate, "execution_ready", False)):
        return "not_execution_ready"
    if not bool(metadata.get("alert_eligible", False)):
        return "not_alert_eligible"
    if proposal_quality_label(candidate) != STRICT_VALID:
        return "not_strict_valid"
    if not bool(metadata.get("backtest_validated", False)):
        return "not_backtest_validated"
    if float(getattr(candidate, "score", 0.0) or 0.0) < effective_auto_execution_min_score(settings):
        return "score_below_auto_floor"
    return None


def fresh_reason(candidate: Any, scanned_at: datetime, now: datetime) -> str | None:
    from app.backtesting.intraday_plan import is_intraday

    limit = INTRADAY_FRESH_SECONDS if is_intraday(getattr(candidate, "timeframe", "")) else SWING_FRESH_SECONDS
    return "stale_candidate" if (now - scanned_at).total_seconds() > limit else None


class MainPathOnly:
    """Stands in for option B's ``PaperOnlyPass`` when the learning thread asks the main path's
    own ``candidate_proposal_blockers`` what it would do. It is a ``PaperOnlyPass`` for the
    verdicts (``state`` and ``is_paper_only`` read them exactly as the gate does, once a pass
    while option B's bar is on) and answers ``paper_only_admitted`` for a key the gate admits
    only through that bar, instead of running B's guard reads (eToro records, the paper book,
    the main broker's clock and assets), which never run from the learning thread."""

    def __init__(self, auto_trading: Any):
        from app.workflow.paper_only import PaperOnlyPass

        self._pass = PaperOnlyPass(auto_trading)
        self._verdicts: dict[str, Any] | None = None

    @property
    def state(self) -> Any:
        return self._pass.state

    def verdict(self, candidate: Any) -> Any:
        """The gate's verdict for the candidate's key (the verdicts parsed once a pass)."""

        from app.performance.strategy_evidence import VERDICTS_KEY

        if self._verdicts is None:
            state = self.state
            try:
                parsed = json.loads((state.get(VERDICTS_KEY) if state is not None else None) or "{}").get("verdicts")
            except (TypeError, ValueError, AttributeError):
                parsed = None
            self._verdicts = parsed if isinstance(parsed, dict) else {}
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        return self._verdicts.get(f"{getattr(candidate, 'strategy_name', '') or ''}:{timeframe}")

    def blockers(self, candidate: Any) -> list[str]:
        return ["paper_only_admitted"] if self._pass.is_paper_only(candidate) else []


def main_path_reason(blockers: list[str]) -> str | None:
    """None when the main path refuses the candidate ONLY for its evidence, else why not."""

    if blockers == [EVIDENCE]:
        return None
    if not blockers:
        return "passes_the_main_gate"
    if blockers == ["paper_only_admitted"]:
        return "passes_the_paper_oos_bar"
    if any(str(blocker).startswith("paper_only_") for blocker in blockers):
        return "main_path_paper_only"
    return f"main_path_blocked:{[blocker for blocker in blockers if blocker != EVIDENCE][0]}"


class LearningAccount:
    """The separate Alpaca PAPER learning account (one per process; see the package docstring)."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        run_logs: Any,
        notifier: Any,
        market_data_engine: Any,
        auto_trading: Any,
        automation: Any,
        client_factory: Any = None,
        now: Any = None,
        owner: str | None = None,
        store: records.Store | None = None,
        market: Any = None,
    ):
        from app.learning_account.client import build_client
        from app.learning_account.market import MarketReads

        self.settings = settings
        self.db = db
        self.logs = run_logs
        self.notifier = notifier
        self.auto_trading = auto_trading
        self.automation = automation
        self.now = now or utc_now
        self.owner = owner or records.PROCESS_OWNER
        self.store = store or records.Store(db)
        self.lease = records.Lease(self.store, self.owner, self.now)
        self.market = market or MarketReads(market_data_engine, settings, now=self.now)
        self._client_factory = client_factory or build_client
        self._client: Any = None
        self._queue: deque[tuple[datetime, Any]] = deque(maxlen=QUEUE_LIMIT)
        self._queue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._finished = False  # stop() is terminal
        self._thread: threading.Thread | None = None
        self.route: str | None = None  # as last recorded by this thread
        self._verified_once = False
        self._verify_at: datetime | None = None
        self.reconcile_at: datetime | None = None
        self.sweep_at: datetime | None = None
        self._verdicts: dict[str, Any] | None = None
        self._verdicts_retry_at: datetime | None = None
        self._logged: dict[str, set[Any]] = {}  # New York day -> what was logged once that day
        self.equity = 0.0
        self.clock_state: dict[str, Any] | None = None
        self.positions_seen: tuple[Any, Any] = (None, None)  # this pass's positions read

    # ---- main path side ------------------------------------------------------------------
    def offer(self, candidates: list[Any], generated_at: Any) -> int:
        """Copy a scan's candidates into the queue (no database, no network, no thread)."""

        stamp = scan_time(generated_at, self.now())
        if stamp is None or self._finished:
            return 0
        copies = [c.model_copy(deep=True) if hasattr(c, "model_copy") else c for c in list(candidates or [])]
        with self._queue_lock:
            self._queue.extend((stamp, candidate) for candidate in copies)
        if copies:
            self._wake.set()
        return len(copies)

    # ---- thread ----------------------------------------------------------------------------
    def start(self) -> bool:
        if self._finished or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._run, name="learning-account", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 15.0) -> None:
        """Stop for good (never restarted): the thread finishes its step and releases the lease."""

        self._finished = True
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def _run(self) -> None:
        try:
            if self._stopping.wait(FIRST_PASS_DELAY_SECONDS):
                return
            while not self._stopping.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the thread outlives one bad pass
                    self.log_once("tick_failed:" + type(exc).__name__, "learning_account_status",
                                  {"route": self.route, "error": f"{type(exc).__name__}: {exc}"[:300]})
                self._wake.wait(TICK_SECONDS)
                self._wake.clear()
        finally:
            with suppress(Exception):  # the lease then expires on its own
                self.lease.release()

    # ---- helpers ---------------------------------------------------------------------------
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self.settings)
        return self._client

    def log(self, event: str, payload: dict[str, Any]) -> None:
        with suppress(Exception):  # a log line never fails a pass
            self.logs.log(event, {**payload, "learning_account": True})

    def log_once(self, key: Any, event: str, payload: dict[str, Any]) -> bool:
        day = ny_day(self.now())
        for other in [name for name in self._logged if name != day]:
            self._logged.pop(other, None)
        seen = self._logged.setdefault(day, set())
        if key in seen:
            return False
        seen.add(key)
        self.log(event, payload)
        return True

    def notify(self, *lines: str) -> None:
        with suppress(Exception):  # a notice never fails a pass
            self.notifier.send_text("\n".join([TELEGRAM_PREFIX, *lines]))

    def skip(self, reason: str, candidate: Any = None) -> None:
        payload = {"reason": reason}
        if candidate is not None:
            payload.update(
                symbol=str(getattr(candidate, "symbol", "") or "").upper(),
                strategy_name=str(getattr(candidate, "strategy_name", "") or ""),
                timeframe=str(getattr(candidate, "timeframe", "") or "").lower(),
            )
        self.log_once(("skipped", reason), "learning_account_skipped", payload)

    def set_route(self, route: str, detail: str | None = None) -> None:
        """Record the route when it changes (on this thread only, never the startup thread)."""

        if route == self.route:
            return
        previous, self.route = self.route, route
        record = {"route": route, "owner": self.owner, "checked_at": self.now().isoformat()}
        if detail:
            record["detail"] = detail
        with suppress(Exception):  # the scorecard then shows the older record
            self.store.put(records.STATUS_KEY, records.dumps(record))
        self.log("learning_account_status", {"route": route, "previous": previous, **({"detail": detail} if detail else {})})
        if route == routes.RUNNING and previous is None:
            self.notify("Account verified: taking fee-band and lane entries.")
        elif route in {routes.ACCOUNT_MISMATCH, routes.TRADING_BLOCKED}:
            self.notify(f"Refused: {route}. No new entries until it clears.")

    def verify(self, now: datetime) -> str:
        """Read the learning account's identity (this thread) and record the route."""

        try:
            identity = self.client().identity()
        except Exception as exc:  # noqa: BLE001 - unreadable: retried soon
            identity, detail = None, type(exc).__name__
        else:
            detail = None
        route = routes.identity_route(self.settings, identity)
        if route == routes.RUNNING:
            self._verified_once = True
            self.equity = float(identity.get("equity") or 0.0)
            self._verify_at = now + timedelta(seconds=REVERIFY_SECONDS)
        else:
            self._verify_at = now + timedelta(seconds=routes.retry_seconds(route))
        self.set_route(route, detail)
        return route

    def verify_for_entry(self, now: datetime) -> bool:
        """The identity again, just before an entry. Unreadable refuses this candidate only (the
        route stays); a different account or a blocked one is recorded and stops entries."""

        try:
            identity = self.client().identity()
        except Exception:  # noqa: BLE001
            return False
        route = routes.identity_route(self.settings, identity)
        if route == routes.RUNNING:
            self.equity = float(identity.get("equity") or 0.0)
            return True
        if route != routes.VERIFICATION_FAILED:
            self._verify_at = now + timedelta(seconds=routes.retry_seconds(route))
            self.set_route(route)
        return False

    def market_clock(self) -> dict[str, Any] | None:
        try:
            self.clock_state = self.client().clock()
        except Exception:  # noqa: BLE001 - unknown clock: no entries, reconcile waits
            self.clock_state = None
        return self.clock_state

    def drain(self) -> list[tuple[datetime, Any]]:
        with self._queue_lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    # ---- one pass --------------------------------------------------------------------------
    def tick(self) -> None:
        from app.learning_account.reconcile import reconcile, sweep_untracked

        now = self.now()
        settings_route = routes.settings_route(self.settings)
        if settings_route != routes.PENDING:
            self.drain()
            self.set_route(settings_route)
            return
        if self._verify_at is None or now >= self._verify_at:
            self.verify(now)
        running = self.route == routes.RUNNING
        # Verified earlier in this process: an unreadable identity still lets reconcile run.
        reconcile_only = self.route == routes.VERIFICATION_FAILED and self._verified_once
        drained = self.drain()  # emptied every pass: what a pass cannot use is dropped
        if not running and not reconcile_only:
            return
        offered = drained if running else []  # a reconcile-only pass enters nothing
        reconcile_due = self.reconcile_at is None or now >= self.reconcile_at
        sweep_due = self.sweep_at is None or now >= self.sweep_at
        if not self.lease.ensure(work=bool(offered) or reconcile_due or sweep_due):
            return
        if not (offered or reconcile_due or sweep_due):
            return
        try:
            book = records.load_book(self.store)
        except records.BookUnreadable as exc:
            self.log_once("book_unreadable", "learning_account_status", {"route": self.route, "error": str(exc)})
            return
        try:
            if reconcile_due:
                book = reconcile(self, book, now)
            if sweep_due:
                sweep_untracked(self, book, now)
            if offered and not self.stopping:
                self.entries(book, offered, now)
        except records.BookConflict:
            self.log_once("book_conflict", "learning_account_status", {"route": self.route, "error": "book_changed"})

    # ---- entries ---------------------------------------------------------------------------
    def global_blockers(self, book: records.Book, now: datetime) -> list[str]:
        try:
            blockers = list(self.automation.scan_blockers())  # the main kill switch or pause
        except Exception:  # noqa: BLE001 - missing or failing: fail closed
            blockers = ["automation_unavailable"]
        reason = cap_reason(book.trades, ny_day(now))
        if reason:
            blockers.append(reason)
        if not blockers:
            clock = self.market_clock()
            if clock is None:
                blockers.append("market_clock_unavailable")
            elif not clock.get("is_open"):
                blockers.append("market_closed")
        return blockers

    def learning_verdicts(self, strategies: set[str], now: datetime) -> dict[str, Any] | None:
        """The learning verdicts: recomputed at most every 30 min while candidates wait, for the
        waiting strategies and those stored; 10 min of backoff after a failure."""

        if self._verdicts is None:
            try:
                parsed = json.loads(self.store.read(records.LEARNING_VERDICTS_KEY) or "{}")
            except Exception:  # noqa: BLE001 - recomputed below
                parsed = {}
            self._verdicts = parsed if isinstance(parsed, dict) else {}
        payload = self._verdicts
        age_ok = False
        try:
            computed = datetime.fromisoformat(str(payload.get("computed_at")))
            age_ok = computed.tzinfo is not None and (now - computed).total_seconds() < VERDICTS_EVERY_SECONDS
        except (TypeError, ValueError):
            age_ok = False
        backing_off = self._verdicts_retry_at is not None and now < self._verdicts_retry_at
        if not age_ok and not backing_off:
            stored = {str(name) for name in (payload.get("strategies") or []) if str(name)}
            wanted = set(strategies) | stored
            try:
                verdicts = compute_learning_verdicts(self.db, self.settings, wanted, now=now)
                payload = {"computed_at": now.isoformat(), "strategies": sorted(wanted), "verdicts": verdicts}
                self.store.put(records.LEARNING_VERDICTS_KEY, records.dumps(payload))
                self._verdicts, self._verdicts_retry_at = payload, None
            except Exception as exc:  # noqa: BLE001 - the stored ones serve meanwhile
                self._verdicts_retry_at = now + timedelta(seconds=VERDICTS_BACKOFF_SECONDS)
                self.log_once(
                    ("verdicts_failed", type(exc).__name__),
                    "learning_account_verdicts_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)[:300]},
                )
                payload = self._verdicts
        if stale(payload, now=now):
            return None
        verdicts = payload.get("verdicts")
        return verdicts if isinstance(verdicts, dict) else None

    def entries(self, book: records.Book, offered: list[tuple[datetime, Any]], now: datetime) -> records.Book:
        waiting: list[tuple[datetime, Any]] = []
        for scanned_at, candidate in offered:
            reason = prefilter_reason(self.settings, candidate) or fresh_reason(candidate, scanned_at, now)
            if reason:
                self.skip(reason, candidate)
            else:
                waiting.append((scanned_at, candidate))
        if not waiting:
            return book
        blockers = self.global_blockers(book, now)
        if blockers:
            for blocker in blockers:
                self.skip(blocker)
            return book
        verdicts = self.learning_verdicts({str(c.strategy_name) for _, c in waiting}, now)
        if verdicts is None:
            self.skip("learning_verdicts_unavailable")
            return book
        main = MainPathOnly(self.auto_trading)
        ranked: list[tuple[tuple[int, float, float], datetime, Any, str, dict[str, Any]]] = []
        for scanned_at, candidate in waiting:
            try:
                blockers = list(self.auto_trading.candidate_proposal_blockers(candidate, paper_only=main))
            except Exception:  # noqa: BLE001 - unknown is refused
                blockers = ["main_path_unreadable"]
            reason = main_path_reason(blockers)
            key = f"{candidate.strategy_name}:{str(candidate.timeframe or '').lower()}"
            kind, why = (None, reason) if reason else classify(
                self.settings, main.verdict(candidate), verdicts.get(key), candidate.timeframe, crypto=False
            )
            if kind is None:
                self.skip(why, candidate)
                continue
            learning = verdicts.get(key) or {}
            ranked.append((rank_key(kind, learning, candidate.score), scanned_at, candidate, kind, learning))
        ranked.sort(key=lambda item: item[0])
        for _, scanned_at, candidate, kind, learning in ranked:
            if self.stopping or self.route != routes.RUNNING:
                break
            reason = cap_reason(book.trades, ny_day(now))
            if reason:
                self.skip(reason, candidate)
                break
            book = self.enter(book, candidate, scanned_at, kind, learning, now)
        return book

    def entry_reasons(self, candidate: Any, quote: Any, price: float) -> list[str]:
        """The main path's own pre-submit checks, on the learning quote."""

        from app.execution.coordinator import ExecutionCoordinator

        reasons: list[str] = []
        if not str(getattr(quote, "source", "") or "").lower().startswith("alpaca"):
            reasons.append("quote_provider_not_alpaca")
        if bool(getattr(quote, "quote_derived_from_history", False)):
            reasons.append("quote_not_direct")
        age = getattr(quote, "data_age_seconds", None)
        if age is not None and float(age) > float(getattr(self.settings, "max_market_data_age_seconds", 120)):
            reasons.append("quote_too_old")
        if not price or price <= 0:
            reasons.append("quote_missing")
            return reasons
        planned = float(getattr(candidate, "entry_price", None) or getattr(candidate, "current_price", None) or 0.0)
        drift = abs(price - planned) / max(planned, 0.01) * 10_000.0 if planned > 0 else math.inf
        if drift > float(getattr(self.settings, "execution_max_entry_drift_bps", 35.0)):
            reasons.append("entry_drift_too_large")
        order = SimpleNamespace(side="buy", stop_loss=candidate.stop_loss, take_profit=candidate.take_profit)
        min_rr = float(getattr(self.settings, "execution_min_reward_to_risk_at_quote", 0.0) or 0.0)
        reasons += ExecutionCoordinator._alpaca_bracket_reasons(SimpleNamespace(order=order), price, min_rr)
        return reasons

    def size(self, candidate: Any, price: float) -> tuple[int, dict[str, Any]]:
        from app.risk.proposal_sizing import risk_based_proposal_notional

        amount, sizing = risk_based_proposal_notional(
            self.settings,
            entry_price=getattr(candidate, "entry_price", None) or getattr(candidate, "current_price", None),
            stop_price=getattr(candidate, "stop_loss", None),
            equity_usd=self.equity,
        )
        half = round(float(amount) * RISK_FRACTION, 2)
        return int(math.floor(half / max(price, 0.01))), {**sizing, "learning_amount_usd": half, "risk_fraction": RISK_FRACTION}

    def too_close_to_the_close(self, timeframe: str, now: datetime) -> bool:
        from app.backtesting.intraday_plan import is_intraday
        from app.learning_account.reconcile import flatten_minutes

        if not is_intraday(timeframe):
            return False
        close = (self.clock_state or {}).get("next_close")
        if not isinstance(close, datetime):
            return True
        return (close - now).total_seconds() <= (flatten_minutes(self.settings) + ENTRY_CLOSE_BUFFER_MINUTES) * 60.0

    def enter(
        self, book: records.Book, candidate: Any, scanned_at: datetime, kind: str, learning: dict[str, Any], now: datetime
    ) -> records.Book:
        symbol = str(candidate.symbol).upper()
        timeframe = str(candidate.timeframe or "").lower()
        if any(t.get("symbol") == symbol and t.get("status") in LIVE for t in book.trades.values()):
            self.skip("symbol_in_book", candidate)
            return book
        trade_id = trade_id_for(candidate, scanned_at)
        if trade_id in book.trades:
            return book
        if self.too_close_to_the_close(timeframe, now):
            self.skip("too_close_to_the_close", candidate)
            return book
        if not self.verify_for_entry(now):  # again before every entry
            self.skip("identity_not_verified", candidate)
            return book
        try:
            held = self.client().positions()
        except Exception:  # noqa: BLE001
            self.skip("positions_unreadable", candidate)
            return book
        if symbol in held:
            self.skip("symbol_held_at_broker", candidate)
            return book
        from app.learning_account.market import ReadsHung

        try:
            quote = self.market.quote(symbol)  # once; any failure refuses
        except ReadsHung:
            self.skip("market_data_reads_hung", candidate)
            return book
        except Exception:  # noqa: BLE001
            self.skip("quote_unavailable", candidate)
            return book
        price = float(getattr(quote, "last_execution", None) or getattr(quote, "ask", None) or getattr(quote, "bid", None) or 0.0)
        reasons = self.entry_reasons(candidate, quote, price) + self.market.direction_reasons(symbol, timeframe)
        if reasons:
            self.skip(reasons[0], candidate)
            return book
        qty, sizing = self.size(candidate, price)
        if qty < 1:
            self.skip("size_below_one_share", candidate)
            return book
        trade = {
            "trade_id": trade_id,
            "symbol": symbol,
            "strategy_name": str(candidate.strategy_name),
            "timeframe": timeframe,
            "kind": kind,
            "basis": learning.get("basis"),
            "evidence_r": learning.get("oos_expectancy_r"),
            "evidence_trades": learning.get("oos_trades"),
            "scan_at": scanned_at.isoformat(),
            "created_at": now.isoformat(),
            "day": ny_day(now),
            "status": "submitting",
            "qty": qty,
            "planned_entry": float(candidate.entry_price or price),
            "quote_price": price,
            "stop_loss": float(candidate.stop_loss),
            "take_profit": float(candidate.take_profit),
            "sizing": sizing,
            "client_order_id": entry_client_order_id(trade_id),
            "close_attempt": 0,
        }
        book = records.save_book(self.store, book, {**book.trades, trade_id: trade})  # before the send
        return self.send(book, trade_id)

    def send(self, book: records.Book, trade_id: str) -> records.Book:
        from app.learning_account.client import refused

        trade = dict(book.trades[trade_id])
        if self.stopping or not self.lease.may_act(self.stopping):
            why = "stopping" if self.stopping else "lease"
            trade.update(status="rejected", reason=f"not_sent:{why}")
            return records.save_book(self.store, book, {**book.trades, trade_id: trade})
        sent_at = self.now()
        try:
            order = self.client().submit_bracket(
                symbol=trade["symbol"],
                qty=int(trade["qty"]),
                take_profit=trade["take_profit"],
                stop_loss=trade["stop_loss"],
                client_order_id=trade["client_order_id"],
            )
        except Exception as exc:  # noqa: BLE001 - refused, or unknown (it keeps its slot)
            status = "rejected" if refused(exc) else "unknown"
            trade.update(status=status, submitted_at=sent_at.isoformat(), reason=f"{type(exc).__name__}: {exc}"[:200])
        else:
            trade.update(
                status="submitted",
                submitted_at=sent_at.isoformat(),
                broker_order_id=order.get("id"),
                order_status=order.get("status"),
            )
        book = records.save_book(self.store, book, {**book.trades, trade_id: trade})
        if trade["status"] != "rejected":  # something is live now: reconciled within a minute
            soon = sent_at + timedelta(seconds=TICK_SECONDS)
            self.reconcile_at = soon if self.reconcile_at is None else min(self.reconcile_at, soon)
        self.log("learning_account_entry", {key: trade.get(key) for key in (
            "trade_id", "symbol", "strategy_name", "timeframe", "kind", "status", "qty", "quote_price",
            "stop_loss", "take_profit", "client_order_id", "broker_order_id", "reason", "evidence_r",
        )})
        if trade["status"] != "rejected":
            self.notify(
                f"Entry {trade['status']}: {trade['symbol']} x{trade['qty']} ({trade['strategy_name']}:{trade['timeframe']}, {trade['kind']})",
                f"Quote {trade['quote_price']:.2f}  stop {trade['stop_loss']:.2f}  target {trade['take_profit']:.2f}",
                "Never copied to eToro; not the main paper account.",
            )
        return book


__all__ = ["LearningAccount", "MainPathOnly", "FEE_BAND"]
