# Curated strategy profile (optional, reversible)

The registry has **32 strategy families**, but many are variants of the same
edge — five trend/continuation strategies, several pullback and breakout
variants, a near-identical relative-strength pair, overlapping VWAP and
mean-reversion setups. Running all 32 mostly produces *correlated* signals:
wasted scan compute and a book that quietly concentrates in one factor.

`app/strategies/curated.py` names **15 structurally distinct edges** — one strong
representative per cluster plus every genuinely distinct edge — and documents in
`SUBSUMED_BY` which families each representative stands in for. A test
(`tests/test_curated_strategies.py`) proves the 15 + the subsumed families
partition the whole registry, so the map can't silently drift.

## This is a profile, not a deletion

Nothing is removed from the registry; the full set stays the default. You opt in
when you want the concentrated book, and revert by clearing the setting. Code
retirement of the redundant variants waits until a live track record shows which
representative actually carries each edge (per the roadmap's sequencing rule —
don't prune the measured strategy set mid-measurement).

## How to apply it

Set the active-strategy list (Railway env var, not the repo) to the curated
names, then redeploy:

```python
python -c "from app.strategies.curated import curated_edge_names; print(','.join(curated_edge_names()))"
```

Put that comma-separated list in `SCREENER_ACTIVE_STRATEGY_NAMES`. To revert,
clear the variable so the full registry runs again.

## The 15 curated edges

| Edge | Representative | Stands in for |
|---|---|---|
| Trend / continuation | `trend_following` | ma_crossover, ema_trend_stack, rsi_trend_continuation, regime_aligned_trend_continuation, pullback_trend |
| Pullback continuation | `multi_timeframe_trend_pullback` | anchored_vwap_pullback_continuation, relative_volume_reclaim_continuation, early_breakout_pullback_continuation, liquidity_expansion_continuation |
| Momentum breakout | `momentum_breakout` | — |
| Relative strength | `relative_strength_momentum` | etf_mega_cap_relative_strength_rotation |
| Overnight drift | `overnight_drift` | — |
| Post-earnings drift | `post_earnings_drift` | — |
| Mean reversion | `connors_rsi2_reversion` | mean_reversion, rsi_reversal, failed_breakdown_reversal |
| Regime-aware MR | `regime_filtered_mean_reversion` | — |
| Market-neutral | `pairs_stat_arb` | — |
| Intraday confluence | `rsi_vwap_ema_confluence` | intraday_vwap_trend, vwap_reclaim |
| Opening range | `opening_range_breakout_retest` | — |
| Channel breakout | `atr_donchian_trend_breakout` | confluence_recovery_breakout |
| Volatility contraction | `volatility_contraction_breakout` | inside_bar_narrow_range_breakout |
| Gap | `gap_continuation_fade` | — |
| Gold | `gold_momentum` | — |

The `SUBSUMED_BY` map in code is the source of truth; this table mirrors it.
