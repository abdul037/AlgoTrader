# Defensive trend rules on index funds (2026-11-01) -- research only

**Question.** A previous study (`docs/research/pullback_study.md`, branch
`claude/wip-pullback-study`) found that the bot's stock strategies add nothing beyond owning
stocks that rose, and its 2011-2026 sample had no long bear market. Would a simple, defensive
trend rule on index funds have protected capital in real bear markets (2008, 2020, 2022) at an
acceptable cost in return? The rule would be a possible low-maintenance core for the account.
Account sizes are $100,000 (paper) and about $10,000 (real eToro AlgoBot).

**Answer: no variant passes the pre-registered rule.**

* **2008: all four protected capital.**
* **2020 and 2022: protection was uneven.** D4 fell as far as SPY in the 2020 crash. D1 lost
  more than SPY over calendar 2022.
* **Every variant gave up more return than the rule allows.**

**The bar.** SPY buy-and-hold, 2007-01-03 .. 2026-10-30, after costs: CAGR **10.78%**, max
drawdown **-55.2%** (all of it in 2008), Sharpe 0.65. To pass, a variant needed a max drawdown no
deeper than **-27.6%** and a CAGR of at least **8.08%**.

* **D1** (SPY while above its 10-month average, else cash): CAGR **7.88%**, max DD **-24.7%**,
  1.7 trades a year. Its worst drawdown came in 2022-23, not 2008. It passes the drawdown test
  and misses the return floor by 0.20 points. **Fail.**
* **D2** (200-day average with a 1% band, checked daily): CAGR **8.01%**, max DD **-25.1%** (in
  2020), 3.2 trades a year. It misses the floor by 0.07 points. At zero cost it would reach
  8.10% and clear it, but the rule is judged after costs. **Fail.**
* **D3** (five funds, 20% each, each in cash below its own average): CAGR **5.50%**, max DD
  **-13.1%**, 45 trades a year. It has the shallowest drawdown but is far below the return floor
  at every average length. **Fail.**
* **D4** (dual momentum: SPY or EFA, else IEF): CAGR **6.94%**, max DD **-33.7%**, because it
  held SPY through the 2020 crash. It fails both tests. **Fail.**

