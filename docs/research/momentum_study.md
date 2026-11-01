# Cross-sectional momentum (monthly, top 20) study (2026-11-01) -- research only

**Question.** Cross-sectional momentum (buy the stocks that rose most over the past year,
rebalance monthly) is the best-documented stock-selection edge in academic finance. On
2011-2026 data, after realistic costs for the bot's account sizes (paper $100,000; the real
eToro AlgoBot account about $10,000, with an operator copy of $500 following it at 5%), does it
add anything beyond simply owning these stocks, owning SPY, or owning the equal-weight S&P 500
(RSP)?

**Answer: three variants pass the pre-registered rule, but the pass comes from the data, not
from momentum. Once the stocks that were not yet in the index are removed, nothing passes.**

* On today's S&P 500 members, monthly top-20 momentum looks spectacular. M1 (12-1 month return)
  makes **32.9% a year at zero cost and 30.6% on the $10,000 account after costs**, against
  13.9% for SPY, 11.9% for RSP and 17.0% for the median random 20. Its Sharpe is 1.15 (random
  95th percentile 1.09; SPY 0.87).
* **M1, M3 (6-1 month) and M4 (volatility-scaled 12-1) pass all three criteria.** Their excess
  over RSP is +15% to +19% a year (t 3.5-3.9, Holm p <= 0.0005). M2 (M1 plus SPY's 10-month
  filter) fails criteria (1) and (3).
* **The pass is a survivorship artefact.** 43% of M1's position-months (M3 42%, M4 33%) are in
  stocks that were *not yet in the S&P 500* at the time, against 19% for the random control.
  They are on today's list because they kept rising, and they returned **+3.96% a month** while
  held, against +1.74% for M1's other holdings. A momentum rule is built to find exactly these
  stocks, so neither the random control nor RSP removes this bias.
* **Rerun on the stocks already in the index at each signal day, every variant fails every
  criterion.** M1 then makes 17.2% a year with a Sharpe of 0.77 (22nd percentile of its own
  random control, below SPY's 0.87). Its excess over RSP is +5.3% a year, t 1.29, Holm p 0.30.
  Even that run is flattered, because members that later left the index are missing.
* **Momentum crashes are visible and recent.** In 2026-07 and 2026-10 the M1 portfolio, by then
  16 of 20 names in information technology (mostly chips, memory, storage and optical
  hardware), lost 24.1% and 20.5% in single months while the random-control mean rose 2.2% and
  5.0%. On 2026-10-30, M1 ($100,000 account) sat 27% below its end-of-June 2026 level.
* **Costs** are 1.7-2.7% a year on the $10,000 account (3.1-4.0% if every resize of a kept name
  also pays $1), and about 0.5-0.8% on $100,000. The $500 copy would pay 27-43% a year if each
  copied position paid the same $1 a side.

**Status.** The question, data and method below were committed (`2a6dfb3`) before any strategy
or control result was computed. One data rule was added afterwards: the HWM break, which is
marked below. The results sections were added once the run was done. Nothing in the method was
changed after results were seen.

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
* **Added after the pre-registration commit (results-blind):** one more break, HWM on
  2020-04-01. That day Arconic Inc. became Howmet and spun off Arconic Corp. yfinance folds the
  spin-off into its split column, so the factor test misses it. The day's open (15.40, also its
  high, against a 12.32 prior close and a 13.20 close) is a when-issued print. It turned up in a
  scan of opens on month-boundary trade days, which found 7 opens more than 10% away from both
  the prior close and the same day's close. The other six (LULU 2011-12-01, LITE 2015-08-03,
  CDNS 2015-12-01, MRNA 2020-12-01, ALGN 2024-02-01, BE 2025-08-01) are left as they are: Yahoo's
  opening prints are noisy, and that noise hits every strategy alike. The scan ran after an
  engine sanity check had printed M1's and SPY's zero-cost total returns, but before any random
  or all-members control was run. With the break, M1's zero-cost CAGR is 32.92%; without it,
  32.68%. Breaks are then 17 in 16 symbols, and the break rule removes 208 of 88,548
  stock-months instead of 194.
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

## Results

