"""Cross-sectional momentum: keep only the strongest names in the universe.

Per-strategy signals are *time-series* (is THIS name set up right now). This is
the *cross-sectional* overlay the audit flagged as missing: rank every candidate
the scan produced against each other by price momentum and admit only the top
slice, so on any given day the book concentrates in the universe's leaders — the
classic momentum-rotation edge, applied as a selection-layer filter rather than a
per-symbol strategy.

Pure and dependency-light. Wired into the screener behind
``cross_sectional_momentum_enabled`` (default off): with the flag off the scan
is unchanged.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# Momentum proxies to try in order, from best to fallback. Enriched candidates
# expose EMA slopes and returns in ``indicators``; strategies may stash an
# explicit momentum in ``metadata``. Score is the last resort so a candidate
# without any momentum field still ranks sensibly rather than dropping to zero.
_METADATA_KEYS = ("momentum_pct", "roc_20", "return_20d")
_INDICATOR_KEYS = ("roc_20", "return_20", "ema_50_slope", "ema_20_slope", "ema_9_slope")


def momentum_value(candidate: Any) -> float:
    """Best-available momentum proxy for a candidate (falls back to its score)."""

    metadata = getattr(candidate, "metadata", {}) or {}
    for key in _METADATA_KEYS:
        value = metadata.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    indicators = getattr(candidate, "indicators", {}) or {}
    for key in _INDICATOR_KEYS:
        value = indicators.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    try:
        return float(getattr(candidate, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def filter_top_momentum(candidates: Iterable[Any], *, top_pct: float) -> list[Any]:
    """Keep the top ``top_pct`` percent of candidates by momentum.

    ``top_pct >= 100`` (or empty input) is a pass-through. Always keeps at least
    one candidate when any exist, so the filter concentrates rather than starves.
    Ties and order among the kept set are preserved from the input.
    """

    items = list(candidates)
    if not items or top_pct >= 100.0:
        return items
    if top_pct <= 0.0:
        top_pct = 1.0  # never keep nothing when there are candidates
    keep = max(1, math.ceil(len(items) * (top_pct / 100.0)))
    threshold = sorted((momentum_value(c) for c in items), reverse=True)[keep - 1]
    # Preserve input order among candidates at or above the cutoff, capped at keep.
    survivors = [c for c in items if momentum_value(c) >= threshold]
    return survivors[:keep] if len(survivors) > keep else survivors