**The three bear markets** (from SPY's peak close to its trough close):

* **2008:** SPY -55.2%. D1 -4.2%, D2 -10.0%, D3 +10.6%, D4 -2.9%.
* **2020 crash:** SPY -33.7%. D1 -11.7%, D2 -24.1%, D3 -3.8%, D4 -33.7%.
* **2022:** SPY -24.5%. D1 -16.8% to the trough, but **-21.0% for the calendar year**: worse
  than SPY's -18.2%, after four badly timed trades. D3 lost 2.7% for the year.

**Two more findings:**

* **No variant's risk-adjusted return differs measurably from SPY's.** Every 95%
  block-bootstrap interval for the Sharpe difference spans zero (D1: -0.03, interval -0.45 ..
  +0.45). A plain 60/40 SPY/IEF mix earned the same CAGR as D1 (7.94%) with a higher Sharpe
  (0.70 against 0.62), but a deeper drawdown (-32.7%).
* **The verdict depends on the setting.** D1 passes both tests with 8-, 9- and 12-month
  averages and fails with 10 and 11. Its CAGR ranges from 7.88% to 9.76% across those lengths:
  a spread of 1.9 points against a 0.20-point miss.

**Pre-registration.** The "Data" and "Method" sections were committed in `43aba39` before any
strategy or benchmark result was computed. They are unchanged below. Three things were added
after the results were seen, and all are labelled ("Changes after pre-registration"). None
touches the verdict.

Nothing here touches live trading. The code is in `research/defensive/`, which `app/` never
imports and which does not import `app`.

## Data

| | source | window | notes |
|---|---|---|---|
| fund daily OHLC | yfinance `Ticker.history(auto_adjust=True)`: split- and dividend-adjusted | 2003-01-02 .. 2026-10-30, or from inception | SPY, QQQ, IWM, EFA, IEF, TLT from 2003-01-02; VNQ from 2004-09-29; GLD from 2004-11-18; DBC from 2006-02-06; BIL from 2007-05-30 |
| unadjusted closes | yfinance `auto_adjust=False` | same | used only for the price-chart signal diagnostic |
| cash before BIL | ^IRX, the 13-week T-bill discount yield | 2003-01-02 .. 2007-05-30 | each day earns the previous close's yield x calendar days / 360; 6 missing days are filled forward |
| cash from BIL | BIL adjusted close | 2007-05-31 .. 2026-10-30 | BIL's return is net of its fee (about 0.14% a year), so cash returns are slightly negative in 2010-2015 |

Checks run before any result (`cli.py check`):

* Every fund has a bar on every SPY trading day from its first bar. No fund has a bar SPY lacks.
* No zero or missing opens. The share of bars with open equal to close is 0.5-3% for the risk
  funds (BIL 44%; it is used close-to-close only).
* The largest daily moves are real market days (2008-10-13, 2020-03-16, 2025-04-09). No split
  or spin-off artefact was found. The auto-adjusted close equals yfinance's own "Adj Close"
  exactly for every fund.

**Test period: 2007-01-03 .. 2026-10-30** (19.8 years, 238 months), the same for every variant
and benchmark. The brief's first choice was 2005-01, but DBC starts on 2006-02-06, and D3 and the
five-asset benchmark need it. A 10-month average of DBC's month-end closes first exists at
2006-11-30. The period still contains the whole 2007-2009 bear market. BIL starts on 2007-05-30,
so January-May 2007 cash comes from ^IRX.

## Method (pre-registered; fixed before any result was computed)

### Signals and execution

* **Signal prices.** Signals use adjusted closes, which include dividends. A month-end is the
  last SPY trading day of a calendar month. An "N-month average" is the simple mean of the last
  N month-end closes, including the current one.
* **Execution.** Every trade fills at the **next trading day's open** (adjusted open). That
  includes month-end signals, D2's daily signal and the benchmarks' rebalances.
  * A position held at the signal day's close earns the overnight move to that open; the new
    position earns from the open to the close.
  * Cash accrues the full day's cash return from the open (its overnight part is taken as zero).
* **Start and end.** Every strategy holds its starting capital in cash at the 2006-12-29
  close. It trades to the positions implied by that day's signals at the 2007-01-03 open.
  Benchmarks buy at the same open. Positions are marked at the 2026-10-30 close, with no final
  sale.
* **Fractional shares**, no minimum order size, no leverage, no tax.

### Costs

* Each order in a risk fund (SPY, EFA, IEF, GLD, DBC) costs **$1 plus 2 bps of the order's
  notional**. A buy and a sell are each one order. A switch from SPY to EFA is two orders.
* Cash is a balance, not a traded fund: moving to or from cash costs nothing beyond the fund
  order.
* Fees are paid out of equity at the trade, so target weights apply to equity after fees.
* **Main run: $10,000 start**, the real account, where the fixed $1 bites hardest. The same
  rules are also run from $100,000 and from $500 (a small copy), and with zero cost.
* **Cash-at-0% sensitivity.** eToro may pay nothing on idle cash. Every main variant is re-run
  with cash earning 0%. Sharpe is still measured against T-bills.

### Variants (exactly these four)

* **D1:** SPY when SPY's month-end close is above its 10-month average, else cash.
* **D2:** SPY or cash, checked at every daily close against SPY's 200-day simple average of
  daily closes, including that day.
  * Enter (from cash) when the close is above 1.01 x the average.
  * Exit (from SPY) when the close is below 0.99 x the average.
  * Otherwise keep the current state.
  * The state machine runs from the first day the average exists (2003-10), starting in cash,
    so its state on 2006-12-29 comes from the data.
* **D3:** five sleeves (SPY, EFA, IEF, GLD, DBC), 20% each.
  * Each sleeve holds its fund when the fund's month-end close is above its 10-month average,
    else that 20% is in cash.
  * Every month-end, every held sleeve is rebalanced to 20% of equity. Each sleeve that changes
    is an order: there is no tolerance band. Trades are counted in two groups: "signal" (a
    sleeve turns on or off) and "rebalance".
  * A sleeve whose average does not exist yet is in cash. This happens once: DBC in January
    2007 under the 12-month robustness setting.
* **D4:** dual momentum. Each month-end, compare SPY's and EFA's 12-month total returns
  (month-end to month-end).
  * Hold the higher one if its return beats cash's 12-month return over the same months.
  * Otherwise hold IEF.
  * SPY wins a tie.

### Benchmarks

* **SPY buy-and-hold.**
* **60/40:** 60% SPY / 40% IEF, rebalanced to target every month-end (two orders a month).
* **Equal-weight five-asset:** SPY, EFA, IEF, GLD and DBC, 20% each, rebalanced every
  month-end. This is D3 always invested, and it is the benchmark used in the tables.
  * The literal never-rebalanced version (bought at 20% each on 2007-01-03, then held) is shown
    in one extra row.

### Metrics

* **CAGR:** from the 2006-12-29 starting capital to the 2026-10-30 close, over calendar time
  (days / 365.25).
* **Max drawdown:** on daily closing equity.
* **Sharpe and Sortino:** from monthly returns in excess of cash's monthly return, annualised.
  * Sharpe = mean / standard deviation (ddof 1) x sqrt(12).
  * Sortino = mean / sqrt(mean(min(excess, 0)^2)) x sqrt(12).
* **Worst calendar year:** over the full years 2007-2025. 2026 to October is shown separately.
* **Trades a year:** orders / test years.
* **Time in market:** the average share of equity held in risk funds at the daily close. IEF
  counts as a risk fund. For D4, the share of months in SPY, EFA and IEF is also shown.
* **Episodes.** Peak and trough dates come from SPY's adjusted closes. In each search window,
  the trough is SPY's lowest close and the peak is SPY's highest close before it.
  * The windows are: 2008 (2007-01 .. 2009-12), the 2020 crash (2020), 2022 (2022) and the
    2025 drop (2025).
  * For each episode and each strategy, three numbers are shown:
    * the return from SPY's peak to SPY's trough;
    * the strategy's own max drawdown from the peak to 12 months after the trough, which counts
      any losses from re-entering too early or too late;
    * the return from the peak to 12 months after the trough.
  * **Current period:** the return and max drawdown from the 2024-12-31 close to the
    2026-10-30 close.

### Statistics

* **Sharpe difference against SPY** buy-and-hold, with a paired circular block bootstrap of
  monthly excess returns.
  * Blocks are 12 months, the same months are drawn for both series, and there are 10,000
    resamples (seed 20261101).
  * Reported: the point difference, the 95% percentile interval and the share of resamples
    with the difference at or below zero.
* **Parameter robustness:** D1 and D3 are re-run with 8-, 9-, 11- and 12-month averages. All are
  reported, and none is picked as best.

### Pass rule (decided before results; judged on the $10,000 after-cost run)

* **(i)** the variant's max drawdown is at most half of SPY buy-and-hold's (in absolute value);
* **(ii)** the variant's CAGR is at least 75% of SPY buy-and-hold's over the full test period;
* **(iii)** D1 and D3 only: (i) and (ii) both also hold for at least 3 of the 4 alternative
  average lengths (8, 9, 11, 12). D2 and D4 are judged on (i) and (ii) alone.

A variant passes only if every criterion that applies to it passes. The $100,000, zero-cost and
cash-at-0% verdicts are shown for information and do not change the verdict.

### Diagnostics (information only, not part of the rule)

* **Buy-and-hold of every downloaded fund** over the test period, before costs.
* **Price-chart signals.** eToro's charts show unadjusted prices. The share of D1/D3 month-end
  signals, and of D2 daily states, that would differ if signals used unadjusted closes.

## Changes after pre-registration

None of these changes touches the verdict:

1. **Wiped-out accounts ($500 run only).** The first full run gave negative equity for D3 and
   the rebalanced five-asset benchmark at $500, because fees exceeded the account. The engine now
   stops an account on the day an order's fees would use up its equity, and its equity is zero
   from then on. Only the $500 rows change, and they are information only.
2. **A diagnostic added after the results:** the constant SPY/cash mix with the same max
   drawdown as each variant ("Same-drawdown SPY/cash mix" below). It answers whether a trend
   rule beats simply holding less stock.
3. **A description added after the results:** D1's 16 exits listed by buy-back price (under
   "Calendar years").

