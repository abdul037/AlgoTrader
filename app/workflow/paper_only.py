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
3. Never a symbol the real eToro account holds or has in flight: reconcile's open list (its
   raw instrument ids mapped back to symbols through the scorecard and the test order) and the
   scorecard's open and pending-fill trades, read as the mirror's room check reads them
   (``etoro_live_room.held_symbols``), once a pass, and again by the execution coordinator for
   a tagged order just before it is placed (``paper_only_held_reasons``). The exit copy
   follows paper BY SYMBOL (``copy_paper_exits``), so a paper-only position there would hold a
   real position open or close it at market. An unreadable record refuses.
4. Day headroom: it must leave 2 of the day's paper trades (``LEAVE_FOR_ETORO`` of
   ``max_trades_per_day``) and 1 of its correlation group's daily entries (``LEAVE_IN_GROUP``
   of ``max_daily_entries_per_correlation_bucket``; unclassified symbols have no group cap),
   counted as paper's own checks count them (today's execution rows). eToro copies up to 3 a
   day (``HARD_MAX_TRADES_PER_DAY``, 2 until 2026-10-26); paper's own cap is far above what it
   uses (at most 2 entries a day so far), so a third copy is not crowded out in practice, but
   it is not reserved either. What it guarantees, exactly: paper-only entries
   never take the day's last two paper trades. Those two can still go to any untagged entry,
   including one the mirror then refuses under its own rules (its +0.03R bar, the 24 h
   streak, the planner ...), so this bounds the copies paper-only entries cost; it does not
   promise two copies. With the production group cap of 2 a group takes a paper-only entry
   only while it has none that day, and keeps one entry for an eToro-eligible one; a second
   eToro-eligible entry in that group that day is then refused. Leaving 2 would bar 23 of the
   24 universe symbols, all grouped (operator decision, 2026-10-22).
5. Book headroom, from paper's own proposal-time risk context (``build_risk_context``: the
   broker's open positions) and today's entries not yet filled (execution rows still
   ``submitted``, ``accepted`` ...; paper's own open-position check does not count those):
   - not a symbol paper already holds or has such an unfilled entry in;
   - fewer than ``MAX_OPEN_PAPER_ONLY`` paper-only positions open or unfilled (a held
     position whose entry is not among the newest ``BOOK_ROWS`` execution rows counts as one:
     it cannot be told apart);
   - open + unfilled + 1 + ``LEAVE_FOR_ETORO`` <= capacity, capacity = the lower of
     ``max_open_positions`` and ``floor(portfolio_max_heat_pct / per-trade risk cap)``.
     The heat check counts every open position at the per-trade risk cap, which bounds this
     entry's own risk too, and it still applies when eToro's room authority replaces paper's
     open-count check, so heat is what such positions use up.
   So a paper-only entry never takes one of the last two places in paper's open room or heat
   (two places; leaving every free eToro slot would bar paper-only entries altogether). The residual, stated: while paper's open room or heat is full, every
   eToro-eligible entry waits for a position to close, on any day; paper-only positions can
   hold up to ``MAX_OPEN_PAPER_ONLY`` (3) of paper's slots for as long as they are open. With
   paper's 12 places, eToro's own nine still fit beside them when every other paper position
   is copied; other paper positions eToro would not copy leave it fewer.
   Steps 4 and 5 come from one read of paper's state a pass (one risk context and one list of
   the newest execution rows), read again after each proposal the pass creates. Unreadable
   inputs refuse.
6. Automatic only: a paper-only proposal is made only when the unattended path would approve
   and execute it at once (``candidate_blockers`` is empty, checked last; so none with paper
   auto-approval off or outside the unattended mode, and none for a strategy the auto-approval
   tier refuses); one whose order was
   not placed in its own pass (refused, its queue item blocked, or an error) is rejected at
   once (``execute``), and a person cannot approve one (``ProposalService.approve_proposal``
   refuses: paper-only entries are automatic only). So none waits: a waiting one would hold
   its symbol against eToro-eligible entries (open proposals block a symbol) and its later
   approval would skip steps 4 and 5. Other proposals still waiting for a person's approval
   are not counted in steps 4 and 5. A paper-only proposal left open by a crash in its own
   pass holds its symbol until it expires (``proposal_expiry_minutes``).
