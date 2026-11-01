# Pullback (ema_trend_stack, daily) edge study (2026-11-01) -- research only

**Question.** Paper trading's best results came from the daily `ema_trend_stack` strategy, and
its open October trades (WMT, PYPL, DIS, TSLA) are up. Production's own daily walk-forward on the
live 25-name universe (24 names today) over about two years says roughly -0.03R a trade. Does buying
the dip in a rising stock have a real, cost-surviving edge across many stocks and many years, or
was October luck? And do simple additions, fixed before any result was seen, improve it?

**Answer: no timing edge was found. The positive numbers come from holding stocks that went up.**

* On 500 of today's S&P 500 members from 2011 to October 2026, the bot's exact rules (V1) earn
  **+0.285R a trade after costs** (22,038 trades, day-clustered t = +14.8).
* **Random entry days in the same stock and the same year, with the same stop distance and
  target, earn +0.317R.** V1 minus that control is **-0.031R** (day-t -1.8, month-t -0.8). The
  pullback and crossover timing adds nothing measurable.
* The universe is today's members, which flatters any long strategy. Today's members beat the
  equal-weight S&P 500 as it actually was (RSP) by **5.9 points a year**. Removing that drift
  roughly halves V1's R (+0.150) and cuts its portfolio Sharpe from 0.84 to 0.49, against 0.85
  for SPY.
* **The pre-registered rule passes one variant, V6** (at most two open positions per GICS
  sector), and only through criterion (c): Sharpe 0.89 against SPY's 0.85. That margin is far
  inside the noise of a 16-year Sharpe ratio (standard error about 0.3), and V6 fails (c) after
  the rough survivorship correction. All nine variants pass (a) and (b). Those criteria test
  "R > 0", which any long-only bracket on rising stocks satisfies.
* **All five market filters (V2, V3, V7, V8, V9) remove trades that made more than the trades
  they keep** (removed +0.32 to +0.64R, kept +0.24 to +0.27R). None should be added.
* On the live 24 names over the last two years, the rules earn **+0.009R** a trade (179 trades,
  t +0.08), in line with production's slightly negative walk-forward.

Nothing here touches live trading. The code is in `research/pullback/`, which `app/` never
imports; it reads `app` read-only.

## Data

| | source | window | symbols |
|---|---|---|---|
| members, GICS sector | Wikipedia "List of S&P 500 companies" constituents table, fetched 2026-11-01 (the `datasets/s-and-p-500-companies` CSV on GitHub lists the same 503 symbols and sectors) | today | 503 |
| daily OHLCV | yfinance, `auto_adjust=True` (split- and dividend-adjusted) | 2010-01-04 .. 2026-10-30 | 500 usable (FDXF, HONA, VYLR have under 122 bars) |
| market series | yfinance: SPY, ^VIX, ^VIX3M, HYG, IEF, RSP | same | 6 |

* **Evaluation window:** signal dates 2011-01-03 .. 2026-10-29, which is 3,981 trading days and
  16 calendar years (2026 runs to October). 2010 is warm-up for the EMAs and SPY's 200-day
  average.
* **Survivorship bias (it flatters every result here).**
  * The universe is today's members. 230 of the 503 joined the index after 2011-01-03 (by
    Wikipedia's "date added"), and 74 have no price history at the start. Companies that were
    members and later left (failures, acquisitions, demotions) are absent.
  * Size of the effect: an equal-weight, daily-rebalanced buy-and-hold of today's members
    returns **17.8% a year** over the window, against **11.9%** for RSP, the equal-weight S&P 500
    as it actually was. That is a gap of ~5.9 points a year.
  * V1 holds a trade 52 bars on average with a 7.5% average stop, so the gap is worth roughly
    **0.13-0.14R a trade**: about half of V1's after-cost R. In the portfolio it is worth about
    3.5-4 points of CAGR. See "Survivorship sensitivity".
* **Cleaning rules** (fixed before any signal was computed; `research/pullback/data.py`):
  * Stale histories: six symbols carry long zero-volume pre-listing histories (AMCR, FERG, SW,
    HWM, CHTR, VRT; OTC or foreign-line quotes). Each starts after its last zero-volume bar.
    Isolated zero-volume bars elsewhere (19 symbols, 1-2 bars each) are dropped.
  * Corporate-action breaks: yfinance adjusts large spin-offs unreliably. DHR shows a fake +61%
    day on 2016-07-05 (Fortive), and EXPE, GNRC and TMUS show distorted days too. Any day where
    yfinance's dividend-adjustment factor moves by more than 15% is a break (16 breaks in 15
    symbols). A trade open across a break is closed at the last close before it (11 of V1's
    trades), and no signal is taken in the 120 bars from it.
  * OHLC clipping: one bar (HUBB 2021-05-05) has its low above its open; highs and lows are
    clipped to contain open and close.
