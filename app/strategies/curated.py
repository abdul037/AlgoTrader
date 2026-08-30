"""Curated non-overlapping strategy edges — a reversible selection profile.

The registry carries 32 strategy families, but many are variants of the same
underlying edge: five trend/trend-continuation strategies, several
pullback-continuation and breakout variants, a near-identical relative-strength
pair, and overlapping VWAP and mean-reversion setups. Running all of them at
once mostly produces *correlated* signals — wasted scan compute and a book that
concentrates in one factor without the operator intending it.

This module names a curated set of ~15 structurally distinct edges (one strong
representative per cluster, plus every genuinely distinct edge) and documents,
in ``SUBSUMED_BY``, which registry families each representative stands in for.

It is a **profile, not a deletion**: nothing is removed from the registry. An
operator opts in by setting ``screener_active_strategy_names`` to
``curated_edge_names()`` (or via the documented env list) when they want the
concentrated set; the full registry stays available and is the default. This is
the reversible first step of the strategy-dedup work — code retirement waits
until a live track record shows which representative actually carries each edge.
"""

from __future__ import annotations

# One representative per distinct edge. Kept deliberately small (~15) so the
# active book spans uncorrelated setups rather than many variants of a few.
CURATED_EDGES: tuple[str, ...] = (
    # Trend / continuation
    "trend_following",
    "multi_timeframe_trend_pullback",
    # Momentum & catalysts
    "momentum_breakout",
    "relative_strength_momentum",
    "overnight_drift",
    "post_earnings_drift",
    # Mean reversion / market-neutral
    "connors_rsi2_reversion",
    "regime_filtered_mean_reversion",
    "pairs_stat_arb",
    # Intraday / VWAP
    "rsi_vwap_ema_confluence",
    "opening_range_breakout_retest",
    # Breakout
    "atr_donchian_trend_breakout",
    "volatility_contraction_breakout",
    # Gaps & instrument-specific
    "gap_continuation_fade",
    "gold_momentum",
)

# For each curated representative, the overlapping registry families it stands
# in for while the curated profile is active. Together with CURATED_EDGES this
# accounts for every registered family, so the mapping is verifiable (see
# tests/test_curated_strategies.py) — if a family is added or renamed the test
# fails until this map is updated.
SUBSUMED_BY: dict[str, tuple[str, ...]] = {
    "trend_following": (
        "ma_crossover",
        "ema_trend_stack",
        "rsi_trend_continuation",
        "regime_aligned_trend_continuation",
        "pullback_trend",
    ),
    "multi_timeframe_trend_pullback": (
        "anchored_vwap_pullback_continuation",
        "relative_volume_reclaim_continuation",
        "early_breakout_pullback_continuation",
        "liquidity_expansion_continuation",
    ),
    # The audit's "near-identical RS pair": keep the broad momentum ranker, fold
    # the ETF-rotation variant into it.
    "relative_strength_momentum": ("etf_mega_cap_relative_strength_rotation",),
    "rsi_vwap_ema_confluence": (
        "intraday_vwap_trend",
        "vwap_reclaim",
    ),
    "atr_donchian_trend_breakout": ("confluence_recovery_breakout",),
    "volatility_contraction_breakout": ("inside_bar_narrow_range_breakout",),
    # Reversal setups fold into the mean-reversion representatives.
    "connors_rsi2_reversion": (
        "mean_reversion",
        "rsi_reversal",
        "failed_breakdown_reversal",
    ),
}


def curated_edge_names() -> list[str]:
    """The curated active-strategy list to assign to ``screener_active_strategy_names``."""

    return list(CURATED_EDGES)


def subsumed_family_names() -> list[str]:
    """Every registry family the curated profile folds into a representative."""

    return [name for names in SUBSUMED_BY.values() for name in names]