All numbers come from one run of `research/momentum/cli.py run` (about 35 seconds); the tables
are generated by `research/momentum/report.py`. 190 holding months, 2011-01-03 open to
2026-10-30 close (15.82 years). 414-499 members eligible a month (465 on average). M2 held cash in
29 of the 190 months. The Sharpe ratio of a 16-year sample has a standard error of roughly 0.3,
so Sharpe gaps of 0.1-0.2 are within noise.

### One row per variant and control

|  | CAGR 0 cost | CAGR $100k | CAGR $10k | max DD (0 cost) | Sharpe 0 cost | Sharpe $100k | Sharpe $10k | ann. vol | worst year | worst month | random-control percentile: Sharpe / CAGR |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **M1** | 32.9% | 32.2% | 30.6% | -40.9% | 1.15 | 1.14 | 1.09 | 26.6% | 2022 +3.2% | 2026-07 -24.1% | 99.2 / 100.0 |
| **M2** | 22.2% | 21.6% | 20.1% | -34.9% | 0.93 | 0.91 | 0.86 | 22.7% | 2022 -16.5% | 2026-07 -24.1% | 45.6 / 100.0 |
| **M3** | 33.1% | 32.0% | 29.5% | -39.6% | 1.21 | 1.18 | 1.10 | 25.3% | 2011 +0.4% | 2026-07 -24.9% | 99.8 / 100.0 |
| **M4** | 29.0% | 28.2% | 26.2% | -38.4% | 1.18 | 1.16 | 1.08 | 22.5% | 2018 -2.3% | 2020-03 -19.3% | 99.6 / 100.0 |
| SPY buy-and-hold | 13.9% | 13.9% | 13.9% | -33.7% | 0.87 | 0.87 | 0.87 | 14.5% | 2022 -18.0% | 2020-03 -16.4% |  |
| RSP buy-and-hold | 11.9% | 11.9% | 11.9% | -39.0% | 0.69 | 0.69 | 0.69 | 15.8% | 2022 -11.3% | 2020-03 -21.5% |  |
| all members, equal weight (~465 names) | 17.3% | n/a | n/a | -38.2% | 1.01 | n/a | n/a | 15.6% | 2022 -10.2% | 2020-03 -20.6% |  |
| random 20, median of 500 | 17.0% | 15.1% | 10.5% | -38.5% | 0.95 | 0.85 | 0.60 | 16.6% |  |  |  |

* "Worst year" and "worst month" are at zero cost. The random-control percentile is the share of
  the 500 random portfolios with a lower zero-cost Sharpe (or CAGR).
* SPY's and RSP's after-cost figures include one $1 fee and one 5 bps spread side at the first
  open, which is why they barely move.
* M4's worst month is the 2020-03 crash (-19.3%). The others' worst month is 2026-07, a
  momentum crash (see below).

### Costs, turnover and account size

|  | one-way turnover / month | names opened / month | cost / year $100k | cost / year $10k | cost / year $10k, resize pays $1 | cost / year $500 copy | CAGR drag $100k | CAGR drag $10k | $10k, resize pays $1: CAGR / Sharpe | $500 copy: CAGR / Sharpe |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **M1** | 30.8% | 5.6 | 0.5% | 1.7% | 3.4% | 27.4% | 0.7% | 2.3% | 28.4% / 1.02 | 0.6% / 0.09 |
| **M2** | 38.0% | 5.8 | 0.5% | 1.8% | 3.1% | 28.2% | 0.6% | 2.1% | 18.5% / 0.80 | -8.3% / -0.34 |
| **M3** | 46.4% | 8.9 | 0.8% | 2.7% | 4.0% | 43.2% | 1.0% | 3.5% | 27.8% / 1.05 | -14.4% / -0.56 |
| **M4** | 38.3% | 7.3 | 0.6% | 2.2% | 3.7% | 35.4% | 0.8% | 2.8% | 24.2% / 1.02 | -10.1% / -0.44 |
| random 20 (median, $100k) | 95.8% | 19.1 | 1.6% |  |  |  |  |  |  |  |
| random 20 (median, $10k) | 95.8% | 19.1 |  | 5.7% |  |  |  |  |  |  |

SPY Sharpe on the $10,000 account: 0.866 (zero cost 0.866); RSP 0.694.

* M1 keeps 14.4 of its 20 names from one month to the next and opens 5.6 new ones. The random
  control redraws almost everything every month (19.1 new names, 96% one-way turnover), which
  is why the pass rule compares it with momentum at zero cost.
