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

## Results

(Added after the runs.)
