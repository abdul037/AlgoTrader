"""Enhanced long-only research strategies (split into cohesive modules)."""

from __future__ import annotations

from app.strategies.enhanced.breakout import (
    ATRDonchianTrendBreakoutStrategy,
    ConfluenceRecoveryBreakoutStrategy,
    EarlyBreakoutPullbackContinuationStrategy,
    InsideBarNarrowRangeBreakoutStrategy,
    VolatilityContractionBreakoutStrategy,
)
from app.strategies.enhanced.continuation import (
    AnchoredVWAPPullbackContinuationStrategy,
    LiquidityExpansionContinuationStrategy,
    RelativeVolumeReclaimContinuationStrategy,
)
from app.strategies.enhanced.gap import (
    GapContinuationFadeStrategy,
)
from app.strategies.enhanced.opening_range import (
    OpeningRangeBreakoutRetestStrategy,
)
from app.strategies.enhanced.reversal import (
    FailedBreakdownReversalStrategy,
    RegimeFilteredMeanReversionStrategy,
)
from app.strategies.enhanced.trend import (
    EtfMegaCapRelativeStrengthRotationStrategy,
    MultiTimeframeTrendPullbackStrategy,
    RegimeAlignedTrendContinuationStrategy,
    RelativeStrengthMomentumStrategy,
)

__all__ = [
    "ATRDonchianTrendBreakoutStrategy",
    "AnchoredVWAPPullbackContinuationStrategy",
    "ConfluenceRecoveryBreakoutStrategy",
    "EarlyBreakoutPullbackContinuationStrategy",
    "EtfMegaCapRelativeStrengthRotationStrategy",
    "FailedBreakdownReversalStrategy",
    "GapContinuationFadeStrategy",
    "InsideBarNarrowRangeBreakoutStrategy",
    "LiquidityExpansionContinuationStrategy",
    "MultiTimeframeTrendPullbackStrategy",
    "OpeningRangeBreakoutRetestStrategy",
    "RegimeAlignedTrendContinuationStrategy",
    "RegimeFilteredMeanReversionStrategy",
    "RelativeStrengthMomentumStrategy",
    "RelativeVolumeReclaimContinuationStrategy",
    "VolatilityContractionBreakoutStrategy",
]
