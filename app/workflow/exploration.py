"""The paper exploration lane (operator Phase 1, 2026-11-10).

No strategy passes the evidence gate, so paper barely trades and nothing builds a live record.
The operator's decision: on the main Alpaca paper account, a candidate refused ONLY because its
strategy lacks evidence may open a small paper-only "exploration" entry, so every strategy and
timeframe (daily and 5m/10m/15m/1h) collects live paper trades, which the leverage simulation
(app/performance/leverage_sim.py) then reads at 1x/2x/5x/10x. Paper money only; exploration never
reaches real money (below).

What the lane bypasses (the evidence family), and nothing else:

* at the proposal gate (``PaperAutoTradingService.candidate_proposal_blockers``):
  ``strategy_lacks_oos_evidence`` (no walk-forward verdict, too few OOS or holdout trades, OOS or
  holdout expectancy not positive, a minute key's venue cost profiles failing) and
  ``strategy_evidence_stale`` (the verdicts were not refreshed within 24 h): ``PROPOSAL_BYPASS``;
* at the unattended path's execution check (``candidate_blockers``):
  ``candidate_not_backtest_validated`` (the screener's per-symbol backtest validation) and the
  tier-3 ``insufficient_strategy_closed_trade_evidence`` (fewer than 30 closed normal trades):
  ``EXECUTION_BYPASS``.

Not bypassed, by decision (open questions for the operator): negative LIVE evidence on the
strategy's own normal paper trades -- ``strategy_inactive`` (strategy health), the auto-demotion
(it drops the specs before the scan, so the lane never sees them; scanning them would cost scan
time inside the 240 s cadence cap and bring back their alerts and tracked signals) and tier 3's
``strategy_expectancy_not_positive`` / ``strategy_profit_factor_below_auto_gate``; the operator's
``strategy_retired``; the governance records (``strategy_not_production_approved``,
``strategy_not_qualified_for_tier3_auto``); and every hard gate (spread, reward:risk, bracket,
relative volume, regular hours, blacklist, sector/symbol/group caps, loss limits, heat, the
auto-approval tier and its clean-lifecycle bootstrap). A candidate with any of those is refused
exactly as before. Only a candidate the evidence gate refused is considered at all, so an
eToro-eligible entry never becomes an exploration one.

Focus (operator 2026-11-22: "Focus practice on intraday"). In its first full week the lane
spread 15 closed entries over 11 strategy/timeframe keys, at most 2 a key, so no key was on course
for the 30 closed trades the leverage proof rule needs; and entries of other keys took the day's
one paper-only entry of a correlation group before a focus signal came (3 of the 5 group refusals
of focus shorts that week). ``paper_exploration_lane_focus`` names the keys to concentrate on
(``<strategy>:<timeframe>``; a short ``<strategy>:short:<timeframe>``, as the leaderboard keys
them). With a focus set, a candidate of any other key is still an exploration candidate, but:

* it is admitted only from ``INTRADAY_LAST_ENTRY`` New York, when the day's intraday entries are
  over (``focus_only`` before that), so it cannot take a symbol, a correlation group's daily
  entry, a place or a trade of the day a focus signal might need; an intraday key outside the
  focus therefore opens nothing (rule 4 refuses it from that same minute); and
* it never takes the last ``FOCUS_RESERVED_OPEN`` (4) of the lane's open places
  (``focus_reserve``), which positions held for days would otherwise fill.

No cap, gate or bypass changes for a focus key, and nothing is loosened for any key: the focus
only orders and withholds. An empty focus is the lane as it was.

Its rules (``ExplorationLane``, one per auto-propose pass, owned by its ``PaperOnlyPass``), in
order, each read once a pass and shared with the OOS-only paper-only entries' rules:

1. Eligible: the lane on (``paper_exploration_lane_enabled``, paper mode, real trading off), an
   equity (never crypto), a timeframe in ``paper_exploration_lane_timeframes`` and a candidate the
   auto-propose loop would not drop anyway (execution-ready, alert-eligible, long, with a stop).
   Shorts step 3 (2026-11-16): with ``paper_short_entries_enabled`` a short is not dropped; it is
   judged on its OWN short verdict and needs Alpaca's borrow and margin facts
   (app/workflow/paper_shorts.py); it shares every rule and cap here with the exploration longs.
2. Order: considered after every eToro-eligible and OOS-only candidate of the pass, the focus
   keys (below) before the others.
3. Bounded work: at most ``paper_exploration_lane_max_per_pass`` (2; 1 until 2026-11-22)
   exploration proposals a pass, none within ``paper_only.PROPOSAL_SECONDS`` of a cadence tick's
   scan stop or after the pass's own entry deadline (the next-tick pass of
   app/workflow/exploration_deferred.py sets one), and at most one unattended-path check
   (``candidate_blockers``) more than that (``SPARE_CHECKS``) a pass.
4. An intraday (5m to 1h) entry only before ``INTRADAY_LAST_ENTRY`` New York: the intraday
   flatten closes those positions 10 minutes before the bell (half days are not known here).
5. The OOS-only entries' guards: never a symbol the real eToro account holds or has in flight
   (rule 3 of app/workflow/paper_only.py: its exit copy follows paper by symbol), leave 2 of the
   day's paper trades and 1 of the correlation group's daily entries, never a symbol paper holds
   or has an unfilled entry in (one position a symbol, normal and exploration alike), leave 2
   places in paper's open-position and heat capacity.
6. Its own caps: at most ``paper_exploration_lane_max_open`` (8) exploration positions open or
   unfilled, and ``paper_exploration_lane_max_new_per_day`` (12) entries a New York day.
7. Automatic only: made only when the unattended path would approve and execute it at once
   (``candidate_blockers`` empty but for ``EXECUTION_BYPASS``); one not placed in its pass is
   rejected at once, and a person cannot approve one (as for every paper-only entry).
   Fail fast (2026-11-24): just before that check the lane reads one fresh quote, as the
   execution check will, and refuses a candidate the quote has already left by more than that
   check's own limit (``entry_drifted``; ``execution_max_entry_drift_bps``). Such a candidate was
   refused anyway, but about 33 s later (``entry_drift_too_large``), which spent the next-tick
   pass's whole entry window: on 11-24 AMD's price had run 150 bps in the 6 minutes since its
   scan, and the pass's 3 other focus candidates were then out of time. 6 of the 25 exploration
   proposals of 11-16 to 11-24 met that refusal (4 of the 8 shorts); their prices were 91 to
   363 s old when they were proposed. No gate is loosened: the limit is the check's own, the
   check still judges every proposal on its own later quote (so an entry must now be within the
   limit twice), and a quote that cannot be read or judged refuses nothing here. At most
   ``MAX_DRIFT_READS`` such reads a pass.
8. Sized at ``paper_exploration_lane_risk_pct`` (0.25%) of paper equity lost at the stop, never
   above the per-trade cap the risk gate enforces (``risk_based_proposal_notional``).
9. Tagged ``paper_only: true`` with ``paper_only_reason: "exploration"`` and the blockers it
   bypassed (``EXPLORATION_BYPASSED_FIELD``).

How real money is kept out: the tag. The eToro live mirror refuses any paper-only order before it
reads anything else (``etoro_live_paper_only``; also not a room reason, so paper's own room rules
decide it; an exploration refusal is logged as ``etoro_live_exploration_not_copied``, so up to 12
a day never crowd the copy rules' refusals out of the scorecard's lists), the coordinator
re-checks the eToro symbol just before the order, the parallel eToro
comparison skips it, and the paper ledger copies the tag into the position and the closed trade,
so the eToro paper record, the strategy-health switch, the auto-demotion, the tier-3
qualification, the auto-approval bootstrap, the go-live and weekly-target readiness and the
Stage 3 record all leave it out (app/performance/paper_only.py). The evidence verdicts are
computed from backtests alone and never read paper trades.

Residuals (stated): exploration losses count toward paper's daily and weekly loss limits, the
loss-streak cooldown and the drawdown governor like any paper trade, so they can halt every paper
entry, eToro-copyable ones included, for the rest of the day; an exploration position holds its
symbol's one paper slot and, after a loss, its re-entry cooldown; it uses one of paper's
trades-a-day and one of its correlation group's daily entries. Exploration and OOS-only entries
together never take paper's last two places or trades of the day.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import time as clock
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

PREFIX = "exploration_lane_"  # the lane's own proposal blockers
PROPOSAL_BYPASS = frozenset({"strategy_lacks_oos_evidence", "strategy_evidence_stale"})
EXECUTION_BYPASS = frozenset(
    {"candidate_not_backtest_validated", "insufficient_strategy_closed_trade_evidence"}
)
EXPLORATION_BYPASSED_FIELD = "paper_exploration_bypassed"  # the blockers an entry bypassed
SPARE_CHECKS = 1  # unattended-path checks (candidate_blockers) a pass may spend beyond its entries
MAX_DRIFT_READS = 5  # fresh quotes a pass may read to fail fast on a drifted entry (a kept cluster)
FOCUS_RESERVED_OPEN = 4  # of the lane's open places, never taken by a key outside a set focus
INTRADAY_LAST_ENTRY = clock(15, 30)  # New York; the flatten closes intraday positions at 15:50
NEW_YORK = ZoneInfo("America/New_York")


def lane_enabled(settings: Any) -> bool:
    """Settings alone: whether the lane can admit anything on this account."""

    return (
        bool(getattr(settings, "paper_exploration_lane_enabled", False))  # absent: off
        and str(getattr(settings, "execution_mode", "")) == "paper"
        and not bool(getattr(settings, "enable_real_trading", False))
    )


def lane_timeframes(settings: Any) -> frozenset[str]:
    return frozenset(
        str(item).strip().lower()
        for item in (getattr(settings, "paper_exploration_lane_timeframes", None) or [])
        if str(item).strip()
    )


def lane_focus(settings: Any) -> frozenset[str]:
    """The focus keys (``<strategy>:<timeframe>``, a short ``<strategy>:short:<timeframe>``);
    empty: no focus."""

    return frozenset(
        str(item).strip().lower()
        for item in (getattr(settings, "paper_exploration_lane_focus", None) or [])
        if str(item).strip()
    )


def max_per_pass(settings: Any) -> int:
    return max(int(getattr(settings, "paper_exploration_lane_max_per_pass", 1) or 0), 0)


def risk_pct(settings: Any) -> float:
    """The lane's risk per entry, as a % of equity: never above what the risk gate allows."""

    from app.risk.rules import sizing_risk_pct

    live = str(getattr(settings, "execution_mode", "paper")) == "live"
    wanted = float(getattr(settings, "paper_exploration_lane_risk_pct", 0.25) or 0.0)
    return max(min(wanted, sizing_risk_pct(settings, live=live)), 0.0)