* The $1 fee is what matters on small accounts. On $100,000 it is 0.02% of a $5,000 position;
  on $10,000 it is 0.20% of a $500 position, and costs take 1.7-2.7 points a year off CAGR.
* If every resize of a kept name also paid $1, the $10,000 costs would rise to 3.1-4.0% a
  year. M1, M3 and M4 would still clear criterion (3); M2 would not.
* **The $500 copy is not viable** on this design if copied positions pay $1 a side: 20
  positions of $25 each, 27-43% of equity a year in fees, CAGR between +0.6% and -14.4%.
  Whether eToro charges copied positions this way was not checked.

### Random control: the distribution

|  | 5th | 25th | 50th | 75th | 95th |
|---|--:|--:|--:|--:|--:|
| zero cost: cagr | 14.6% | 16.1% | 17.0% | 18.2% | 19.9% |
| zero cost: sharpe | 0.80 | 0.89 | 0.95 | 1.00 | 1.09 |
| zero cost: max dd | -43.1% | -40.4% | -38.5% | -36.6% | -34.0% |
| $100,000: cagr | 12.8% | 14.3% | 15.1% | 16.3% | 18.0% |
| $100,000: sharpe | 0.71 | 0.79 | 0.85 | 0.90 | 1.00 |
| $100,000: max dd | -43.2% | -40.5% | -38.6% | -36.7% | -34.1% |
| $10,000: cagr | 8.2% | 9.6% | 10.5% | 11.6% | 13.2% |
| $10,000: sharpe | 0.46 | 0.55 | 0.60 | 0.66 | 0.74 |
| $10,000: max dd | -43.7% | -40.9% | -38.9% | -37.0% | -34.3% |

|  | Sharpe (0 cost) | random 95th | percentile | CAGR (0 cost) | random 95th | percentile | Sharpe percentile, $100k vs random $100k | Sharpe percentile, $10k vs random $10k |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| **M1** | 1.155 | 1.093 | 99.2 | 32.9% | 19.9% | 100.0 | 100.0 | 100.0 |
| **M2** | 0.934 | 1.093 | 45.6 | 22.2% | 19.9% | 100.0 | 78.0 | 99.8 |
| **M3** | 1.206 | 1.093 | 99.8 | 33.1% | 19.9% | 100.0 | 100.0 | 100.0 |
| **M4** | 1.184 | 1.093 | 99.6 | 29.0% | 19.9% | 100.0 | 100.0 | 100.0 |

Random 20 with M2's cash months (informational): median CAGR 10.9%, median Sharpe 0.76, 95th percentile Sharpe 0.93; M2's percentile against it: 95.2 on Sharpe, 100.0 on CAGR.

M1, M3 and M4 beat the 95th percentile of the zero-cost random control on both CAGR and
Sharpe. On Sharpe the margin is thin: 1.155, 1.206 and 1.184 against 1.093, so 0.06-0.11
above it. M2's Sharpe (0.934) is at the 46th percentile: its CAGR (22.2%) beats every random
portfolio, but with 22.7% volatility against the random median's 16.6%. Against random
portfolios with the same cash months, M2 sits just above the 95th percentile (95.2).

### Excess returns and the tests

