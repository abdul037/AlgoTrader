"""Dynamic liquidity-filtered execution universe construction."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LiquiditySnapshot(BaseModel):
    symbol: str
    price: float = Field(gt=0.0)
    average_volume: float = Field(ge=0.0)
    average_dollar_volume: float = Field(ge=0.0)
    spread_bps: float = Field(ge=0.0)
    tradable: bool = True


def build_liquidity_universe(
    snapshots: list[LiquiditySnapshot],
    *,
    min_price: float,
    min_average_volume: float,
    min_average_dollar_volume: float,
    max_spread_bps: float,
    limit: int,
) -> list[str]:
    """Return the most liquid currently tradable symbols passing execution filters."""

    eligible = [
        item
        for item in snapshots
        if item.tradable
        and item.price >= min_price
        and item.average_volume >= min_average_volume
        and item.average_dollar_volume >= min_average_dollar_volume
        and item.spread_bps <= max_spread_bps
    ]
    ranked = sorted(
        eligible,
        key=lambda item: (item.average_dollar_volume, item.average_volume),
        reverse=True,
    )
    return [item.symbol.upper() for item in ranked[: max(limit, 0)]]