7. Tag: its proposal and order carry ``paper_only: true`` and ``paper_only_reason: "oos_only"``
   (``tags``), which make the mirror refuse it (``etoro_live_paper_only``) and keep it out of
   the strategy-name switches, the auto-approval bootstrap and the go-live and weekly-target
   readiness (app/performance/paper_only.py); its notices say it is not copied (``notice``).

With ``paper_oos_bar_enabled`` or ``require_strategy_oos_evidence`` off (or outside the main
paper account) nothing is paper-only and the pass reads nothing: the gate reads the runtime
state itself, exactly as before option B. A focused swing run's paper-only specs ride on its
scan (app/workflow/paper_only_riders.py): their candidates get a second pass of their own after
the run's pass, which (``only=True``) considers only the candidates that are paper-only by its
own verdicts read (a key whose verdict changed since the run listed it is left out). The riders'
step reads the pass's ``room`` and ``taken`` before it spends anything on candidate checks, and
the pass reads paper again (``refresh``) before its candidates.

Not changed (residuals, stated): paper-only losses count toward paper's daily and weekly loss
limits, the consecutive-loss cooldown and the drawdown governor like any paper trade, so they
can halt eToro-eligible entries for the rest of the day; a paper-only position holds its
symbol's one paper slot (and its re-entry cooldown after a loss). Paper's per-symbol limit
(``positions_by_symbol``, the broker's positions) keeps an untagged entry out of a symbol with
an open paper-only position; for the seconds between a paper-only order's submission and its
fill the broker shows no position, so only the pass's own proposals and the open proposals
guard that symbol then. Paper-only outcomes also feed the learning meta-model's training data,
which can only lower a candidate's score or block it (``LearningService.apply_to_snapshot``),
never make one eligible: at most it changes which eligible candidate ranks first, never the
eToro caps.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

from app.utils.time import utc_now

LEAVE_FOR_ETORO = 2  # paper trades a day, and open-position room, a paper-only entry must leave
LEAVE_IN_GROUP = 1  # entries in its correlation group that day a paper-only entry must leave
# Paper-only positions open or unfilled at once: the one place to change it (every rule, test
# and notice reads this name).
MAX_OPEN_PAPER_ONLY = 3  # paper's 12 places less eToro's 9 (operator decision, 2026-10-22)
MAX_PER_PASS = 1  # paper-only proposals an auto-propose pass may create
PROPOSAL_SECONDS = 35.0  # the pass's reads and one paper-only proposal (measured 20-35 s)
BOOK_ROWS = 500  # newest execution rows read to tell paper-only positions apart
PREFIX = "paper_only_"  # its proposal blockers
_UNREADABLE = object()  # a read that failed: the pass's paper-only entries stand aside
_UNFILLED = frozenset({"submitted", "partially_filled", "accepted", "new", "pending_new"})
_ENTRY_STATUSES = _UNFILLED | {"filled"}  # placed or filled, as entries_today_by_bucket counts
_LOGGED: dict[str, set[tuple[Any, ...]]] = {}  # paper-only blocks logged today, this process


class PaperOnlyPass:
    """One auto-propose pass's paper-only decisions; ``auto_trading`` is the
    ``PaperAutoTradingService`` (None: no gate, so nothing is paper-only)."""

    def __init__(self, auto_trading: Any, *, only: bool = False):
        self.auto = auto_trading
        self.only = only  # the riders' pass: nothing but paper-only candidates is considered
        self.created_count = 0  # every proposal the pass created (even if it then raised)
        self._reads: dict[str, Any] = {}
        self._paper_only_created = 0

    def enabled(self) -> bool:
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
        if not self.enabled():
            return runtime_state
        if "verdicts" not in self._reads:  # a failed read raises, as the gate's own read did
            self._reads["verdicts"] = runtime_state.get(VERDICTS_KEY)
        return SimpleNamespace(get={VERDICTS_KEY: self._reads["verdicts"]}.get)

    def is_paper_only(self, candidate: Any) -> bool:
        """True when the main paper gate admits ``candidate`` only through the paper OOS bar."""

        from app.broker import crypto as crypto_symbols
        from app.performance.strategy_evidence import VERDICTS_KEY, paper_oos_only

        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        if not self.enabled() or crypto_symbols.is_crypto_symbol(symbol):
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
        """The pass's candidates, every paper-only one after all the others, ranks kept; in a
        paper-only pass, its paper-only candidates alone (by this pass's verdicts read)."""

        if self.only:
            return [candidate for candidate in candidates if self.is_paper_only(candidate)]
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
        book = self._read("paper", self._paper_book)
        if book is _UNREADABLE:
            return [PREFIX + "paper_book_unreadable"]
        reason = _headroom(self.auto.settings, (book.trades, book.by_bucket), symbol)
        reason = reason or _book_headroom(self.auto.settings, book, _bare(symbol))
        if reason:
            return [PREFIX + reason]
        return self._automatic(candidate)

    def room(self) -> str | None:
        """The blocker every paper-only candidate of this pass would meet whatever its symbol
        (the pass limit, the cadence budget, an unreadable record, paper's day and book
        headroom, from the same reads ``blockers`` uses), else None: the riders' step checks it
        before it spends anything on candidate checks."""

        if self._paper_only_created >= MAX_PER_PASS:
            return PREFIX + "pass_limit"
        if _budget_spent():
            return PREFIX + "cadence_budget_spent"
        if self._read("held", self._etoro_held) is _UNREADABLE:
            return PREFIX + "etoro_live_state_unreadable"
        book = self._read("paper", self._paper_book)
        if book is _UNREADABLE:
            return PREFIX + "paper_book_unreadable"
        reason = _headroom(self.auto.settings, (book.trades, {}), "")  # no symbol: no group
        reason = reason or _book_headroom(self.auto.settings, book, "")  # nor a held symbol
        return PREFIX + reason if reason else None

    def taken(self) -> set[str]:
        """The symbols no paper-only entry of this pass may open, as ``room`` read them (call it
        first): the eToro account's held and in flight (rule 3), paper's held and unfilled
        (rule 5)."""

        held, book = self._reads.get("held"), self._reads.get("paper")
        taken = {_bare(symbol) for symbol in held} if isinstance(held, set) else set()
        if isinstance(book, SimpleNamespace):
            taken |= set(book.held) | set(book.unfilled)
        return taken

    def refresh(self) -> None:
        """Read eToro's held symbols and paper again before the next candidate (rules 3 to 5
        on a fresh read)."""

        self._reads.pop("held", None)
        self._reads.pop("paper", None)

    def _automatic(self, candidate: Any) -> list[str]:
        """Rule 6: [] when the unattended path would approve and execute it at once."""

        try:
            reasons = list(self.auto.candidate_blockers(candidate))
        except Exception:  # noqa: BLE001 - unknown is refused
            reasons = ["candidate_blockers_unreadable"]
        if not reasons:
            return []
        return [PREFIX + "not_automatic", *(f"{PREFIX}auto_{reason}" for reason in reasons)]

    def execute(self, service: Any, proposal: Any, candidate: Any, funnel: Any) -> Any:
        """``approve_enqueue_execute`` for a proposal the pass created; a paper-only one whose
        order was not placed in this pass is rejected at once (rule 6)."""

        from app.models.execution_queue import ExecutionQueueStatus
        from app.performance.paper_only import is_paper_only

        auto = service.auto_trading
        if not is_paper_only(getattr(getattr(proposal, "order", None), "metadata", None)):
            return auto.approve_enqueue_execute(proposal, candidate, funnel=funnel)
        processed = None
        try:
            processed = auto.approve_enqueue_execute(proposal, candidate, funnel=funnel)
        finally:
            if str(getattr(processed, "status", "") or "") != ExecutionQueueStatus.EXECUTED:
                _withdraw(service, proposal, processed)
        return processed

    def tags(self, candidate: Any) -> dict[str, Any]:
        """The proposal/order metadata of a paper-only candidate, else {}."""

        from app.performance.paper_only import OOS_ONLY, PAPER_ONLY_FIELD, PAPER_ONLY_REASON_FIELD

        if not self.is_paper_only(candidate):
            return {}
        return {PAPER_ONLY_FIELD: True, PAPER_ONLY_REASON_FIELD: OOS_ONLY}

    def notice(self, candidate: Any) -> list[str]:
        """The proposal notice's extra line for a paper-only candidate, else []."""

        if not self.is_paper_only(candidate):
            return []
        return ["Paper-only (not copied to eToro)", "Automatic only: /approve is refused for it"]

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

        self.created_count += 1
        if self.is_paper_only(candidate):
            self._paper_only_created += 1
        self._reads.pop("paper", None)

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

    def _paper_book(self) -> SimpleNamespace:
        """One read of paper's state for the pass: its proposal-time risk context (open
        positions; today's trades as paper's trades-a-day check counts them) and one list of
        the newest execution rows, which gives today's entries per correlation group (as
        ``entries_today_by_bucket`` counts them for paper's group check), today's entries not
        yet filled and which of those and the open positions are paper-only."""

        from app.performance.paper_only import request_is_paper_only
        from app.risk.context import build_risk_context, entries_today_by_bucket

        broker = getattr(getattr(self.auto, "proposals", None), "broker", None)
        context = build_risk_context(self.auto.settings, broker, self.auto.executions)
        rows = list(self.auto.executions.list(limit=BOOK_ROWS))  # newest first
        newest = SimpleNamespace(list=lambda limit=200: rows[:limit])  # paper's own 200 rows
        by_bucket = entries_today_by_bucket(newest, _start_of_day())
        held = {_bare(symbol) for symbol, count in context.positions_by_symbol.items() if count}
        latest: dict[str, Any] = {}  # each symbol's newest entry row
        for execution in rows:
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
            trades=int(context.trades_today),
            by_bucket=by_bucket,
            open=int(context.open_positions),
            held=held,
            unfilled=unfilled,
            paper_only=paper_only,
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


def scan_wanted(service: Any) -> bool:
    """Whether a focused swing run takes paper-only riders at all: something can be paper-only
    on this account (``PaperOnlyPass.enabled``) and its proposals would be made and executed
    automatically (rule 6): paper auto-approval on, which also turns auto-propose on
    (``auto_propose_candidates``), and the unattended mode."""

    settings = getattr(service, "settings", None)
    return (
        PaperOnlyPass(getattr(service, "auto_trading", None)).enabled()
        and bool(getattr(settings, "paper_auto_approve_proposals", False))
        and str(getattr(settings, "paper_auto_operation_mode", "shadow")) == "unattended"
    )


def _withdraw(service: Any, proposal: Any, processed: Any) -> None:
    """Reject a paper-only proposal whose order was not placed in its pass (never raises)."""

    from app.models.approval import ApprovalDecisionRequest
    from app.performance.paper_only import AUTO_REVIEWER

    reason = str(getattr(processed, "validation_reason", "") or "not_executed")
    placed = _placed(service, proposal)
    rejected = False
    if not placed:  # an order the broker has is left to its proposal; its position is tagged
        try:
            service.proposal_service.reject_proposal(
                proposal.id,
                ApprovalDecisionRequest(
                    reviewer=AUTO_REVIEWER,
                    notes=f"Paper-only entries are automatic only; not placed in its pass: {reason}",
                ),
            )
            rejected = True
        except Exception:  # noqa: BLE001 - not open any more (expired, executed): nothing holds
            rejected = False
    with suppress(Exception):  # a log line never fails the pass
        service.run_logs.log(
            "paper_only_proposal_withdrawn",
            {
                "proposal_id": proposal.id,
                "symbol": str(getattr(proposal.order, "symbol", "") or ""),
                "queue_status": str(getattr(processed, "status", "") or "") or None,
                "reason": reason,
                "rejected": rejected,
                "order_status": placed or None,
            },
        )


def _placed(service: Any, proposal: Any) -> str:
    """The status of the proposal's latest execution when its order reached the broker
    (submitted or filled: an error after the submission), else "" (also when unreadable)."""

    from app.models.execution import ExecutionStatus

    with suppress(Exception):
        row = service.auto_trading.executions.get_latest_by_proposal_id(proposal.id)
        status = str(getattr(row, "status", "") or "")
        if status in {ExecutionStatus.SUBMITTED, ExecutionStatus.FILLED}:
            return status
    return ""


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
