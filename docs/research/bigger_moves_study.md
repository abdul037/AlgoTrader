# Bigger moves (larger targets, longer holds, weekly bars) study (2026-11-10) -- research only

**Question.** Production's walk-forward evidence (runtime_state `strategy_evidence:verdicts`,
refreshed 2026-11-10) has no daily strategy that clears costs. Per $100 risked (1R), out of
sample on the live 25-symbol universe over about two years:

| strategy | OOS trades | before costs | after costs |
|---|--:|--:|--:|
| inside_bar_narrow_range_breakout | 264 | +0.047R | -0.038R |
| overnight_drift | 1101 | +0.038R | -0.017R |
| connors_rsi2_reversion | 361 | +0.023R | -0.032R |
| relative_strength_momentum | 288 | +0.014R | -0.056R |
| mean_reversion | 6 | -- | +0.18R |

Costs average about $6.2 a trade (spread $2.43, eToro's $1 a leg $2.00, overnight financing
$1.76) and are roughly fixed per trade. If the signals carry any edge, aiming the same signals at
bigger moves (larger targets, longer holds, weekly bars) should let the edge grow while the cost
per trade stays put. Does any variant of these five strategies clear costs, beat random entries
in the same stocks, and hold up across time and point-in-time index membership? And does
mean_reversion, with six production trades, have an edge once it gets enough trades to judge?

**Answer: nothing passes. 0 of 29 variants clear the pre-registered rule, and none comes
close on the part that tests the signals.**

* **Bigger targets do raise after-cost R, but random entries raise it as much or more.**
  connors_rsi2 goes from +0.021R (as is) to +0.098R (target x2) and +0.187R (target x3);
  relative_strength_momentum from -0.022R to +0.058R and +0.128R; inside_bar from -0.040R to
  +0.022R and +0.084R. Random entry days in the same stock and year, with the same stop, target
  and time exit, go from +0.102R to +0.302R (connors) and from +0.121R to +0.333R (momentum).
  The bigger move is the drift of these stocks in a 15-year bull market, not the signal.
* **No variant beats its random-entry control.** 24 of 29 have a negative excess; every
  Holm-adjusted p is 1.00. The best raw one-sided p (0.049, mean_reversion on weekly bars) is
  about what the best of 29 tests with no effect gives by chance.
* 9 variants clear +0.03R on the main period and 8 of them also have a positive sealed holdout
  (rule 1), but only 2 are stable across sub-periods (rule 3), and all 29 fail rule 2.
* **"Costs are fixed per trade" is only half true.** Spread and the $1 legs are fixed, but
  overnight financing grows with the hold and becomes the largest cost of every longer-hold
  variant: inside_bar $8.6 -> $25.3 a trade from V0 to V2, momentum $14.7 -> $39.1, connors
  $11.5 -> $33.9. Taking financing out (sensitivity only; a real 1x eToro stock pays none)
  lifts R by up to 0.4R but leaves every excess over the control where it was, because the
  control pays the same financing.
* **Time exits (V3, V4) do not help.** No V3 or V4 variant reaches +0.03R after costs (best:
  connors V4 +0.012R, overnight_drift V4 -0.008R).
* **overnight_drift loses in every variant:** -0.052R to -0.054R for V0-V2 on 284,000-304,000
  trades (V0: +0.012R before $6.6 of costs a trade), -0.022R and -0.008R with 10- and 20-bar
  holds.
* **mean_reversion, given enough trades, has no edge.** On 2,720 point-in-time trades
  (2011-2025) it makes **-0.079R after costs** (+0.038R before), +0.008R over its control
  (month-t 0.18), and -0.290R on 182 holdout trades. Production's +0.18R was 6 trades of noise.
  Its weekly version (V5: 405 trades, +0.213R, +0.216R over the control, holdout 27 trades
  +0.147R) passes rules 1 and 3 but not rule 2 (month-t 1.67, Holm p 1.00).
* **V0 calibration.** Over 2011-2025 on the point-in-time S&P 500, V0 lands within about 0.05R
  of production (inside_bar -0.040R vs -0.038R, relative_strength_momentum -0.022R vs -0.056R, connors
  +0.021R vs -0.032R, overnight_drift -0.054R vs -0.017R). Production's own walk-forward,
  replicated with the real engine on the live 24 names, gives overnight_drift -0.020R on 1,407
  trades (production -0.017R on 1,101); the other three come out 0.06-0.17R better than
  production but are not distinguishable from zero (month-t <= 1.13); see "Calibration".
* **The production gate could not measure these variants as traded anyway.** Its 14-day test
  folds close 60-82% of inside_bar, momentum and connors OOS trades at the fold end (median hold
  2-7 days), so a target x3 or a 20-bar hold would be scored as a cut-off trade. (Separately,
  every replicated fold leaves out the embargo session between its warm-up and its test
  window, so the test bars' indicators span a missing session; see "Calibration".)

**No parameter change is recommended for live trading.**

**Status: PRE-REGISTERED.** The question, data, variants, method and pass rule below were
committed before any signal, trade or result of this study was computed (`3521ecd`); the code
and both parity checks next (`f3038b8`); the main-period results before the sealed holdout was
computed (`325598a`); then the holdout and the calibration. Nothing in the method was changed
after a result was seen. Added after the main results, as reported-only diagnostics: the
no-financing excess table and the calibration's signal-overlap and fold-gap columns.

Nothing here touches live trading. The code is in `research/bigger_moves/`, which `app/` never
imports; it imports `app` read-only (the five strategy classes for the parity check, the
`BacktestEngine` and the production walk-forward helpers for the calibration).

## Data

| | source | window | symbols |
|---|---|---|---|
| members, GICS sector, "date added" | the pullback study's cache: Wikipedia "List of S&P 500 companies", fetched 2026-11-01 (the GitHub `datasets/s-and-p-500-companies` CSV lists the same 503) | today | 503 |
| daily OHLCV | the pullback study's cache: yfinance, `auto_adjust=True` (split- and dividend-adjusted) | 2010-01-04 .. 2026-10-30 | 500 usable members (FDXF, HONA, VYLR have under 122 bars) |
| live universe | same cache; the 24 names the pullback and intraday studies recorded as the live scan universe: AAPL ADBE AMD AMZN AVGO BAC COST CRM CSCO DIS GOOGL INTC IWM JPM META MSFT NFLX NVDA PYPL QQQ SPY TSLA WMT XOM | same | 24 |

* The cache is reused read-only (downloaded 2026-11-01, last bar 2026-10-30). It is the data
  both earlier studies used, so the three studies are comparable. The production verdicts run
  on Alpaca bars up to 2026-11-09; the seven missing sessions cannot change a 15-year verdict.
* `MARKET_UNIVERSE_SYMBOLS` (25 names) is a Railway variable, not in the repository, and the
  production `backtests` rows are not readable from here. The 24-name list above is what the
  earlier studies recorded; the 25th name is unknown and is left out.
* **Cleaning** is the pullback study's (`research/pullback/data.py`), unchanged: stale
  pre-listing histories start after their last zero-volume bar, isolated zero-volume bars are
  dropped, OHLC is clipped to contain open and close, and a day on which yfinance's
  dividend-adjustment factor jumps by more than 15% is a corporate-action break (16 breaks in 15
  symbols). The momentum study's extra break (HWM 2020-04-01) is added. Here a break splits a
  symbol's history into separate segments: indicators restart after the break (as for a new
  listing), no signal is taken until the strategy's own `required_bars` have passed in the new
  segment, and a trade open at a break closes at the last close before it (`data_break`).
* **Weekly bars** (V5) are built from the cleaned daily bars of each segment: open of the
  week's first session, high and low over the week, close of its last session, summed volume,
  labelled with the week's first session.
* **Survivorship.** The universe is today's members. Companies that were members and later
  left are absent; that part of the bias cannot be removed with this data and flatters every
  long result here. The other part (stocks present for years before they joined the index) is
  removed by the point-in-time restriction below.

## Strategies and variants (exactly these; nothing is tuned beyond this grid)

The five strategies are the production classes with the production 1d spec kwargs
(`get_strategy_specs(timeframe="1d")`). Their exits, as the engine runs them:

