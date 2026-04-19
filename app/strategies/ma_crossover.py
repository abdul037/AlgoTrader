"""Moving average crossover strategy."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    """Simple swing-oriented moving average crossover strategy."""

    name = "ma_crossover"

    def __init__(self, fast_window: int = 5, slow_window: int = 20):
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.required_bars = slow_window + 2

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["fast_ma"] = frame["close"].rolling(self.fast_window).mean()
        frame["slow_ma"] = frame["close"].rolling(self.slow_window).mean()
        last = frame.iloc[-1]
        previous = frame.iloc[-2]

        if pd.isna(last["fast_ma"]) or pd.isna(last["slow_ma"]):
            return None

        fast_above = last["fast_ma"] > last["slow_ma"]
        was_below = previous["fast_ma"] <= previous["slow_ma"]
        fast_below = last["fast_ma"] < last["slow_ma"]
        was_above = previous["fast_ma"] >= previous["slow_ma"]
        ma_gap = abs(last["fast_ma"] - last["slow_ma"])
        confidence = min(ma_gap / max(last["close"], 1.0) * 10, 0.95)

        if fast_above and was_below:
            entry = float(last["close"])
            stop = float(last["close"] * 0.94)
            target = float(last["close"] * 1.10)
            risk = max(entry - stop, 0.01)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale=(
                    f"Fast MA ({last['fast_ma']:.2f}) crossed above slow MA "
                    f"({last['slow_ma']:.2f}) on the latest bar."
                ),
                confidence=confidence,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "swing",
                    "signal_role": "entry_long",
                    "fast_ma": float(last["fast_ma"]),
                    "slow_ma": float(last["slow_ma"]),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        if fast_below and was_above:
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale=(
                    f"Fast MA ({last['fast_ma']:.2f}) crossed below slow MA "
                    f"({last['slow_ma']:.2f}); trend support weakened."
                ),
                confidence=confidence,
                price=float(last["close"]),
                metadata={
                    "style": "trend_following",
                    "signal_role": "entry_short_watch",
                    "fast_ma": float(last["fast_ma"]),
                    "slow_ma": float(last["slow_ma"]),
                },
            )

        return None