| series | variant | mean / month | x12 | t | Newey-West t (3) | one-sided p | Holm p |
|---|---|--:|--:|--:|--:|--:|--:|
| over RSP, $100k after costs (criterion 2) | M1 | +1.60% | +19.2% | 3.78 | 4.16 | 0.0001 | 0.0003 |
| over RSP, $100k after costs (criterion 2) | M2 | +0.81% | +9.7% | 1.75 | 2.03 | 0.0407 | 0.0407 |
| over RSP, $100k after costs (criterion 2) | M3 | +1.56% | +18.7% | 3.93 | 4.35 | <0.0001 | 0.0002 |
| over RSP, $100k after costs (criterion 2) | M4 | +1.24% | +14.9% | 3.52 | 3.61 | 0.0003 | 0.0005 |
| over RSP, zero cost | M1 | +1.64% | +19.7% | 3.89 | 4.28 | <0.0001 | 0.0002 |
| over RSP, zero cost | M2 | +0.85% | +10.2% | 1.85 | 2.14 | 0.0333 | 0.0333 |
| over RSP, zero cost | M3 | +1.62% | +19.5% | 4.10 | 4.53 | <0.0001 | 0.0001 |
| over RSP, zero cost | M4 | +1.30% | +15.6% | 3.67 | 3.76 | 0.0002 | 0.0003 |
| over RSP, $10k after costs | M1 | +1.50% | +17.9% | 3.54 | 3.89 | 0.0003 | 0.0010 |
| over RSP, $10k after costs | M2 | +0.70% | +8.4% | 1.52 | 1.77 | 0.0647 | 0.0647 |
| over RSP, $10k after costs | M3 | +1.40% | +16.7% | 3.53 | 3.90 | 0.0003 | 0.0010 |
| over RSP, $10k after costs | M4 | +1.11% | +13.3% | 3.14 | 3.22 | 0.0010 | 0.0020 |
| over the random-control mean, zero cost | M1 | +1.25% | +15.0% | 3.05 | 3.35 | 0.0013 | 0.0039 |
| over the random-control mean, zero cost | M2 | +0.45% | +5.5% | 1.02 | 1.18 | 0.1556 | 0.1556 |
| over the random-control mean, zero cost | M3 | +1.23% | +14.8% | 3.20 | 3.53 | 0.0008 | 0.0033 |
| over the random-control mean, zero cost | M4 | +0.90% | +10.8% | 2.66 | 2.69 | 0.0043 | 0.0086 |

The universe itself: random-control mean minus RSP +4.7% a year (t 9.46); all members minus RSP +4.7% (t 9.82). Variant minus SPY ($100k): M1 +17.6% (t 3.71); M2 +8.1% (t 1.58); M3 +17.1% (t 3.85); M4 +13.3% (t 3.40).

The universe beats RSP by itself. The average stock on today's list beat the equal-weight S&P
500, as it actually was, by 4.7 points a year (t 9.5). That is the ordinary survivorship gap,
and the random control already carries it. M1's excess over the random-control mean (+15.0% a
year, t 3.05) is what survives that correction. The next section shows it does not survive
the "future member" part of the bias.

### Pass/fail per criterion

| variant | (1) Sharpe > random 95th (0 cost) | (2) excess over RSP, $100k, Holm p < 0.10 | (3) $10k Sharpe >= SPY | verdict | (3) with resize fee | already-in-index universe: (1) / (2) / (3) |
|---|---|---|---|---|---|---|
| **M1** | pass (1.155 vs 1.093) | pass (Holm p 0.0003) | pass (1.089 vs 0.866) | **PASS** | pass | **fail** / **fail** / **fail** |
| **M2** | **fail** (0.934 vs 1.093) | pass (Holm p 0.0407) | **fail** (0.856 vs 0.866) | fail | **fail** | **fail** / **fail** / **fail** |
| **M3** | pass (1.206 vs 1.093) | pass (Holm p 0.0002) | pass (1.099 vs 0.866) | **PASS** | pass | **fail** / **fail** / **fail** |
| **M4** | pass (1.184 vs 1.093) | pass (Holm p 0.0005) | pass (1.084 vs 0.866) | **PASS** | pass | **fail** / **fail** / **fail** |

**As pre-registered: M1, M3 and M4 pass, and M2 fails.** M2 fails (1), and it fails (3) by
0.010 of Sharpe (0.856 against 0.866). The conservative resize-fee cost model changes no
verdict.

The already-in-index universe, a pre-registered sensitivity outside the rule, turns all three
passes into fails on all three criteria. The rule is not softened or hardened here: the
verdict stays PASS for M1, M3 and M4. But the next section shows why that PASS is not evidence
of an edge.

### Survivorship: why the pass does not mean an edge

| variant | position-months | share not yet in the index | their mean month | the rest's mean month | names kept a month | names in the largest sector |
|---|--:|--:|--:|--:|--:|--:|
| M1 | 3800 | 42.7% | +3.96% | +1.74% | 14.4 | 7.3 |
| M2 | 3220 | 43.0% | +3.76% | +1.04% | 13.2 | 7.0 |
| M3 | 3800 | 42.2% | +3.67% | +1.94% | 11.1 | 7.2 |
| M4 | 3800 | 32.7% | +3.75% | +1.66% | 12.7 | 6.6 |

For comparison, 18.9% of all eligible stock-months, and 19.5% of the random control's, are in stocks not yet in the index.