| strategy | stop | target | time stop | own SELL closes a long |
|---|---|---|---|---|
| inside_bar_narrow_range_breakout (`InsideBarNarrowRangeBreakoutStrategy(timeframe="1d")`) | min(previous low, close - 1 ATR14) | close + 2.1 x risk (`risk_multiple=2.1`) | none | never emitted |
| relative_strength_momentum (`RelativeStrengthMomentumStrategy(timeframe="1d")`) | max(12-bar low, close - 2 ATR14) | close + 2.4 x risk (`risk_multiple=2.4`) | none | never emitted |
| connors_rsi2_reversion (`ConnorsRSI2ReversionStrategy(timeframe="1d")`) | min(10-bar swing low, close - 2.5 ATR, close - 1 ATR) | the 5-bar mean, at least close + 1.2 x risk (no target kwarg) | none | never emitted |
| overnight_drift (`OvernightDriftStrategy(timeframe="1d")`) | close - 1.5 ATR (`stop_atr_mult`) | close + 1.0 ATR (`target_atr_mult`) | `max_hold_bars=1`: out at the open of the bar after the entry bar | never emitted |
| mean_reversion (`MeanReversionStrategy(lookback=20, zscore_threshold=1.8)`) | 5-bar low x 0.99 | the 20-bar mean, or close + 1.8 x risk if that is not above the close (no target kwarg) | none | yes: z >= +1.8 below the 40-bar mean |

Variants. "Target distance" is target minus the signal's close; every stop is unchanged.

| variant | inside_bar | relative_strength_momentum | connors_rsi2 | overnight_drift | mean_reversion |
|---|---|---|---|---|---|
| V0 as is | `risk_multiple=2.1` | `risk_multiple=2.4` | as is | `target_atr_mult=1.0`, `max_hold_bars=1` | as is |
| V1 target x2 | `risk_multiple=4.2` | `risk_multiple=4.8` | target distance x2 (no kwarg: code change) | `target_atr_mult=2.0`, `max_hold_bars=1` | target distance x2 (no kwarg: code change) |
| V2 target x3 | `risk_multiple=6.3` | `risk_multiple=7.2` | target distance x3 (code change) | `target_atr_mult=3.0`, `max_hold_bars=1` | target distance x3 (code change) |
| V3 no target, out after 10 bars | no target, `max_hold_bars=10` (code change) | same | same | no target, `max_hold_bars=10` (target removal is a code change) | same; its SELL still closes |
| V4 no target, out after 20 bars | no target, `max_hold_bars=20` (code change) | same | same | no target, `max_hold_bars=20` | same; its SELL still closes |
| V5 weekly bars, defaults | `timeframe="1w"` on 1w bars | `timeframe="1w"` on 1w bars | `timeframe="1w"` on 1w bars | **skipped** | the same kwargs on 1w bars |

