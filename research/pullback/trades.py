"""Per-signal trade paths for every exit rule, and the one-position-per-symbol chain.

Execution model (pre-registered):

* A BUY on bar N's close enters at bar N+1's open (gaps included). The stop and target are the
  signal's price levels; R is measured from the actual fill: R = (exit - entry) / (entry - stop).
* A fill at or below the stop, or at or above the target, has no valid long bracket (a broker
  rejects it); the signal is skipped and counted (``invalid_bracket``).
* Stop and target are checked intrabar from the fill bar on; when one bar touches both, the stop
  wins. A gap through the stop fills at the open. A gap over the target fills at the target
  (the backtest engine's convention; conservative).
* A trade still open on the last bar closes at its close (``end_of_data``). A trade open across a
  corporate-action break (see ``data.clean_symbol``) closes at the last close before it
  (``data_break``) and no signal is taken in the 120 bars from the break.
* Costs: $1 a side on a $1,000 position plus a 10 bps round-trip spread = 30 bps of the entry
  notional per round trip, i.e. ``0.003 * entry / (entry - stop)`` R a trade.

Exit rules: ``bracket`` (V1, the bot), ``breakeven`` (V4: from the bar after the high first
reaches entry + 1R, the stop is the entry price), ``trend`` (V5: no target; exit at the next
open after a close below the 20 EMA; the initial stop still applies), ``engine`` (calibration
only: V1 plus the backtest engine's exit at the next open on the strategy's own SELL signal).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ROUND_TRIP_COST = 0.0030  # 2 x $1 on $1,000 (20 bps) + 10 bps spread
EXIT_RULES = ("bracket", "breakeven", "trend", "engine")
BREAK_WARMUP_BARS = 120


@dataclass(frozen=True)
class SymbolBars:
    symbol: str
    sector: str
    dates: np.ndarray  # datetime64[ns]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema_20: np.ndarray
    buy: np.ndarray
    sell: np.ndarray
    stop: np.ndarray
    target: np.ndarray
    relative_volume: np.ndarray
    setup: np.ndarray
    breaks: np.ndarray  # bar indices where a corporate-action break lands


def from_signal_frame(symbol: str, sector: str, frame: pd.DataFrame, break_dates: list[pd.Timestamp]) -> SymbolBars:
    dates = pd.to_datetime(frame["timestamp"]).dt.tz_localize(None).to_numpy()
    index_of = {pd.Timestamp(d): i for i, d in enumerate(dates)}
    breaks = np.array(sorted(index_of[d] for d in break_dates if d in index_of), dtype=int)
    return SymbolBars(
        symbol=symbol, sector=sector, dates=dates,
        open=frame["open"].to_numpy(float), high=frame["high"].to_numpy(float),
        low=frame["low"].to_numpy(float), close=frame["close"].to_numpy(float),
        ema_20=frame["ema_20"].to_numpy(float), buy=frame["buy"].to_numpy(bool),
        sell=frame["sell"].to_numpy(bool), stop=frame["stop"].to_numpy(float),
        target=frame["target"].to_numpy(float), relative_volume=frame["relative_volume"].to_numpy(float),
        setup=frame["setup"].to_numpy(object), breaks=breaks,
    )


def _exit_path(bars: SymbolBars, entry_idx: int, entry: float, stop: float, target: float, rule: str,
               last_idx: int) -> tuple[int, float, str, bool]:
    """(exit bar index, exit price, reason, filled at that bar's open)."""

    risk = entry - stop
    current_stop = stop
    use_target = rule != "trend"
    o, h, lo, c = bars.open, bars.high, bars.low, bars.close
    for k in range(entry_idx, last_idx + 1):
        if lo[k] <= current_stop:
            if o[k] < current_stop:
                return k, float(o[k]), "stop" if current_stop == stop else "breakeven_stop", True
            return k, float(current_stop), "stop" if current_stop == stop else "breakeven_stop", False
        if use_target and h[k] >= target:
            return k, float(target), "target", False
        if rule == "breakeven" and current_stop < entry and h[k] >= entry + risk:
            current_stop = entry  # effective from the next bar
        if rule == "trend" and c[k] < bars.ema_20[k]:
            if k + 1 <= last_idx:
                return k + 1, float(o[k + 1]), "trend_exit", True
            return k, float(c[k]), "end_of_data" if last_idx == len(c) - 1 else "data_break", False
        if rule == "engine" and bars.sell[k]:
            if k + 1 <= last_idx:
                return k + 1, float(o[k + 1]), "sell_signal", True
            return k, float(c[k]), "end_of_data" if last_idx == len(c) - 1 else "data_break", False
    reason = "end_of_data" if last_idx == len(c) - 1 else "data_break"
    return last_idx, float(c[last_idx]), reason, False


def signal_trades(bars: SymbolBars, rule: str, *, eval_start: np.datetime64) -> pd.DataFrame:
    """Every BUY signal (from ``eval_start``) as the trade it would be if taken."""

    n = len(bars.close)
    rows = []
    blocked = np.zeros(n, dtype=bool)
    for b in bars.breaks:
        blocked[b : b + BREAK_WARMUP_BARS] = True
    for s in np.flatnonzero(bars.buy):
        if bars.dates[s] < eval_start or s + 1 >= n or blocked[s]:
            continue
        e = s + 1
        entry, stop, target = float(bars.open[e]), float(bars.stop[s]), float(bars.target[s])
        later_breaks = bars.breaks[bars.breaks > s]
        last_idx = int(later_breaks[0]) - 1 if len(later_breaks) else n - 1
        if e > last_idx:
            continue  # the fill bar is itself across a break
        record = {
            "symbol": bars.symbol, "sector": bars.sector, "signal_idx": int(s),
            "signal_date": bars.dates[s], "entry_date": bars.dates[e], "entry": entry,
            "stop": stop, "target": target, "signal_close": float(bars.close[s]),
            "relative_volume": float(bars.relative_volume[s]) if np.isfinite(bars.relative_volume[s]) else 0.0,
            "setup": bars.setup[s],
        }
        if not (stop < entry < target):
            record.update({"valid": False, "exit_idx": s, "exit_date": bars.dates[s], "exit": np.nan,
                           "reason": "invalid_bracket", "exit_at_open": False})
            rows.append(record)
            continue
        k, price, reason, at_open = _exit_path(bars, e, entry, stop, target, rule, last_idx)
        record.update({"valid": True, "exit_idx": int(k), "exit_date": bars.dates[k], "exit": price,
                       "reason": reason, "exit_at_open": at_open})
        rows.append(record)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    risk = frame["entry"] - frame["stop"]
    frame["risk_pct"] = risk / frame["entry"]
    frame["r_gross"] = (frame["exit"] - frame["entry"]) / risk
    frame["cost_r"] = ROUND_TRIP_COST * frame["entry"] / risk
    frame["r_net"] = frame["r_gross"] - frame["cost_r"]
    frame["bars_held"] = frame["exit_idx"] - frame["signal_idx"]
    return frame


def chain(candidates: pd.DataFrame, allowed: pd.Series | None = None) -> pd.DataFrame:
    """One open position per symbol: walk each symbol's valid signals in order and take one
    only when the previous taken trade has exited on or before the signal bar."""

    if candidates.empty:
        return candidates
    pool = candidates[candidates["valid"]]
    if allowed is not None:
        pool = pool[allowed.reindex(pool.index).fillna(False).to_numpy(bool)]
    taken = []
    for _symbol, group in pool.groupby("symbol", sort=False):
        free_from = -1
        for row in group.sort_values("signal_idx").itertuples():
            if row.signal_idx >= free_from:
                taken.append(row.Index)
                free_from = row.exit_idx
    return pool.loc[sorted(taken)]
