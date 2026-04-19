"""Conservative momentum strategy for GOLD."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class GoldMomentumStrategy(BaseStrategy):
    """Use medium-term breakout confirmation for GOLD."""

    name = "gold_momentum"
    required_bars = 25

    def __init__(self, breakout_window: int = 15, trend_window: int = 20):
        self.breakout_window = breakout_window
        self.trend_window = trend_window
        self.required_bars = max(breakout_window, trend_window) + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["breakout_high"] = frame["high"].rolling(self.breakout_window).max().shift(1)
        frame["trend_ma"] = frame["close"].rolling(self.trend_window).mean()
        frame["mom_5"] = frame["close"].pct_change(5)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]

        if pd.isna(last["breakout_high"]) or pd.isna(last["trend_ma"]):
            return None

        breakout = last["close"] > last["breakout_high"]
        uptrend = last["close"] > last["trend_ma"] and last["mom_5"] > 0
        if breakout and uptrend:
            confidence = min(float(last["mom_5"]) * 5, 0.85)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Gold closed above its prior breakout range with positive medium-term momentum.",
                confidence=max(confidence, 0.35),
                price=float(last["close"]),
                stop_loss=float(last["trend_ma"] * 0.985),
                take_profit=float(last["close"] * 1.08),
                metadata={
                    "breakout_high": float(last["breakout_high"]),
                    "momentum_5": float(last["mom_5"]),
                },
            )

        if last["close"] < last["trend_ma"] and prev["close"] >= prev["trend_ma"]:
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Gold lost its medium-term trend support after momentum faded.",
                confidence=0.45,
                price=float(last["close"]),
                metadata={"trend_ma": float(last["trend_ma"])},
            )

        return None