* **V5 and "supports 1w".** None of the five has a registered 1w `StrategySpec`, so putting any
  V5 live needs a new spec line. Here a strategy supports 1w when its rules contain nothing tied
  to the daily session. inside_bar, relative_strength_momentum and connors take a `timeframe`
  kwarg that only selects intraday VWAP handling, and mean_reversion has no session logic, so
  all four run on weekly bars with their defaults. **overnight_drift is skipped:** it is defined
  as a close-to-next-open hold (`max_hold_bars=1`, "the closest the bar model expresses to a
  close-to-open capture"), which has no weekly meaning; V3/V4 already test its longer holds.
  Weekly bars use the live 1w window (520 bars, `bars_for_timeframe("1w")`) and the engine's
  rules; a weekly trade has no time stop (paper honours `max_hold_bars` on 1d only).
* **The engine's own-SELL exit is kept in every variant** (only mean_reversion emits SELLs):
  the evidence path closes a long at the next open after the strategy's SELL, so a variant that
  passes here describes the trade the gate would measure.
* The family has **29 variants** (5 strategies x V0-V4, plus V5 for four strategies).

## Method

### Signals (vectorised, verified against the real classes)

* Each strategy's rules are re-implemented with numpy/pandas over a symbol's whole cleaned
  segment, reproducing what the live bot computes on its window: the last 400 daily bars
  (`bars_for_timeframe("1d")`) or the last 520 weekly bars. Rolling indicators equal a fresh
  computation on that window. The EMAs (`ewm(adjust=False)`, seeded at the window's first bar)
  do not, so the 50- and 200-bar EMAs are corrected exactly to the live window's seed:
  `EMA_window(t) = EMA_full(t) + (1 - a)^(W-1) x (x(t-W+1) - EMA_full(t-W+1))`. (A 200-bar EMA
  still carries ~2% of its seed after 400 bars, enough to flip a "close above the 200-EMA" test
  near the line.) Prices are rounded to 4 decimals where the class rounds them.
* **Parity check (must pass before any run):** each real class's `generate_signal` is called on
  every bar of six symbols' histories (WMT, PYPL, DIS, TSLA, AAPL, JPM), each call given the
  live window (400 daily or 520 weekly bars), and its action, stop and target are compared with
  the vectorised signals. Any mismatch is a bug to fix, not a result.
* **Execution parity:** for three symbols, every strategy and variant is also run through the
  real `BacktestEngine` (full history, the production fee-inclusive cost model; V1-V4 through
  the kwargs above, or a thin wrapper that rescales or drops the target and sets
  `metadata['max_hold_bars']`). Its trades are compared with this study's trade paths in
  engine mode (indicators over the whole frame, as the engine's prefix cache computes them):
  entry and exit dates, fills, exit reasons, and financing per dollar of notional.

### Trades (the engine's rules, exactly)

* A BUY on bar N's close fills at bar N+1's open plus half the spread (5 bps). Stop and target
  are the signal's levels. A plan whose fill gaps beyond the stop or target is still entered and
  exits as the engine would (stop at the open, target at the target); such trades are counted.
* Each bar, in order: the time stop (`max_hold_bars` bars after the entry bar, out at that
  bar's open); the stop if the low touches it (fill at the stop, or the open if it gapped
  through); the target if the high touches it (fill at the target); the strategy's own SELL on
  the bar's close (fill at the next open). The stop wins a bar that touches both. The exit fill
  gives up half the spread.
* One position per symbol: a BUY while a position is open is ignored; a BUY on the exit bar
  itself may fill at the next open.
* A trade still open at the end of its period closes at that period's last close
  (`end_of_data`); a trade open at a break closes at the last close before it.
* **Sizing and R** as production's batch backtests: $10,000 cash, 1% risk, so 1R = $100;
  quantity = $100 / (fill - stop), capped at $10,000 of notional (1x). R = dollar P&L / $100.
  When the cap binds (stop closer than 1% to the fill), a trade risks less than $100, as in
  production.
* **Costs: the production model** `with_etoro_trade_fees(CostModel(), crypto=False)`: 10 bps
  round-trip spread (in the fills), $1 a leg, and overnight financing of 0.015% of the entry
  notional per calendar day held, with each Friday close crossed charged three times. The
  verdict uses this model. Reported alongside, never as the verdict: before-cost R (same
  quantity, raw prices, no fees), and **no-financing R** (spread and fees only; a real 1x eToro
  stock pays no financing).

### Universe, periods and holdout

* **Main universe: point-in-time S&P 500.** A signal counts only if the stock was in the index
  on the signal day by Wikipedia's "date added" (members that left and rejoined count from the
  latest date). 273 of today's members were in the index on 2011-01-03, 480 on 2025-11-03. The
  random control draws only in-index days too. The same run on today's members with no
  restriction is reported to show how much the restriction moves each number.
* **Secondary view: the live 24 names**, no restriction (three are ETFs), over the main period
  and over the last two years.
* **Main period:** signals from 2011-01-03 (2010 is warm-up) to 2025-10-31; trades still open at
  2025-10-31's close are closed there, so no main-period trade sees a holdout price. Nothing is
  fitted on it (the grid is fixed above), so all of it is out of sample.
* **Sealed holdout:** the last 12 months, signals from 2025-11-03 (daily) or the week starting
  2025-11-03 (weekly) to the end of the data (2026-10-30), positions starting flat. It is
  computed by a separate command after the main-period results are committed.
* Sub-periods: 2011-2015, 2016-2020 and 2021 to 2025-10-31, by signal date.

### Random-entry control

Every main-period trade gets five control trades: random bars of the same symbol in the same
calendar year (in-index days only in the point-in-time universe; never inside a break's
warm-up or on a segment's last bar), each entered at the next open with the trade's own plan
geometry: the same stop distance as a fraction of the signal close, the same target multiple
(target distance over stop distance; none when the variant has none), the same time stop and
the same own-SELL exit rule, and the same costs. The control has no one-position-per-symbol
constraint. `numpy.random.default_rng` seeds are fixed per symbol. A trade's **excess** is its
after-cost R minus the mean after-cost R of its own five control trades.

### Statistics

* Means per trade. t statistics are cluster-robust (CR1) by **entry month** (primary: trades
  overlap for days to weeks) and, for context, by entry day; p values are one-sided from the t
  distribution with G-1 degrees of freedom (`research/pullback/stats.py`).
* Holm's step-down adjustment over the 29 variants, on the one-sided p of the mean excess over
  the control (month-clustered).

### Pass rule (every part required; evaluated on the point-in-time universe)

1. **Expectancy.** Main-period after-cost R >= +0.03R with >= 40 trades, and sealed-holdout
   after-cost R > 0 with >= 10 trades.
2. **Timing.** Mean excess over the random-entry control > 0 with a Holm-adjusted one-sided p
   < 0.05 (month-clustered t). Whether 0.10 would change any verdict is reported, but the
   verdict uses 0.05.
3. **Stability.** (a) In at least 2 of the 3 sub-periods, the variant has >= 10 trades,
   after-cost R >= +0.03R and mean excess over the control > 0. (b) Not driven by one year:
   with the calendar year of the largest summed excess removed, after-cost R is still >= +0.03R
   and mean excess is still > 0.
4. **Point-in-time membership.** Rules 1-3 are evaluated on the point-in-time universe, which is
   what this rule asks; the today's-members run is reported next to it only to measure the
   restriction.

A variant passes only if all four hold. Each part's pass or fail is reported and never
softened. A "nothing passes" result is a valid outcome.

### Calibration of V0 against production (informational; not part of the rule)

* **Production replication:** the real engine and the production walk-forward
  (`splitter_for("1d")` folds: 180-day train, 14-day test and step, 1-day embargo, 28-day
  holdout; `_with_warmup` with the deep warm-up `DEEP_WARMUP_TIMEFRAMES` gives connors and
  overnight_drift; the fee-inclusive cost model; 1R = $100) on the live 24 names' last 500
  daily bars, pooled as `app/performance/strategy_evidence.py` pools them.
* This study's V0 trade paths on the same names and window, without folds.
* The gap between the two and production's own numbers is explained (fold cut-offs, EMA seeds
  in short fold windows, the data feed, 24 vs 25 names).

## Parity checks (both passed before the study ran)

**Signals: 139,284 bars, 0 mismatches.** Each real class was called on every bar of six
symbols' histories with the live window (400 daily or 520 weekly bars); action, price, stop and
target equal the vectorised signals (relative difference 0 at float precision, so the EMA seed
correction is exact).

**Execution: 14,499 of 14,499 trades identical** to the real `BacktestEngine` (production
fee-inclusive model, full history of WMT, AAPL and TSLA, every variant through its kwargs or
the target/max-hold wrapper): entry and exit dates, both fills, exit reason, financing to the
engine's six-decimal rounding, and the $2 of fees.

<details><summary>Per symbol and variant</summary>

| symbol | strategy | tf | bars | BUY real = vectorised | SELL | mismatches | max rel. diff stop / target |
|---|---|---|--:|--:|--:|--:|---|
| WMT | inside_bar_narrow_range_breakout | 1d | 4233 | 138 = 138 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | inside_bar_narrow_range_breakout | 1d | 2849 | 96 = 96 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | inside_bar_narrow_range_breakout | 1d | 4233 | 135 = 135 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | inside_bar_narrow_range_breakout | 1d | 4111 | 188 = 188 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | inside_bar_narrow_range_breakout | 1d | 4233 | 155 = 155 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | inside_bar_narrow_range_breakout | 1d | 4233 | 161 = 161 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | inside_bar_narrow_range_breakout | 1w | 878 | 33 = 33 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | inside_bar_narrow_range_breakout | 1w | 591 | 24 = 24 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | inside_bar_narrow_range_breakout | 1w | 878 | 28 = 28 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | inside_bar_narrow_range_breakout | 1w | 853 | 45 = 45 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | inside_bar_narrow_range_breakout | 1w | 878 | 33 = 33 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | inside_bar_narrow_range_breakout | 1w | 878 | 36 = 36 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | relative_strength_momentum | 1d | 4233 | 403 = 403 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | relative_strength_momentum | 1d | 2849 | 233 = 233 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | relative_strength_momentum | 1d | 4233 | 323 = 323 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | relative_strength_momentum | 1d | 4111 | 457 = 457 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | relative_strength_momentum | 1d | 4233 | 483 = 483 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | relative_strength_momentum | 1d | 4233 | 429 = 429 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | relative_strength_momentum | 1w | 878 | 107 = 107 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | relative_strength_momentum | 1w | 591 | 43 = 43 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | relative_strength_momentum | 1w | 878 | 71 = 71 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | relative_strength_momentum | 1w | 853 | 134 = 134 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | relative_strength_momentum | 1w | 878 | 130 = 130 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | relative_strength_momentum | 1w | 878 | 112 = 112 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | connors_rsi2_reversion | 1d | 4233 | 714 = 714 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | connors_rsi2_reversion | 1d | 2849 | 323 = 323 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | connors_rsi2_reversion | 1d | 4233 | 604 = 604 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | connors_rsi2_reversion | 1d | 4111 | 601 = 601 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | connors_rsi2_reversion | 1d | 4233 | 759 = 759 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | connors_rsi2_reversion | 1d | 4233 | 718 = 718 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | connors_rsi2_reversion | 1w | 878 | 123 = 123 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | connors_rsi2_reversion | 1w | 591 | 30 = 30 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | connors_rsi2_reversion | 1w | 878 | 119 = 119 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | connors_rsi2_reversion | 1w | 853 | 141 = 141 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | connors_rsi2_reversion | 1w | 878 | 136 = 136 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | connors_rsi2_reversion | 1w | 878 | 128 = 128 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | overnight_drift | 1d | 4233 | 1248 = 1248 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| PYPL | overnight_drift | 1d | 2849 | 660 = 660 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | overnight_drift | 1d | 4233 | 1009 = 1009 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | overnight_drift | 1d | 4111 | 990 = 990 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | overnight_drift | 1d | 4233 | 1336 = 1336 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | overnight_drift | 1d | 4233 | 1249 = 1249 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| WMT | mean_reversion | 1d | 4233 | 11 = 11 | 8 = 8 | 0 | 0e+00 / 0e+00 |
| PYPL | mean_reversion | 1d | 2849 | 3 = 3 | 3 = 3 | 0 | 0e+00 / 0e+00 |
| DIS | mean_reversion | 1d | 4233 | 9 = 9 | 1 = 1 | 0 | 0e+00 / 0e+00 |
| TSLA | mean_reversion | 1d | 4111 | 9 = 9 | 5 = 5 | 0 | 0e+00 / 0e+00 |
| AAPL | mean_reversion | 1d | 4233 | 8 = 8 | 4 = 4 | 0 | 0e+00 / 0e+00 |
| JPM | mean_reversion | 1d | 4233 | 13 = 13 | 4 = 4 | 0 | 0e+00 / 0e+00 |
| WMT | mean_reversion | 1w | 878 | 6 = 6 | 2 = 2 | 0 | 0e+00 / 0e+00 |
| PYPL | mean_reversion | 1w | 591 | 0 = 0 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| DIS | mean_reversion | 1w | 878 | 4 = 4 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| TSLA | mean_reversion | 1w | 853 | 0 = 0 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| AAPL | mean_reversion | 1w | 878 | 1 = 1 | 0 = 0 | 0 | 0e+00 / 0e+00 |
| JPM | mean_reversion | 1w | 878 | 3 = 3 | 1 = 1 | 0 | 0e+00 / 0e+00 |

| variant | engine trades | study trades | identical |
|---|--:|--:|--:|
| inside_bar V0 | 341 | 341 | 341 |
| inside_bar V1 | 276 | 276 | 276 |
| inside_bar V2 | 231 | 231 | 231 |
| inside_bar V3 | 374 | 374 | 374 |
| inside_bar V4 | 340 | 340 | 340 |
| inside_bar V5 | 90 | 90 | 90 |
| rs_momentum V0 | 270 | 270 | 270 |
| rs_momentum V1 | 177 | 177 | 177 |
| rs_momentum V2 | 139 | 139 | 139 |
| rs_momentum V3 | 383 | 383 | 383 |
| rs_momentum V4 | 279 | 279 | 279 |
| rs_momentum V5 | 91 | 91 | 91 |
| connors_rsi2 V0 | 337 | 337 | 337 |
| connors_rsi2 V1 | 186 | 186 | 186 |
| connors_rsi2 V2 | 117 | 117 | 117 |
| connors_rsi2 V3 | 647 | 647 | 647 |
| connors_rsi2 V4 | 441 | 441 | 441 |
| connors_rsi2 V5 | 80 | 80 | 80 |
| overnight_drift V0 | 2807 | 2807 | 2807 |
| overnight_drift V1 | 2616 | 2616 | 2616 |
| overnight_drift V2 | 2592 | 2592 | 2592 |
| overnight_drift V3 | 947 | 947 | 947 |
| overnight_drift V4 | 638 | 638 | 638 |
| mean_reversion V0 | 19 | 19 | 19 |
| mean_reversion V1 | 19 | 19 | 19 |
| mean_reversion V2 | 19 | 19 | 19 |
| mean_reversion V3 | 19 | 19 | 19 |
| mean_reversion V4 | 19 | 19 | 19 |
| mean_reversion V5 | 5 | 5 | 5 |

</details>

## Results: main period (committed before the holdout was computed)

One run of `research/bigger_moves/cli.py run` (86 seconds on 4 cores): signals 2011-01-03 ..
2025-10-31, trades cut at 2025-10-31's close, 3.5 million trade rows across the 29 variants and
both universes. The sealed holdout had not been computed when this section was committed; only
the main table's last column (holdout trades / R) was filled in afterwards.

### Point-in-time universe, one row per variant

| variant | change | trades | win % | before costs R | after costs R | no-financing R | cost $ spread / fee / financing | median bars | random control R | excess | month-t | Holm p | holdout trades / R |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| inside_bar V0 | none (risk_multiple=2.1) | 36497 | 37.2 | +0.108 | -0.040 | +0.046 | 4.12 / 2.00 / 8.59 | 6 | -0.030 | -0.010 | -0.36 | 1.000 | 3160 / -0.034 |
| inside_bar V1 | risk_multiple=4.2 | 28197 | 25.7 | +0.254 | +0.022 | +0.193 | 4.06 / 2.00 / 17.11 | 9 | +0.019 | +0.003 | 0.07 | 1.000 | 2694 / +0.039 |
| inside_bar V2 | risk_multiple=6.3 | 22879 | 20.6 | +0.398 | +0.084 | +0.338 | 4.00 / 2.00 / 25.31 | 11 | +0.049 | +0.036 | 0.65 | 1.000 | 2402 / +0.110 |
| inside_bar V3 | take-profit removed, max_hold_bars=10 (code change) | 39300 | 38.7 | +0.093 | -0.037 | +0.031 | 4.18 / 2.00 / 6.83 | 11 | -0.014 | -0.023 | -0.88 | 1.000 | 3234 / +0.022 |
| inside_bar V4 | take-profit removed, max_hold_bars=20 (code change) | 35530 | 32.8 | +0.160 | -0.012 | +0.099 | 4.14 / 2.00 / 11.03 | 11 | +0.006 | -0.017 | -0.45 | 1.000 | 2962 / +0.077 |
| inside_bar V5 | new StrategySpec(timeframe='1w') with the defaults | 8277 | 42.4 | +0.324 | +0.100 | +0.287 | 1.79 / 2.00 / 18.63 | 7 | +0.109 | -0.009 | -0.17 | 1.000 | 722 / -0.006 |
| rs_momentum V0 | none (risk_multiple=2.4) | 26866 | 35.7 | +0.175 | -0.022 | +0.125 | 2.99 / 2.00 / 14.72 | 14 | +0.121 | -0.143 | -4.16 | 1.000 | 2384 / -0.002 |
| rs_momentum V1 | risk_multiple=4.8 | 17386 | 25.3 | +0.386 | +0.058 | +0.337 | 2.95 / 2.00 / 27.87 | 20 | +0.246 | -0.188 | -3.92 | 1.000 | 1736 / +0.069 |
| rs_momentum V2 | risk_multiple=7.2 | 13546 | 20.7 | +0.568 | +0.128 | +0.519 | 2.94 / 2.00 / 39.10 | 21 | +0.333 | -0.204 | -3.26 | 1.000 | 1440 / +0.127 |
| rs_momentum V3 | take-profit removed, max_hold_bars=10 (code change) | 40301 | 43.9 | +0.083 | -0.032 | +0.030 | 3.25 / 2.00 / 6.27 | 11 | +0.038 | -0.070 | -3.27 | 1.000 | 3253 / +0.035 |
| rs_momentum V4 | take-profit removed, max_hold_bars=20 (code change) | 29396 | 40.1 | +0.130 | -0.026 | +0.079 | 3.12 / 2.00 / 10.51 | 20 | +0.090 | -0.116 | -3.51 | 1.000 | 2418 / +0.080 |
| rs_momentum V5 | new StrategySpec(timeframe='1w') with the defaults | 7332 | 42.8 | +0.442 | +0.125 | +0.408 | 1.39 / 2.00 / 28.30 | 14 | +0.387 | -0.262 | -6.31 | 1.000 | 790 / +0.018 |
| connors_rsi2 V0 | none | 30227 | 52.7 | +0.175 | +0.021 | +0.136 | 1.86 / 2.00 / 11.53 | 17 | +0.102 | -0.080 | -2.97 | 1.000 | 2632 / +0.024 |
| connors_rsi2 V1 | target distance x2 (no kwarg: code change) | 15756 | 39.8 | +0.371 | +0.098 | +0.333 | 1.80 / 2.00 / 23.58 | 34 | +0.222 | -0.125 | -2.99 | 1.000 | 1671 / +0.083 |
| connors_rsi2 V2 | target distance x3 (no kwarg: code change) | 11351 | 33.8 | +0.564 | +0.187 | +0.526 | 1.79 / 2.00 / 33.92 | 47 | +0.302 | -0.115 | -2.07 | 1.000 | 1378 / +0.155 |
| connors_rsi2 V3 | take-profit removed, max_hold_bars=10 (code change) | 70080 | 49.6 | +0.083 | -0.006 | +0.043 | 2.04 / 2.00 / 4.88 | 11 | +0.033 | -0.039 | -2.01 | 1.000 | 5514 / -0.011 |
| connors_rsi2 V4 | take-profit removed, max_hold_bars=20 (code change) | 47068 | 47.3 | +0.139 | +0.012 | +0.098 | 2.03 / 2.00 / 8.64 | 21 | +0.070 | -0.058 | -2.09 | 1.000 | 3727 / +0.012 |
| connors_rsi2 V5 | new StrategySpec(timeframe='1w') with the defaults | 7321 | 59.3 | +0.345 | +0.118 | +0.317 | 0.79 / 2.00 / 19.88 | 16 | +0.135 | -0.017 | -0.45 | 1.000 | 961 / +0.113 |
| overnight_drift V0 | none (target_atr_mult=1.0, max_hold_bars=1) | 304079 | 46.0 | +0.012 | -0.054 | -0.045 | 3.71 / 2.00 / 0.85 | 2 | -0.045 | -0.009 | -2.33 | 1.000 | 23099 / -0.051 |
| overnight_drift V1 | target_atr_mult=2.0 | 286137 | 45.0 | +0.014 | -0.052 | -0.043 | 3.70 / 2.00 / 0.96 | 2 | -0.040 | -0.012 | -2.78 | 1.000 | 21690 / -0.054 |
| overnight_drift V2 | target_atr_mult=3.0 | 284480 | 45.0 | +0.015 | -0.052 | -0.042 | 3.70 / 2.00 / 0.97 | 2 | -0.039 | -0.013 | -2.97 | 1.000 | 21554 / -0.055 |
| overnight_drift V3 | max_hold_bars=10, take-profit removed (code change) | 102237 | 42.6 | +0.103 | -0.022 | +0.047 | 3.65 / 2.00 / 6.91 | 11 | +0.025 | -0.047 | -2.08 | 1.000 | 8205 / -0.050 |
| overnight_drift V4 | max_hold_bars=20, take-profit removed (code change) | 69407 | 36.3 | +0.160 | -0.008 | +0.104 | 3.60 / 2.00 / 11.19 | 14 | +0.051 | -0.059 | -1.72 | 1.000 | 5716 / -0.015 |
| mean_reversion V0 | none | 2720 | 41.1 | +0.038 | -0.079 | -0.044 | 6.26 / 2.00 / 3.43 | 2 | -0.087 | +0.008 | 0.18 | 1.000 | 182 / -0.290 |
| mean_reversion V1 | target distance x2 (no kwarg: code change) | 2686 | 28.2 | +0.068 | -0.091 | -0.015 | 6.26 / 2.00 / 7.55 | 3 | -0.052 | -0.039 | -0.60 | 1.000 | 182 / -0.351 |
| mean_reversion V2 | target distance x3 (no kwarg: code change) | 2680 | 22.6 | +0.146 | -0.048 | +0.063 | 6.26 / 2.00 / 11.13 | 4 | +0.003 | -0.051 | -0.63 | 1.000 | 182 / -0.640 |
| mean_reversion V3 | take-profit removed, max_hold_bars=10 (code change) | 2681 | 29.8 | +0.131 | -0.025 | +0.048 | 6.26 / 2.00 / 7.28 | 4 | -0.041 | +0.016 | 0.24 | 1.000 | 182 / -0.317 |
| mean_reversion V4 | take-profit removed, max_hold_bars=20 (code change) | 2681 | 23.6 | +0.162 | -0.036 | +0.079 | 6.26 / 2.00 / 11.49 | 4 | +0.077 | -0.112 | -1.29 | 1.000 | 182 / -0.545 |
| mean_reversion V5 | new StrategySpec(timeframe='1w') with the defaults | 405 | 35.8 | +0.354 | +0.213 | +0.283 | 5.13 / 2.00 / 6.94 | 2 | -0.003 | +0.216 | 1.67 | 1.000 | 27 / +0.147 |

### Stability (rule 3)

| variant | trades: after-cost R (excess) in 2011-15 / 2016-20 / 2021-25 | sub-periods holding | without the best year: R (excess) |
|---|---|--:|---|
| inside_bar V0 | 10152: -0.07 (-0.02) / 12486: +0.03 (+0.01) / 13859: -0.08 (-0.01) | 1 | 2020: -0.055 (-0.016) |
| inside_bar V1 | 7850: +0.00 (+0.02) / 9527: +0.16 (+0.02) / 10820: -0.08 (-0.03) | 1 | 2020: -0.008 (-0.005) |
| inside_bar V2 | 6501: +0.07 (+0.05) / 7666: +0.27 (+0.05) / 8712: -0.07 (+0.01) | 2 | 2020: +0.035 (+0.018) |
| inside_bar V3 | 10907: -0.04 (-0.00) / 13482: +0.00 (-0.03) / 14911: -0.07 (-0.03) | 0 | 2013: -0.051 (-0.027) |
| inside_bar V4 | 9848: -0.04 (-0.03) / 12122: +0.05 (-0.01) / 13560: -0.05 (-0.01) | 0 | 2024: -0.021 (-0.026) |
| inside_bar V5 | 2277: +0.00 (-0.06) / 2814: +0.26 (+0.00) / 3186: +0.03 (+0.02) | 2 | 2023: +0.078 (-0.033) |
| rs_momentum V0 | 7890: -0.04 (-0.14) / 8976: +0.04 (-0.15) / 10000: -0.06 (-0.14) | 0 | 2020: -0.035 (-0.150) |
| rs_momentum V1 | 5067: +0.04 (-0.18) / 5933: +0.19 (-0.19) / 6386: -0.05 (-0.19) | 0 | 2020: +0.024 (-0.206) |
| rs_momentum V2 | 3959: +0.17 (-0.23) / 4725: +0.27 (-0.19) / 4862: -0.05 (-0.20) | 0 | 2020: +0.059 (-0.230) |
| rs_momentum V3 | 12064: -0.06 (-0.08) / 13536: -0.02 (-0.08) / 14701: -0.02 (-0.05) | 0 | 2023: -0.036 (-0.075) |
| rs_momentum V4 | 8783: -0.05 (-0.11) / 9840: -0.02 (-0.15) / 10773: -0.01 (-0.09) | 0 | 2020: -0.030 (-0.122) |
| rs_momentum V5 | 1898: +0.28 (-0.28) / 2820: +0.14 (-0.23) / 2614: -0.01 (-0.28) | 0 | 2011: +0.120 (-0.259) |
| connors_rsi2 V0 | 9182: +0.04 (-0.06) / 10183: +0.06 (-0.09) / 10862: -0.02 (-0.09) | 0 | 2011: +0.023 (-0.088) |
| connors_rsi2 V1 | 4863: +0.15 (-0.06) / 5416: +0.16 (-0.16) / 5477: -0.01 (-0.15) | 0 | 2011: +0.102 (-0.145) |
| connors_rsi2 V2 | 3514: +0.26 (-0.06) / 3958: +0.29 (-0.14) / 3879: +0.02 (-0.14) | 0 | 2011: +0.191 (-0.147) |
| connors_rsi2 V3 | 21035: -0.01 (-0.03) / 23362: -0.00 (-0.05) / 25683: -0.01 (-0.04) | 0 | 2021: -0.015 (-0.046) |
| connors_rsi2 V4 | 13971: +0.02 (-0.02) / 15856: +0.03 (-0.09) / 17241: -0.01 (-0.07) | 0 | 2021: -0.001 (-0.068) |
| connors_rsi2 V5 | 1298: +0.06 (+0.11) / 3146: +0.21 (-0.03) / 2877: +0.05 (-0.07) | 1 | 2014: +0.099 (-0.033) |
| overnight_drift V0 | 90335: -0.06 (-0.01) / 103055: -0.06 (-0.01) / 110689: -0.05 (-0.01) | 0 | 2018: -0.054 (-0.011) |
| overnight_drift V1 | 84914: -0.06 (-0.02) / 97039: -0.05 (-0.01) / 104184: -0.04 (-0.01) | 0 | 2018: -0.053 (-0.014) |
| overnight_drift V2 | 84421: -0.06 (-0.02) / 96485: -0.05 (-0.01) / 103574: -0.04 (-0.01) | 0 | 2018: -0.053 (-0.016) |
| overnight_drift V3 | 30108: -0.04 (-0.04) / 34533: +0.01 (-0.05) / 37596: -0.04 (-0.05) | 0 | 2018: -0.017 (-0.052) |
| overnight_drift V4 | 20584: -0.02 (-0.04) / 23355: +0.03 (-0.07) / 25468: -0.04 (-0.07) | 0 | 2024: -0.021 (-0.069) |
| mean_reversion V0 | 830: -0.03 (+0.06) / 829: +0.00 (+0.07) / 1061: -0.18 (-0.08) | 0 | 2015: -0.088 (-0.010) |
| mean_reversion V1 | 814: -0.09 (-0.02) / 818: +0.09 (+0.11) / 1054: -0.23 (-0.17) | 1 | 2020: -0.113 (-0.064) |
| mean_reversion V2 | 811: -0.12 (-0.05) / 815: +0.20 (+0.13) / 1054: -0.19 (-0.19) | 1 | 2019: -0.102 (-0.088) |
| mean_reversion V3 | 812: -0.01 (+0.09) / 815: +0.14 (+0.12) / 1054: -0.17 (-0.12) | 1 | 2012: -0.056 (-0.024) |
| mean_reversion V4 | 812: -0.09 (-0.18) / 815: +0.19 (+0.07) / 1054: -0.16 (-0.20) | 1 | 2019: -0.097 (-0.158) |
| mean_reversion V5 | 130: +0.38 (+0.43) / 111: +0.22 (+0.23) / 164: +0.08 (+0.03) | 3 | 2012: +0.133 (+0.165) |

### Universes

| variant | point-in-time: trades / R / excess | today's members, no restriction: trades / R / excess | live 24, 2011-2025: trades / R / control R / excess (month-t) |
|---|---|---|---|
| inside_bar V0 | 36497 / -0.040 / -0.010 | 45905 / -0.026 / -0.007 | 2401 / -0.018 / +0.048 / -0.066 (-1.51) |
| inside_bar V1 | 28197 / +0.022 / +0.003 | 35506 / +0.052 / +0.008 | 1873 / +0.114 / +0.141 / -0.028 (-0.37) |
| inside_bar V2 | 22879 / +0.084 / +0.036 | 28900 / +0.136 / +0.016 | 1513 / +0.270 / +0.296 / -0.026 (-0.26) |
| inside_bar V3 | 39300 / -0.037 / -0.023 | 49500 / -0.025 / -0.030 | 2575 / +0.021 / +0.050 / -0.029 (-0.69) |
| inside_bar V4 | 35530 / -0.012 / -0.017 | 44713 / +0.011 / -0.013 | 2297 / +0.112 / +0.131 / -0.019 (-0.27) |
| inside_bar V5 | 8277 / +0.100 / -0.009 | 10420 / +0.152 / -0.006 | 520 / +0.226 / +0.283 / -0.057 (-0.64) |
| rs_momentum V0 | 26866 / -0.022 / -0.143 | 34513 / +0.007 / -0.136 | 1945 / +0.086 / +0.191 / -0.104 (-1.78) |
| rs_momentum V1 | 17386 / +0.058 / -0.188 | 22281 / +0.126 / -0.172 | 1243 / +0.261 / +0.440 / -0.179 (-1.99) |
| rs_momentum V2 | 13546 / +0.128 / -0.204 | 17316 / +0.229 / -0.211 | 946 / +0.491 / +0.740 / -0.249 (-1.88) |
| rs_momentum V3 | 40301 / -0.032 / -0.070 | 51659 / -0.025 / -0.081 | 2733 / +0.028 / +0.100 / -0.072 (-2.15) |
| rs_momentum V4 | 29396 / -0.026 / -0.116 | 37571 / -0.007 / -0.117 | 2013 / +0.053 / +0.202 / -0.149 (-2.76) |
| rs_momentum V5 | 7332 / +0.125 / -0.262 | 9285 / +0.208 / -0.253 | 534 / +0.418 / +0.682 / -0.265 (-3.10) |
| connors_rsi2 V0 | 30227 / +0.021 / -0.080 | 38193 / +0.046 / -0.079 | 2216 / +0.101 / +0.176 / -0.075 (-1.66) |
| connors_rsi2 V1 | 15756 / +0.098 / -0.125 | 19972 / +0.152 / -0.115 | 1138 / +0.285 / +0.374 / -0.089 (-1.30) |
| connors_rsi2 V2 | 11351 / +0.187 / -0.115 | 14223 / +0.273 / -0.129 | 831 / +0.436 / +0.683 / -0.248 (-2.57) |
| connors_rsi2 V3 | 70080 / -0.006 / -0.039 | 88840 / +0.007 / -0.035 | 4781 / +0.042 / +0.072 / -0.030 (-1.01) |
| connors_rsi2 V4 | 47068 / +0.012 / -0.058 | 59530 / +0.030 / -0.053 | 3222 / +0.113 / +0.139 / -0.026 (-0.57) |
| connors_rsi2 V5 | 7321 / +0.118 / -0.017 | 8811 / +0.149 / -0.016 | 520 / +0.263 / +0.333 / -0.069 (-0.96) |
| overnight_drift V0 | 304079 / -0.054 / -0.009 | 385472 / -0.050 / -0.007 | 21033 / -0.056 / -0.046 / -0.009 (-1.31) |
| overnight_drift V1 | 286137 / -0.052 / -0.012 | 362629 / -0.048 / -0.010 | 19669 / -0.048 / -0.037 / -0.011 (-1.40) |
| overnight_drift V2 | 284480 / -0.052 / -0.013 | 360554 / -0.048 / -0.012 | 19527 / -0.046 / -0.030 / -0.016 (-2.13) |
| overnight_drift V3 | 102237 / -0.022 / -0.047 | 129229 / -0.005 / -0.042 | 6986 / +0.063 / +0.120 / -0.057 (-1.55) |
| overnight_drift V4 | 69407 / -0.008 / -0.059 | 87493 / +0.022 / -0.062 | 4688 / +0.148 / +0.178 / -0.030 (-0.51) |
| mean_reversion V0 | 2720 / -0.079 / +0.008 | 3447 / -0.063 / +0.013 | 163 / -0.077 / -0.069 / -0.008 (-0.07) |
| mean_reversion V1 | 2686 / -0.091 / -0.039 | 3406 / -0.053 / -0.002 | 161 / +0.075 / +0.120 / -0.044 (-0.23) |
| mean_reversion V2 | 2680 / -0.048 / -0.051 | 3399 / +0.004 / -0.010 | 161 / +0.296 / +0.021 / +0.276 (1.17) |
| mean_reversion V3 | 2681 / -0.025 / +0.016 | 3401 / +0.014 / -0.039 | 161 / +0.216 / +0.227 / -0.011 (-0.04) |
| mean_reversion V4 | 2681 / -0.036 / -0.112 | 3401 / +0.008 / -0.051 | 161 / +0.206 / +0.105 / +0.101 (0.37) |
| mean_reversion V5 | 405 / +0.213 / +0.216 | 507 / +0.172 / +0.190 | 23 / +1.292 / -0.040 / +1.332 (2.56) |

The pass-rule table as it stood before the holdout is in `325598a`; the final one is below.

## Results: sealed holdout (2025-11-03 .. 2026-10-30)

Computed after the main-period results were committed (24 seconds). Positions start flat. The
holdout trade count and after-cost R of each variant are also in the main table's last column.

| variant | point-in-time holdout: trades / R / no-financing R | live 24 holdout: trades / R |
|---|---|---|
| inside_bar V0 | 3160 / -0.034 / +0.024 | 151 / +0.186 |
| inside_bar V1 | 2694 / +0.039 / +0.147 | 124 / +0.068 |
| inside_bar V2 | 2402 / +0.110 / +0.259 | 111 / +0.138 |
| inside_bar V3 | 3234 / +0.022 / +0.073 | 158 / +0.052 |
| inside_bar V4 | 2962 / +0.077 / +0.158 | 142 / +0.115 |
| inside_bar V5 | 722 / -0.006 / +0.099 | 24 / -0.144 |
| rs_momentum V0 | 2384 / -0.002 / +0.090 | 129 / -0.191 |
| rs_momentum V1 | 1736 / +0.069 / +0.226 | 100 / -0.224 |
| rs_momentum V2 | 1440 / +0.127 / +0.343 | 87 / -0.118 |
| rs_momentum V3 | 3253 / +0.035 / +0.081 | 167 / -0.080 |
| rs_momentum V4 | 2418 / +0.080 / +0.153 | 125 / +0.042 |
| rs_momentum V5 | 790 / +0.018 / +0.175 | 42 / +0.011 |
| connors_rsi2 V0 | 2632 / +0.024 / +0.102 | 148 / -0.039 |
| connors_rsi2 V1 | 1671 / +0.083 / +0.222 | 102 / +0.015 |
| connors_rsi2 V2 | 1378 / +0.155 / +0.337 | 80 / -0.037 |
| connors_rsi2 V3 | 5514 / -0.011 / +0.024 | 307 / -0.040 |
| connors_rsi2 V4 | 3727 / +0.012 / +0.075 | 211 / -0.046 |
| connors_rsi2 V5 | 961 / +0.113 / +0.231 | 53 / +0.094 |
| overnight_drift V0 | 23099 / -0.051 / -0.045 | 1286 / -0.060 |
| overnight_drift V1 | 21690 / -0.054 / -0.048 | 1202 / -0.062 |
| overnight_drift V2 | 21554 / -0.055 / -0.048 | 1193 / -0.066 |
| overnight_drift V3 | 8205 / -0.050 / -0.001 | 455 / -0.106 |
| overnight_drift V4 | 5716 / -0.015 / +0.064 | 318 / -0.093 |
| mean_reversion V0 | 182 / -0.290 / -0.269 | 11 / -0.252 |
| mean_reversion V1 | 182 / -0.351 / -0.303 | 11 / -0.533 |
| mean_reversion V2 | 182 / -0.640 / -0.570 | 11 / -0.675 |
| mean_reversion V3 | 182 / -0.317 / -0.275 | 11 / -0.219 |
| mean_reversion V4 | 182 / -0.545 / -0.479 | 11 / -0.485 |
| mean_reversion V5 | 27 / +0.147 / +0.214 | 4 / +1.642 |

The last 12 months were good for the bigger-target and longer-hold variants of inside_bar,
momentum and connors (13 of their 15 are positive) and bad for mean_reversion (-0.29R to -0.64R
on 182 trades) and overnight_drift (-0.015R to -0.055R).

## Verdict

| variant | 1 main >= +0.03R, >= 40 | 1 holdout > 0, >= 10 | 2 beats control (Holm p < 0.05) | 2 at 0.10? | 3a 2 of 3 sub-periods | 3b not one year | 4 point-in-time | verdict |
|---|---|---|---|---|---|---|---|---|
| inside_bar V0 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| inside_bar V1 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| inside_bar V2 | pass | pass | **fail** | no | pass | pass | by construction | fail |
| inside_bar V3 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| inside_bar V4 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| inside_bar V5 | pass | **fail** | **fail** | no | pass | **fail** | by construction | fail |
| rs_momentum V0 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| rs_momentum V1 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| rs_momentum V2 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| rs_momentum V3 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| rs_momentum V4 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| rs_momentum V5 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V0 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V1 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V2 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V3 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V4 | **fail** | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| connors_rsi2 V5 | pass | pass | **fail** | no | **fail** | **fail** | by construction | fail |
| overnight_drift V0 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| overnight_drift V1 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| overnight_drift V2 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| overnight_drift V3 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| overnight_drift V4 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V0 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V1 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V2 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V3 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V4 | **fail** | **fail** | **fail** | no | **fail** | **fail** | by construction | fail |
| mean_reversion V5 | pass | pass | **fail** | no | pass | pass | by construction | fail |

**0 of 29 pass.** Rule 2 fails for every variant, so no holdout or sub-period result could have
changed the verdict. Rule 4 is met by construction (rules 1-3 run on the point-in-time
universe); without the restriction (today's members on every day) R is 0.04R lower to 0.10R
higher, but the excess over the control moves by at most 0.06R and changes no verdict (see
"Universes").

Closest to passing, for the record:

* **mean_reversion V5 (weekly bars)**: passes 1 and 3 (405 trades, +0.213R; holdout 27 trades,
  +0.147R; all three sub-periods positive), fails 2: +0.216R over the control, month-t 1.67, raw
  one-sided p 0.049, Holm p 1.00. One of 29 tests at p 0.049 is what chance gives. It is also
  thin: 405 trades from about 480 stocks over 15 years, 23 of them on the live 24 names.
* **inside_bar V2 (risk_multiple=6.3)**: passes 1 and 3 (22,879 trades, +0.084R; holdout
  +0.110R), fails 2: +0.036R over the control, month-t 0.65.

## No-financing sensitivity (not the verdict)

| variant | after costs R | no-financing R | control no-financing R | excess | excess, no financing |
|---|--:|--:|--:|--:|--:|
| inside_bar V0 | -0.040 | +0.046 | +0.055 | -0.010 | -0.009 |
| inside_bar V1 | +0.022 | +0.193 | +0.189 | +0.003 | +0.005 |
| inside_bar V2 | +0.084 | +0.338 | +0.296 | +0.036 | +0.041 |
| inside_bar V3 | -0.037 | +0.031 | +0.054 | -0.023 | -0.023 |
| inside_bar V4 | -0.012 | +0.099 | +0.117 | -0.017 | -0.018 |
| inside_bar V5 | +0.100 | +0.287 | +0.278 | -0.009 | +0.009 |
| rs_momentum V0 | -0.022 | +0.125 | +0.257 | -0.143 | -0.132 |
| rs_momentum V1 | +0.058 | +0.337 | +0.517 | -0.188 | -0.180 |
| rs_momentum V2 | +0.128 | +0.519 | +0.724 | -0.204 | -0.205 |
| rs_momentum V3 | -0.032 | +0.030 | +0.100 | -0.070 | -0.070 |
| rs_momentum V4 | -0.026 | +0.079 | +0.196 | -0.116 | -0.117 |
| rs_momentum V5 | +0.125 | +0.408 | +0.653 | -0.262 | -0.245 |
| connors_rsi2 V0 | +0.021 | +0.136 | +0.209 | -0.080 | -0.073 |
| connors_rsi2 V1 | +0.098 | +0.333 | +0.441 | -0.125 | -0.108 |
| connors_rsi2 V2 | +0.187 | +0.526 | +0.625 | -0.115 | -0.099 |
| connors_rsi2 V3 | -0.006 | +0.043 | +0.082 | -0.039 | -0.039 |
| connors_rsi2 V4 | +0.012 | +0.098 | +0.157 | -0.058 | -0.059 |
| connors_rsi2 V5 | +0.118 | +0.317 | +0.327 | -0.017 | -0.011 |
| overnight_drift V0 | -0.054 | -0.045 | -0.037 | -0.009 | -0.008 |
| overnight_drift V1 | -0.052 | -0.043 | -0.031 | -0.012 | -0.012 |
| overnight_drift V2 | -0.052 | -0.042 | -0.029 | -0.013 | -0.013 |
| overnight_drift V3 | -0.022 | +0.047 | +0.093 | -0.047 | -0.046 |
| overnight_drift V4 | -0.008 | +0.104 | +0.164 | -0.059 | -0.060 |
| mean_reversion V0 | -0.079 | -0.044 | -0.049 | +0.008 | +0.004 |
| mean_reversion V1 | -0.091 | -0.015 | +0.025 | -0.039 | -0.040 |
| mean_reversion V2 | -0.048 | +0.063 | +0.113 | -0.051 | -0.050 |
| mean_reversion V3 | -0.025 | +0.048 | +0.032 | +0.016 | +0.016 |
| mean_reversion V4 | -0.036 | +0.079 | +0.194 | -0.112 | -0.115 |
| mean_reversion V5 | +0.213 | +0.283 | +0.075 | +0.216 | +0.207 |

Financing is charged to a trade and to its control alike (same notional, similar holds), so
removing it moves R a lot and the excess hardly at all.

## What the variants show

* **Bigger targets.** With the stop unchanged, a target x2 or x3 holds each trade longer, so
  R rises: every entry is long, in stocks that drifted up over 2011-2025, and a far target
  holds them through more of that drift. The random-entry control, with the same stop and
  target, rises in step (inside_bar) or faster (momentum, connors). For momentum the gap widens
  with the target (excess -0.14R -> -0.19R -> -0.20R): buying after a 20-day surge does worse
  than buying the same stock on a random day of the same year, consistent with short-term
  reversal.
* **Time exits.** Removing the target and leaving after 10 or 20 bars turns every strategy into
  "hold N days"; after costs this is about zero or negative, and below the control everywhere
  except mean_reversion V3 (+0.016R, month-t 0.24).
* **Weekly bars.** Wider weekly stops mean a smaller notional per $100 of risk (median $735-
  $1,650 for inside_bar, momentum and connors, against $1,600-$3,700 for their daily versions),
  so spread falls to $0.8-$1.8 a trade, but trades last weeks and financing runs $18-$28 a
  trade. After-cost R is +0.10R to +0.21R, and only mean_reversion's is above its control.
* **Costs.** Spread is $0.8-$6.3 a trade and the fee $2.00 in every variant; financing is $0.9
  (overnight_drift V0) to $39 (momentum V2). Production's $6.2 average is over fold-cut trades,
  more than half of them overnight_drift's one-day holds.

## mean_reversion's verdict

| | trades | after costs R | before costs R | control R | excess (month-t) | holdout trades / R |
|---|--:|--:|--:|--:|--:|---|
| production (live 25, ~2 years) | 6 | +0.18 | n/a | n/a | n/a | n/a |
| V0, point-in-time S&P 500, 2011-2025 | 2,720 | -0.079 | +0.038 | -0.087 | +0.008 (0.18) | 182 / -0.290 |
| V0, live 24 names, 2011-2025 | 163 | -0.077 | n/a | -0.069 | -0.008 (-0.07) | 11 / -0.252 |
| V5 weekly, point-in-time | 405 | +0.213 | +0.354 | -0.003 | +0.216 (1.67) | 27 / +0.147 |

The daily rule (a close 1.8 standard deviations below its 20-day mean while still above its
40-day mean) gives about 2,700 trades in 15 years across up to 480 stocks. Its target, the
20-day mean, is close (median hold 2 bars) and its stop is close too (median 1.7% away, so a
~$6,000 position per $100 of risk): $8.3 of spread and fees plus $3.4 of financing take more
than its +0.038R before costs. No variant fixes that.

## Calibration: V0 against production (informational)

Production's walk-forward (`splitter_for("1d")`, `_with_warmup` with the deep warm-up, the
fee-inclusive model, 1R = $100) was replicated with the real engine on the live 24 names' last
500 daily bars of this cache: 37 folds a symbol, OOS signals 2025-05-01 .. 2026-10-01, holdout
2026-10-03 .. 2026-10-30. "Study V0, no folds" is this study's trade paths on the same names
and signal window, each trade run to its own exit.

| strategy | production (brief): trades / before / after | replication: trades / before / after | replication cost $ spread / fee / financing | closed at fold end % | median days held | replication holdout: trades / R | study V0, no folds: trades / before / after | study median bars | replicated entries: on a live-window BUY / off it, on test bar 1-2 / also a study entry |
|---|---|---|---|--:|--:|---|---|--:|--:|
| inside_bar | 264 / +0.047 / -0.038 | 288 / +0.132 / +0.057 | 3.46 / 2.00 / 2.06 | 60 | 2 | 15 / -0.373 | 224 / +0.355 / +0.221 | 6 | 227 / 28 / 176 |
| rs_momentum | 288 / +0.014 / -0.056 | 303 / +0.186 / +0.115 | 2.46 / 2.00 / 2.60 | 67 | 6 | 20 / -0.686 | 188 / +0.300 / +0.152 | 11 | 249 / 10 / 93 |
| connors_rsi2 | 361 / +0.023 / -0.032 | 459 / +0.081 / +0.025 | 1.68 / 2.00 / 1.98 | 82 | 7 | 28 / -0.540 | 204 / +0.270 / +0.142 | 19 | 412 / 39 / 109 |
| overnight_drift | 1101 / +0.038 / -0.017 | 1407 / +0.036 / -0.020 | 3.01 / 2.00 / 0.57 | 7 | 1 | 115 / -0.143 | 1931 / +0.020 / -0.039 | 2 | 1405 / 0 / 1355 |
| mean_reversion | 6 / n/a / +0.180 | 12 / +0.173 / +0.099 | 4.56 / 2.00 / 0.80 | 25 | 0 | 1 / -1.039 | 11 / +0.024 / -0.070 | 2 | 9 / 0 / 9 |

* **overnight_drift replicates production almost exactly** (-0.020R against -0.017R; costs
  $5.6 a trade). Its one-bar hold is never cut by a fold, and it has no volume filter.
* **inside_bar, momentum and connors** come out +0.057R, +0.115R and +0.025R in the
  replication, 0.06-0.17R better than production's -0.038R, -0.056R and -0.032R. Each is within
  about 1-2 standard errors of a ~300-trade pool (replication month-t 0.77, 1.13 and 0.61). The
  likely sources, none of which this study can remove: the feed (Yahoo's consolidated bars
  here, Alpaca IEX in production; inside_bar's and momentum's relative-volume tests and the
  inside/narrow-range test read volume and range, which differ most on IEX); the window
  (production's bars run to 2026-11-09, so its folds and its 28-day holdout sit 7 sessions
  later); 24 of the 25 names; and production pooling each symbol's newest row from the last 7
  days. Production's lower trade counts for connors (361 against 459) and overnight_drift (1,101
  against 1,407) may also mean that not every symbol had a row with the deep warm-up released on
  2026-11-07 by 2026-11-10; that was not checked.
* **Signals mostly agree; the trades differ because of fold cut-offs.** 75-100% of the
  replicated entries sit on a bar where this study's live-window rule also signals a BUY. But
  60-82% of the replicated inside_bar, momentum and connors trades end at a fold's end (median
  2-7 days held), which frees the symbol for the next signal: hence more, and different, trades
  than without folds.
