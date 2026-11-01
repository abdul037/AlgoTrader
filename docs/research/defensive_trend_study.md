# Defensive trend rules on index funds (2026-11-01) -- research only

**Question.** A previous study (`docs/research/pullback_study.md`, branch
`claude/wip-pullback-study`) found that the bot's stock strategies add nothing beyond owning
stocks that rose, and its 2011-2026 sample had no long bear market. Would a simple, defensive
trend rule on index funds have protected capital in real bear markets (2008, 2020, 2022) at an
acceptable cost in return? The rule would be a possible low-maintenance core for the account.
Account sizes are $100,000 (paper) and about $10,000 (real eToro AlgoBot).

**Status: pre-registration.** This section, "Data" and "Method" were committed before any
strategy or benchmark result was computed. Results are added below them in a later commit, and
nothing in the method is changed after results are seen.

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

## Results

To follow in the next commit.
