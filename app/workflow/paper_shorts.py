"""Paper short entries in the auto-propose pass (shorts step 3, operator 2026-11-16).

The live swing and intraday scans already return bracketed ``entry_short`` candidates; the loop
dropped every one (``candidate_propose_drop_reason`` -> ``entry_short``). With
``paper_short_entries_enabled`` (``app.risk.short_side.paper_shorts_enabled``: paper mode, real
trading off, Alpaca the paper and equities broker) they are routed into the same loop, with every
gate in it. No new signal is made. Off: nothing here is read and every short is dropped exactly
as before. Design: docs/notes/paper_shorts.md.

A short's evidence is its OWN short verdict (``SHORT_VERDICTS_KEY``, key
``<strategy>:short:<timeframe>``, written by the off-hours short research pass), never the long
one. ``short_evidence_blocker`` judges it as the paper gate judges a long: admitted when it
``passed``, or when ``paper_oos_bar_enabled`` and its ``paper_oos_passed``; refused as
``strategy_lacks_oos_evidence`` (missing or failing) or ``strategy_evidence_stale`` (older than
24 h), a retired strategy first. It always applies to a short, whatever
``require_strategy_oos_evidence`` says.

(a) A short its verdict admits is a paper-only short (``paper_only_reason: "short"``), sized at
    the risk gate's per-trade budget and taking every paper-only rule of app/workflow/paper_only.py
    (after the eToro-eligible entries, one a pass, never an eToro-held symbol, day and book
    headroom, the paper-only open cap, automatic only).
(b) A short refused ONLY for its evidence may be an exploration short: the exploration lane
    (app/workflow/exploration.py) unchanged, its caps shared with the exploration longs.

Both: equities only; Alpaca's asset and account are read (once per symbol a pass) and a name that
is not tradable, shortable, easy to borrow, marginable, with a maintenance margin, on an account
with shorting on, is refused before any proposal (``short_*`` blockers; the risk gate checks the
same facts again at proposal and at execution time). The one evidence-family execution blocker
judged on the LONG (``SHORT_EXECUTION_BYPASS``: the tier-3 count of closed long trades) says
nothing about a short and is bypassed for a verdict short, recorded on the order
(``SHORT_BYPASSED_FIELD``); every other execution blocker still refuses. Review 2026-11-16: since
the screener reads a short's own ``<strategy>:short`` backtest rows, its per-symbol
``candidate_not_backtest_validated`` speaks about the short and refuses it as it refuses the
mirrored long. An exploration short is past the lane's ``EXECUTION_BYPASS``, as exploration longs. Every short is
tagged paper-only with ``position_side: "short"`` and ``opens_short: true``, so the eToro live
mirror refuses it twice over (``etoro_live_paper_only`` and ``etoro_live_short_not_enabled``).
"""

from __future__ import annotations

import json
from typing import Any

from app.risk.short_side import (
    OPENS_SHORT_FIELD,
    POSITION_SIDE_FIELD,
    SHORT_REASON,
    SHORT_SIDE,
    is_short_candidate,
    paper_shorts_enabled,
    short_asset_facts,
    short_fact_blockers,
)

SHORT_BYPASSED_FIELD = "paper_short_bypassed"  # the long-evidence execution blockers a short bypassed
# A verdict short's execution blockers judged on the long side only (review 2026-11-16).
SHORT_EXECUTION_BYPASS = frozenset({"insufficient_strategy_closed_trade_evidence"})
_UNREADABLE = object()


def short_verdict_key(strategy: Any, timeframe: Any) -> str:
    return f"{strategy or ''}:short:{str(timeframe or '').lower()}"


def short_evidence_blocker(
    settings: Any, raw_verdicts: Any, *, strategy: str, timeframe: Any
) -> str | None:
    """A short's evidence blocker from the short verdicts payload (``raw_verdicts``: the JSON text
    or the parsed dict of ``SHORT_VERDICTS_KEY``); None when its verdict admits it."""

    from app.performance.strategy_evidence import (
        PAPER_OOS_PASSED_FIELD,
        STRATEGY_RETIRED,
        _stale,
    )
    from app.strategies import is_retired

    if is_retired(strategy):
        return STRATEGY_RETIRED
    try:
        cached = raw_verdicts if isinstance(raw_verdicts, dict) else json.loads(raw_verdicts or "{}")
    except (TypeError, ValueError):
        cached = {}
    cached = cached if isinstance(cached, dict) else {}
    verdicts = cached.get("verdicts")
    verdict = verdicts.get(short_verdict_key(strategy, timeframe)) if isinstance(verdicts, dict) else None
    if not isinstance(verdict, dict):
        return "strategy_lacks_oos_evidence"
    oos_bar = bool(getattr(settings, "paper_oos_bar_enabled", False)) and bool(
        verdict.get(PAPER_OOS_PASSED_FIELD)
    )
    if not (bool(verdict.get("passed")) or oos_bar):
        return "strategy_lacks_oos_evidence"
    if not cached.get("computed_at") or _stale(cached.get("computed_at")):
        return "strategy_evidence_stale"  # a short verdict must say when it was computed
    return None