The code was also reorganised to pass the repository's lint rules, with no change in logic.
The main table and the verdicts were regenerated afterwards and are unchanged.

## Engine checks

* **SPY buy-and-hold at zero cost.** Final equity equals SPY's 2026-10-30 adjusted close
  divided by its 2007-01-03 adjusted open (difference 5e-15).
* **D1 and D2 replayed independently.** A separate day-by-day replay gives the same equity
  curve on every day (largest relative difference 9e-15 and 8e-15).
* **Fee arithmetic.** D3's total fees equal $1 x orders + 2 bps x traded notional (difference
  1e-13).
* **Signal dates, as a sanity check.** D1 sells at the 2008-01-02 open (December 2007's close
  was below its average) and buys back at the 2009-07-01 open.

## Results

Test period 2007-01-03 .. 2026-10-30, starting from capital at the 2006-12-29 close. Cash (the
spliced ^IRX/BIL series) returned 1.48% a year. All rows are after costs unless marked.

### Main table ($10,000 start, after costs)

| strategy | CAGR | max DD (peak .. trough) | Sharpe | Sortino | ann. vol | worst full year | trades/yr (signal / rebalance) | time in market | final equity | fees paid |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| D1 | 7.88% | -24.7% (2022-01-03 .. 2023-03-13) | 0.62 | 0.94 | 10.8% | -21.0% (2022) | 1.7 (1.7 / 0.0) | 79% | $45,014 | $187 |
| D2 | 8.01% | -25.1% (2020-02-19 .. 2020-06-11) | 0.60 | 0.91 | 11.4% | -13.2% (2022) | 3.2 (3.2 / 0.0) | 79% | $46,111 | $303 |
| D3 | 5.50% | -13.1% (2011-04-29 .. 2012-06-04) | 0.65 | 1.05 | 6.3% | -4.6% (2015) | 45.0 (9.1 / 35.9) | 68% | $28,911 | $1,017 |
| D4 | 6.94% | -33.7% (2020-02-19 .. 2020-03-23) | 0.47 | 0.71 | 12.8% | -17.6% (2022) | 3.7 (3.7 / 0.0) | 100% | $37,815 | $364 |
| SPY buy-and-hold | 10.78% | -55.2% (2007-10-09 .. 2009-03-09) | 0.65 | 0.98 | 15.3% | -36.8% (2008) | 0.05 (one buy) | 100% | $76,129 | $3 |
| 60/40 SPY/IEF | 7.94% | -32.7% (2007-12-10 .. 2009-03-09) | 0.70 | 1.06 | 9.4% | -18.1% (2008) | 24.0 (0.1 / 23.9) | 100% | $45,509 | $496 |
| EW5 rebalanced | 6.43% | -35.0% (2008-05-21 .. 2009-03-09) | 0.52 | 0.76 | 10.3% | -18.9% (2008) | 60.0 (0.3 / 59.7) | 100% | $34,451 | $1,213 |
| EW5 never rebalanced | 7.03% | -32.6% (2008-05-21 .. 2008-11-20) | 0.57 | 0.83 | 10.4% | -17.0% (2008) | 0.25 (five buys) | 100% | $38,491 | $7 |

Notes on the table:

* **Columns.** "Signal" trades turn a holding on or off; "rebalance" trades resize a holding
  kept. EW5 is the equal-weight five-asset benchmark (SPY, EFA, IEF, GLD, DBC).
