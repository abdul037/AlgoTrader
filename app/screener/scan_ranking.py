"""The universe scan's ranking and top-k cut, and the paper-only specs that ride along in it.

A focused swing run hands its paper-only specs (operator option B, 2026-10-21: specs the main
paper gate admits on their pooled OOS evidence alone; app/workflow/swing_focus.py) to the same
scan as its eToro-passing specs (``paper_only_spec_keys``). ``scan_spec_keys`` adds them to the
scan's specs; on each symbol and timeframe they are evaluated after the other specs, on the same
bars and quote (``scan_support._strategy_specs_for_timeframe``).

``top_candidates`` ranks and cuts the two kinds apart. The other specs' candidates are ranked,
filtered (the cross-sectional momentum overlay, when on) and cut to the scan's top-k exactly as
if they were the scan's only candidates, so a paper-only candidate never takes one of their
places, however it scores. The paper-only specs' candidates get their own ``PAPER_ONLY_TOP_K``
and come after them (their ranks follow). A candidate is the paper-only specs' when its
``strategy_name:timeframe`` is one of their keys (a built-in strategy's signals carry its
spec's name). Without paper-only specs the cut is the scan's single cut, as before.
"""

from __future__ import annotations

from typing import Any

from app.screener.scan_support import _normalize_spec_keys

PAPER_ONLY_TOP_K = 3  # paper-only candidates a scan keeps, beside (never inside) its top-k


def scan_spec_keys(
    strategy_spec_keys: list[str] | None, paper_only_spec_keys: list[str] | None
) -> tuple[set[str], set[str]]:
    """(every spec key the scan evaluates, the paper-only ones among them). Paper-only keys ride
    along only with a requested spec list (none requested: every spec, as before), and a key in
    that list is never paper-only."""

    requested = _normalize_spec_keys(strategy_spec_keys)
    paper_only = _normalize_spec_keys(paper_only_spec_keys) - requested if requested else set()
    return requested | paper_only, paper_only


def top_candidates(
    service: Any,
    candidates: list[Any],
    *,
    limit: int | None,
    paper_only_keys: set[str],
    evaluated_spec_keys: set[str],
) -> tuple[list[Any], dict[str, Any]]:
    """The scan's kept candidates, ranked from 1, and its coverage entry for the paper-only specs
    (counts only; {} without them)."""

    paper_only = [item for item in candidates if candidate_key(item) in paper_only_keys]
    others = [item for item in candidates if candidate_key(item) not in paper_only_keys]
    kept = _cut(service, others, limit or service.settings.screener_top_k)
    coverage: dict[str, Any] = {}
    if paper_only_keys:
        extra = _cut(service, paper_only, PAPER_ONLY_TOP_K)
        kept += extra
        coverage["paper_only"] = {
            "specs": len(paper_only_keys),
            "specs_evaluated": len(paper_only_keys & evaluated_spec_keys),
            "candidates_found": len(paper_only),
            "candidates": len(extra),
        }
    ranked = [item.model_copy(update={"rank": index + 1}) for index, item in enumerate(kept)]
    return ranked, coverage


def candidate_key(candidate: Any) -> str:
    """A candidate's spec key, as the scan's spec keys spell it."""

    strategy = str(getattr(candidate, "strategy_name", "") or "").strip().lower()
    return f"{strategy}:{str(getattr(candidate, 'timeframe', '') or '').strip().lower()}"


def _cut(service: Any, candidates: list[Any], limit: int) -> list[Any]:
    ranked = sorted(candidates, key=service._ranking_key, reverse=True)
    # Cross-sectional momentum overlay: concentrate in the universe's leaders by
    # dropping all but the top-momentum slice before truncation. No-op unless
    # cross_sectional_momentum_enabled.
    if bool(getattr(service.settings, "cross_sectional_momentum_enabled", False)) and ranked:
        from app.screener.cross_sectional_momentum import filter_top_momentum

        top_pct = float(getattr(service.settings, "cross_sectional_momentum_top_pct", 30.0) or 30.0)
        ranked = filter_top_momentum(ranked, top_pct=top_pct)
    top_k = min(limit, len(ranked)) if ranked else 0
    return ranked[:top_k]