* **Not the bot's feed.** The bot reads Alpaca (IEX) bars; these are Yahoo's consolidated,
  adjusted bars.

## Method (pre-registered in the brief; nothing was tuned after results)

* **Signals.** These are the exact `EMATrendStackStrategy(timeframe="1d")` rules with weak signals
  off (the default). Indicators come from `app.indicators._enrich` and are computed the same
  way.
  * A BUY needs a 9 > 20 > 50 EMA stack with a rising 20 EMA (5-bar difference) and a rising
    50 EMA (8-bar difference), plus one of:
    * a bounce: the previous close at or below the 20 EMA, and this close above it; or
    * a crossover: the 9 EMA crosses above the 20 EMA while the close is above the 50 EMA. The
      stack is not required for this one.
  * Stop = min(10-bar prior swing low, 50 EMA, close - ATR14), with risk = max(close - stop,
    0.85 ATR). Since the stop is at least one ATR below, the floor never binds. Target =
    close + 2.2 x risk.
  * No BUY before a symbol's 120th bar. A plan with a non-positive price is dropped, as
    `_build_signal` does.
  * Long only: the bot does not short.
* **Execution.**
  * A signal on bar N's close fills at bar N+1's open, gaps included. Stop and target are the
    signal's price levels, and R is measured from the actual fill: R = (exit - entry) /
    (entry - stop).
  * Stop and target are checked intrabar from the fill bar on. When one bar touches both, the
    stop wins. A gap through the stop fills at the open; a gap over the target fills at the
    target (the engine's convention, conservative).
  * A fill at or below the stop or at or above the target has no valid bracket and is skipped:
    202 of 67,152 BUY signals (0.3%).
  * One open position per symbol. A trade still open on 2026-10-30 is closed at that close (292
    V1 trades).
* **Costs.** $1 a side on a $1,000 position plus a 10 bps round-trip spread: 30 bps of the
  entry notional per round trip, subtracted as `0.003 x entry / (entry - stop)` R. That is
  0.058R a V1 trade on average. Zero-cost R is shown alongside.
* **Variants** (V7-V9 were added by the operator before any result was seen):
  * V1 baseline: the bot's rules exactly.
  * V2 market trend: enter only when SPY closed above its 200-day average on the signal day.
  * V3 market fear: enter only when ^VIX closed below 25 on the signal day.
  * V4 protect gains: once a trade's high reaches entry + 1R, the stop moves to the entry
    price, effective from the next bar.
  * V5 trend exit: V1's entries and initial stop, no target. Exit at the next open after a
    close below the 20 EMA.
  * V6 sector spread: the V1 portfolio with at most 2 open positions per GICS sector.
  * V7 fear term structure: enter only when ^VIX closed below ^VIX3M.
  * V8 breadth: enter only when at least 50% of the study universe (today's members, 423-500
    names counted each day) closed above their own 50-day average.
  * V9 credit: enter only when the HYG/IEF close ratio (adjusted closes) is above its 50-day
    average.
* **Trade-level population.** V1-V5 and V7-V9 are judged on every signal, one position per
  symbol, across all 500 symbols. V6 exists only as a portfolio, so it is judged on the 968
  trades its portfolio took. V1's own portfolio trades earn the same R (+0.315 against V6's
  +0.315).