* **D4's holdings.** D4 was in SPY 144 months, EFA 55 and IEF 39, with 36 switches.
* **$100,000 start.** Only the fixed $1 fee differs, and CAGRs move by at most 0.4 points:
  D1 7.89%, D2 8.03%, D3 5.78%, D4 6.96%, SPY 10.78%, 60/40 8.08%, EW5 rebalanced 6.82%. Max
  drawdowns and Sharpe ratios move by at most 0.4 points and 0.04. No verdict changes.

### The bear markets

Peak and trough dates are SPY's adjusted closes:

* 2008: 2007-10-09 .. 2009-03-09
* 2020 crash: 2020-02-19 .. 2020-03-23
* 2022: 2022-01-03 .. 2022-10-12
* 2025 drop: 2025-02-19 .. 2025-04-08

**Return from SPY's peak to SPY's trough**

| strategy | 2008 | 2020 crash | 2022 | 2025 drop |
|---|--:|--:|--:|--:|
| D1 | -4.2% | -11.7% | -16.8% | -8.7% |
| D2 | -10.0% | -24.1% | -10.8% | -8.4% |
| D3 | +10.6% | -3.8% | -1.5% | -5.8% |
| D4 | -2.9% | -33.7% | -19.1% | -18.8% |
| SPY buy-and-hold | -55.2% | -33.7% | -24.5% | -18.8% |
| 60/40 SPY/IEF | -32.7% | -18.9% | -20.7% | -10.6% |
| EW5 rebalanced | -26.3% | -18.6% | -11.9% | -7.6% |
| EW5 never rebalanced | -20.2% | -17.2% | -16.0% | -9.0% |

**Worst drawdown from SPY's peak to 12 months after its trough** (this includes losses after
re-entering)

| strategy | 2008 (to 2010-03-09) | 2020 crash (to 2021-03-23) | 2022 (to 2023-10-12) | 2025 drop (to 2026-04-08) |
|---|--:|--:|--:|--:|
| D1 | -9.9% | -12.6% | -24.7% | -10.0% |
| D2 | -15.8% | -25.1% | -19.6% | -8.8% |
| D3 | -9.7% | -5.6% | -7.6% | -5.9% |
| D4 | -17.5% | -33.7% | -21.0% | -18.8% |
| SPY buy-and-hold | -55.2% | -33.7% | -24.5% | -18.8% |
| 60/40 SPY/IEF | -32.7% | -18.9% | -20.9% | -10.6% |
| EW5 rebalanced | -35.0% | -19.3% | -16.4% | -7.8% |
| EW5 never rebalanced | -32.6% | -17.9% | -17.3% | -9.8% |

**Return from SPY's peak to 12 months after its trough** (did it catch the rebound?), and the
**current period** (2024-12-31 .. 2026-10-30)

| strategy | 2008 | 2020 crash | 2022 | 2025 drop | current: return | current: max DD |
|---|--:|--:|--:|--:|--:|--:|
| D1 | +20.0% | +15.0% | -14.4% | +3.4% | +14.1% | -10.0% |
| D2 | +10.7% | -1.4% | -10.0% | +5.0% | +23.5% | -8.8% |
| D3 | +21.8% | +9.9% | -0.6% | +23.2% | +32.8% | -6.5% |
| D4 | -1.3% | -4.2% | -17.5% | +19.2% | +25.5% | -18.8% |
| SPY buy-and-hold | -23.0% | +17.7% | -6.7% | +11.9% | +31.5% | -18.8% |
| 60/40 SPY/IEF | -6.9% | +12.3% | -10.6% | +10.2% | +21.0% | -10.6% |
| EW5 rebalanced | +0.2% | +10.5% | -2.6% | +27.1% | +38.3% | -7.8% |
| EW5 never rebalanced | +1.4% | +10.2% | -4.8% | +28.5% | +36.4% | -9.8% |

What happened in each episode:

* **2008, the slow bear market these rules are built for.** Every variant was mostly out
  before the worst of it.
  * D1 sold at the 2008-01-02 open, 5.9% below SPY's peak. It bought back at the 2009-07-01
    open, 37% above the trough.
  * D2 sold on 2007-11-12, bought back and sold again twice in December 2007, and sold on
    2008-01-02. It bought back on 2009-06-02.
  * D3 left SPY and EFA in January 2008. From October 2008 it held only IEF (20%) and cash
    (80%).
  * D4 moved from EFA to IEF on 2008-02-01 and back to EFA on 2009-10-01.
* **2020, a crash too fast for monthly signals.**
  * D1 sold at the 2020-03-02 open, already 11.9% below the peak. It bought back on
    2020-06-01, 36% above the trough.
  * D2 sold on 2020-02-28 and bought back on 2020-03-03 after a one-day rally. It sold again at
    the 2020-03-09 open, which was 7.4% below the previous close and 18.6% below the peak, and
    re-entered on 2020-05-28. It lost 24.1% (SPY 33.7%) and then missed the rebound.
  * D4 held SPY through the whole fall. It moved to IEF on 2020-04-01, after the bottom, and
    back to SPY on 2020-06-01.
