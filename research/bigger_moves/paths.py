"""Trade paths under the backtest engine's rules and the production cost model.

Execution (``app/backtesting/engine.py``):

* A BUY on bar N's close fills at bar N+1's open plus half the spread. Quantity is $100 of risk
  over |fill - stop|, capped at $10,000 of notional (1R = $100, as production's batch runs).
* Each bar in order: the time stop (``max_hold`` bars after the entry bar: out at this bar's
  open); the stop (low <= stop: fill at min(stop, open)); the target (high >= target: fill at
  the target); the strategy's own SELL (fill at the next bar's open). Every exit fill gives up
  half the spread.
* One position per symbol; a BUY on the exit bar may fill at the next open.
* A trade still open at the period's last bar closes at its close (``end_of_data``, or
  ``data_break`` when a corporate-action break ends the segment first).

Costs: ``with_etoro_trade_fees(CostModel(), crypto=False)``. Spread 10 bps round trip (in the
fills), $1 a leg, financing 0.015% of the entry notional per calendar day (exit date minus entry
date) with every Friday crossed (entry date < Friday <= exit date) charged three times.
``parity.py`` checks these against the real engine and cost model.
"""

from __future__ import annotations

import math

import numpy as np

HALF_SPREAD = 0.0005  # CostModel().spread_bps 10 / 2
FEE_PER_LEG = 1.0  # ETORO_STOCK_FEE_USD_PER_LEG
FINANCING_DAILY = 0.00015  # CostModel().overnight_fee_daily_pct
WEEKEND_EXTRA = 2.0  # weekend_multiplier 3 - 1
RISK_USD = 100.0
CASH_USD = 10_000.0
MIN_NOTIONAL = 50.0

TRADE_FIELDS = (
    "signal_idx", "entry_idx", "exit_idx", "reason", "r_net", "r_gross", "spread_r", "fee_r",
    "fin_r", "notional", "risk_pct", "gap", "entry_open", "exit_raw",
)


def fridays_through(day: int) -> int:
    """Fridays on or before epoch day ``day`` (1970-01-02, epoch day 1, was a Friday)."""

    return (day + 6) // 7


def costed(entry_open: float, stop: float, raw_exit: float, entry_day: int, exit_day: int
           ) -> tuple[float, float, float, float, float, float] | None:
    """(r_net, r_gross, spread_r, fee_r, fin_r, notional) of one trade, or None when the
    engine would not enter (zero stop distance or a notional under $50)."""

    fill = entry_open * (1.0 + HALF_SPREAD)
    distance = abs(fill - stop)
    if distance <= 0.0:
        return None
    quantity = RISK_USD / distance
    if quantity * fill > CASH_USD:
        quantity = CASH_USD / fill
    notional = quantity * fill
    if notional < MIN_NOTIONAL:
        return None
    exit_fill = raw_exit * (1.0 - HALF_SPREAD)
    days = exit_day - entry_day
    financing = 0.0
    if days > 0:
        crossings = fridays_through(exit_day) - fridays_through(entry_day)
        financing = notional * FINANCING_DAILY * (days + crossings * WEEKEND_EXTRA)
    fees = 2.0 * FEE_PER_LEG
    gross = quantity * (raw_exit - entry_open)
    spread = quantity * (fill - entry_open) + quantity * (raw_exit - exit_fill)
    net = gross - spread - financing - fees
    return net / RISK_USD, gross / RISK_USD, spread / RISK_USD, fees / RISK_USD, financing / RISK_USD, notional


def exit_path(o: list, h: list, lo: list, c: list, sell: list, entry: int, stop: float, target: float | None,
              max_hold: int | None, last: int, break_end: bool) -> tuple[int, float, str]:
    """(exit bar index whose date the engine records, raw exit price, reason)."""

    k = entry
    while k <= last:
        if max_hold is not None and k - entry >= max_hold:
            return k, o[k], "time_stop"
        if lo[k] <= stop:
            return k, (stop if stop < o[k] else o[k]), "stop_loss"
        if target is not None and h[k] >= target:
            return k, target, "take_profit"
        if sell[k] and k + 1 <= last:
            return k + 1, o[k + 1], "strategy_exit"
        k += 1
    return last, c[last], "data_break" if break_end else "end_of_data"


