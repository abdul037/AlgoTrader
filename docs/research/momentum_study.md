# Cross-sectional momentum (monthly, top 20) study (2026-11-01) -- research only

**Question.** Cross-sectional momentum (buy the stocks that rose most over the past year,
rebalance monthly) is the best-documented stock-selection edge in academic finance. On
2011-2026 data, after realistic costs for the bot's account sizes (paper $100,000; the real
eToro AlgoBot account about $10,000, with an operator copy of $500 following it at 5%), does it
add anything beyond simply owning these stocks, owning SPY, or owning the equal-weight S&P 500
(RSP)?

**Status: pre-registration.** This section was written and committed before any strategy or
control result was computed. The results sections are added in a later commit; nothing in this
method is changed after results are seen.

Nothing here touches live trading. The code is in `research/momentum/`, which `app/` never
imports, and it does not import `app`.

## Data

| | source | window | symbols |
|---|---|---|---|
| members, GICS sector, "date added" | the pullback study's cache: Wikipedia "List of S&P 500 companies", fetched 2026-11-01 (GitHub `datasets/s-and-p-500-companies` lists the same 503) | today | 503 |
| daily OHLCV | the pullback study's cache: yfinance, `auto_adjust=True` (split- and dividend-adjusted open and close) | 2010-01-04 .. 2026-10-30 | 503 |
| SPY, RSP | same cache, same adjustment | same | 2 |
| cash | yfinance ^IRX (13-week T-bill discount yield), downloaded 2026-11-01 into this study's own directory; BIL kept as a fallback | 2009-06 .. 2026-10-30 | 1 |