* **2022, when stocks and bonds fell together.**
  * D1 traded four times in 2022: out on 03-01, in on 04-01, out on 05-02, in on 12-01. It
    then went out again on 2023-01-03 and back on 2023-02-01. Each re-entry was followed by a
    fall, and its worst drawdown of the whole test (-24.7%) ends on 2023-03-13.
  * D4 moved to IEF on 2022-06-01, with SPY already 12.8% below its peak. IEF then fell 7.2% to
    SPY's trough.
  * 60/40 lost 20.7%, because IEF fell with SPY.
  * D3 lost least (-2.7% for the year). From June to September 2022 it held only DBC (up 19%
    in 2022) and cash, and in October-November only cash.
* **2025 drop.** D1 sold at the 2025-04-01 open, 8.8% below the peak and a week before the
  trough. It bought back on 2025-06-02, 18.4% above the trough. D4 held SPY throughout.
* **Current period.** D1 is up 14.1% against SPY's 31.5%. It sold in the 2025 dip and again
  for April 2026, and both times bought back higher: 5.4% above its 2025 sale price and 10.3%
  above its 2026 one.

### Calendar years ($10,000)

| strategy | 2007 | 2008 | 2009 | 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (to Oct) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| D1 | +5% | +2% | +22% | -2% | -2% | +10% | +32% | +13% | -7% | +5% | +22% | -7% | +7% | +16% | +29% | -21% | +11% | +25% | +12% | +2% |
| D2 | -5% | +2% | +20% | -0% | -3% | +13% | +32% | +7% | -11% | +11% | +22% | -7% | +20% | -1% | +29% | -13% | +10% | +25% | +13% | +9% |
| D3 | +15% | +5% | +11% | +3% | +1% | -4% | +9% | -1% | -5% | +1% | +9% | -1% | +8% | +8% | +11% | -3% | +4% | +11% | +20% | +11% |
| D4 | +9% | +6% | -1% | +5% | +0% | +8% | +16% | +13% | -8% | +6% | +20% | -7% | +15% | -4% | +29% | -18% | +10% | +25% | +17% | +7% |
| SPY buy-and-hold | +5% | -37% | +26% | +15% | +2% | +16% | +32% | +13% | +1% | +12% | +22% | -5% | +31% | +18% | +29% | -18% | +26% | +25% | +18% | +12% |
| 60/40 SPY/IEF | +7% | -18% | +12% | +13% | +7% | +11% | +15% | +12% | +2% | +8% | +14% | -2% | +22% | +16% | +15% | -17% | +17% | +14% | +14% | +6% |
| EW5 rebalanced | +16% | -19% | +17% | +15% | +3% | +10% | +0% | -4% | -8% | +8% | +13% | -6% | +18% | +11% | +14% | -6% | +11% | +11% | +25% | +11% |
| EW5 never rebalanced | +17% | -17% | +15% | +17% | +5% | +8% | -5% | -1% | -5% | +7% | +14% | -5% | +20% | +15% | +12% | -10% | +15% | +17% | +30% | +5% |

**The trend rules pay for 2008 in the choppy years.** D1 is 38.6 points ahead of SPY in 2008.
It trails by 15 points or more in 2010 (-17.4), 2019 (-24.5) and 2023 (-15.4). It trails by 5-10
points in 2012, 2015, 2016, 2025 and 2026 to October. Each time it sold on a dip and bought back
higher. D1 lost more than SPY in 2015, 2018 and 2022. D2 beat SPY in 2022 (-13.2% against
-18.2%) but trailed it by 19.2 points in 2020.

**D1's 16 exits, by buy-back price** (adjusted opens; a description, not a test):

| sold | bought back | buy-back vs sale | | sold | bought back | buy-back vs sale |
|---|---|--:|---|---|---|--:|
| 2008-01-02 | 2009-07-01 | **-34.6%** | | 2019-01-02 | 2019-03-01 | +14.0% |
| 2010-07-01 | 2010-08-02 | +8.6% | | 2019-06-03 | 2019-07-01 | +8.3% |
| 2010-09-01 | 2010-10-01 | +8.3% | | 2020-03-02 | 2020-06-01 | +2.4% |
| 2011-09-01 | 2012-02-01 | +9.4% | | 2022-03-01 | 2022-04-01 | +4.5% |
| 2015-09-01 | 2015-11-02 | +8.4% | | 2022-05-02 | 2022-12-01 | +0.0% |
| 2016-02-01 | 2016-04-01 | +6.7% | | 2023-01-03 | 2023-02-01 | +5.4% |
| 2018-11-01 | 2018-12-03 | +3.2% | | 2023-11-01 | 2023-12-01 | +8.7% |
| | | | | 2025-04-01 | 2025-06-02 | +5.4% |
| | | | | 2026-04-01 | 2026-05-01 | +10.3% |

* **Only one exit bought back cheaper:** 2008, 34.6% below the sale price.
* One (May-December 2022) bought back at the same price. The other 14 bought back 2.4-14.0%
  higher, less the cash interest earned while out.
* So D1's value rests entirely on catching the rare long decline, and most exits cost money.

### Costs and account size