Already-in-index universe (271 eligible in 2011-01, 498 in 2026-10):

|  | CAGR 0 cost | CAGR $10k | max DD | Sharpe 0 cost | Sharpe $100k | Sharpe $10k | random Sharpe percentile | excess over RSP, $100k (x12) | excess over random mean (x12) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| M1 | 17.2% | 15.1% | -37.9% | 0.77 | 0.75 | 0.69 | 21.8 | +5.3% (t 1.29, Holm p 0.2964) | +3.2% (t 0.79, Holm p 0.6437) |
| M2 | 9.3% | 7.2% | -34.0% | 0.51 | 0.48 | 0.40 | 0.0 | -2.7% (t -0.58, Holm p 0.7195) | -4.7% (t -1.04, Holm p 0.8512) |
| M3 | 18.3% | 15.0% | -36.6% | 0.82 | 0.78 | 0.69 | 40.2 | +5.9% (t 1.50, Holm p 0.2704) | +4.1% (t 1.04, Holm p 0.5971) |
| M4 | 16.8% | 14.2% | -37.7% | 0.83 | 0.80 | 0.71 | 45.4 | +4.2% (t 1.17, Holm p 0.2964) | +2.2% (t 0.63, Holm p 0.6437) |
| random 20 (median) | 14.8% |  | -38.7% | 0.84 |  |  | 95th: 1.00 |  |  |
| all members (0 cost) | 14.9% |  | -38.4% | 0.89 |  |  |  |  |  |

* **Today's list contains the future.** A stock that was not in the S&P 500 in, say, 2015 is
  on today's list only because it later grew big enough to join. In this sample those stocks
  were 19% of eligible stock-months, and the random control holds them at that rate.
* **Momentum seeks them out.** Momentum buys what has been rising, and the stocks that rose
  hardest before joining the index are exactly the ones it buys. 33-43% of each variant's
  holdings were not yet in the index. While held they averaged +3.7% to +4.0% a month, against
  +1.0% to +1.9% for the variants' other holdings.
* **Without them, the edge goes.** In the already-in-index universe:
  * every variant's Sharpe (0.51-0.83) is below its random control's median (0.84) and below
    SPY's (0.87);
  * M1-M4's excess over RSP shrinks to -2.7% .. +5.9% a year, none of it significant (Holm p
    0.27-0.72);
  * their excess over the random mean shrinks to -4.7% .. +4.1% a year (t -1.04 to +1.04).
* **Still optimistic.** This universe still leaves out companies that were members and later
  left (failures, acquisitions, demotions). A momentum rule would have held some of them on
  their way up before they fell, so a point-in-time test with those names included would most
  likely look worse still.

### By year, with 2015-16, 2018, 2020 and 2022 called out

|  | 2011 | 2012 | 2013 | 2014 | **2015** | **2016** | 2017 | **2018** | 2019 | **2020** | 2021 | **2022** | 2023 | 2024 | 2025 | 2026 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| M1 | +5% | +42% | +53% | +34% | +7% | +10% | +34% | +6% | +48% | +51% | +19% | +3% | +43% | +92% | +53% | +41% |
| M2 | +5% | +30% | +53% | +34% | +2% | +4% | +34% | +5% | +7% | +49% | +19% | -17% | +21% | +92% | +32% | +8% |
| M3 | -0% | +12% | +50% | +23% | +12% | +26% | +44% | +22% | +45% | +66% | +27% | +4% | +56% | +51% | +51% | +41% |
| M4 | +12% | +18% | +50% | +22% | +14% | +12% | +37% | -3% | +32% | +49% | +15% | +2% | +38% | +76% | +29% | +71% |
| random-control mean (0 cost) | +6% | +22% | +37% | +20% | +2% | +22% | +26% | -4% | +38% | +20% | +32% | -11% | +22% | +22% | +16% | +17% |
| all members (0 cost) | +5% | +22% | +37% | +20% | +2% | +22% | +26% | -4% | +38% | +20% | +32% | -10% | +22% | +22% | +16% | +17% |
| SPY | +3% | +16% | +29% | +14% | -1% | +15% | +21% | -6% | +34% | +18% | +29% | -18% | +25% | +26% | +18% | +11% |
| RSP | +0% | +17% | +33% | +15% | -5% | +17% | +18% | -9% | +31% | +12% | +29% | -11% | +12% | +14% | +11% | +15% |

