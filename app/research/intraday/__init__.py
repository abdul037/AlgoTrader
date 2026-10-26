"""Intraday-native research candidates (operator-approved research, 2026-10-26).

RESEARCH ONLY. Nothing here is in ``app.strategies.STRATEGY_REGISTRY`` or ``STRATEGY_SPECS``:
the registry has no research-only flag that keeps a key out of the live scans, so these
strategies are never scanned, proposed, mirrored to eToro or judged by the live evidence gate.
They follow the ``BaseStrategy`` contract so the production walk-forward
(``BatchBacktestService._run_strategy``) can judge them exactly as it judges a live 5m/10m/15m
key; ``harness.py`` does that offline on frames the caller supplies. See
docs/research/intraday_edge_study.md for the study and its results.
"""

from app.research.intraday.strategies import (
    CANDIDATE_VARIANTS,
    GapOpenStrategy,
    LateDayMomentumStrategy,
    OpeningRangeBreakoutStrategy,
    RelativeVolumeMomentumStrategy,
    VWAPDeviationReversionStrategy,
    build_candidate,
)

__all__ = [
    "CANDIDATE_VARIANTS",
    "GapOpenStrategy",
    "LateDayMomentumStrategy",
    "OpeningRangeBreakoutStrategy",
    "RelativeVolumeMomentumStrategy",
    "VWAPDeviationReversionStrategy",
    "build_candidate",
]
