"""Paper-only entries in the auto-propose pass (operator option B, 2026-10-21).

An entry the main paper gate admits ONLY through the paper OOS bar
(``strategy_evidence.paper_oos_only``: its pooled OOS evidence passes, its holdout does not) is
paper-only: the eToro live mirror refuses it. It shares the paper account with the entries the
mirror copies, so it stands aside for them. What it may cost eToro copies is bounded and stated
here ("fewer copies, never extra ones"); no paper risk gate is loosened: every candidate still
meets every gate, and these rules only refuse more. ``PaperOnlyPass`` (one per auto-propose
pass) gives a paper-only entry these rules:

1. Order: the pass considers every other candidate before a paper-only one (a stable split of
   the ranked list), so a paper-only candidate never takes a symbol, or a shared daily
   allowance, from an eToro-eligible one in the same pass.
2. Bounded work: at most ``MAX_PER_PASS`` paper-only proposal a pass, and none once a cadence
   tick is within ``PROPOSAL_SECONDS`` of its scan stop (the tick's post-scan reserve stays for
   the work it was reserved for).
3. Never a symbol the real eToro account holds or has in flight: reconcile's open list, the
   scorecard's open and pending-fill trades and the test order, read as the mirror's room
   check reads them (``etoro_live_room.held_symbols``), once a pass, and again by the
   execution coordinator for a tagged order just before it is placed
   (``paper_only_held_reasons``: a proposal may wait for an approval). The exit copy follows
   paper BY SYMBOL (``copy_paper_exits``), so a paper-only position there would hold a real
   position open or close it at market. An unreadable record refuses.
4. Day headroom: it must leave 2 of the day's paper trades (``LEAVE_FOR_ETORO`` of
   ``max_trades_per_day``) and 1 of its correlation group's daily entries (``LEAVE_IN_GROUP``
   of ``max_daily_entries_per_correlation_bucket``; unclassified symbols have no group cap),
   counted as paper's own checks count them (today's execution rows). Two is the most eToro
   copies a day (``HARD_MAX_TRADES_PER_DAY``). What it guarantees, exactly: paper-only entries
   never take the day's last two paper trades. Those two can still go to any untagged entry,
   including one the mirror then refuses under its own rules (its +0.03R bar, the 24 h
   streak, the planner ...), so this bounds the copies paper-only entries cost; it does not
   promise two copies. With the production group cap of 2 a group takes a paper-only entry
   only while it has none that day, and keeps one entry for an eToro-eligible one; a second
   eToro-eligible entry in that group that day is then refused. Leaving 2 would bar 23 of the
   24 universe symbols, all grouped (operator decision, 2026-10-22).
5. Book headroom, from paper's own proposal-time risk context (``build_risk_context``: the
   broker's open positions) and today's entries not yet filled (execution rows still
   ``submitted``, ``accepted`` ...; paper's own open-position check does not count those), so
   positions held for days cannot crowd out eToro-eligible entries:
   - not a symbol paper already holds or has such an unfilled entry in;
   - fewer than ``MAX_OPEN_PAPER_ONLY`` (4) paper-only positions open or unfilled (a held
     position whose entry is not among the newest ``BOOK_ROWS`` execution rows counts as one:
     it cannot be told apart);
   - open + unfilled + 1 + ``LEAVE_FOR_ETORO`` <= capacity, capacity = the lower of
     ``max_open_positions`` and ``floor(portfolio_max_heat_pct / per-trade risk cap)``.
     The heat check counts every open position at the per-trade risk cap, which bounds this
     entry's own risk too, and it still applies when eToro's room authority replaces paper's
     open-count check, so heat is what such positions would use up.
   Two matches eToro's daily copy cap (leaving every free eToro slot would bar paper-only
   entries altogether); the operator-accepted residual is that, with paper full but for two,
   a third eToro-eligible entry that day waits for a position to close.
   Steps 4 and 5 are read once a pass and again after each proposal the pass creates;
   proposals still waiting for a person's approval are not counted (the unattended account
   has none). Unreadable inputs refuse.
6. Tag: its proposal and order carry ``paper_only: true`` and ``paper_only_reason: "oos_only"``
   (``tags``), which make the mirror refuse it (``etoro_live_paper_only``) and keep it out of
   the strategy-name switches, the auto-approval bootstrap and the go-live and weekly-target
   readiness (app/performance/paper_only.py); its notices say it is not copied (``notice``).

With ``paper_oos_bar_enabled`` or ``require_strategy_oos_evidence`` off (or outside the main
paper account) nothing is paper-only and the pass reads nothing: the gate reads the runtime
state itself, exactly as before option B.

Not changed (residuals, stated): paper-only losses count toward paper's daily and weekly loss
limits, the consecutive-loss cooldown and the drawdown governor like any paper trade, so they
can halt eToro-eligible entries for the rest of the day; a paper-only position holds its
symbol's one paper slot (and its re-entry cooldown after a loss). Paper's per-symbol limit
(``positions_by_symbol``, the broker's positions) keeps an untagged entry out of a symbol with
an open paper-only position; for the seconds between a paper-only order's submission and its
fill the broker shows no position, so only the pass's own proposals and the open proposals
guard that symbol then.
"""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace
from typing import Any