|  | 2015-16 | 2018 | 2020 | 2022 | 2026 (Jan-Oct) |
|---|--:|--:|--:|--:|--:|
| M1 | +17.8% | +5.8% | +50.7% | +2.8% | +41.4% |
| M2 | +5.7% | +4.5% | +48.7% | -16.8% | +8.4% |
| M3 | +42.0% | +21.9% | +65.9% | +4.1% | +41.4% |
| M4 | +27.5% | -2.9% | +49.4% | +2.4% | +70.9% |
| random-control mean (0 cost) | +23.9% | -3.9% | +20.3% | -10.6% | +16.6% |
| all members (0 cost) | +23.6% | -4.0% | +20.2% | -10.2% | +17.1% |
| SPY | +13.7% | -6.4% | +18.2% | -18.0% | +11.1% |
| RSP | +11.8% | -9.5% | +12.5% | -11.3% | +14.9% |

* 2015-16: M1 lagged the random-control mean (+17.8% against +23.9%) in the early-2016 rebound
  of beaten-down stocks. M3 did well (+42.0%).
* 2018: all variants held up better than the market (M1 +5.8%, SPY -6.4%), apart from the
  2018-10 crash month (M1 -18.4%).
* 2020: momentum did very well (M1 +50.7%, SPY +18.2%). M2's filter put it in cash for 2020-03
  to 2020-05, so it missed the April rebound: in 2020-04 it was flat while the random-control
  mean rose 16.9%.
* 2022: M1 +2.8% against SPY -18.0%; it held energy stocks. M2 lost 16.8% that year, worse than
  M1 and RSP. Its filter was in cash for 8 months in which M1 made +25.0% in total, and invested in
  the other 4, in which M1 lost 17.8% ($100,000 account).
* M1 beat RSP in 14 of 16 calendar years and the random-control mean in 14. In the
  already-in-index universe the full-period edge is gone, so these year counts say little about
  momentum itself.

### Momentum crashes

| variant | month | variant | random mean | difference |
|---|---|--:|--:|--:|
| M1 | 2026-07 | -24.1% | +2.2% | -26.3% |
| M1 | 2026-10 | -20.5% | +5.0% | -25.5% |
| M1 | 2018-10 | -18.4% | -7.6% | -10.8% |
| M1 | 2024-07 | -6.3% | +4.0% | -10.3% |
| M1 | 2021-03 | -5.5% | +4.5% | -10.1% |
| M2 | 2026-07 | -24.1% | +2.2% | -26.3% |
| M2 | 2026-10 | -20.5% | +5.0% | -25.5% |
| M2 | 2020-04 | +0.0% | +16.9% | -16.9% |
| M2 | 2019-01 | +0.2% | +11.5% | -11.3% |
| M2 | 2018-10 | -18.4% | -7.6% | -10.8% |
| M3 | 2026-07 | -24.9% | +2.2% | -27.2% |
| M3 | 2026-10 | -12.4% | +5.0% | -17.5% |
| M3 | 2025-03 | -16.7% | -4.7% | -12.0% |
| M3 | 2021-12 | -6.7% | +5.1% | -11.7% |
| M3 | 2018-10 | -15.6% | -7.6% | -8.0% |
| M4 | 2026-07 | -18.7% | +2.2% | -20.9% |
| M4 | 2026-10 | -5.7% | +5.0% | -10.7% |
| M4 | 2021-03 | -4.9% | +4.5% | -9.5% |
| M4 | 2024-07 | -3.0% | +4.0% | -7.0% |
| M4 | 2018-10 | -14.0% | -7.6% | -6.4% |

| variant | deepest fall relative to the random mean | peak (month end) | trough (month end) | back to the peak by |
|---|--:|---|---|---|
| M1 | -30.1% | 2026-06 | 2026-10 | None |
| M2 | -36.5% | 2018-08 | 2019-10 | 2024-11 |
| M3 | -26.6% | 2026-06 | 2026-07 | None |
| M4 | -20.5% | 2026-06 | 2026-07 | None |