class ExplorationLane:
    """One auto-propose pass's exploration decisions; ``paper`` is the pass's ``PaperOnlyPass``
    (its reads of the eToro account, paper's book and the verdicts are shared)."""

    def __init__(self, paper: Any):
        self.paper = paper
        self.created = 0  # exploration proposals the pass created
        self.checks = 0  # candidate_blockers reads spent on the lane
        self.drift_reads = 0  # fresh quotes read to fail fast on a drifted entry
        self._admitted: dict[int, tuple[Any, list[str]]] = {}  # id -> (candidate, bypassed)
        self._likely: dict[tuple[str, str, bool], bool] = {}
        self.skip: str | None = None  # why the lane stood aside for the last refusal it saw

    @property
    def settings(self) -> Any:
        return getattr(self.paper.auto, "settings", None)

    def enabled(self) -> bool:
        return getattr(self.paper.auto, "runtime_state", None) is not None and lane_enabled(
            self.settings
        )

    def eligible(self, candidate: Any) -> bool:
        """Rule 1 without the blockers: the lane on, an equity, a lane timeframe, and a candidate
        the auto-propose loop would not drop anyway."""

        return self.ineligible(candidate) is None

    def ineligible(self, candidate: Any) -> str | None:
        """Why rule 1 (without the blockers) rules ``candidate`` out, else None."""

        from app.broker import crypto as crypto_symbols

        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        if not self.enabled():
            return "lane_off"
        if not symbol or not str(getattr(candidate, "strategy_name", "") or ""):
            return "no_symbol_or_strategy"
        if crypto_symbols.is_crypto_symbol(symbol):
            return "crypto"
        if timeframe not in lane_timeframes(self.settings):
            return f"timeframe_{timeframe or 'none'}"
        drop = self.paper.drop_reason(candidate)  # a short only with the paper short switch
        return f"not_ready_{drop}" if drop else None

    def key(self, candidate: Any) -> str:
        """The candidate's leaderboard key: ``<strategy>:<timeframe>``, a short
        ``<strategy>:short:<timeframe>``."""

        from app.workflow.paper_shorts import short_verdict_key

        strategy = str(getattr(candidate, "strategy_name", "") or "")
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        if self.paper.shorts.applies(candidate):
            return short_verdict_key(strategy, timeframe)
        return f"{strategy}:{timeframe}"

    def focused(self, candidate: Any) -> bool:
        """Whether the lane's focus, when one is set, names the candidate's key (no focus: every
        candidate)."""

        focus = lane_focus(self.settings)
        return not focus or self.key(candidate).lower() in focus

    def likely(self, candidate: Any) -> bool:
        """For the pass's order (rule 2): an eligible candidate the evidence gate refuses for a
        reason the lane bypasses, from the pass's own verdicts read (cached per key)."""

        if not self.eligible(candidate):
            return False
        from app.performance.strategy_evidence import evidence_blocker

        strategy = str(getattr(candidate, "strategy_name", "") or "")
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        short = self.paper.shorts.applies(candidate)  # paper shorts: its own short verdict
        key = (strategy, timeframe, short)
        if key not in self._likely:
            try:
                blocker = (
                    self.paper.shorts.evidence(candidate)
                    if short
                    else evidence_blocker(
                        self.settings, self.paper.state, strategy=strategy, timeframe=timeframe, paper=True
                    )
                )
            except Exception:  # noqa: BLE001 - the gate itself refuses an unreadable state
                blocker = None
            self._likely[key] = blocker in PROPOSAL_BYPASS
        return self._likely[key]

    def blockers(self, candidate: Any, refused: list[str]) -> list[str] | None:
        """For a candidate the evidence gate refused (``refused``: every proposal blocker it met):
        None when the lane does not apply (the caller keeps ``refused``), else the lane's own
        blockers, [] when admitted (then ``admitted`` names what it bypassed). When it returns
        None, ``skip`` says why (observability only, 2026-11-13)."""

        self.skip = None
        try:
            if not refused or not set(refused) <= PROPOSAL_BYPASS:
                self.skip = "other_blockers"
            else:
                self.skip = self.ineligible(candidate)
            return None if self.skip else self._verdict(candidate, refused)
        except Exception as exc:  # noqa: BLE001 - fails closed: the gate's own refusal stands
            self.skip = f"error_{type(exc).__name__}"
            return None

    def _verdict(self, candidate: Any, refused: list[str]) -> list[str]:
        reason = self._room(candidate)
        if reason:
            return [PREFIX + reason]
        if self.paper.shorts.applies(candidate):  # a short: Alpaca's borrow and margin facts
            facts = self.paper.shorts.fact_blockers(candidate)
            if facts:
                return [PREFIX + fact for fact in facts]
        if self.checks >= max_per_pass(self.settings) + SPARE_CHECKS:
            return [PREFIX + "check_limit"]
        if self._entry_drifted(candidate):
            return [PREFIX + "entry_drifted"]
        self.checks += 1
        try:
            reasons = list(self.paper.auto.candidate_blockers(candidate))
        except Exception:  # noqa: BLE001 - unknown is refused
            reasons = ["candidate_blockers_unreadable"]
        remaining = [reason for reason in reasons if reason not in EXECUTION_BYPASS]
        if remaining:
            return [PREFIX + "not_automatic", *(f"{PREFIX}auto_{reason}" for reason in remaining)]
        bypassed = sorted({*refused, *(reason for reason in reasons if reason in EXECUTION_BYPASS)})
        self._admitted[id(candidate)] = (candidate, bypassed)
        return []

    def _entry_drifted(self, candidate: Any) -> bool:
        """Fail fast (rule 7): whether a fresh quote, read as the execution check reads it, is
        already further from the candidate's planned entry than that check allows. False when it
        cannot be told (no coordinator, no limit, an unreadable or unusable quote) and once the
        pass has spent its reads: the execution check then decides, as before."""

        if self.drift_reads >= MAX_DRIFT_READS:
            return False
        read = getattr(getattr(self.paper.auto, "execution", None), "planned_entry_drift_bps", None)
        limit = getattr(self.settings, "execution_max_entry_drift_bps", None)
        if not callable(read) or not isinstance(limit, int | float) or limit <= 0:
            return False
        self.drift_reads += 1
        try:
            drift = read(candidate)
        except Exception:  # noqa: BLE001 - unknown: not refused here
            return False
        if not isinstance(drift, int | float) or drift <= float(limit):
            return False
        with suppress(Exception):  # a log line never fails the pass
            self.paper.auto.logs.log(
                "exploration_entry_drifted",
                {
                    "symbol": str(getattr(candidate, "symbol", "") or "").upper(),
                    "key": self.key(candidate),
                    "drift_bps": round(float(drift), 1),
                    "limit_bps": float(limit),
                },
            )
        return True

    def _room(self, candidate: Any) -> str | None:
        """The focus and rules 3 to 6 (the pass limit, the clock, the cadence budget, the eToro
        account, paper's day and book, the lane's caps), from the pass's shared reads."""

        from app.workflow import paper_only as rules

        settings = self.settings
        focused = self.focused(candidate)
        late = _new_york_now().time() >= INTRADAY_LAST_ENTRY
        if not focused and not late:
            return "focus_only"  # until the day's intraday (focus) entries are over
        if self.created >= max_per_pass(settings):
            return "pass_limit"
        if _intraday(candidate) and late:
            return "intraday_entry_too_late"
        if rules._budget_spent():
            return "cadence_budget_spent"
        if self.paper.past_entry_deadline():
            return "pass_time_spent"
        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        held = self.paper._read("held", self.paper._etoro_held)
        if held is rules._UNREADABLE:
            return "etoro_live_state_unreadable"
        if symbol in held:
            return "etoro_live_symbol_held"
        book = self.paper._read("paper", self.paper._paper_book)
        if book is rules._UNREADABLE:
            return "paper_book_unreadable"
        reason = rules._headroom(settings, (book.trades, book.by_bucket), symbol)
        if reason:
            return reason
        daily = int(getattr(settings, "paper_exploration_lane_max_new_per_day", 0) or 0)
        if book.exploration_today >= daily:
            return "daily_cap"
        cap = int(getattr(settings, "paper_exploration_lane_max_open", 0) or 0)
        if not focused and len(book.exploration) >= cap - FOCUS_RESERVED_OPEN:
            return "focus_reserve"  # the lane's last open places stay free for the focus keys
        return rules._book_headroom(
            settings, book, rules._bare(symbol), lane=book.exploration, cap=cap
        )

    def admitted(self, candidate: Any) -> list[str] | None:
        """The blockers an admitted candidate bypassed, else None (not admitted this pass)."""

        entry = self._admitted.get(id(candidate))
        return entry[1] if entry is not None and entry[0] is candidate else None

    def tags(self, candidate: Any) -> dict[str, Any]:
        """The proposal/order metadata of an admitted candidate, else {}."""

        from app.performance.paper_only import (
            EXPLORATION,
            PAPER_ONLY_FIELD,
            PAPER_ONLY_REASON_FIELD,
        )

        bypassed = self.admitted(candidate)
        if bypassed is None:
            return {}
        tags: dict[str, Any] = {
            PAPER_ONLY_FIELD: True,
            PAPER_ONLY_REASON_FIELD: EXPLORATION,
            EXPLORATION_BYPASSED_FIELD: bypassed,
        }
        if not (getattr(candidate, "metadata", None) or {}).get("timeframe"):
            # The intraday flatten and the ledger read the order's timeframe from its metadata.
            tags["timeframe"] = str(getattr(candidate, "timeframe", "") or "").lower()
        return {**tags, **self.paper.shorts.order_tags(candidate)}  # a short's markers and facts


def _intraday(candidate: Any) -> bool:
    from app.automation.intraday_exit import INTRADAY_TIMEFRAMES

    return str(getattr(candidate, "timeframe", "") or "").lower() in INTRADAY_TIMEFRAMES


def _new_york_now() -> Any:
    return utc_now().astimezone(NEW_YORK)