| strategy | CAGR zero cost | CAGR $100,000 | CAGR $10,000 | CAGR $500 | CAGR $10,000, cash 0% | trades/yr | fees paid, $10,000 start | fees paid, $500 start |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| D1 | 7.92% | 7.89% | 7.88% | 7.70% | 7.59% | 1.7 | $187 | $41 |
| D2 | 8.10% | 8.03% | 8.01% | 7.59% | 7.72% | 3.2 | $303 | $74 |
| D3 | 5.85% | 5.78% | 5.50% | wiped out by fees on 2023-11-01 | 5.12% | 45.0 | $1,017 | $736 |
| D4 | 7.04% | 6.96% | 6.94% | 6.48% | 6.94% | 3.7 | $364 | $87 |
| SPY buy-and-hold | 10.78% | 10.78% | 10.78% | 10.76% | 10.78% | 0.05 | $3 | $1 |
| 60/40 SPY/IEF | 8.10% | 8.08% | 7.94% | 3.79% | 7.94% | 24.0 | $496 | $477 |
| EW5 rebalanced | 6.87% | 6.82% | 6.43% | wiped out by fees on 2017-08-01 | 6.43% | 60.0 | $1,213 | $640 |
| EW5 never rebalanced | 7.03% | 7.03% | 7.03% | 6.98% | 7.03% | 0.25 | $7 | $5 |

* **Few trades, so the fee barely matters.** D1, D2 and D4 trade 1.7-3.7 times a year. Even
  from $500, all costs together take 0.22 points a year from D1's CAGR, 0.52 from D2's and 0.56
  from D4's, against zero cost.
* **D3's monthly rebalancing is what costs.** It makes 35.9 rebalance orders a year against
  9.1 signal orders. From $10,000 that costs 0.35 points of CAGR against zero cost. From $500,
  each $1 order is about 1% of a $100 sleeve, so fees outrun returns and the account is used up
  by 2023-11-01.
  * The rule as pre-registered has no tolerance band, so this is the rule's cost as written.
  * A $500 account cannot run a monthly-rebalanced five-fund rule at $1 an order.
* **Idle cash at 0%** (eToro may pay nothing on it) costs D1 and D2 0.29 points a year and D3
  0.38. D4 never holds cash.

### Sharpe difference against SPY (block bootstrap, $10,000)

This is a paired circular block bootstrap of monthly excess returns, with 12-month blocks and
10,000 resamples (seed 20261101). The interval is the 2.5th-97.5th percentile of the difference.

| strategy | Sharpe | minus SPY's | 95% interval | share of resamples <= 0 |
|---|--:|--:|--:|--:|
| SPY buy-and-hold | 0.65 | | | |
| D1 | 0.62 | -0.03 | -0.45 .. +0.45 | 0.591 |
| D2 | 0.60 | -0.05 | -0.41 .. +0.40 | 0.621 |
| D3 | 0.65 | -0.00 | -0.54 .. +0.51 | 0.534 |
| D4 | 0.47 | -0.18 | -0.58 .. +0.29 | 0.794 |
| 60/40 SPY/IEF | 0.70 | +0.05 | -0.12 .. +0.22 | 0.261 |
| EW5 rebalanced | 0.52 | -0.13 | -0.47 .. +0.19 | 0.798 |
| EW5 never rebalanced | 0.57 | -0.08 | -0.46 .. +0.28 | 0.685 |
| D1 (8-month) | 0.81 | +0.16 | -0.22 .. +0.59 | 0.269 |
| D1 (9-month) | 0.71 | +0.06 | -0.35 .. +0.52 | 0.446 |
| D1 (11-month) | 0.62 | -0.03 | -0.45 .. +0.44 | 0.594 |
| D1 (12-month) | 0.69 | +0.04 | -0.38 .. +0.50 | 0.476 |
| D3 (8-month) | 0.78 | +0.13 | -0.42 .. +0.63 | 0.344 |
| D3 (9-month) | 0.69 | +0.04 | -0.49 .. +0.55 | 0.473 |
| D3 (11-month) | 0.63 | -0.02 | -0.55 .. +0.49 | 0.556 |
| D3 (12-month) | 0.71 | +0.06 | -0.45 .. +0.54 | 0.444 |

**No rule's risk-adjusted return is distinguishable from SPY's.** Every interval spans zero,
and the intervals are about +/-0.45 wide. Over 20 years a Sharpe difference would need to be
near half a unit to show.

The trend rules did not raise Sharpe. They cut the one deep loss (2008), which a monthly Sharpe
ratio weighs no more than many small ones.

### Robustness: D1 and D3 with other average lengths ($10,000)

All ten settings are shown, and none is preferred.

| setting | CAGR | max DD | Sharpe | worst full year | trades/yr | time in market | 2008 max DD | 2022 max DD | (i) DD <= half SPY's | (ii) CAGR >= 75% SPY's |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| D1 8-month | 9.76% | -24.7% | 0.81 | -21.0% (2022) | 1.5 | 78% | -9.9% | -24.7% | pass | pass |
| D1 9-month | 8.87% | -24.7% | 0.71 | -21.0% (2022) | 1.6 | 79% | -9.9% | -24.7% | pass | pass |
| **D1 10-month (main)** | 7.88% | -24.7% | 0.62 | -21.0% (2022) | 1.7 | 79% | -9.9% | -24.7% | pass | **fail** |
| D1 11-month | 7.88% | -24.7% | 0.62 | -21.0% (2022) | 1.6 | 79% | -9.9% | -24.7% | pass | **fail** |
| D1 12-month | 8.80% | -22.2% | 0.69 | -11.8% (2022) | 1.4 | 79% | -9.9% | -14.8% | pass | pass |
| D3 8-month | 6.38% | -11.9% | 0.78 | -4.6% (2015) | 44.7 | 67% | -10.8% | -7.0% | pass | **fail** |
| D3 9-month | 5.92% | -12.3% | 0.69 | -4.6% (2015) | 44.9 | 67% | -12.1% | -7.6% | pass | **fail** |
| **D3 10-month (main)** | 5.50% | -13.1% | 0.65 | -4.6% (2015) | 45.0 | 68% | -9.7% | -7.6% | pass | **fail** |
| D3 11-month | 5.33% | -13.1% | 0.63 | -4.9% (2015) | 45.1 | 68% | -9.6% | -8.1% | pass | **fail** |
| D3 12-month | 5.77% | -9.6% | 0.71 | -4.9% (2015) | 44.8 | 68% | -9.6% | -7.2% | pass | **fail** |