from app.utils.time import utc_now

LEAVE_FOR_ETORO = 2  # paper trades a day, and open-position room, a paper-only entry must leave
LEAVE_IN_GROUP = 1  # entries in its correlation group that day a paper-only entry must leave
MAX_OPEN_PAPER_ONLY = 4  # paper-only positions open or unfilled at once
MAX_PER_PASS = 1  # paper-only proposals an auto-propose pass may create
PROPOSAL_SECONDS = 20.0  # one paper-only proposal, its execution and the copy check
BOOK_ROWS = 500  # newest execution rows read to tell paper-only positions apart
PREFIX = "paper_only_"  # its proposal blockers
_UNREADABLE = object()  # a read that failed: the pass's paper-only entries stand aside
_UNFILLED = frozenset({"submitted", "partially_filled", "accepted", "new", "pending_new"})
_ENTRY_STATUSES = _UNFILLED | {"filled"}  # placed or filled, as entries_today_by_bucket counts
_LOGGED: dict[str, set[tuple[Any, ...]]] = {}  # paper-only blocks logged today, this process


class PaperOnlyPass:
    """One auto-propose pass's paper-only decisions; ``auto_trading`` is the
    ``PaperAutoTradingService`` (None: no gate, so nothing is paper-only)."""

    def __init__(self, auto_trading: Any):
        self.auto = auto_trading
        self._reads: dict[str, Any] = {}
        self._paper_only_created = 0

    def _enabled(self) -> bool:
        """Settings alone: whether anything can be paper-only on this account."""

        settings = getattr(self.auto, "settings", None)
        return (
            getattr(self.auto, "runtime_state", None) is not None
            and str(getattr(settings, "execution_mode", "")) == "paper"
            and not bool(getattr(settings, "enable_real_trading", False))
            and bool(getattr(settings, "require_strategy_oos_evidence", False))
            and bool(getattr(settings, "paper_oos_bar_enabled", False))  # absent: off
        )

    @property
    def state(self) -> Any:
        """For ``evidence_blocker``: a read-only runtime_state holding the verdicts as this pass
        first read them; while nothing can be paper-only, the runtime_state itself (None
        without one), which the gate reads as it did before option B."""

        from app.performance.strategy_evidence import VERDICTS_KEY

        runtime_state = getattr(self.auto, "runtime_state", None)
        if not self._enabled():
            return runtime_state
        if "verdicts" not in self._reads:  # a failed read raises, as the gate's own read did
            self._reads["verdicts"] = runtime_state.get(VERDICTS_KEY)
        return SimpleNamespace(get={VERDICTS_KEY: self._reads["verdicts"]}.get)

    def is_paper_only(self, candidate: Any) -> bool:
        """True when the main paper gate admits ``candidate`` only through the paper OOS bar."""

        from app.broker import crypto as crypto_symbols
        from app.performance.strategy_evidence import VERDICTS_KEY, paper_oos_only

        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        if not self._enabled() or crypto_symbols.is_crypto_symbol(symbol):
            return False
        if "parsed" not in self._reads:
            try:
                parsed = json.loads(self.state.get(VERDICTS_KEY) or "{}").get("verdicts")
            except (TypeError, ValueError, AttributeError):
                parsed = None  # unreadable: the gate refuses every candidate anyway
            self._reads["parsed"] = parsed if isinstance(parsed, dict) else {}
        verdicts = self._reads["parsed"]
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        verdict = verdicts.get(f"{getattr(candidate, 'strategy_name', '') or ''}:{timeframe}")
        return paper_oos_only(self.auto.settings, verdict, timeframe)

    def order(self, candidates: list[Any]) -> list[Any]:
        """The pass's candidates, every paper-only one after all the others, ranks kept."""

        return sorted(candidates, key=self.is_paper_only)

    def blockers(self, candidate: Any) -> list[str]:
        """Why a paper-only candidate must stand aside; [] for any other, and without a read
        for one the auto-propose loop drops next anyway (``candidate_propose_drop_reason``)."""

        from app.automation.reliability import candidate_propose_drop_reason

        if not self.is_paper_only(candidate) or candidate_propose_drop_reason(candidate):
            return []
        if self._paper_only_created >= MAX_PER_PASS:
            return [PREFIX + "pass_limit"]
        if _budget_spent():
            return [PREFIX + "cadence_budget_spent"]
        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        held = self._read("held", self._etoro_held)
        if held is _UNREADABLE:
            return [PREFIX + "etoro_live_state_unreadable"]
        if symbol in held:
            return [PREFIX + "etoro_live_symbol_held"]
        day = self._read("day", self._paper_day)
        if day is _UNREADABLE:
            return [PREFIX + "paper_day_unreadable"]
        reason = _headroom(self.auto.settings, day, symbol)
        if reason:
            return [PREFIX + reason]
        book = self._read("book", self._paper_book)
        if book is _UNREADABLE:
            return [PREFIX + "paper_book_unreadable"]
        reason = _book_headroom(self.auto.settings, book, _bare(symbol))
        return [PREFIX + reason] if reason else []

    def tags(self, candidate: Any) -> dict[str, Any]:
        """The proposal/order metadata of a paper-only candidate, else {}."""

        from app.performance.paper_only import OOS_ONLY, PAPER_ONLY_FIELD, PAPER_ONLY_REASON_FIELD

        if not self.is_paper_only(candidate):
            return {}
        return {PAPER_ONLY_FIELD: True, PAPER_ONLY_REASON_FIELD: OOS_ONLY}

    def notice(self, candidate: Any) -> list[str]:
        """The proposal notice's extra line for a paper-only candidate, else []."""

        return ["Paper-only (not copied to eToro)"] if self.is_paper_only(candidate) else []

    def log_blocked(self, candidate: Any, blockers: list[str]) -> bool:
        """Whether the loop logs this safety block: always for an entry that is not paper-only,
        and once a UTC day (in this process) per symbol, key and blockers for one that is: a
        paper-only candidate refused for room comes back every scan."""

        if not self.is_paper_only(candidate):
            return True
        day = utc_now().date().isoformat()
        for other in [key for key in _LOGGED if key != day]:
            _LOGGED.pop(other, None)
        key = (
            str(getattr(candidate, "symbol", "") or "").upper(),
            str(getattr(candidate, "strategy_name", "") or ""),
            str(getattr(candidate, "timeframe", "") or "").lower(),
            tuple(blockers),
        )
        seen = _LOGGED.setdefault(day, set())
        if key in seen:
            return False
        seen.add(key)
        return True

    def created(self, candidate: Any) -> None:
        """The pass created a proposal: the next paper-only candidate reads paper's day and
        book again (by then the entry, if it was placed, is one of today's execution rows)."""

        if self.is_paper_only(candidate):
            self._paper_only_created += 1
        self._reads.pop("day", None)
        self._reads.pop("book", None)

    def _read(self, name: str, read: Any) -> Any:
        if name not in self._reads:
            try:
                self._reads[name] = read()
            except Exception:  # noqa: BLE001 - fail closed, for the rest of the pass too
                self._reads[name] = _UNREADABLE
        return self._reads[name]

    def _etoro_held(self) -> set[str]:
        """The symbols the eToro live room counts, from the mirror's records (one round trip);
        raises when one is unreadable (the room check itself would read it as empty)."""

        from app.broker.etoro_live_room import held_symbols

        mirror = getattr(getattr(self.auto, "execution", None), "etoro_live_mirror", None)
        return held_symbols(getattr(mirror, "state", None) or self.auto.runtime_state)

    def _paper_day(self) -> tuple[int, dict[str, int]]:
        """Today's paper trades and entries per correlation group, as ``build_risk_context``
        counts them for paper's own trades-a-day and group checks."""

        from app.risk.context import entries_today_by_bucket

        executions = self.auto.executions
        start_of_day = _start_of_day()
        return int(executions.count_since(start_of_day)), entries_today_by_bucket(
            executions, start_of_day
        )

    def _paper_book(self) -> SimpleNamespace:
        """Paper's open positions as its proposal-time risk check reads them, today's entries
        not yet filled and how many of both are paper-only."""

        from app.performance.paper_only import request_is_paper_only
        from app.risk.context import build_risk_context

        broker = getattr(getattr(self.auto, "proposals", None), "broker", None)
        context = build_risk_context(self.auto.settings, broker, self.auto.executions)
        held = {_bare(symbol) for symbol, count in context.positions_by_symbol.items() if count}
        latest: dict[str, Any] = {}  # each symbol's newest entry row
        for execution in self.auto.executions.list(limit=BOOK_ROWS):  # newest first
            symbol = _bare((execution.request_payload or {}).get("symbol"))
            if symbol and str(execution.status or "").lower() in _ENTRY_STATUSES:
                latest.setdefault(symbol, execution)
        since = _start_of_day().isoformat()
        unfilled = {
            symbol
            for symbol, execution in latest.items()
            if symbol not in held
            and str(execution.status or "").lower() in _UNFILLED
            and str(execution.created_at or "") >= since
        }
        paper_only = {
            symbol
            for symbol in held | unfilled
            if symbol not in latest or request_is_paper_only(latest[symbol].request_payload)
        }
        return SimpleNamespace(
            open=int(context.open_positions), held=held, unfilled=unfilled, paper_only=paper_only
        )


