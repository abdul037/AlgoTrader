# Paper shorts in the screener: every quality gate mirrored (2026-11-16)

Production finding: paper shorts were switched on at 12:31 UTC and no short traded. All 42
`entry_short` scan decisions were rejected. Rule for this fix: with `paper_short_entries_enabled`
on, a bracketed short (`signal_role` "entry_short") passes a screener quality gate only where its
mirror-image long would, at the same strictness. Longs stay byte-identical. Code:
`app/screener/short_mirror.py`. Tests: `tests/test_paper_shorts_screener.py`.

Mirror used for checking: reflect every price about a constant, `p -> 2K - p`.
- EMAs, VWAP and MACD reflect with the price.
- RSI becomes `100 - RSI`.
- ATR, ADX, volume and efficiency stay the same.
- Swing highs and lows swap.
- Percent measures, such as momentum, keep only their sign, because they divide by a different
  price.

## 1. Already side-aware before this fix (verified by tests, not changed)

`app/intelligence/market_regime.py` reads `signal_role` for every score the filters gate on:

| Score | Scale | How a short reads it |
|---|---|---|
| Market trend, benchmark strength, sector strength, timeframe alignment (`_trend_score`) | 0–1 (1.0 / 0.68 / 0.2) | The same score with the conditions mirrored: close < EMA20 < EMA50 and a falling slope |
| Relative strength vs market and vs sector | signed % | Negated, so the gates `>= 0` and `>= -0.25` keep the same strictness |
| Breadth | 0–1 | The negated IWM-vs-benchmark relative, run through the same thresholds |
| Momentum state and extension | signed | Negated |
| Regime label | text | "risk-off trend" and "squeeze risk" |

Also already side-aware:
- **Filters context** (`app/screener/filters.py`): `momentum_pct` and `_regime_alignment_score`
  are mirrored.
- **Direction-free gates** (`filters.evaluate`): price, volume, spread, ATR %, trend strength
  (an absolute EMA gap) and efficiency ratio (an absolute net path).
- **Accuracy** (`app/screener/accuracy.py`): levels, VWAP, EMA stack and MACD.
- **Ranking** (`rank_live_signal`): it uses the mirrored intelligence. Actionability and
  `direction_label` follow the score and the action.
- **Snapshots** (`service_snapshots`): `execution_ready` and `alert_eligible`, plus
  `service_diagnostics.execution_blockers`, are symmetric.
- **Trade plan** (`app/intelligence/trade_plan.py`) and the risk:reward calculation.

## 2. Mirrored here (only for a short, only while the switch is on)

| Gate | Before (short) | Now (short) | Long |
|---|---|---|---|
| Accuracy RSI "constructive" | 30–52 | 28–52 (`100 - 72`, `100 - 48`) | 48–72 |
| Accuracy RSI "exhausted" | < 24 | < 22 (`100 - 78`) | > 78 |
| Indicator confluence RSI band | 35–48 | 32–48 (`100 - 68`, `100 - 52`) | 52–68 |
| Per-symbol backtest the snapshot, filters and ranking read | the **long** strategy's rows | the short's own rows, `<strategy>:short` (the short research pass's walk-forward rows); with none, the short is treated as a long with no backtest | `<strategy>` |
| Paper near-miss promotion (`scan_promotion.py`) | closed: `paper_near_miss_long_only` and `paper_near_miss_short_blocked` | promoted on the same rules, with the bracket mirrored (target < entry < stop) and `direction_label` "sell" | unchanged |

The near-miss promotion was the production blocker. A long that fails only soft gates is
promoted, and shorts were shut out of that route entirely.

`compute_confluence_score` takes `mirror_short=False` by default, so strategies and the short
research (steps 1–2) are unchanged. Only the screener passes the flag.

## 3. Left long-only on purpose

- **Supervised weak-valid promotion and its diagnostic BUY signals**: these wait for a person's
  approval, and a paper short is automatic only.
- **Enhanced strategies** (`app/strategies/enhanced/*`, for example trend.py l.67, 213 and 312):
  their checks are long entry conditions inside long-only generators (`_common.py` always emits
  `entry_long`). A short version would be a new signal, not a mirrored gate.
- **`StrategyEnhancementService`**: a diagnostic report only.
- **`app/signals/evaluation.py`**: the live eToro alert path, which no short may reach.
- **Regime router** (`regime_router_enabled`, off by default): it routes whole strategy specs by a
  long-oriented regime. Turned on in a downtrend, it would switch off the momentum and trend
  specs that also emit the shorts. Mirroring it means routing per signal rather than per spec.
- **`direction_label: "buy"` in the payloads of two rejected-row scan decisions**
  (`service_scan.py`): cosmetic, and the file is at 999 lines.

## 4. For the operator: the near-miss path is equally loose on both sides

`.env.example` sets `PAPER_NEAR_MISS_MAX_SCORE_GAP=10`, so promotion needs a score of 50 or more.
`paper_near_miss_allowed_reasons` lists every market-direction gate as soft: market regime, sector,
benchmark, relative strength and timeframe alignment. As a result:
- today, a long in a falling market that scores 50 or more and fails only those gates is
  promoted;
- its mirror, a short in a rising market, now is too. That is the same strictness, as asked
  (`test_near_miss_is_equally_loose_for_both_sides`).

At the code default gap of 5 (score 55 or more), both are refused in the test scenario. To refuse
counter-trend entries, narrow the allowed reasons or the gap. That applies to both sides.

## 5. Tests (`tests/test_paper_shorts_screener.py`)

- **Long byte-identity.** The 91d0a6d sources of the four edited modules are kept verbatim in
  `tests/screener_long_parity_reference/`, with their sha256 values pinned and checked against
  `git show`. The same seeded long inputs, with the switch on and off, go through old and new
  code. So do shorts, short watches and exits with the switch off. Covered functions:
  - `compute_confluence_score`;
  - `build_market_context`;
  - `build_accuracy_profile`;
  - `ScreenerFilterPipeline.evaluate`;
  - `_paper_near_miss_blockers`;
  - `_maybe_promote_paper_near_miss`, including its log payload.

  The `json.dumps` output must be identical. `_backtest_name` passes a long through.
- **Mirror exactness.** Under the reflection, a short scores what the long scores:
  - confluence;
  - the accuracy profile (every score, reason and measurement);
  - the context's confluence, regime alignment and efficiency;
  - `_trend_score`, the extension, relative strength (negated), breadth and regime alignment;
  - near-miss blockers.

  Without the mirror, the old RSI bands were stricter on some cases.
- **End to end through `scan_universe`.** The scenario is a weak stock (JPM) with SPY, QQQ, IWM
  and XLF falling, built as the exact reflection of a strong world:
  - the short is a clean `execution_ready`, alert-eligible candidate ("sell", "risk-off trend",
    every gate passed, read from `trend_following:short`);
  - the same stock as a long is rejected;
  - the strong stock's short is rejected with exactly the weak long's reasons;
  - the weak short equals the strong long;
  - a short with only the long's rows is not validated;
  - a short near-miss is promoted as its mirrored long is, and with the switch off it is
    blocked, as before.

Full suite on `3d8e695`: 2132 passed, 5 skipped (Postgres-only), 0 failed (10 min 46 s); ruff
clean on every changed Python file; architecture fitness green (service_scan.py still 999 lines,
service.py 428, operations.py and workflow/service.py untouched).
