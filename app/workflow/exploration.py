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

Its rules (``ExplorationLane``, one per auto-propose pass, owned by its ``PaperOnlyPass``), in
order, each read once a pass and shared with the OOS-only paper-only entries' rules:

1. Eligible: the lane on (``paper_exploration_lane_enabled``, paper mode, real trading off), an
   equity (never crypto), a timeframe in ``paper_exploration_lane_timeframes`` and a candidate the
   auto-propose loop would not drop anyway (execution-ready, alert-eligible, long, with a stop).
2. Order: considered after every eToro-eligible and OOS-only candidate of the pass.
3. Bounded work: at most ``paper_exploration_lane_max_per_pass`` (1) exploration proposal a pass,
   none within ``paper_only.PROPOSAL_SECONDS`` of a cadence tick's scan stop, and at most
   ``CHECKS_PER_PASS`` unattended-path checks (``candidate_blockers``) a pass.
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
CHECKS_PER_PASS = 2  # unattended-path checks (candidate_blockers) a pass may spend on the lane
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
        self._admitted: dict[int, tuple[Any, list[str]]] = {}  # id -> (candidate, bypassed)
        self._likely: dict[tuple[str, str], bool] = {}
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

        from app.automation.reliability import candidate_propose_drop_reason
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
        drop = candidate_propose_drop_reason(candidate)
        return f"not_ready_{drop}" if drop else None

    def likely(self, candidate: Any) -> bool:
        """For the pass's order (rule 2): an eligible candidate the evidence gate refuses for a
        reason the lane bypasses, from the pass's own verdicts read (cached per key)."""

        if not self.eligible(candidate):
            return False
        from app.performance.strategy_evidence import evidence_blocker

        strategy = str(getattr(candidate, "strategy_name", "") or "")
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        key = (strategy, timeframe)
        if key not in self._likely:
            try:
                blocker = evidence_blocker(
                    self.settings, self.paper.state, strategy=strategy, timeframe=timeframe, paper=True
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
        if self.checks >= CHECKS_PER_PASS:
            return [PREFIX + "check_limit"]
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

    def _room(self, candidate: Any) -> str | None:
        """Rules 3 to 6 (the pass limit, the clock, the cadence budget, the eToro account, paper's
        day and book, the lane's caps), from the pass's shared reads."""

        from app.workflow import paper_only as rules

        settings = self.settings
        if self.created >= max(int(getattr(settings, "paper_exploration_lane_max_per_pass", 1) or 0), 0):
            return "pass_limit"
        if _intraday(candidate) and _new_york_now().time() >= INTRADAY_LAST_ENTRY:
            return "intraday_entry_too_late"
        if rules._budget_spent():
            return "cadence_budget_spent"
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
        return tags


def _intraday(candidate: Any) -> bool:
    from app.automation.intraday_exit import INTRADAY_TIMEFRAMES

    return str(getattr(candidate, "timeframe", "") or "").lower() in INTRADAY_TIMEFRAMES


def _new_york_now() -> Any:
    return utc_now().astimezone(NEW_YORK)
