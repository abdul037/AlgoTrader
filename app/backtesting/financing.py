"""Pure financing helpers for realistic backtest carrying costs."""

from __future__ import annotations


def overnight_financing_cost(
    position_notional: float,
    days_held: int,
    annual_rate: float = 0.07,
) -> float:
    if days_held <= 0:
        return 0.0
    return position_notional * annual_rate * (days_held / 365.0)
