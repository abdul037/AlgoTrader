"""Monthly calendar, eligibility, momentum scores and a vectorised equal-weight portfolio.

Conventions (pre-registered in ``docs/research/momentum_study.md``):

* Holding month k runs from the open of its first trading day (T_k) to the open of the next
  month's first trading day (T_{k+1}). The signal is month k-1's last close (S_k). The final
  month (2026-10) ends at the last close in the data (2026-10-30).
* Scores use adjusted closes at month-ends: 12-1 = C(end of k-2) / C(end of k-13) - 1, and
  6-1 = C(end of k-2) / C(end of k-7) - 1. Volatility is the standard deviation of daily simple
  returns over the 12 months ending at S_k.
* Equal weight at every rebalance. Costs are a fraction of equity: ``fee_frac`` per name traded
  ($1 / account size) plus ``spread`` per unit of notional traded (5 bps a side).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.momentum.data import Panel, month_calendar

FIRST_MONTH = "2011-01"
TOP_N = 20
FFILL_LIMIT = 5
MIN_DAILY_RETURNS = 200


@dataclass
class Months:
    labels: list[str]
    t_in: np.ndarray  # row of T_k
    t_out: np.ndarray  # row of T_{k+1}; for the final month, the last row (exit at its close)
    final_close_exit: np.ndarray  # True only for the last month
    signal: np.ndarray  # row of S_k
    end1: np.ndarray  # last row of month k-2
    end6: np.ndarray  # last row of month k-7
    end12: np.ndarray  # last row of month k-13 (row 0 when that month predates the data)
    days: np.ndarray  # calendar days from T_k to the exit


def build_months(dates: pd.DatetimeIndex, first: str = FIRST_MONTH) -> Months:
    cal = month_calendar(dates)
    row = {date: i for i, date in enumerate(dates)}
    periods = list(cal.index)
    start = periods.index(pd.Period(first, "M"))
    labels, t_in, t_out, final, signal, end1, end6, end12, days = ([] for _ in range(9))

    def last_row(period: pd.Period) -> int:
        return row[cal.loc[period, "last"]] if period in cal.index else 0

    for j in range(start, len(periods)):
        month = periods[j]
        labels.append(str(month))
        t_in.append(row[cal.loc[month, "first"]])
        is_last = j == len(periods) - 1
        out_row = len(dates) - 1 if is_last else row[cal.loc[periods[j + 1], "first"]]
        t_out.append(out_row)
        final.append(is_last)
        signal.append(t_in[-1] - 1)
        end1.append(last_row(month - 2))
        end6.append(last_row(month - 7))
        end12.append(last_row(month - 13))
        days.append((dates[out_row] - dates[t_in[-1]]).days)
    as_array = lambda values: np.asarray(values)  # noqa: E731
    return Months(labels, as_array(t_in), as_array(t_out), as_array(final), as_array(signal),
                  as_array(end1), as_array(end6), as_array(end12), as_array(days))


@dataclass
class Universe:
    """Per-month eligibility, scores, period returns and within-month paths for every member."""

    months: Months
    symbols: list[str]
    eligible: np.ndarray  # K x N bool
    mom12: np.ndarray
    mom6: np.ndarray
    vol12: np.ndarray
    ret: np.ndarray  # K x N open-to-open period return (NaN where not eligible)
    exit_fallback: np.ndarray  # K x N bool: exit used the last close (no open on T_{k+1})
    paths: list[np.ndarray]  # per month: days x N, close / entry open (row t_in .. t_out-1)
    bench_ret: pd.DataFrame  # K x {SPY, RSP}
    bench_paths: list[np.ndarray]
    cash_ret: np.ndarray  # K: T-bill return over the period, yield known at S_k
    spy_below_sma10: np.ndarray  # K bool: SPY closed month k-1 below its 10-month SMA
    in_index: np.ndarray  # K x N bool: Wikipedia "date added" on or before S_k
    meta: dict = field(default_factory=dict)


def build_universe(panel: Panel) -> Universe:
    dates = panel.dates
    months = build_months(dates)
    opens = panel.open.to_numpy()
    raw_close = panel.close.to_numpy()
    close = panel.close.ffill(limit=FFILL_LIMIT).to_numpy()
    daily = raw_close[1:] / raw_close[:-1] - 1.0  # NaN across any dropped bar
    breaks = panel.break_matrix().to_numpy()
    break_cum = np.vstack([np.zeros((1, breaks.shape[1])), np.cumsum(breaks, axis=0)])
    k_count, n_sym = len(months.labels), len(panel.symbols)
    eligible = np.zeros((k_count, n_sym), dtype=bool)
    mom12 = np.full((k_count, n_sym), np.nan)
    mom6 = np.full((k_count, n_sym), np.nan)
    vol12 = np.full((k_count, n_sym), np.nan)
    ret = np.full((k_count, n_sym), np.nan)
    fallback = np.zeros((k_count, n_sym), dtype=bool)
    paths: list[np.ndarray] = []
    bench_paths: list[np.ndarray] = []
    b_open = panel.bench_open.to_numpy()
    b_close = panel.bench_close.ffill().to_numpy()
    bench_ret = np.zeros((k_count, b_open.shape[1]))
    cash_ret = np.zeros(k_count)
    below = np.zeros(k_count, dtype=bool)
    spy_close = panel.bench_close["SPY"].to_numpy()
    cal = month_calendar(dates)
    month_end_rows = [dates.get_loc(day) for day in cal["last"]]
    month_end_rows = np.asarray(month_end_rows)
    added = pd.to_datetime(panel.members.set_index("yf_symbol").loc[panel.symbols, "date_added"]).to_numpy()
    in_index = np.zeros((k_count, n_sym), dtype=bool)

    for j in range(k_count):
        s, a1, a6, a12 = months.signal[j], months.end1[j], months.end6[j], months.end12[j]
        t_in, t_out = months.t_in[j], months.t_out[j]
        c_s, c_1, c_6, c_12 = close[s], close[a1], close[a6], close[a12]
        window = daily[a12:s]  # returns ending on rows a12+1 .. s
        valid = np.isfinite(window).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            vol = np.nanstd(np.where(np.isfinite(window), window, np.nan), axis=0, ddof=1)
            m12 = c_1 / c_12 - 1.0
            m6 = c_1 / c_6 - 1.0
        no_break = (break_cum[t_out + 1] - break_cum[a12 + 1]) == 0  # no break in (a12, t_out]
        entry = opens[t_in]
        ok = (np.isfinite(c_s) & np.isfinite(c_1) & np.isfinite(c_6) & np.isfinite(c_12)
              & (valid >= MIN_DAILY_RETURNS) & np.isfinite(vol)
              & (vol > 0) & no_break & np.isfinite(entry) & (entry > 0))
        if months.final_close_exit[j]:
            exit_px = close[t_out]
            path_rows = slice(t_in, t_out + 1)
            fb = np.zeros(n_sym, dtype=bool)
        else:
            exit_open = opens[t_out]
            fb = ~np.isfinite(exit_open)
            exit_px = np.where(fb, close[t_out - 1], exit_open)
            path_rows = slice(t_in, t_out)
        with np.errstate(invalid="ignore", divide="ignore"):
            r = exit_px / entry - 1.0
            path = pd.DataFrame(close[path_rows]).ffill().fillna(pd.Series(entry)).to_numpy() / entry
        ok &= np.isfinite(r)
        eligible[j] = ok
        mom12[j] = np.where(ok, m12, np.nan)
        mom6[j] = np.where(ok, m6, np.nan)
        vol12[j] = np.where(ok, vol, np.nan)
        ret[j] = np.where(ok, r, np.nan)
        fallback[j] = ok & fb
        paths.append(np.where(ok[None, :], path, np.nan))
        if months.final_close_exit[j]:
            bench_ret[j] = b_close[t_out] / b_open[t_in] - 1.0
        else:
            bench_ret[j] = b_open[t_out] / b_open[t_in] - 1.0
        bench_paths.append(b_close[path_rows] / b_open[t_in])
        cash_ret[j] = float(panel.irx.iloc[s]) * months.days[j] / 360.0
        prior_ends = month_end_rows[month_end_rows <= s][-10:]
        below[j] = spy_close[s] < np.mean(spy_close[prior_ends])
        in_index[j] = added <= dates[s].to_datetime64()

    meta = {
        "first_trade_day": str(dates[months.t_in[0]].date()),
        "last_day": str(dates[-1].date()),
        "months": k_count,
        "eligible_min": int(eligible.sum(axis=1).min()),
        "eligible_max": int(eligible.sum(axis=1).max()),
        "eligible_first": int(eligible[0].sum()),
        "eligible_last": int(eligible[-1].sum()),
        "exit_fallbacks": int(fallback.sum()),
        "cash_source": panel.cash_source,
        "spy_below_sma10_months": int(below.sum()),
    }
    return Universe(months, list(panel.symbols), eligible, mom12, mom6, vol12, ret, fallback, paths,
                    pd.DataFrame(bench_ret, columns=list(panel.bench_open.columns), index=months.labels),
                    bench_paths, cash_ret, below, in_index, meta)


def top_n(scores: np.ndarray, symbols: list[str], n: int = TOP_N) -> list[np.ndarray]:
    """Per month, the indices of the n highest finite scores (ties: symbol ascending)."""

    order_names = np.argsort(np.argsort(np.asarray(symbols)))  # rank of each symbol name
    picks = []
    for row in scores:
        finite = np.flatnonzero(np.isfinite(row))
        order = np.lexsort((order_names[finite], -row[finite]))
        picks.append(finite[order[:n]])
    return picks


@dataclass
class Costs:
    name: str
    fee_frac: float  # $1 / account size, charged per name traded
    spread: float  # per unit of notional traded, one side
    resize_fee: bool  # True: a continuing name resized to equal weight also pays the $1


ZERO = Costs("zero-cost", 0.0, 0.0, False)


def account(size: float, resize_fee: bool = False) -> Costs:
    suffix = ", resize pays $1" if resize_fee else ""
    return Costs(f"${size:,.0f}{suffix}", 1.0 / size, 0.0005, resize_fee)


@dataclass
class Result:
    monthly: np.ndarray  # reps x K, net monthly returns
    gross: np.ndarray  # reps x K, before this month's costs
    cost: np.ndarray  # reps x K, cost as a fraction of equity
    turnover: np.ndarray  # reps x K, one-way (half the sum of |weight changes|, cash included)
    entries: np.ndarray  # reps x K, names opened
    exits: np.ndarray  # reps x K, names closed
    fees: np.ndarray  # reps x K, names that paid the $1
    equity_daily: np.ndarray  # reps x (days): start, then each close, each period-end open
    daily_index: list  # matching labels (date strings)


def simulate(universe: Universe, targets: list[np.ndarray], costs: Costs,
             cash_months: np.ndarray | None = None) -> Result:
    """Equal-weight portfolio(s). ``targets``: per month, a 1-D array of symbol indices (one
    portfolio; the count may vary by month) or a reps x n array (``reps`` portfolios). Months
    flagged in ``cash_months`` (K or reps x K) hold T-bills instead."""

    k_count, n_sym = universe.ret.shape
    single = np.asarray(targets[0]).ndim == 1
    reps = 1 if single else targets[0].shape[0]
    cash_flags = np.zeros((reps, k_count), dtype=bool) if cash_months is None else np.broadcast_to(cash_months, (reps, k_count))
    w_prev = np.zeros((reps, n_sym))
    cash_prev = np.ones(reps)
    equity = np.ones(reps)
    out = {name: np.zeros((reps, k_count)) for name in ("monthly", "gross", "cost", "turnover", "entries", "exits", "fees")}
    daily = [equity.copy()]
    labels = ["start"]
    rows = np.arange(reps)[:, None]
    for j in range(k_count):
        w_tgt = np.zeros((reps, n_sym))
        if single:
            idx = np.asarray(targets[j], dtype=int)
            if len(idx):
                w_tgt[0, idx] = 1.0 / len(idx)
        else:
            idx = targets[j]
            w_tgt[rows, idx] = 1.0 / idx.shape[1]
        cash_tgt = np.zeros(reps)
        flag = cash_flags[:, j]
        w_tgt[flag] = 0.0
        cash_tgt[flag] = 1.0
        diff = np.abs(w_tgt - w_prev)
        opened = (w_prev == 0) & (w_tgt > 0)
        closed = (w_prev > 0) & (w_tgt == 0)
        resized = (w_prev > 0) & (w_tgt > 0) & (diff > 0)
        n_fee = opened.sum(axis=1) + closed.sum(axis=1) + (resized.sum(axis=1) if costs.resize_fee else 0)
        cost = costs.fee_frac * n_fee + costs.spread * diff.sum(axis=1)
        turnover = 0.5 * (diff.sum(axis=1) + np.abs(cash_tgt - cash_prev))
        r = np.nan_to_num(universe.ret[j], nan=0.0)
        growth = w_tgt @ (1.0 + r) + cash_tgt * (1.0 + universe.cash_ret[j])
        after_cost = equity * (1.0 - cost)
        path = np.nan_to_num(universe.paths[j], nan=0.0)  # days x N (only held names matter)
        n_days = path.shape[0]
        frac = np.arange(1, n_days + 1) / n_days
        cash_path = cash_tgt[:, None] * (1.0 + universe.cash_ret[j] * frac[None, :])
        daily.extend(list((after_cost[:, None] * (w_tgt @ path.T + cash_path)).T))
        new_equity = after_cost * growth
        if not universe.months.final_close_exit[j]:
            daily.append(new_equity.copy())
        out["monthly"][:, j] = new_equity / equity - 1.0
        out["gross"][:, j] = growth - 1.0
        out["cost"][:, j] = cost
        out["turnover"][:, j] = turnover
        out["entries"][:, j] = opened.sum(axis=1)
        out["exits"][:, j] = closed.sum(axis=1)
        out["fees"][:, j] = n_fee
        with np.errstate(invalid="ignore", divide="ignore"):
            w_prev = np.where(growth[:, None] > 0, w_tgt * (1.0 + r)[None, :] / growth[:, None], 0.0)
            cash_prev = np.where(growth > 0, cash_tgt * (1.0 + universe.cash_ret[j]) / growth, 0.0)
        equity = new_equity
    equity_daily = np.vstack(daily).T
    return Result(out["monthly"], out["gross"], out["cost"], out["turnover"], out["entries"],
                  out["exits"], out["fees"], equity_daily, labels)


def buy_and_hold(universe: Universe, symbol: str, costs: Costs) -> Result:
    """One ETF bought at the first open (one fee + one spread side) and held."""

    k_count = len(universe.months.labels)
    r = universe.bench_ret[symbol].to_numpy()
    col = list(universe.bench_ret.columns).index(symbol)
    equity = 1.0 - (costs.fee_frac + costs.spread)
    monthly = r.copy()
    monthly[0] = (1.0 + r[0]) * equity - 1.0
    daily = [1.0]
    for j in range(k_count):
        path = universe.bench_paths[j][:, col]
        daily.extend(list(equity * path))
        equity *= 1.0 + r[j]
        if not universe.months.final_close_exit[j]:
            daily.append(equity)
    zeros = np.zeros((1, k_count))
    cost = zeros.copy()
    cost[0, 0] = costs.fee_frac + costs.spread
    return Result(monthly[None, :], r[None, :], cost, zeros, zeros, zeros, zeros,
                  np.asarray(daily)[None, :], [])
