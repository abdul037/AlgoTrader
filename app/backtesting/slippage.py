"""Pure slippage helpers for realistic backtest fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SlippageModel:
    """Estimate spread and impact slippage in basis points."""

    half_spread_bps_by_symbol: dict[str, float] = field(default_factory=dict)
    default_half_spread_bps: float = 5.0
    k: float = 1.0

    def slippage_bps(
        self,
        symbol: str,
        side: Literal["long", "short"],
        position_notional: float,
        avg_dollar_volume_30d: float,
        volatility_factor: float = 1.0,
    ) -> float:
        half_spread = self.half_spread_bps_by_symbol.get(symbol, self.default_half_spread_bps)
        if avg_dollar_volume_30d <= 0:
            return half_spread * 5.0
        impact_bps = self.k * (position_notional / avg_dollar_volume_30d) * volatility_factor * 10_000.0
        return max(half_spread, impact_bps)

    def adjust_fill(
        self,
        side: Literal["long", "short"],
        action: Literal["entry", "exit"],
        midpoint: float,
        slippage_bps: float,
    ) -> float:
        direction = 1 if (
            (side == "long" and action == "entry")
            or (side == "short" and action == "exit")
        ) else -1
        return midpoint * (1 + direction * slippage_bps / 10_000.0)