* **Portfolio.**
  * $100,000 start. Each entry risks 0.5% of the equity marked at the signal day's close.
    Notional is capped at 25% of that equity and at the cash on hand (no leverage).
  * At most 12 open positions. When a day has more signals than free slots, the highest
    signal-day relative volume (volume / 20-day average, the strategy's own field) goes first,
    with ties broken alphabetically.
  * Each day, exits that fill at the open come first, then entries, then intrabar exits and the
    close mark. Costs are 15 bps of the notional at entry and 15 bps at exit, and idle cash
    earns 0%.
  * Sharpe uses daily returns, annualised by sqrt(252), with a 0% risk-free rate for both the
    portfolio and SPY.
* **Statistics.**
  * Day-clustered t on after-cost R (CR1 cluster-robust standard error, clusters = entry date),
    with a one-sided p.
  * Holm adjustment over all nine variants.
  * Splits by calendar year (entry date) and by market condition on the signal day. Rising =
    SPY above its 200-day average and that average above its level 20 days earlier (3,251
    days). Falling = SPY below its 200-day average (603 days). Sideways = the rest (127 days).
  * Trades are held for weeks, so trades on nearby days overlap and day clustering overstates
    t. A month-clustered t is shown for context.
* **Pass rule** (fixed in the brief):
  * (a) after-cost expectancy > 0 with Holm one-sided p < 0.10;
  * (b) positive in >= 60% of calendar years, and falling-market R >= -0.10;
  * (c) portfolio Sharpe above SPY buy-and-hold's, or max DD <= half of SPY's with CAGR >= 70%
    of SPY's.

## Parity check

The real `EMATrendStackStrategy(timeframe="1d").generate_signal` was called on every bar of six
symbols' full histories (23,892 bars). Each call got the window the live bot hands it: the last
400 bars, the live 1d fetch (`bars_for_timeframe`). Its action, stop and target were compared
with the vectorised signals computed once over the whole history.

**0 mismatches.** All 769 BUYs and 600 SELLs agree, and stops and targets match within 6e-8
relative. The residual is the EMA seed of a 400-bar window, (49/51)^400 ~ 1e-7.

The trade paths were then checked against the real `BacktestEngine`. The engine used the
zero-cost model and the same bars, with this package's `engine` exit rule: V1 plus the engine's
exit at the next open on the strategy's own SELL. **450 of the engine's 451 trades are
identical** in entry date, entry price, exit date and exit price. The one exception is TSLA on
2018-09-28: the engine entered at an open below the stop and was stopped out at that open, a
trade this study skips as an invalid bracket.

<!-- parity -->
| symbol | bars checked | first bar | BUY (real = vectorised) | SELL (real = vectorised) | mismatches | max rel. diff stop / target | engine trades | identical trades |
|---|--:|---|--:|--:|--:|---|--:|--:|
| WMT | 4233 | 2010-01-04 | 153 = 153 | 103 = 103 | 0 | 1e-08 / 2e-08 | 81 | 81 |
| PYPL | 2849 | 2015-07-06 | 82 = 82 | 88 = 88 | 0 | 2e-08 / 3e-08 | 51 | 51 |
| DIS | 4233 | 2010-01-04 | 126 = 126 | 107 = 107 | 0 | 3e-08 / 5e-08 | 76 | 76 |
| TSLA | 4111 | 2010-06-29 | 119 = 119 | 122 = 122 | 0 | 1e-08 / 2e-08 | 82 | 81 |
| AAPL | 4233 | 2010-01-04 | 132 = 132 | 83 = 83 | 0 | 1e-08 / 2e-08 | 82 | 82 |
| JPM | 4233 | 2010-01-04 | 157 = 157 | 97 = 97 | 0 | 1e-08 / 3e-08 | 79 | 79 |

## Results: one row per variant

SPY buy-and-hold over the same dates (2011-01-03 .. 2026-10-30): CAGR 13.9%, max DD -33.7%,
Sharpe 0.85. "years +" counts the calendar years with positive after-cost R. R columns are per
trade.

| variant | trades | win % | zero-cost R | after-cost R | day-t | Holm p | years + | falling R (n) | rising R (n) | sideways R (n) | CAGR | max DD | Sharpe | (a) | (b) | (c) | verdict |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|---|---|
| V1 baseline: the bot's rules | 22038 | 43.1 | +0.344 | +0.285 | +14.8 | <1e-10 | 13/16 | +0.545 (3368) | +0.240 (18031) | +0.190 (639) | 8.1% | -19.8% | 0.84 | pass | pass | **fail** | fail |
| V2 SPY above its 200-day SMA | 21883 | 42.1 | +0.310 | +0.248 | +12.8 | <1e-10 | 12/16 | none | +0.250 (20348) | +0.220 (1535) | 5.3% | -22.8% | 0.59 | pass | pass (vacuous) | **fail** | fail |
| V3 ^VIX below 25 | 21955 | 42.3 | +0.316 | +0.255 | +13.9 | <1e-10 | 13/16 | +0.389 (2157) | +0.239 (19025) | +0.282 (773) | 7.9% | -17.6% | 0.84 | pass | pass | **fail** | fail |
| V4 stop to entry at +1R | 26498 | 32.8 | +0.268 | +0.208 | +12.5 | <1e-10 | 13/16 | +0.368 (3895) | +0.184 (21803) | +0.098 (800) | 7.1% | -18.3% | 0.74 | pass | pass | **fail** | fail |
| V5 no target, exit below the 20 EMA | 66950 | 31.7 | +0.100 | +0.023 | +2.2 | 0.013 | 10/16 | +0.079 (7555) | +0.019 (57031) | -0.059 (2364) | 7.1% | -23.1% | 0.59 | pass | pass | **fail** | fail |
| V6 at most 2 open per sector | 968 | 43.5 | +0.386 | +0.315 | +5.1 | 3.9e-07 | 15/16 | +0.336 (206) | +0.312 (747) | +0.166 (15) | 9.1% | -18.6% | 0.89 | pass | pass | pass | **PASS** |
| V7 ^VIX below ^VIX3M | 22391 | 42.8 | +0.331 | +0.272 | +14.1 | <1e-10 | 13/16 | +0.472 (3097) | +0.240 (18583) | +0.218 (711) | 7.6% | -20.1% | 0.80 | pass | pass | **fail** | fail |
| V8 breadth >= 50% above 50-day | 20918 | 41.8 | +0.304 | +0.240 | +11.7 | <1e-10 | 13/16 | +0.429 (1874) | +0.225 (18214) | +0.143 (830) | 7.2% | -20.9% | 0.80 | pass | pass | **fail** | fail |
| V9 HYG/IEF above its 50-day SMA | 19448 | 42.0 | +0.309 | +0.246 | +11.3 | <1e-10 | 13/16 | +0.388 (2016) | +0.227 (16606) | +0.281 (826) | 7.5% | -16.7% | 0.81 | pass | pass | **fail** | fail |

Notes on the table:

* **Holm p values.** The exact values are 1.7e-47 (V1), 5.4e-36 (V2), 4.9e-42 (V3), 6.5e-35
  (V4), 0.0125 (V5), 3.9e-07 (V6), 2.5e-43 (V7), 2.2e-30 (V8) and 7.3e-29 (V9). They are this
  small because the null is "R = 0". That null is the wrong one for a long-only rule on stocks
  that rose; see the random-entry control.
* **Month-clustered t** (trades overlap for weeks): V1 +6.4, V2 +6.1, V3 +6.1, V4 +5.5, V5 +1.2,
  V6 +4.9, V7 +6.2, V8 +5.6, V9 +5.4.
* **V2's falling-market bucket is empty by construction**, so criterion (b)'s falling-market
  test is met vacuously.
* **Why (c) fails.** Every variant except V6 fails because none beats SPY's Sharpe and none
  halves SPY's drawdown while keeping 70% of its CAGR. To halve the drawdown a variant would
  need a max DD of at most 16.9% and a CAGR of at least 9.7%.
* **Holds and exits.** The median V1 hold is 27 bars (mean 52). Exits: 12,473 stops, 9,262
  targets, 292 open at the end, 11 at data breaks. 8.4% of trades gapped through their stop.

### Portfolio detail

The "same max DD" mix holds a constant, daily-rebalanced share of SPY (the rest in cash at 0%),
chosen so that its worst drawdown equals the variant's.

| variant | trades taken | their after-cost R | CAGR | max DD | Sharpe | ann. vol | exposure | SPY mix with the same max DD: weight / CAGR / Sharpe | signals skipped (no slot / no cash) |
|---|--:|--:|--:|--:|--:|--:|--:|---|--:|
| V1 | 842 | +0.315 | 8.1% | -19.8% | 0.84 | 9.9% | 66% | 0.55 / 7.9% / 0.85 | 64245 / 30 |
| V2 | 814 | +0.239 | 5.3% | -22.8% | 0.59 | 9.6% | 61% | 0.64 / 9.1% / 0.85 | 56996 / 34 |
| V3 | 812 | +0.350 | 7.9% | -17.6% | 0.84 | 9.5% | 62% | 0.49 / 7.0% / 0.85 | 57662 / 32 |
| V4 | 1122 | +0.198 | 7.1% | -18.3% | 0.74 | 10.0% | 67% | 0.51 / 7.2% / 0.85 | 64041 / 32 |
| V5 | 3474 | +0.085 | 7.1% | -23.1% | 0.59 | 12.9% | 88% | 0.65 / 9.2% / 0.85 | 62063 / 1413 |
| V6 | 968 | +0.315 | 9.1% | -18.6% | 0.89 | 10.3% | 70% | 0.52 / 7.4% / 0.85 | 64116 / 39 |
| V7 | 840 | +0.346 | 7.6% | -20.1% | 0.80 | 9.7% | 63% | 0.56 / 8.0% / 0.85 | 61796 / 41 |
| V8 | 736 | +0.342 | 7.2% | -20.9% | 0.80 | 9.3% | 60% | 0.59 / 8.3% / 0.85 | 51763 / 30 |
| V9 | 753 | +0.331 | 7.5% | -16.7% | 0.81 | 9.5% | 61% | 0.46 / 6.6% / 0.85 | 43470 / 46 |
| SPY buy-and-hold | | | 13.9% | -33.7% | 0.85 | 17.0% | 100% | | |
| SPY 50% / cash 50% (daily rebalanced) | | | 7.1% | -18.0% | 0.85 | 8.5% | 50% | | |

Read plainly, V1's portfolio behaves like 55% SPY and 45% cash: same drawdown, about the same
return (8.1% against 7.9%) and the same Sharpe. That is before any survivorship correction.
The 12 slots are nearly always full (exposure 60-70%, trades held for weeks), so the portfolio
takes about 1 signal in 25, and which ones it takes depends on the relative-volume tie-break.

### By calendar year (after-cost R a trade; 2018, 2020 and 2022 in bold)

| variant | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | **2018** | 2019 | **2020** | 2021 | **2022** | 2023 | 2024 | 2025 | 2026 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| V1 | +0.17 | +0.52 | +0.70 | +0.34 | -0.20 | +0.48 | +0.62 | -0.07 | +0.46 | +0.66 | +0.21 | -0.16 | +0.33 | +0.23 | +0.23 | +0.10 |
| V2 | -0.03 | +0.54 | +0.69 | +0.34 | -0.31 | +0.27 | +0.62 | -0.05 | +0.28 | +0.50 | +0.24 | -0.61 | +0.23 | +0.24 | +0.15 | +0.05 |
| V3 | +0.00 | +0.54 | +0.69 | +0.34 | -0.21 | +0.49 | +0.61 | -0.07 | +0.46 | +0.24 | +0.26 | -0.33 | +0.28 | +0.23 | +0.18 | +0.11 |
| V4 | +0.05 | +0.40 | +0.57 | +0.26 | -0.21 | +0.33 | +0.49 | -0.08 | +0.43 | +0.46 | +0.18 | -0.18 | +0.24 | +0.19 | +0.16 | +0.06 |
| V5 | -0.08 | +0.04 | +0.21 | +0.00 | -0.17 | -0.03 | +0.22 | -0.09 | +0.09 | +0.03 | +0.09 | -0.14 | +0.03 | +0.09 | -0.03 | +0.03 |
| V6 | +0.38 | +0.35 | +0.83 | +0.70 | +0.03 | +0.39 | +0.73 | +0.14 | +0.47 | +0.09 | +0.25 | +0.12 | +0.21 | +0.49 | -0.15 | +0.21 |
| V7 | +0.16 | +0.56 | +0.68 | +0.32 | -0.21 | +0.47 | +0.62 | -0.09 | +0.44 | +0.54 | +0.24 | -0.19 | +0.30 | +0.21 | +0.19 | +0.11 |
| V8 | +0.19 | +0.51 | +0.67 | +0.25 | -0.35 | +0.37 | +0.60 | -0.10 | +0.28 | +0.52 | +0.20 | -0.27 | +0.25 | +0.25 | +0.17 | +0.02 |
| V9 | +0.20 | +0.55 | +0.71 | +0.10 | -0.19 | +0.31 | +0.55 | -0.07 | +0.19 | +0.70 | +0.21 | -0.21 | +0.31 | +0.08 | +0.18 | +0.17 |
| SPY total return | +1% | +16% | +32% | +13% | +1% | +12% | +22% | -5% | +31% | +18% | +29% | -18% | +26% | +25% | +18% | +12% |

The rule's R tracks the market year by year: it is negative in 2015, 2018 and 2022, the flat or
falling SPY years. V6's year row is noisier (60-85 trades a year) and is not comparable
row-for-row with the others, which hold all signals.

| variant | 2018: trades / after-cost R / portfolio return | 2020: trades / after-cost R / portfolio return | 2022: trades / after-cost R / portfolio return |
|---|---|---|---|
| V1 | 1371 / -0.067 / +2.5% | 1377 / +0.664 / -0.7% | 1373 / -0.158 / -7.1% |
| V2 | 1266 / -0.047 / +10.6% | 1521 / +0.501 / -4.4% | 332 / -0.608 / -17.3% |
| V3 | 1350 / -0.070 / +5.0% | 906 / +0.239 / -1.6% | 1038 / -0.325 / -11.8% |
| V4 | 1589 / -0.076 / +6.6% | 1622 / +0.462 / -10.6% | 1644 / -0.182 / -2.3% |
| V5 | 3830 / -0.090 / -4.4% | 4419 / +0.034 / -1.5% | 3373 / -0.139 / -11.7% |
| V6 | 85 / +0.142 / +7.4% | 82 / +0.093 / +2.1% | 79 / +0.119 / -4.6% |
| V7 | 1305 / -0.086 / -0.4% | 1498 / +0.542 / +8.4% | 1441 / -0.186 / -8.3% |
| V8 | 1141 / -0.099 / +1.1% | 1498 / +0.519 / -2.0% | 995 / -0.269 / -13.4% |
| V9 | 1169 / -0.068 / +8.3% | 1335 / +0.696 / +5.1% | 1374 / -0.208 / -8.1% |
| SPY | -4.6% | +18.3% | -18.2% |

A year's "after-cost R" belongs to the trades entered that year. Its "portfolio return" is the
portfolio's calendar-year change, which includes trades entered the year before and only the
1-in-25 signals the portfolio took, so the two can disagree.

## The five market filters: what they remove

Each row splits V1's own 22,038 trades by the filter's value on the signal day. "Removed" is what
the filter would have blocked. The variant's own trade count differs from "kept" because a
blocked signal frees the symbol for a later one.

| filter | days it is off | V1 trades removed | removed: after-cost R (win %) | removed: day-t / month-t | V1 trades kept | kept: after-cost R (win %) | kept: day-t / month-t | R removed in total | variant's own trades |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| V2 SPY above its 200-day SMA | 15% | 3368 | +0.545 (49%) | +8.4 / +3.5 | 18670 | +0.238 (42%) | +12.5 / +5.8 | +1834 R | 21883 |
| V3 ^VIX below 25 | 12% | 2560 | +0.641 (53%) | +8.7 / +4.3 | 19478 | +0.238 (42%) | +12.6 / +5.5 | +1641 R | 21955 |
| V7 ^VIX below ^VIX3M | 8% | 1207 | +0.627 (52%) | +6.7 / +3.5 | 20831 | +0.265 (43%) | +13.6 / +6.0 | +757 R | 22391 |
| V8 breadth >= 50% above 50-day | 26% | 4985 | +0.398 (47%) | +9.0 / +4.1 | 17053 | +0.252 (42%) | +11.9 / +5.7 | +1985 R | 20918 |
| V9 HYG/IEF above its 50-day SMA | 35% | 7426 | +0.322 (44%) | +9.7 / +4.2 | 14612 | +0.266 (43%) | +11.3 / +5.3 | +2393 R | 19448 |

**Every filter blocks trades that made more money than the ones it lets through.** The fear
filters show it most: VIX at 25 or above, or VIX above VIX3M, removed trades at +0.63-0.64R
against +0.24-0.27R kept.

The reason is mechanical. These rules only buy a stock that is itself in an uptrend. One that is
still trending up while the market is stressed tends to be a leader. In 2011-2026 every stress
episode (2011, 2015-16, 2018, 2020, 2022) was followed by a recovery within about a year, and
the stops are wide, so the leaders bought in stress rode the recovery.

Two cautions:

* The removed trades come from fewer, clustered episodes: 46-126 months, against 169-188 for
  the kept ones. A long bear market with no quick recovery is not in this sample.
* In the portfolio, no filter raised Sharpe by more than 0.01 (V3 0.844 against V1's 0.836).
  V3 and V9 cut the max drawdown by 2-3 points, but at a lower CAGR. V2 is the worst (5.3%
  CAGR, Sharpe 0.59).

On this evidence, a market filter on this strategy blocks money-making trades, which is
exactly what the operator does not want.

## Is it the timing or the stocks? Random-entry control (diagnostic, added after the results)

This control is not part of the pre-registered method; it was added once the main results were
seen. The pass rule tests "R > 0", but any long position with a 1R stop and a 2.2R target on a
rising stock earns positive R.

So each V1 trade was matched with five entries:

* on random bars of the same symbol in the same calendar year;
* each filling at the next open;
* with the same stop distance (% of entry) and the same target multiple;
* exited by the same rules with the same costs.

That is 110,190 control trades in all.

| | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | all |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| V1 after-cost R | +0.17 | +0.52 | +0.70 | +0.34 | -0.20 | +0.48 | +0.62 | -0.07 | +0.46 | +0.66 | +0.21 | -0.16 | +0.33 | +0.23 | +0.23 | +0.10 | +0.285 |
| random-entry control | +0.10 | +0.69 | +0.75 | +0.36 | -0.15 | +0.64 | +0.70 | -0.01 | +0.35 | +0.53 | +0.24 | -0.05 | +0.40 | +0.30 | +0.23 | +0.13 | +0.317 |

Random entries earn **+0.317R** after costs (win rate 44.2%, mean hold 49 bars), against V1's
+0.285R (43.1%, 52 bars). Each V1 trade minus its own symbol-year control averages **-0.031R**
(day-clustered t -1.78, month-clustered t -0.77). The control beats V1 in 13 of 16 years.

**The signal does not pick better entry days than chance in the same stock.** The positive R is
the drift of these stocks over these years, and much of that drift is survivorship.

An unplanned observation, not tested: V1's bounce entries, the "buy the dip" part, earn +0.16R
(6,957 trades). Its crossover entries earn +0.34R (15,081).

## Survivorship sensitivity (rough; not part of the rule)

The 5.9-point annual gap between today's members and RSP is taken out of every held position as
a constant daily drag. The trade-level R and the portfolio are recomputed with it.

| variant | after-cost R | adjusted R (day-t) | CAGR | adjusted CAGR | Sharpe | adjusted Sharpe | adjusted max DD | (c) on adjusted figures |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| V1 | +0.285 | +0.150 (+8.2) | 8.1% | 4.4% | 0.84 | 0.49 | -22.1% | fail |
| V2 | +0.248 | +0.116 (+6.2) | 5.3% | 2.0% | 0.59 | 0.26 | -25.6% | fail |
| V3 | +0.255 | +0.122 (+6.9) | 7.9% | 4.4% | 0.84 | 0.50 | -20.5% | fail |
| V4 | +0.208 | +0.103 (+6.4) | 7.1% | 3.5% | 0.74 | 0.39 | -22.0% | fail |
| V5 | +0.023 | -0.030 (-3.0) | 7.1% | 2.3% | 0.59 | 0.24 | -39.1% | fail |
| V6 | +0.315 | +0.191 (+3.2) | 9.1% | 5.2% | 0.89 | 0.54 | -18.8% | fail |
| V7 | +0.272 | +0.138 (+7.5) | 7.6% | 4.1% | 0.80 | 0.46 | -23.9% | fail |
| V8 | +0.240 | +0.107 (+5.4) | 7.2% | 3.9% | 0.80 | 0.47 | -26.9% | fail |
| V9 | +0.246 | +0.116 (+5.6) | 7.5% | 4.2% | 0.81 | 0.48 | -19.2% | fail |

The remaining positive R is ordinary market drift: RSP itself rose 11.9% a year. It is not
evidence of timing; the random-entry control already earns it. The gap is a single average
applied to every stock. The true bias of a trend-following selection could be larger or
smaller, so treat this table as an order of magnitude.

## Comparison with production's -0.03R (context only)

Here the live 24-name universe is used: AAPL ADBE AMD AMZN AVGO BAC COST CRM CSCO DIS GOOGL INTC
IWM JPM META MSFT NFLX NVDA PYPL QQQ SPY TSLA WMT XOM. `MARKET_UNIVERSE_SYMBOLS` is a Railway
variable that is not in the repository. This is the list the intraday study recorded as live24,
and the ops log speaks of "the 24 universe symbols". The window is the last 500 daily bars
(about 2024-11 .. 2026-10), production's 1d history length.

| run on the live 24 names | trades | win % | after-cost R | day-t | median hold | note |
|---|--:|--:|--:|--:|--:|---|
| this study, V1 (stop/target only, 30 bps) | 179 | 35.8 | +0.009 | +0.08 | 20 bars | zero-cost +0.057R; 17 still open at the end |
| this study, V1 + exit on the strategy's own SELL (engine rule), 30 bps | 255 | 31.0 | -0.000 | -0.00 | 13 bars | |
| production walk-forward replicated (`splitter_for("1d")`, eToro fee-inclusive model, 1R = $100) | 285 | 45.6 | -0.055 | -1.11 | 6 days | 72% of trades closed by the fold end |
| the same engine and costs, one run without folds | 186 | 29.0 | -0.060 | -0.65 | 17 days | overnight financing 0.078R a trade |

What the rows show:

* **The replication lands close to production.** It uses 37 folds a symbol, the first OOS entry
  2025-05-05, and Yahoo's data rather than Alpaca's, and gives -0.055R against production's
  -0.03R.
* **On these 24 names over these two years, the strategy earns about zero however it is
  measured.** The October paper winners sit inside that: one good month of a strategy whose
  two-year expectancy on the live names is ~0, in a rising market.

Three things in the production evidence path, noticed but not changed, are worth a look:

1. **Fold cut-off.** Production's daily walk-forward runs each 14-day test fold on its own, and
   the engine closes any trade still open at a fold's end (`end_of_data`). 72% of the
   replicated OOS trades ended that way, with a median hold of 6 days. Live, the same
   positions keep their GTC brackets for weeks (median 27 bars here). Production's daily
   expectancy therefore describes a truncated version of the strategy, not the one traded.
2. **Opposite-signal exit.** The engine closes a long on the strategy's own SELL (a bearish
   crossover or bounce) at the next open. The live bracket does not, as far as this study
   found.
3. **Financing.** The evidence cost model charges 0.015% a day of overnight financing,
   triple-charged over weekends. Over the full holds that is 0.078R a trade, which a real 1x
   stock (eToro, or Alpaca paper) does not pay. The cost model's docstring says this is
   deliberate conservatism.

## Pass/fail verdict

| variant | (a) R > 0, Holm p < 0.10 | (b) >= 60% years positive, falling >= -0.10R | (c) Sharpe > SPY or DD <= 1/2 SPY with CAGR >= 70% | verdict |
|---|---|---|---|---|
| V1 | pass (+0.285R, p 1.7e-47) | pass (13/16; falling +0.545) | fail (0.84 vs 0.85; DD 19.8% vs 16.9% needed) | fail |
| V2 | pass (+0.248R) | pass (12/16; falling empty) | fail (0.59) | fail |
| V3 | pass (+0.255R) | pass (13/16; falling +0.389) | fail (0.84) | fail |
| V4 | pass (+0.208R) | pass (13/16; falling +0.368) | fail (0.74) | fail |
| V5 | pass (+0.023R, p 0.013) | pass (10/16; falling +0.079) | fail (0.59) | fail |
| V6 | pass (+0.315R, p 3.9e-07) | pass (15/16; falling +0.336) | pass (0.89 vs 0.85) | **PASS** |
| V7 | pass (+0.272R) | pass (13/16; falling +0.472) | fail (0.80) | fail |
| V8 | pass (+0.240R) | pass (13/16; falling +0.429) | fail (0.80) | fail |
| V9 | pass (+0.246R) | pass (13/16; falling +0.388) | fail (0.81) | fail |

**V6 passes as the rule was written, and the rule is not softened or hardened here.** Still, the
pass is weak evidence for three reasons:

* **The (c) margin is noise.** V6 beats SPY's Sharpe by 0.04, while the standard error of a
  16-year Sharpe is about 0.3. V6 and V1 differ only in which 1-in-25 signals the portfolio
  happened to take; their trades earn the same +0.315R.
* **(c) does not survive the rough survivorship correction:** V6's Sharpe falls to 0.54.
* **(a) and (b) say nothing about timing.** Random entries in the same stocks pass them too.

## Conclusion (plain language)

The bot's daily "buy the dip in a rising stock" rules made money on paper across 500 large US
stocks from 2011 to 2026, about +0.29R a trade after costs. But buying the same stocks on random
days in the same year, with the same stop and target, made slightly more (+0.32R), so the dip
timing itself adds nothing. The test also used today's S&P 500 list, which leaves out the
companies that fell out of the index. Correcting roughly for that halves the result, and the
portfolio then trails simply holding SPY. Only V6 (at most two open positions per sector) passed
the pre-registered rule, by a Sharpe margin (0.89 against 0.85) that is well inside the noise and
disappears after the survivorship correction; that is not evidence of an edge. All five market
filters (SPY trend, VIX level, VIX term structure, breadth, credit) blocked trades that made
more money than the ones they let through, so none should be added. The break-even stop (V4) and
the 20-EMA trend exit (V5) both made results worse. On the live 24 names over the last two
years the rules earned about zero a trade, which matches production's slightly negative
walk-forward: October's winners are what a rising market gives almost any long position.

**What to paper-trade next:** nothing here earns more risk or bigger eToro copies. If the
strategy keeps running on paper, expect it to behave like roughly half the S&P 500's return with
smaller drawdowns (its portfolio looks like 55% SPY / 45% cash). V6's sector cap is the one
change that cost nothing in this test. **What remains uncertain:** the sample is one long bull
market with quick recoveries, and a long bear market is not in it; the survivorship correction
is rough; and the data is Yahoo's, while the bot signals intraday on Alpaca's unfinished daily
bar.

## Things this study does not model

* **Intraday signalling.** Live 1d entries are usually signalled 1.5-4 hours after the open on
  the unfinished bar (ops log), not on the completed close.
* **Live gates.** None of these are modelled: the evidence gate, relative-volume floor,
  correlation-bucket and group caps, daily entry caps, re-entry cooldown after a loss, the
  35 bps entry-drift check, eToro's room and copy rules.
* **Reward:risk re-check at the quote** (>= 1.0). It would have refused 110 V1 trades (0.5%),
  which averaged +0.064R against +0.286R for the rest.
* **The paper simulator's 20-day timed exit.** It applies to `self_simulated` positions; the
  Alpaca paper account keeps GTC brackets.

## Reproduce

```
python -I research/pullback/cli.py data      --data-dir DATA
python -I research/pullback/cli.py parity    --data-dir DATA --out OUT
python -I research/pullback/cli.py run       --data-dir DATA --out OUT
python -I research/pullback/cli.py calibrate --data-dir DATA --out OUT
python -I research/pullback/report.py OUT > OUT/tables.md
```

About 10 minutes on 4 cores. Data were downloaded on 2026-11-01 and are not committed. Yahoo
revises history, so a later download can move the numbers slightly.
