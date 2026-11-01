"""Portfolio simulation (pre-registered).

$100,000 start; each entry risks 0.5% of the equity marked at the signal day's close
(shares = risk / (fill - stop)), notional capped at 25% of that equity and at the cash on hand
(no leverage); at most 12 open positions and one per symbol; V6 adds at most 2 open per GICS
sector. When a day has more signals than free slots, the highest signal-day relative volume
(volume / 20-day average, the strategy's own ``relative_volume``) goes first, ties by symbol.

Order of events on a day: exits that fill at the open (gaps through a stop, trend and SELL
exits), then the day's entries at the open, then intrabar exits (stop / target) and the close
mark. Slots and sector counts for tomorrow's entries are taken after today's close, net of the
positions already known to exit at tomorrow's open. Costs are the per-trade model: 15 bps of the
entry notional when it opens and 15 bps when it closes (30 bps a round trip, as in the R
figures). Idle cash earns nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HALF_COST = 0.0015


@dataclass
class PortfolioConfig:
    start_equity: float = 100_000.0
    risk_fraction: float = 0.005
    notional_cap_fraction: float = 0.25
    max_positions: int = 12
    sector_cap: int | None = None
    min_notional: float = 100.0
    haircut_per_day: float = 0.0
    """Sensitivity only (not in the rule): a held position loses this log-return a trading day,
    the rough survivorship drift of today's members over the equal-weight index (see report)."""


@dataclass
class PortfolioResult:
    equity: pd.Series
    invested: pd.Series
    trades: pd.DataFrame
    skipped_no_slot: int = 0
    skipped_no_cash: int = 0
    notes: dict[str, float] = field(default_factory=dict)