* "2008 max DD" and "2022 max DD" are measured from SPY's peak to 12 months after SPY's trough.
* D1's 10- and 11-month CAGRs agree by coincidence. Their switch dates differ (in 2009 and
  2023), and the two end $23 apart.
* **The drawdown protection is robust.** All ten settings keep the max drawdown between -9.6%
  and -24.7%, against SPY's -55.2%, and all ten hold the 2008 drawdown to 12.1% or less.
* **The return is not robust.** D1's CAGR runs from 7.88% to 9.76%, and whether it clears
  8.08% depends on the length chosen. D3 is below the floor at every length (5.33-6.38%).

### Pass/fail verdict (the rule: $10,000, after costs)

SPY buy-and-hold: CAGR 10.78%, max DD -55.2%. Limits: max DD no deeper than -27.6%, CAGR at
least 8.08%.

| variant | (i) max DD <= half of SPY's | (ii) CAGR >= 75% of SPY's | (iii) holds at >= 3 of 4 other lengths | verdict |
|---|---|---|---|---|
| D1 | pass (-24.7%) | **fail** (7.88% < 8.08%) | pass (8, 9, 12 pass; 11 fails) | **fail** |
| D2 | pass (-25.1%) | **fail** (8.01% < 8.08%) | n/a | **fail** |
| D3 | pass (-13.1%) | **fail** (5.50% < 8.08%) | **fail** (0 of 4) | **fail** |
| D4 | **fail** (-33.7%) | **fail** (6.94% < 8.08%) | n/a | **fail** |

**Nothing passes.** The rule is not softened here. Note that D1's (iii) passes: three of its
four alternative lengths would have passed (i) and (ii). But (iii) is a robustness condition on
top of a main-setting pass, and D1's main setting fails (ii).

The other settings, for information only (they do not change the verdict):

| setting | D1 | D2 | D3 | D4 |
|---|---|---|---|---|
| $100,000 | fail (ii: 7.89%) | fail (ii: 8.03%) | fail (ii) | fail (i, ii) |
| zero cost | fail (ii: 7.92%) | **pass** (8.10% >= 8.08%; DD -25.1%) | fail (ii) | fail (i, ii) |
| $10,000, cash 0% | fail (ii: 7.59%) | fail (ii: 7.72%) | fail (ii) | fail (i, ii) |
| $500 | fail (ii: 7.70%) | fail (ii: 7.59%) | fail (wiped out by fees) | fail (i, ii) |

D2 would pass only with no costs at all, by 0.02 points. That is far inside the noise of a
20-year CAGR.

## Diagnostics (information only)

### Same-drawdown SPY/cash mix (added after the results)

The mix holds a constant share of SPY, with the rest in cash at T-bill returns. It is rebalanced
daily at zero cost, and the share is chosen so its max drawdown over the test period equals
the strategy's.

| strategy | max DD | CAGR | Sharpe | SPY share of the same-DD mix | mix CAGR | mix Sharpe |
|---|--:|--:|--:|--:|--:|--:|
| D1 | -24.7% | 7.88% | 0.62 | 0.40 | 5.6% | 0.69 |
| D2 | -25.1% | 8.01% | 0.60 | 0.41 | 5.7% | 0.69 |
| D3 | -13.1% | 5.50% | 0.65 | 0.22 | 3.8% | 0.70 |
| D4 | -33.7% | 6.94% | 0.47 | 0.56 | 7.1% | 0.68 |
| 60/40 SPY/IEF | -32.7% | 7.94% | 0.70 | 0.54 | 6.9% | 0.68 |
| EW5 rebalanced | -35.0% | 6.43% | 0.52 | 0.58 | 7.3% | 0.68 |

* **At the same worst drawdown, D1, D2 and D3 earned 1.7-2.3 points a year more than simply
  holding less SPY.** That is what dodging 2008 bought.
* **The mix has the better Sharpe** (0.69-0.70 against 0.60-0.65). The trend rules avoided one
  deep fall and paid for it in many small whipsaws.
* D4 did no better than the mix.
* This diagnostic was not pre-registered. It leans on 2008, which sets SPY's max drawdown and
  which the trend rules happened to sidestep.

### Buy-and-hold of every downloaded fund (before costs, 2006-12-29 .. 2026-10-30)