|  | max DD peak | trough | depth |
|---|---|---|--:|
| M1 | 2020-02-19 | 2020-03-23 | -40.9% |
| M2 | 2026-06-22 | 2026-07-29 | -34.9% |
| M3 | 2020-02-19 | 2020-03-18 | -39.6% |
| M4 | 2020-02-19 | 2020-03-23 | -38.4% |
| SPY | 2020-02-19 | 2020-03-23 | -33.7% |
| RSP | 2020-02-12 | 2020-03-23 | -39.0% |

* **2026-07 and 2026-10 are classic momentum crashes**: sharp reversals in a crowded winner
  group.
* By mid-2026 the 12-1 winners were almost all chips, memory, storage and optical names (over the
  look-back SNDK rose 37-fold, MU 8-fold, INTC 5-fold). In July 2026 M1 lost 24.1% (SNDK
  -44%, GLW -44%, MRVL -36%, INTC -35%) while SPY rose 0.6%. In October 2026 it lost 20.5%
  (INTC -45%, SNDK -34%, TER -32%) while RSP rose 4.9%.
* M1's cumulative return relative to the random mean fell 30% from 2026-06 to 2026-10 and had
  not recovered by the end of the data. M4 (volatility-scaled) fell least: 20.5%.
* The other big relative losses are 2018-10 (M1 -18.4% against -7.6%), 2024-07 (the July 2024
  rotation out of that year's tech winners, plus CrowdStrike's outage, CRWD -40%) and 2021-03
  (the value rotation).
* **Concentration.** On average the largest GICS sector holds 6.6-7.3 of the 20 names. Nothing
  caps it.

### Picks at three dates

* M1 2011-01: NFLX LVS CMG FFIV DECK UAL CMI AKAM ILMN URI CRM ULTA FTNT BKNG LULU IT SWKS WYNN DLTR DPZ
* M1 2018-12: TWLO TKO AXON CVNA DXCM LULU FTNT TPL XYZ DECK AMD NFLX HCA VEEV CMG ZBRA GDDY TJX NOW DPZ
* M1 2026-10: SNDK MU LITE MRNA WDC STX DELL AMD INTC CIEN COHR TER MRVL BE TWLO LRCX AMAT ILMN HPE VLO
* M2 2011-01: NFLX LVS CMG FFIV DECK UAL CMI AKAM ILMN URI CRM ULTA FTNT BKNG LULU IT SWKS WYNN DLTR DPZ
* M2 2018-12: TWLO TKO AXON CVNA DXCM LULU FTNT TPL XYZ DECK AMD NFLX HCA VEEV CMG ZBRA GDDY TJX NOW DPZ
* M2 2026-10: SNDK MU LITE MRNA WDC STX DELL AMD INTC CIEN COHR TER MRVL BE TWLO LRCX AMAT ILMN HPE VLO
* M3 2011-01: LVS BKNG URI FTNT FFIV CF NFLX CMG RCL MOS FCX CRM DECK AMZN F TPR HAL EXPD SWKS CMI
* M3 2018-12: DXCM MKC TWLO CIEN FTNT CVNA LULU AMD CASY HCA LLY DG CHD CI ROST XYZ TKO MRK ELV CLX
* M3 2026-10: MU DELL MRNA SNDK PANW CRWD AMD HUM HPE MRVL STX FTNT INTC DDOG CNC TWLO NTAP ILMN ZBRA CRL
* M4 2011-01: NFLX CMG AZO ERIE LVS FFIV ILMN DLTR CMI DECK IT ORLY AME AKAM ROST TSCO HRL UAL YUM SWKS
* M4 2018-12: TKO LULU FTNT TWLO AXON DXCM HCA TPL TJX MKC DECK CVNA MRK AES DPZ JKHY MSI XYZ TGT GDDY
* M4 2026-10: SNDK MU LITE WDC STX VLO DELL TGT MPC PSX STT TRGP MRK NUE AMD FDX ILMN JNJ FTNT JBHT

## Conclusion (plain language)