def _headroom(settings: Any, day: tuple[int, dict[str, int]], symbol: str) -> str | None:
    from app.risk.sectors import UNKNOWN_BUCKET, correlation_bucket_for_symbol

    trades, by_bucket = day
    if trades + 1 + LEAVE_FOR_ETORO > int(getattr(settings, "max_trades_per_day", 999999)):
        return "headroom_trades_per_day"
    cap = int(getattr(settings, "max_daily_entries_per_correlation_bucket", 0) or 0)
    bucket = correlation_bucket_for_symbol(symbol)
    if cap > 0 and bucket != UNKNOWN_BUCKET and by_bucket.get(bucket, 0) + 1 + LEAVE_IN_GROUP > cap:
        return "headroom_correlation_bucket"
    return None


def _book_headroom(settings: Any, book: SimpleNamespace, symbol: str) -> str | None:
    from app.risk.rules import effective_max_risk_per_trade_pct

    if symbol in book.held or symbol in book.unfilled:
        return "paper_symbol_held"
    if len(book.paper_only) >= MAX_OPEN_PAPER_ONLY:
        return "positions_cap"
    after = book.open + len(book.unfilled) + 1 + LEAVE_FOR_ETORO
    if after > int(getattr(settings, "max_open_positions", 0) or 0):
        return "headroom_open_positions"
    heat_pct = float(getattr(settings, "portfolio_max_heat_pct", 0.0) or 0.0)
    live = str(getattr(settings, "execution_mode", "")) == "live"
    per_trade_pct = effective_max_risk_per_trade_pct(settings, live=live)
    if heat_pct > 0 and per_trade_pct > 0 and after > math.floor(heat_pct / per_trade_pct + 1e-9):
        return "headroom_portfolio_heat"
    return None


def _budget_spent() -> bool:
    """True inside a cadence tick within ``PROPOSAL_SECONDS`` of its scan stop."""

    from app.workflow.cadence_budget import current_budget

    budget = current_budget()
    return (
        budget is not None
        and budget.enforces_scan_stop
        and time.monotonic() >= budget.scan_stop_at - PROPOSAL_SECONDS
    )


def _bare(symbol: Any) -> str:
    """A symbol as the broker and the order rows both spell it (``BTC/USD`` is ``BTCUSD``)."""

    return str(symbol or "").strip().upper().replace("/", "")


def _start_of_day() -> Any:
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