class Segment:
    """One segment's arrays as Python lists (fast scalar access in the path loop)."""

    def __init__(self, bars, signals: dict[str, np.ndarray], target: np.ndarray, last: int, break_end: bool):
        frame = bars.frame
        self.o = frame["open"].to_numpy(float).tolist()
        self.h = frame["high"].to_numpy(float).tolist()
        self.lo = frame["low"].to_numpy(float).tolist()
        self.c = frame["close"].to_numpy(float).tolist()
        self.day = bars.date.astype("int64").tolist()
        self.close_day = bars.close_date
        self.sell = signals["sell"].tolist()
        self.buy = signals["buy"]
        self.price = signals["price"]
        self.stop = signals["stop"]
        self.target = target
        self.last = last
        self.break_end = break_end


def run_trade(seg: Segment, s: int, stop: float, target: float | None, max_hold: int | None) -> tuple | None:
    e = s + 1
    k, raw, reason = exit_path(seg.o, seg.h, seg.lo, seg.c, seg.sell, e, stop, target, max_hold, seg.last,
                               seg.break_end)
    result = costed(seg.o[e], stop, raw, seg.day[e], seg.day[k])
    if result is None:
        return None
    net, gross, spread, fee, fin, notional = result
    fill = seg.o[e] * (1.0 + HALF_SPREAD)
    gap = "below_stop" if fill <= stop else ("above_target" if target is not None and fill >= target else "")
    return (s, e, k, reason, net, gross, spread, fee, fin, notional, abs(fill - stop) / fill, gap, seg.o[e], raw)


def chain(seg: Segment, allowed: np.ndarray, max_hold: int | None) -> list[tuple]:
    """Walk the segment's BUYs in order, one position at a time (the engine's loop)."""

    trades = []
    free_from = -1
    for s in np.flatnonzero(seg.buy & allowed).tolist():
        if s < free_from or s + 1 > seg.last:
            continue
        target = seg.target[s]
        trade = run_trade(seg, s, float(seg.stop[s]), None if math.isnan(target) else float(target), max_hold)
        if trade is None:
            continue
        trades.append(trade)
        free_from = trade[2]
    return trades


def control(seg_pool: list[tuple[Segment, np.ndarray]], year_of: list[np.ndarray], trade_year: int, price: float,
            stop: float, target: float | None, max_hold: int | None, draws: int, rng: np.random.Generator
            ) -> tuple[float, float, float, int]:
    """Mean (net, gross, no-financing) R of ``draws`` random entries in the same symbol and
    calendar year, with the trade's stop distance (fraction of the signal close), target
    multiple and time stop. ``seg_pool`` pairs each segment with its eligible signal bars."""

    stop_frac = (price - stop) / price
    multiple = None if target is None else (target - price) / (price - stop)
    candidates = []
    for (seg, eligible), years in zip(seg_pool, year_of, strict=True):
        bars = eligible[years[eligible] == trade_year]
        if len(bars):
            candidates.append((seg, bars))
    if not candidates:
        return math.nan, math.nan, math.nan, 0
    sizes = np.array([len(bars) for _, bars in candidates])
    picks = rng.integers(0, sizes.sum(), size=draws)
    offsets = np.concatenate([[0], np.cumsum(sizes)])
    nets, grosses, nofins = [], [], []
    for pick in picks.tolist():
        which = int(np.searchsorted(offsets, pick, side="right") - 1)
        seg, bars = candidates[which]
        r = int(bars[pick - offsets[which]])
        close = seg.c[r]
        r_stop = close * (1.0 - stop_frac)
        r_target = None if multiple is None else close + multiple * (close - r_stop)
        trade = run_trade(seg, r, r_stop, r_target, max_hold)
        if trade is None:
            continue
        nets.append(trade[4])
        grosses.append(trade[5])
        nofins.append(trade[4] + trade[8])
    if not nets:
        return math.nan, math.nan, math.nan, 0
    return float(np.mean(nets)), float(np.mean(grosses)), float(np.mean(nofins)), len(nets)
