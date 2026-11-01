"""Daily backtest of target-weight schedules, with trades at the open and fees per order.

A schedule maps an execution date to target weights (fund -> weight, the rest in cash). On an
execution date, the holdings at the previous close earn the overnight move to the open, the book
is traded to the targets at the open, and the new holdings earn the open-to-close move. Cash
earns the day's cash return from the open (its overnight part is zero).

Fees: each order in a fund costs ``fixed_fee`` dollars plus ``bps_fee`` of its notional. Fees are
paid out of equity at the trade, so the target weights apply to equity after fees. If an
order's fees would use up all the equity, the account is wiped out: equity is zero from that day
and ``meta["ruined"]`` holds the date.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EPS_DOLLARS = 1e-6


@dataclass
class Market:
    opens: pd.DataFrame  # adjusted opens, calendar x funds
    closes: pd.DataFrame  # adjusted closes, calendar x funds
    cash: pd.Series  # daily cash return on the calendar (T-bills)

    @property
    def calendar(self) -> pd.DatetimeIndex:
        return self.closes.index


@dataclass
class Costs:
    fixed_fee: float = 1.0
    bps_fee: float = 0.0002
    capital: float = 10_000.0
    cash_earns: bool = True  # False: idle cash earns 0% (Sharpe still measured against T-bills)

    def label(self) -> str:
        if self.fixed_fee == 0 and self.bps_fee == 0:
            return "zero cost"
        tail = "" if self.cash_earns else ", cash 0%"
        return f"${self.capital:,.0f}{tail}"


@dataclass
class Result:
    name: str
    equity: pd.Series  # daily closing equity; first value is the capital
    exposure: pd.Series  # share of equity in funds at each close
    weights: pd.DataFrame  # holdings' weights at each close
    orders: pd.DataFrame  # one row per order
    fees_paid: float
    meta: dict = field(default_factory=dict)


def run(
    market: Market,
    schedule: dict[pd.Timestamp, dict[str, float]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    costs: Costs,
    name: str,
    base_date: pd.Timestamp,
) -> Result:
    funds = list(market.closes.columns)
    col = {f: i for i, f in enumerate(funds)}
    cal = market.calendar[(market.calendar >= start) & (market.calendar <= end)]
    opens = market.opens.loc[cal].to_numpy()
    closes = market.closes.loc[cal].to_numpy()
    prev_close = market.closes.shift(1).loc[cal].to_numpy()
    cash_ret = market.cash.loc[cal].to_numpy() if costs.cash_earns else np.zeros(len(cal))

    holdings = np.zeros(len(funds))
    cash = costs.capital
    prev_target = np.zeros(len(funds))
    equity = np.empty(len(cal))
    exposure = np.empty(len(cal))
    weights = np.zeros((len(cal), len(funds)))
    orders: list[tuple] = []
    fees_paid = 0.0
    ruined = None

    for i, day in enumerate(cal):
        if ruined is not None:
            equity[i], exposure[i] = 0.0, 0.0
            continue
        if i > 0:
            held = holdings != 0
            holdings[held] *= opens[i, held] / prev_close[i, held]
        target_map = schedule.get(day)
        if target_map is not None:
            target = np.zeros(len(funds))
            for fund, w in target_map.items():
                target[col[fund]] = w
            if np.isnan(opens[i][target > 0]).any():
                raise ValueError(f"{name}: no open for a target fund on {day.date()}")
            book = holdings.sum() + cash
            fees = 0.0
            for _ in range(4):  # fixed point: targets on equity after fees
                desired = target * (book - fees)
                delta = desired - holdings
                traded = np.abs(delta) > EPS_DOLLARS
                new_fees = (
                    costs.fixed_fee * traded.sum() + costs.bps_fee * np.abs(delta[traded]).sum()
                )
                if abs(new_fees - fees) < 1e-9:
                    break
                fees = new_fees
            fees = new_fees
            if book - fees <= 0:
                ruined = day
                fees_paid += book
                equity[i], exposure[i] = 0.0, 0.0
                continue
            desired = target * (book - fees)
            delta = desired - holdings
            traded = np.abs(delta) > EPS_DOLLARS
            for j in np.flatnonzero(traded):
                kind = "signal" if (prev_target[j] > 0) != (target[j] > 0) else "rebalance"
                orders.append(
                    (day, funds[j], "buy" if delta[j] > 0 else "sell", abs(delta[j]), kind)
                )
            cash = book - fees - desired.sum()
            holdings = desired
            fees_paid += fees
            prev_target = target
        held = holdings != 0
        holdings[held] *= closes[i, held] / opens[i, held]
        cash *= 1.0 + cash_ret[i]
        total = holdings.sum() + cash
        equity[i] = total
        exposure[i] = holdings.sum() / total
        weights[i] = holdings / total

    eq = pd.Series(
        np.concatenate([[costs.capital], equity]),
        index=pd.DatetimeIndex([base_date]).append(cal),
        name=name,
    )
    return Result(
        name=name,
        equity=eq,
        exposure=pd.Series(exposure, index=cal),
        weights=pd.DataFrame(weights, index=cal, columns=funds),
        orders=pd.DataFrame(orders, columns=["date", "fund", "side", "notional", "kind"]),
        fees_paid=fees_paid,
        meta={"ruined": None if ruined is None else str(ruined.date())},
    )