* **Cleaning** is the pullback study's, fixed there before any result, copied unchanged:
  * stale pre-listing histories (more than 10 zero-volume bars: AMCR, FERG, SW, HWM, CHTR, VRT)
    start after the last zero-volume bar; isolated zero-volume bars are dropped;
  * a day on which yfinance's dividend-adjustment factor jumps by more than 15% is a
    corporate-action break (16 breaks in 15 symbols, e.g. DHR's fake +61% day on 2016-07-05).
* A data check of every daily move beyond +-30% (133 of them) found no other obvious error; the
  big ones match known events (the 2020-03-09 oil crash, PCG, VRTX, CVNA, SMCI, GL). MRNA's
  +177% on 2026-08-19 could not be checked against an outside source and is kept.
* **Survivorship bias (it flatters every long strategy here).** The universe is today's
  members. Companies that were members and later left (failures, acquisitions, demotions) are
  absent, and stocks that joined the index after rising are present for the years before they
  joined. Of the 414 stocks eligible in January 2011, only 271 were index members at the time
  (by Wikipedia's "date added"). The pullback study measured today's members beating RSP by
  about 5.9 points a year. Here the bias is measured three ways: the random control, the
  all-members control, and RSP.
* Momentum has a specific extra exposure to this bias: a stock that rose strongly is exactly
  the stock that later gets added to the index, and a stock that rose and then collapsed out of
  the index is exactly the one a momentum rule would have held on the way down. Neither the
  random control nor RSP removes that part. The "already in the index" sensitivity below
  removes the first half of it; the second half cannot be measured with this data.
* **Not the bot's feed.** The bot reads Alpaca (IEX) and eToro quotes; these are Yahoo's
  consolidated, adjusted bars.

## Method (pre-registered; nothing is tuned after results)

### Calendar

* Holding months: 2011-01 .. 2026-10 (190 months). Month k's portfolio is chosen from month
  k-1's last close (the signal day) and bought at the open of month k's first trading day; it is
  sold at the open of month k+1's first trading day. The final month (2026-10) is marked at the
  last close in the data (2026-10-30), because November's first open is not in the data.
* Trading days are SPY's.

### Universe and eligibility (the same for every variant and every control)

A member is eligible for month k if, at the signal day:

* it has an adjusted close (carried forward at most 5 trading days) at the signal day and at
  the month-ends 1, 6 and 12 months before it;
* it has at least 200 valid daily returns in the 12 months ending at the signal day;
* no corporate-action break falls between the 12-month look-back start and the month's exit
  (a symmetric data-cleaning rule; it removes 194 of 88,548 stock-months);
* it has an open on the buy day.

If a held stock has no open on the exit day, it exits at its last close before that day. For
the first signal (2010-12-31), the month-end 12 months earlier (2009-12-31) predates the data;
the first bar, 2010-01-04, stands in for it. 414 to 499 members are eligible each month.

### Variants (exactly these four)

* **M1**: the 20 eligible stocks with the highest 12-1 month total return: adjusted close at
  the end of month k-2 divided by the adjusted close at the end of month k-13, minus 1 (the 12
  months ending one month before the signal day).
* **M2**: M1, but in any month where SPY's adjusted close on the signal day is below the mean of
  its last 10 month-end adjusted closes (the signal day's included), hold cash for the month at
  the T-bill rate.
* **M3**: the top 20 by 6-1 month return: end of k-2 over end of k-7.
* **M4**: the top 20 by 12-1 month return divided by the standard deviation of daily simple
  returns over the 12 months ending at the signal day (risk-adjusted momentum).

Ties are broken by symbol, ascending. Two share classes of one company (GOOGL/GOOG, FOXA/FOX,
NWSA/NWS) are separate members and can both be held.

### Portfolio and costs

* Equal weight: at every rebalance each of the 20 names is set to 1/20 of equity. Fully
  invested; no leverage.
* Cash (M2's cash months) earns ^IRX's yield on the signal day x calendar days / 360. If ^IRX
  were missing, BIL's return would be used, else 0%.
* Costs are charged as if the account always held its starting size (a fixed fraction of
  equity, so a growing balance does not dilute them):
  * **$1 per side per position** (eToro's fee on a real US stock buy and sell): $1 for every
    position opened and $1 for every position closed. On the $10,000 account a position is
    $500, so $1 is 0.20% of it; on the $100,000 account, 0.02%.
  * **10 bps round-trip spread**: 5 bps of every dollar traded, each side. A name kept from one
    month to the next is resized back to 1/20 and pays the spread on the resize.
  * Primary cost model: a resize of a kept name pays the spread only, not the $1 (the brief's
    "$1 per side per position" read literally: such a name is neither opened nor closed).
  * Conservative sensitivity, reported next to it for the $10,000 account: every resized kept
    name also pays $1 (on eToro a resize is a partial close or an extra position, each with its
    own fee).
  * Accounts reported: zero cost, $100,000, $10,000, and, for information only, the $500 copy
    if each of its positions paid the same $1 a side (whether eToro charges copy positions this
    way was not checked).
* SPY and RSP buy-and-hold pay one $1 fee and one 5 bps spread side at the first open.
* Turnover: one-way, half the sum of absolute weight changes (cash included), averaged over
  months 2..190 (month 1 is the initial purchase). Names opened a month are reported too.
* Cost drag: zero-cost CAGR minus after-cost CAGR.

### Controls

* **(a) Random control**: 20 eligible members drawn uniformly without replacement, independently
  each month, held and costed exactly like the variants; 500 repetitions (numpy
  `default_rng(20261101)`). Its distribution (5th, 25th, 50th, 75th, 95th percentiles) of CAGR,
  Sharpe and max drawdown is reported. "Random-control mean" is the mean monthly return across
  the 500 repetitions.
* **(b) SPY** buy-and-hold. **(c) RSP** buy-and-hold.
* **(d) All members**: every eligible member, equal weight, rebalanced monthly. Reported at zero
  cost only: at 414-499 names a $10,000 account would hold about $22 a name, which the $1 fee
  makes untradable.
* Informational only (not in the pass rule):
  * **Random + M2's filter**: the random control with M2's cash months, 500 repetitions, to
    separate M2's market filter from its stock selection.
  * **Already-in-index universe**: all four variants and the random control rerun on the
    members whose Wikipedia "date added" is on or before the signal day (271 eligible in
    2011-01, 498 in 2026-10). Members that left the index and rejoined count only from the
    latest date added. This removes the "future member" part of the survivorship bias.
  * The share of each variant's position-months in stocks not yet in the index at the time, and
    their average return.

### Statistics

* Monthly returns over the 190 holding months. CAGR from the first open (2011-01-03) to the last
  close (2026-10-30). Max drawdown from daily marks (each close, and each month's exit open).
* Sharpe: mean monthly return minus the month's T-bill return, over its standard deviation,
  times sqrt(12). Same definition for every strategy and benchmark.
* Worst calendar year (2026 is January-October), worst month, and a by-year table with 2015-16,
  2018, 2020 and 2022 called out.
* Momentum crashes: each variant's five worst months against the random-control mean, and the
  deepest peak-to-trough fall of its cumulative return relative to the random-control mean,
  with dates.
* Excess returns, each a monthly series:
  * over **RSP**: the variant's monthly return minus RSP's. Primary basis: the $100,000
    after-cost returns (the paper account, where a new strategy would run first). The zero-cost
    and $10,000 bases are reported too.
  * over the **random-control mean**: both at zero cost (stock selection alone).
  * For each: mean, plain t (mean over standard error, n = 190), one-sided p (t distribution,
    189 df), and Holm-adjusted one-sided p across the 4 variants (one Holm family per excess
    series). A Newey-West t (3 lags) is shown for context.

### Pass rule (fixed in the brief; cost basis fixed here)

A variant passes only if all three hold:

1. its **zero-cost** Sharpe is above the 95th percentile of the zero-cost random control's
   Sharpe over the full period (zero cost on both sides, because the random control's
   near-100% monthly turnover would otherwise hand it a cost penalty momentum does not pay);
2. its monthly excess over RSP, **$100,000 after costs**, has Holm-adjusted one-sided p < 0.10;
3. its **$10,000 after-cost** Sharpe (primary cost model) is at least SPY's Sharpe on the same
   account.

Pass or fail is reported per criterion and never softened. If the conservative cost model or
the already-in-index universe would change a verdict, that is reported next to it, but the
verdict stays the primary one.