On the list of today's S&P 500 companies, buying the 20 stocks that rose most over the past
year and rebalancing monthly looks like a great strategy: 26-31% a year on the $10,000
account after costs, against 14% for simply holding SPY, and three of the four versions (M1, M3,
M4) pass every test that was fixed in advance. That result cannot be trusted, because today's
list is partly a list of past winners: about 4 in 10 of the stocks momentum bought were not yet
in the S&P 500 at the time, and they are in it now because they kept rising. Rerun only on
companies that were already in the index on each date, every version fails every test: M1,
M3 and M4 make about 17-18% a year at zero cost, their risk-adjusted returns (Sharpe 0.77-0.83)
are below simply holding SPY (0.87), and their lead over the equal-weight index is small enough
to be luck. Even that
rerun is too kind, because companies that dropped out of the index, often after falling, are
missing from the data. Momentum also has violent losing months: in July and October 2026 the
portfolio, by then mostly chip and hardware stocks, lost 24% and 20% in single months while the
average stock rose, and the SPY trend filter (M2) did not help: it cut returns and kept the
portfolio in cash through the 2020 rebound. So on this data momentum is not a demonstrated
edge, and it should not get real money: not the $10,000 eToro account, and not the $500 copy,
where $1 fees on 20 positions of $25 could take a quarter or more of the account each year.
Paper-trading it as a new strategy in the bot (monthly rebalance, 20 positions) would do no
harm, but expect a concentrated book with 22-27% annual volatility (SPY: 15%), and expect
to need years of monthly results before anyone can tell skill from luck. What remains
uncertain: a fair test needs a historical membership list that includes the companies that
left the index, which this data does not have; the published evidence for momentum uses such
data, and finds the effect weaker among the largest stocks and prone to crashes; and the 2026
price moves (the chip boom and crash, Moderna's +177% day) were not checked against a second
source.

## Approximations and things this study does not model

* **Universe.** Today's members only, with no delisted or removed companies. The
  already-in-index sensitivity counts a member only from its latest Wikipedia "date added", so
  a company that left and rejoined is missing during its earlier spell.
* **First signal.** The 12-month anchor for the 2010-12-31 signal is the 2010-01-04 close, one
  trading day short of 2009-12-31, which is not in the data.
* **Last month.** 2026-10 is marked at the 2026-10-30 close, not sold at November's first open.
* **Fills.** Trades fill at Yahoo's adjusted opens. Opening prints are noisy (see the open scan
  under Data). There is no slippage beyond the 5 bps a side and no market impact, which is
  reasonable for $500-$5,000 positions in S&P 500 stocks.
* **Exits.** Two held-stock exits had no open on the exit day and used the last close.
* **Position sizes.** Fractional dollar amounts are assumed, which eToro allows.
* **Costs are a fixed fraction of the starting account size.** A real $10,000 account that grew
  would pay relatively less over time; one that withdrew profits would not.
* **Resizes.** In the primary cost model, a resize of a kept name pays the spread but no $1.
  The conservative model, reported alongside, charges the $1 too.
* **Fees not modelled.** No FX conversion, overnight or withdrawal fees, and no taxes. Yahoo's
  dividend adjustment treats dividends as reinvested gross, with no withholding.
* **Cash.** ^IRX's discount yield x days / 360, fixed on the signal day. This is not a traded
  instrument, and eToro may pay nothing on idle cash; at 0% cash, M2 would be worse.
* **Sharpe** subtracts the T-bill return, uses monthly returns and is annualised by sqrt(12).
  Max drawdown uses each daily close and each month's exit open.
* **Random control.** It redraws its 20 names independently every month (96% turnover), so it is
  compared with momentum at zero cost (criterion 1). Its after-cost distributions are shown for
  context only.
* **Data checks.** Daily moves beyond +-30% and month-boundary opens were checked by eye, and
  HWM 2020-04-01 was added as a break after the pre-registration commit. Moves after mid-2026
  (the chip-sector run-up and crash, MRNA 2026-08-19) were not cross-checked against another
  source.
* **The bot.** None of its live gates, position caps, intraday signalling or eToro room and copy
  rules are modelled. The study does not say how this would interact with the bot's existing
  strategies.

## Reproduce

```
python -I research/momentum/cli.py data   --extra-dir EXTRA                                 # ^IRX, BIL (~5 s)
python -I research/momentum/cli.py run    --price-dir PRICES --extra-dir EXTRA --out OUT    # ~35 s
python -I research/momentum/report.py OUT > OUT/tables.md
```

`PRICES` is the pullback study's cache (`research/pullback/cli.py data` on
`claude/wip-pullback-study`): `prices/`, `raw/` and `constituents/`. It is read, never written.
Data were downloaded on 2026-11-01 and are not committed. Yahoo revises history, so a later
download can move the numbers slightly. The random control uses `default_rng(20261101)`.
