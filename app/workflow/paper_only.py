"""Paper-only entries in the auto-propose pass (operator option B, 2026-10-21).

An entry the main paper gate admits ONLY through the paper OOS bar
(``strategy_evidence.paper_oos_only``: its pooled OOS evidence passes, its holdout does not) is
paper-only: the eToro live mirror refuses it. It shares the paper account with the entries
the mirror copies, so it stands aside for them. ``PaperOnlyPass`` (one per auto-propose pass,
every decision from one read of the verdicts) gives it four rules:

1. Order: the pass considers every other candidate before a paper-only one (a stable split of
   the ranked list), so a paper-only candidate never takes a symbol, or a shared daily
   allowance, from an eToro-eligible one in the same pass.
2. Never a symbol the real eToro account holds or has in flight: reconcile's open list, the
   scorecard's open and pending-fill trades and the test order, read as the mirror's room
   check reads them (``effective_open_symbols``), once a pass. The exit copy follows paper BY
   SYMBOL (``copy_paper_exits``), so a paper-only position there would hold a real position
   open or close it at market. An unreadable record refuses the pass's paper-only entries.
   A symbol the pass itself sends to eToro is already taken by its own proposal.
3. Headroom: a paper-only entry must leave 2 of the day's paper trades (``LEAVE_FOR_ETORO`` of
   ``max_trades_per_day``) and 1 of its correlation group's daily entries (``LEAVE_IN_GROUP``
   of ``max_daily_entries_per_correlation_bucket``; unclassified symbols have no group cap),
   counted as paper's own checks count them (today's execution rows), read once a pass and
   again after each proposal the pass creates. Two is the most eToro copies a day
   (``HARD_MAX_TRADES_PER_DAY``), so whatever paper-only entries come first, two
   eToro-eligible entries still fit that day. With the production group cap of 2 a group
   takes a paper-only entry only while it has none that day, and keeps one entry for an
   eToro-eligible one; a second eToro-eligible entry in that group that day is then refused
   (fewer copies, never extra ones). Leaving 2 would bar 23 of the 24 universe symbols, all
   grouped (operator decision, 2026-10-22). Proposals still waiting for a person's approval
   are not counted (the unattended account has none). Unreadable counts refuse.
4. Tag: its proposal and order carry ``paper_only: true`` and ``paper_only_reason: "oos_only"``
   (``tags``), which make the mirror refuse it (``etoro_live_paper_only``) and keep it out of
   the strategy-name switches and the go-live readiness (app/performance/paper_only.py).

Not changed (residuals, stated): paper-only losses count toward paper's daily and weekly loss
limits, the consecutive-loss cooldown and the drawdown governor like any paper trade, so they
can halt eToro-eligible entries for the rest of the day; a paper-only position holds its
symbol's one paper slot (and its re-entry cooldown after a loss) and its share of paper's
portfolio heat. No paper gate is loosened: every candidate still meets every gate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.utils.time import utc_now

LEAVE_FOR_ETORO = 2  # paper trades a day a paper-only entry must leave
LEAVE_IN_GROUP = 1  # entries in its correlation group that day a paper-only entry must leave
PREFIX = "paper_only_"  # its proposal blockers
_UNREADABLE = object()  # a read that failed: the pass's paper-only entries stand aside


class PaperOnlyPass:
    """One auto-propose pass's paper-only decisions; ``auto_trading`` is the
    ``PaperAutoTradingService`` (None: no gate, so nothing is paper-only)."""

    def __init__(self, auto_trading: Any):
        self.auto = auto_trading
        self._reads: dict[str, Any] = {}

    @property
    def state(self) -> Any:
        """A read-only runtime_state holding the verdicts as this pass first read them, for
        ``evidence_blocker``; None without one (the gate is then off, as before)."""

        from app.performance.strategy_evidence import VERDICTS_KEY

        runtime_state = getattr(self.auto, "runtime_state", None)
        if runtime_state is None:
            return None
        if "verdicts" not in self._reads:  # a failed read raises, as the gate's own read did
            self._reads["verdicts"] = runtime_state.get(VERDICTS_KEY)
        return SimpleNamespace(get={VERDICTS_KEY: self._reads["verdicts"]}.get)

    def is_paper_only(self, candidate: Any) -> bool:
        """True when the main paper gate admits ``candidate`` only through the paper OOS bar."""

        from app.broker import crypto as crypto_symbols
        from app.performance.strategy_evidence import VERDICTS_KEY, paper_oos_only

        settings = getattr(self.auto, "settings", None)
        state = self.state
        if (
            state is None
            or str(getattr(settings, "execution_mode", "")) != "paper"
            or bool(getattr(settings, "enable_real_trading", False))
            or not bool(getattr(settings, "require_strategy_oos_evidence", False))
            or crypto_symbols.is_crypto_symbol(str(getattr(candidate, "symbol", "") or "").upper())
        ):
            return False
        if "parsed" not in self._reads:
            try:
                parsed = json.loads(state.get(VERDICTS_KEY) or "{}").get("verdicts")
            except (TypeError, ValueError, AttributeError):
                parsed = None  # unreadable: the gate refuses every candidate anyway
            self._reads["parsed"] = parsed if isinstance(parsed, dict) else {}
        verdicts = self._reads["parsed"]
        timeframe = str(getattr(candidate, "timeframe", "") or "").lower()
        verdict = verdicts.get(f"{getattr(candidate, 'strategy_name', '') or ''}:{timeframe}")
        return paper_oos_only(settings, verdict, timeframe)

    def order(self, candidates: list[Any]) -> list[Any]:
        """The pass's candidates, every paper-only one after all the others, ranks kept."""

        return sorted(candidates, key=self.is_paper_only)

    def blockers(self, candidate: Any) -> list[str]:
        """Why a paper-only candidate must stand aside; [] for any other, and without a read
        for one the auto-propose loop drops next anyway (``candidate_propose_drop_reason``)."""

        from app.automation.reliability import candidate_propose_drop_reason

        if not self.is_paper_only(candidate) or candidate_propose_drop_reason(candidate):
            return []
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
        return [PREFIX + reason] if reason else []

    def tags(self, candidate: Any) -> dict[str, Any]:
        """The proposal/order metadata of a paper-only candidate, else {}."""

        from app.performance.paper_only import OOS_ONLY, PAPER_ONLY_FIELD, PAPER_ONLY_REASON_FIELD

        if not self.is_paper_only(candidate):
            return {}
        return {PAPER_ONLY_FIELD: True, PAPER_ONLY_REASON_FIELD: OOS_ONLY}

    def created(self) -> None:
        """The pass created a proposal: the next paper-only candidate reads paper's day again
        (by then the entry, if it was placed, is one of today's execution rows)."""

        self._reads.pop("day", None)

    def _read(self, name: str, read: Any) -> Any:
        if name not in self._reads:
            try:
                self._reads[name] = read()
            except Exception:  # noqa: BLE001 - fail closed, for the rest of the pass too
                self._reads[name] = _UNREADABLE
        return self._reads[name]

    def _etoro_held(self) -> set[str]:
        """The symbols the eToro live room counts, from the mirror's records; raises when one
        is unreadable (the room check itself would read it as empty)."""

        from app.broker.etoro_live_mirror import STATE_KEY
        from app.broker.etoro_live_room import effective_open_symbols
        from app.broker.etoro_live_scorecard import SCORECARD_KEY
        from app.broker.etoro_live_test_order import TEST_STATE_KEY

        mirror = getattr(getattr(self.auto, "execution", None), "etoro_live_mirror", None)
        state = getattr(mirror, "state", None) or self.auto.runtime_state
        values = {key: state.get(key) for key in (STATE_KEY, SCORECARD_KEY, TEST_STATE_KEY)}
        records = {key: json.loads(raw or "{}") for key, raw in values.items()}
        if not all(isinstance(record, dict) for record in records.values()):
            raise ValueError("an eToro live record is not a JSON object")
        return set(effective_open_symbols(SimpleNamespace(get=values.get), records[STATE_KEY]))

    def _paper_day(self) -> tuple[int, dict[str, int]]:
        """Today's paper trades and entries per correlation group, as ``build_risk_context``
        counts them for paper's own trades-a-day and group checks."""

        from app.risk.context import entries_today_by_bucket

        executions = self.auto.executions
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(executions.count_since(start_of_day)), entries_today_by_bucket(
            executions, start_of_day
        )


def _headroom(settings: Any, day: tuple[int, dict[str, int]], symbol: str) -> str | None:
    from app.risk.sectors import UNKNOWN_BUCKET, correlation_bucket_for_symbol

    trades, by_bucket = day
    if trades + 1 + LEAVE_FOR_ETORO > int(getattr(settings, "max_trades_per_day", 999999)):
        return "headroom_trades_per_day"
    cap = int(getattr(settings, "max_daily_entries_per_correlation_bucket", 0) or 0)
    bucket = correlation_bucket_for_symbol(symbol)
    if (
        cap > 0
        and bucket != UNKNOWN_BUCKET
        and by_bucket.get(bucket, 0) + 1 + LEAVE_IN_GROUP > cap
    ):
        return "headroom_correlation_bucket"
    return None