| fund | from | CAGR | max DD (peak .. trough) | worst full year |
|---|---|--:|--:|--:|
| SPY | 2006-12-29 | 10.8% | -55.2% (2007-10-09 .. 2009-03-09) | -36.8% (2008) |
| QQQ | 2006-12-29 | 15.9% | -53.4% (2007-10-31 .. 2008-11-20) | -41.7% (2008) |
| IWM | 2006-12-29 | 8.1% | -58.6% (2007-07-13 .. 2009-03-09) | -34.1% (2008) |
| EFA | 2006-12-29 | 4.8% | -61.0% (2007-10-31 .. 2009-03-09) | -41.0% (2008) |
| IEF | 2006-12-29 | 3.1% | -23.9% (2020-08-04 .. 2023-10-19) | -15.2% (2022) |
| TLT | 2006-12-29 | 2.8% | -48.4% (2020-08-04 .. 2023-10-19) | -31.2% (2022) |
| GLD | 2006-12-29 | 9.1% | -45.6% (2011-08-22 .. 2015-12-17) | -28.3% (2013) |
| DBC | 2006-12-29 | 2.7% | -76.4% (2008-07-02 .. 2020-04-27) | -31.8% (2008) |
| VNQ | 2006-12-29 | 5.3% | -73.1% (2007-02-07 .. 2009-03-06) | -37.0% (2008) |
| BIL | 2007-05-30 | 1.4% | -0.8% (2008-09-29 .. 2015-12-09) | -0.1% (2015) |

QQQ, IWM, TLT and VNQ are not used by any variant.

Three of D3's five funds, EFA (4.8%), IEF (3.1%) and DBC (2.7%), returned far less than SPY over
these years. That limits both D3 and the EW5 benchmarks, whatever the trend rule does.

### Price-chart signals

eToro's charts show unadjusted prices. These signals would have differed if computed on
unadjusted closes:

* **D1:** 2 of 238 month-ends.
* **D2:** 78 of 4,989 days (1.6%).
* **D3:** 57 of 1,190 sleeve-months: IEF 33, EFA 14, DBC 8, SPY 2, GLD 0. The differences
  are mostly in the funds that pay the most interest or dividends; GLD pays none.

D1 run from a price chart would have matched the tested rule in 236 of 238 months.

## Conclusion (plain language)

**None of the four rules passed the test set in advance.** The simplest one, D1 (hold SPY only
while its month-end price is above its 10-month average, otherwise hold cash), would have
protected money in a slow bear market like 2008: it turned SPY's 55% fall into a loss of at most
10%. It did not protect reliably in faster or choppier ones. In the 2020 crash it sold only
after a 12% fall and bought back well above the bottom, and in 2022 it lost 21%, more than SPY's
18%, because it sold and bought back at bad moments.

The price was about a quarter of SPY's return: 7.9% a year against 10.8%, so $10,000 became
about $45,000 instead of $76,000, just under the 75% of SPY's return that was required. Since
the start of 2025 it is up 14% against SPY's 32%. D1 needs fewer than two trades a year, so
eToro's $1 fee hardly matters even for a $500 copy (7.7% a year instead of 7.9%). The five-fund
rule needs about 45 trades a year, and at $1 a trade it would have used up a $500 copy by 2023.

A plain 60% SPY / 40% bond-fund mix earned the same 7.9% a year with a deeper 2008 fall (33%)
but a smoother ride, and no rule earned measurably more per unit of risk than simply holding
SPY. The verdict also hinges on the setting: an 8-, 9- or 12-month average would have passed,
the planned 10-month did not. So treat these rules as a way to swap some return for a smaller
worst case, not as an edge.

**What remains uncertain.** These 20 years hold only one slow bear market, the kind these rules
handle well, and the next may look more like 2022 (stocks and bonds falling together) or 2020
(too fast for a monthly check). eToro may also pay nothing on idle cash (about 0.3 points a year
less for D1), and dividend withholding tax and eToro's exact fees are not modelled.

## Things this study does not model

* **Taxes and dividend withholding.** Adjusted closes reinvest gross dividends. A non-US
  holder of US ETFs on eToro typically receives dividends net of withholding tax, which lowers
  every equity row, SPY's included. The rule compares like with like, but the absolute CAGRs
  are high for such a holder.
* **eToro's real costs.** The $1 per order + 2 bps is the brief's assumption. It was not
  checked against eToro's current schedule for ETFs or for copy trading. Also not modelled:
  spreads wider than 2 bps at stressed opens, currency conversion, and minimum order sizes.
  With $500 and 20% sleeves, many D3 rebalance orders would be a few dollars.
* **Execution.** Fills are at Yahoo's adjusted open, with no slippage beyond the 2 bps. The
  operator would need to place the order after the signal close for the next open.
* **Cash.** Cash is BIL's return, net of its fee, and ^IRX for January-May 2007. On a switch
  day, cash earns the full day from the open.
* **Where the rules come from.**
  * D1 is the 10-month timing rule published by Faber in 2007, and D3 is a version of that
    paper's five-asset model (which used REITs where D3 uses gold). This test period comes
    after that paper's data.
  * D4 is a form of dual momentum, published around 2012-2014, so part of this period was in
    its authors' sample.
  * Whether picking well-known rules is itself a form of hindsight is not tested.
* **Yahoo's data**, downloaded on 2026-11-01, not the bot's Alpaca feed. Yahoo revises history.

## Reproduce

```
python -I research/defensive/cli.py data  --data-dir DATA
python -I research/defensive/cli.py check --data-dir DATA
python -I research/defensive/cli.py run   --data-dir DATA --out OUT
```

The run takes about 10 seconds. `OUT/tables.md` holds every table above, and `OUT/results.json`
holds the figures. Data are not committed.
