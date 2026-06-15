"""Base strategy contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from math import isfinite
from typing import Any

import pandas as pd

from app.models.signal import Signal, SignalAction


class BaseStrategy(ABC):
    """Strategy interface."""

    name: str = "base"
    required_bars: int = 20

    @abstractmethod
    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        """Generate a signal from normalized OHLCV data."""

    def _ensure_length(self, data: pd.DataFrame) -> bool:
        return len(data) >= self.required_bars

    def _build_signal(self, **values: Any) -> Signal | None:
        """Build a signal only when its trade-plan prices are valid."""

        if not valid_trade_plan(
            action=values.get("action"),
            price=values.get("price"),
            stop_loss=values.get("stop_loss"),
            take_profit=values.get("take_profit"),
        ):
            return None
        return Signal(**values)


def valid_trade_plan(
    *,
    action: SignalAction | str | None,
    price: Any,
    stop_loss: Any,
    take_profit: Any,
) -> bool:
    """Return whether optional stop and target prices form a valid plan."""

    numeric: dict[str, float | None] = {}
    for name, raw in {"price": price, "stop_loss": stop_loss, "take_profit": take_profit}.items():
        if raw is None:
            numeric[name] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False
        if not isfinite(value) or value <= 0:
            return False
        numeric[name] = value
    entry = numeric["price"]
    stop = numeric["stop_loss"]
    target = numeric["take_profit"]
    if entry is None or stop is None or target is None:
        return True
    normalized_action = action.value if isinstance(action, SignalAction) else str(action or "").lower()
    if normalized_action == SignalAction.BUY.value:
        return stop < entry < target
    if normalized_action == SignalAction.SELL.value:
        return target < entry < stop
    return True