* **A fold skips one session.** In all 888 replicated folds per strategy, the fold frame
  (`_with_warmup`: the bars before `train_end` plus the test window) leaves out the session in
  the one-day embargo `[train_end, test_start)`. The test bars' previous bar, ATR, relative
  volume and EMAs then span a missing session that the live bot never skips. Of the replicated
  entries that are not on a live-window BUY, 28 of 61 (inside_bar), 10 of 54 (momentum) and 39
  of 47 (connors) fall on a fold's first two test bars; the rest come from rolling windows that
  still span the gap, and, for momentum, the 200-EMA seeded at the start of a ~134-bar fold
  frame. The step is 14 days, so whether a run drops a session depends only on the weekday of
  its first bar. Not changed here; noted for whoever owns the evidence path.
* **The gate measures a truncated strategy.** Without folds, the same names and window give
  +0.22R, +0.15R and +0.14R (median holds 6-19 bars); with folds +0.06R, +0.12R and +0.03R. Any
  variant whose hold exceeds ~10 bars (V1-V4 here) would be scored by production mostly on
  cut-off trades. Over 2011-2025 on the point-in-time S&P 500, the full-length V0 numbers
  (-0.040R, -0.022R, +0.021R, -0.054R) are close to production's.

## Things this study does not model

* **Removed members.** Companies that left the S&P 500 are absent, which flatters every long
  result. The point-in-time restriction removes only the "joined later" part; the random control
  shares the remaining bias, so the excess is the cleaner number.