class ShortLane:
    """One auto-propose pass's short decisions; ``paper`` is the pass's ``PaperOnlyPass``."""

    def __init__(self, paper: Any):
        self.paper = paper
        self._verdicts: Any = None
        self._read = False
        self._facts: dict[str, Any] = {}
        self._bypassed: dict[int, tuple[Any, list[str]]] = {}

    @property
    def settings(self) -> Any:
        return getattr(self.paper.auto, "settings", None)

    def enabled(self) -> bool:
        return getattr(self.paper.auto, "runtime_state", None) is not None and paper_shorts_enabled(
            self.settings
        )

    def applies(self, candidate: Any) -> bool:
        """A short candidate this pass judges as a short (the switch on, an equity)."""

        from app.broker import crypto as crypto_symbols

        return (
            is_short_candidate(candidate)
            and self.enabled()
            and not crypto_symbols.is_crypto_symbol(str(getattr(candidate, "symbol", "") or ""))
        )

    def evidence(self, candidate: Any) -> str | None:
        """Its short evidence blocker, from the pass's one read of the short verdicts (a failed
        read refuses: no verdict)."""

        from app.performance.strategy_evidence import SHORT_VERDICTS_KEY

        if not self._read:
            self._read = True
            try:
                self._verdicts = self.paper.auto.runtime_state.get(SHORT_VERDICTS_KEY)
            except Exception:  # noqa: BLE001 - unreadable: every short lacks evidence
                self._verdicts = None
        return short_evidence_blocker(
            self.settings,
            self._verdicts,
            strategy=str(getattr(candidate, "strategy_name", "") or ""),
            timeframe=getattr(candidate, "timeframe", None),
        )

    def admitted(self, candidate: Any) -> bool:
        """Path (a): a short its own verdict admits."""

        return self.applies(candidate) and self.evidence(candidate) is None

    def facts(self, candidate: Any) -> Any:
        """Alpaca's short facts for the candidate's symbol, read once a pass (``_UNREADABLE``)."""

        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        if symbol not in self._facts:
            try:
                self._facts[symbol] = short_asset_facts(self.paper.auto.alpaca, symbol)
            except Exception:  # noqa: BLE001 - unknown facts refuse
                self._facts[symbol] = _UNREADABLE
        return self._facts[symbol]

    def fact_blockers(self, candidate: Any) -> list[str]:
        """``short_*`` reasons the symbol's facts rule the short out (unreadable refuses)."""

        facts = self.facts(candidate)
        if facts is _UNREADABLE:
            return ["short_asset_unreadable"]
        return [f"short_{reason}" for reason in short_fact_blockers(facts)]

    def bypass(self, candidate: Any, reasons: list[str]) -> list[str]:
        """The execution blockers left once the long-evidence family is set aside (recorded)."""

        bypassed = sorted(reason for reason in reasons if reason in SHORT_EXECUTION_BYPASS)
        self._bypassed[id(candidate)] = (candidate, bypassed)
        return [reason for reason in reasons if reason not in SHORT_EXECUTION_BYPASS]

    def order_tags(self, candidate: Any) -> dict[str, Any]:
        """The short markers and Alpaca's facts for a short's proposal and order, else {}."""

        from app.automation.reliability import proposal_quality_label

        if not self.applies(candidate):
            return {}
        facts = self.facts(candidate)
        tags: dict[str, Any] = dict(facts) if isinstance(facts, dict) else {}
        tags.update({OPENS_SHORT_FIELD: True, POSITION_SIDE_FIELD: SHORT_SIDE})  # whatever was read
        tags["proposal_quality"] = proposal_quality_label(candidate, allow_short=True)
        entry = self._bypassed.get(id(candidate))
        if entry is not None and entry[0] is candidate:
            tags[SHORT_BYPASSED_FIELD] = entry[1]
        if not (getattr(candidate, "metadata", None) or {}).get("timeframe"):
            tags["timeframe"] = str(getattr(candidate, "timeframe", "") or "").lower()
        return tags

    def tags(self, candidate: Any) -> dict[str, Any]:
        """Path (a)'s full tags: paper-only, reason ``SHORT_REASON``, the markers and facts."""

        from app.performance.paper_only import PAPER_ONLY_FIELD, PAPER_ONLY_REASON_FIELD

        return {PAPER_ONLY_FIELD: True, PAPER_ONLY_REASON_FIELD: SHORT_REASON, **self.order_tags(candidate)}