def simulate(candidates: pd.DataFrame, calendar: pd.DatetimeIndex, closes: dict[str, np.ndarray],
             config: PortfolioConfig) -> PortfolioResult:
    """``candidates``: valid signal trades (``trades.signal_trades`` rows) the variant allows.
    ``closes[symbol]``: closes aligned to ``calendar`` (forward-filled) for the daily mark."""

    cal_pos = {pd.Timestamp(d): i for i, d in enumerate(calendar)}
    cands = candidates[candidates["valid"]].copy()
    cands["signal_c"] = [cal_pos.get(pd.Timestamp(d), -1) for d in cands["signal_date"]]
    cands["entry_c"] = [cal_pos.get(pd.Timestamp(d), -1) for d in cands["entry_date"]]
    cands["exit_c"] = [cal_pos.get(pd.Timestamp(d), -1) for d in cands["exit_date"]]
    cands = cands[(cands["signal_c"] >= 0) & (cands["entry_c"] >= 0) & (cands["exit_c"] >= 0)]
    cands = cands.sort_values(["signal_c", "relative_volume", "symbol"], ascending=[True, False, True])
    by_signal_day = {day: group for day, group in cands.groupby("signal_c", sort=False)}

    n_days = len(calendar)
    equity = np.empty(n_days)
    invested = np.empty(n_days)
    cash = config.start_equity
    equity_prev = config.start_equity
    positions: dict[str, dict] = {}
    pending: dict[int, list] = defaultdict(list)
    taken: list[dict] = []
    skipped_slot = skipped_cash = 0

    def close_position(symbol: str) -> None:
        nonlocal cash
        pos = positions.pop(symbol)
        proceeds = pos["shares"] * pos["exit"] * np.exp(-config.haircut_per_day * (pos["exit_c"] - pos["entry_c"]))
        cost = HALF_COST * pos["notional"]
        cash += proceeds - cost
        pos["record"]["pnl_usd"] = proceeds - pos["notional"] - 2 * HALF_COST * pos["notional"]

    for t in range(n_days):
        for symbol in [s for s, p in positions.items() if p["exit_c"] == t and p["exit_at_open"]]:
            close_position(symbol)
        for row in pending.pop(t, []):
            if row.symbol in positions:
                continue
            risk_per_share = row.entry - row.stop
            notional = config.risk_fraction * equity_prev / risk_per_share * row.entry
            notional = min(notional, config.notional_cap_fraction * equity_prev, cash / (1 + HALF_COST))
            if notional < config.min_notional:
                skipped_cash += 1
                continue
            shares = notional / row.entry
            cash -= notional * (1 + HALF_COST)
            record = {
                "symbol": row.symbol, "sector": row.sector, "signal_date": row.signal_date,
                "entry_date": row.entry_date, "exit_date": row.exit_date, "entry": row.entry,
                "stop": row.stop, "exit": row.exit, "reason": row.reason, "r_gross": row.r_gross,
                "r_net": row.r_net, "notional": notional, "risk_usd": shares * risk_per_share,
                "equity_at_signal": equity_prev,
            }
            taken.append(record)
            positions[row.symbol] = {
                "shares": shares, "notional": notional, "exit": row.exit, "exit_c": row.exit_c,
                "exit_at_open": row.exit_at_open, "sector": row.sector, "record": record,
                "entry_c": t,
            }
        for symbol in [s for s, p in positions.items() if p["exit_c"] == t]:
            close_position(symbol)
        value = sum(p["shares"] * closes[s][t] * np.exp(-config.haircut_per_day * (t - p["entry_c"]))
                    for s, p in positions.items())
        equity[t] = cash + value
        invested[t] = value
        equity_prev = equity[t]

        todays = by_signal_day.get(t)
        if todays is None:
            continue
        staying = {s: p for s, p in positions.items() if not (p["exit_c"] == t + 1 and p["exit_at_open"])}
        slots = config.max_positions - len(staying) - sum(len(v) for v in pending.values())
        sector_count: dict[str, int] = defaultdict(int)
        for p in staying.values():
            sector_count[p["sector"]] += 1
        for rows in pending.values():
            for r in rows:
                sector_count[r.sector] += 1
        for row in todays.itertuples():
            if row.symbol in positions or any(row.symbol == r.symbol for rows in pending.values() for r in rows):
                continue
            if slots <= 0:
                skipped_slot += 1
                continue
            if config.sector_cap is not None and sector_count[row.sector] >= config.sector_cap:
                skipped_slot += 1
                continue
            pending[row.entry_c].append(row)
            slots -= 1
            sector_count[row.sector] += 1

    # Positions still open at the end are already marked at the last close inside ``equity``;
    # their records carry the end-of-data exit computed by the trade path.
    for symbol in list(positions):
        close_position(symbol)
    trades = pd.DataFrame(taken)
    return PortfolioResult(
        equity=pd.Series(equity, index=calendar), invested=pd.Series(invested, index=calendar),
        trades=trades, skipped_no_slot=skipped_slot, skipped_no_cash=skipped_cash,
    )


def drawdown(series: pd.Series) -> float:
    peak = series.cummax()
    return float(((series / peak) - 1.0).min())


def metrics(equity: pd.Series, invested: pd.Series | None = None) -> dict[str, float]:
    days = (equity.index[-1] - equity.index[0]).days
    total = float(equity.iloc[-1] / equity.iloc[0])
    returns = equity.pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0
    out = {
        "total_return": total - 1.0,
        "cagr": total ** (365.25 / days) - 1.0 if days > 0 and total > 0 else -1.0,
        "max_drawdown": drawdown(equity),
        "sharpe": sharpe,
        "ann_vol": float(returns.std() * np.sqrt(252)),
    }
    if invested is not None:
        out["exposure"] = float((invested / equity).mean())
        out["days_invested"] = float((invested > 0).mean())
    return out


def constant_mix(asset: pd.Series, weight: float) -> pd.Series:
    """Daily-rebalanced ``weight`` in ``asset`` and the rest in cash at 0%."""

    returns = asset.pct_change().fillna(0.0)
    return (1.0 + weight * returns).cumprod() * 100_000.0


def mix_matching_drawdown(asset: pd.Series, target_drawdown: float) -> tuple[float, pd.Series]:
    """The constant-mix weight whose max drawdown equals ``target_drawdown`` (bisection)."""

    if drawdown(asset) >= target_drawdown:  # the asset's own drawdown is already smaller
        return 1.0, constant_mix(asset, 1.0)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if drawdown(constant_mix(asset, mid)) < target_drawdown:
            hi = mid
        else:
            lo = mid
    return lo, constant_mix(asset, lo)