* **Intraday signalling and live gates.** Live 1d entries are signalled on the unfinished daily
  bar, and the evidence gate, relative-volume floor, correlation and group caps, daily entry
  caps, re-entry cooldown, 35 bps entry-drift check and eToro's room and copy rules are not
  modelled. The engine enters plans whose open gaps past the stop (overnight_drift V0: 1,632 of
  304,079) or the target (3,449); they are kept, as the evidence path keeps them.
* **The strategy's own SELL.** The engine closes a mean_reversion long at the next open after
  its own SELL; this study follows the engine.
* **Weekly bars** are built from Yahoo's daily bars; the live 1w feed is Alpaca's.

## Reproduce

```
python -I research/bigger_moves/cli.py parity    --data-dir DATA --out OUT
python -I research/bigger_moves/cli.py run       --data-dir DATA --out OUT
python -I research/bigger_moves/cli.py holdout   --data-dir DATA --out OUT
python -I research/bigger_moves/cli.py calibrate --data-dir DATA --out OUT
python -I research/bigger_moves/report.py OUT > OUT/tables.md
```

`DATA` is the pullback study's cache (downloaded 2026-11-01, last bar 2026-10-30; not
committed). About 15 minutes on 4 cores, 10 of them the parity check. The simulation is
deterministic (fixed seeds); Yahoo revises history, so a fresh download can move numbers
slightly.
