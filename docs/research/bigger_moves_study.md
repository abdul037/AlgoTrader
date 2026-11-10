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

**Status: PRE-REGISTERED.** The question, data, variants, method and pass rule below were
committed before any signal, trade or result of this study was computed. The results sections
are added after the runs; nothing in the method is changed after a result is seen. The sealed
holdout is computed by a separate command only after the main-period results are committed.

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

## Results: main period (committed before the holdout was computed)

One run of `research/bigger_moves/cli.py run` (86 seconds on 4 cores): signals 2011-01-03 ..
2025-10-31, trades cut at 2025-10-31's close, 3.5 million trade rows across the 29 variants and
both universes. The sealed holdout had not been computed when this section was committed.

### Point-in-time universe, one row per variant

| variant | change | trades | win % | before costs R | after costs R | no-financing R | cost $ spread / fee / financing | median bars | random control R | excess | month-t | Holm p | holdout trades / R |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| inside_bar V0 | none (risk_multiple=2.1) | 36497 | 37.2 | +0.108 | -0.040 | +0.046 | 4.12 / 2.00 / 8.59 | 6 | -0.030 | -0.010 | -0.36 | 1.000 | sealed |
| inside_bar V1 | risk_multiple=4.2 | 28197 | 25.7 | +0.254 | +0.022 | +0.193 | 4.06 / 2.00 / 17.11 | 9 | +0.019 | +0.003 | 0.07 | 1.000 | sealed |
| inside_bar V2 | risk_multiple=6.3 | 22879 | 20.6 | +0.398 | +0.084 | +0.338 | 4.00 / 2.00 / 25.31 | 11 | +0.049 | +0.036 | 0.65 | 1.000 | sealed |
| inside_bar V3 | take-profit removed, max_hold_bars=10 (code change) | 39300 | 38.7 | +0.093 | -0.037 | +0.031 | 4.18 / 2.00 / 6.83 | 11 | -0.014 | -0.023 | -0.88 | 1.000 | sealed |
| inside_bar V4 | take-profit removed, max_hold_bars=20 (code change) | 35530 | 32.8 | +0.160 | -0.012 | +0.099 | 4.14 / 2.00 / 11.03 | 11 | +0.006 | -0.017 | -0.45 | 1.000 | sealed |
| inside_bar V5 | new StrategySpec(timeframe='1w') with the defaults | 8277 | 42.4 | +0.324 | +0.100 | +0.287 | 1.79 / 2.00 / 18.63 | 7 | +0.109 | -0.009 | -0.17 | 1.000 | sealed |
| rs_momentum V0 | none (risk_multiple=2.4) | 26866 | 35.7 | +0.175 | -0.022 | +0.125 | 2.99 / 2.00 / 14.72 | 14 | +0.121 | -0.143 | -4.16 | 1.000 | sealed |
| rs_momentum V1 | risk_multiple=4.8 | 17386 | 25.3 | +0.386 | +0.058 | +0.337 | 2.95 / 2.00 / 27.87 | 20 | +0.246 | -0.188 | -3.92 | 1.000 | sealed |
| rs_momentum V2 | risk_multiple=7.2 | 13546 | 20.7 | +0.568 | +0.128 | +0.519 | 2.94 / 2.00 / 39.10 | 21 | +0.333 | -0.204 | -3.26 | 1.000 | sealed |
| rs_momentum V3 | take-profit removed, max_hold_bars=10 (code change) | 40301 | 43.9 | +0.083 | -0.032 | +0.030 | 3.25 / 2.00 / 6.27 | 11 | +0.038 | -0.070 | -3.27 | 1.000 | sealed |
| rs_momentum V4 | take-profit removed, max_hold_bars=20 (code change) | 29396 | 40.1 | +0.130 | -0.026 | +0.079 | 3.12 / 2.00 / 10.51 | 20 | +0.090 | -0.116 | -3.51 | 1.000 | sealed |
| rs_momentum V5 | new StrategySpec(timeframe='1w') with the defaults | 7332 | 42.8 | +0.442 | +0.125 | +0.408 | 1.39 / 2.00 / 28.30 | 14 | +0.387 | -0.262 | -6.31 | 1.000 | sealed |
| connors_rsi2 V0 | none | 30227 | 52.7 | +0.175 | +0.021 | +0.136 | 1.86 / 2.00 / 11.53 | 17 | +0.102 | -0.080 | -2.97 | 1.000 | sealed |
| connors_rsi2 V1 | target distance x2 (no kwarg: code change) | 15756 | 39.8 | +0.371 | +0.098 | +0.333 | 1.80 / 2.00 / 23.58 | 34 | +0.222 | -0.125 | -2.99 | 1.000 | sealed |
| connors_rsi2 V2 | target distance x3 (no kwarg: code change) | 11351 | 33.8 | +0.564 | +0.187 | +0.526 | 1.79 / 2.00 / 33.92 | 47 | +0.302 | -0.115 | -2.07 | 1.000 | sealed |
| connors_rsi2 V3 | take-profit removed, max_hold_bars=10 (code change) | 70080 | 49.6 | +0.083 | -0.006 | +0.043 | 2.04 / 2.00 / 4.88 | 11 | +0.033 | -0.039 | -2.01 | 1.000 | sealed |
| connors_rsi2 V4 | take-profit removed, max_hold_bars=20 (code change) | 47068 | 47.3 | +0.139 | +0.012 | +0.098 | 2.03 / 2.00 / 8.64 | 21 | +0.070 | -0.058 | -2.09 | 1.000 | sealed |
| connors_rsi2 V5 | new StrategySpec(timeframe='1w') with the defaults | 7321 | 59.3 | +0.345 | +0.118 | +0.317 | 0.79 / 2.00 / 19.88 | 16 | +0.135 | -0.017 | -0.45 | 1.000 | sealed |
| overnight_drift V0 | none (target_atr_mult=1.0, max_hold_bars=1) | 304079 | 46.0 | +0.012 | -0.054 | -0.045 | 3.71 / 2.00 / 0.85 | 2 | -0.045 | -0.009 | -2.33 | 1.000 | sealed |
| overnight_drift V1 | target_atr_mult=2.0 | 286137 | 45.0 | +0.014 | -0.052 | -0.043 | 3.70 / 2.00 / 0.96 | 2 | -0.040 | -0.012 | -2.78 | 1.000 | sealed |
| overnight_drift V2 | target_atr_mult=3.0 | 284480 | 45.0 | +0.015 | -0.052 | -0.042 | 3.70 / 2.00 / 0.97 | 2 | -0.039 | -0.013 | -2.97 | 1.000 | sealed |
| overnight_drift V3 | max_hold_bars=10, take-profit removed (code change) | 102237 | 42.6 | +0.103 | -0.022 | +0.047 | 3.65 / 2.00 / 6.91 | 11 | +0.025 | -0.047 | -2.08 | 1.000 | sealed |
| overnight_drift V4 | max_hold_bars=20, take-profit removed (code change) | 69407 | 36.3 | +0.160 | -0.008 | +0.104 | 3.60 / 2.00 / 11.19 | 14 | +0.051 | -0.059 | -1.72 | 1.000 | sealed |
| mean_reversion V0 | none | 2720 | 41.1 | +0.038 | -0.079 | -0.044 | 6.26 / 2.00 / 3.43 | 2 | -0.087 | +0.008 | 0.18 | 1.000 | sealed |
| mean_reversion V1 | target distance x2 (no kwarg: code change) | 2686 | 28.2 | +0.068 | -0.091 | -0.015 | 6.26 / 2.00 / 7.55 | 3 | -0.052 | -0.039 | -0.60 | 1.000 | sealed |
| mean_reversion V2 | target distance x3 (no kwarg: code change) | 2680 | 22.6 | +0.146 | -0.048 | +0.063 | 6.26 / 2.00 / 11.13 | 4 | +0.003 | -0.051 | -0.63 | 1.000 | sealed |
| mean_reversion V3 | take-profit removed, max_hold_bars=10 (code change) | 2681 | 29.8 | +0.131 | -0.025 | +0.048 | 6.26 / 2.00 / 7.28 | 4 | -0.041 | +0.016 | 0.24 | 1.000 | sealed |
| mean_reversion V4 | take-profit removed, max_hold_bars=20 (code change) | 2681 | 23.6 | +0.162 | -0.036 | +0.079 | 6.26 / 2.00 / 11.49 | 4 | +0.077 | -0.112 | -1.29 | 1.000 | sealed |
| mean_reversion V5 | new StrategySpec(timeframe='1w') with the defaults | 405 | 35.8 | +0.354 | +0.213 | +0.283 | 5.13 / 2.00 / 6.94 | 2 | -0.003 | +0.216 | 1.67 | 1.000 | sealed |

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

### Pass rule on the main period

| variant | 1 main >= +0.03R, >= 40 | 1 holdout > 0, >= 10 | 2 beats control (Holm p < 0.05) | 2 at 0.10? | 3a 2 of 3 sub-periods | 3b not one year | 4 point-in-time | verdict |
|---|---|---|---|---|---|---|---|---|
| inside_bar V0 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| inside_bar V1 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| inside_bar V2 | pass | sealed | **fail** | no | pass | pass | by construction | pending |
| inside_bar V3 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| inside_bar V4 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| inside_bar V5 | pass | sealed | **fail** | no | pass | **fail** | by construction | pending |
| rs_momentum V0 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| rs_momentum V1 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| rs_momentum V2 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| rs_momentum V3 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| rs_momentum V4 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| rs_momentum V5 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V0 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V1 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V2 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V3 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V4 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| connors_rsi2 V5 | pass | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| overnight_drift V0 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| overnight_drift V1 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| overnight_drift V2 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| overnight_drift V3 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| overnight_drift V4 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V0 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V1 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V2 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V3 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V4 | **fail** | sealed | **fail** | no | **fail** | **fail** | by construction | pending |
| mean_reversion V5 | pass | sealed | **fail** | no | pass | pass | by construction | pending |
