"""Trend-following pullback strategy for equities."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class PullbackTrendStrategy(BaseStrategy):
    """Look for bullish pullbacks inside a broader uptrend."""

    name = "pullback_trend"
    required_bars = 35

    def __init__(self, trend_window: int = 30, pullback_window: int = 10):
        self.trend_window = trend_window
        self.pullback_window = pullback_window
        self.required_bars = max(trend_window, pullback_window) + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["trend_ma"] = frame["close"].rolling(self.trend_window).mean()
        frame["pullback_ma"] = frame["close"].rolling(self.pullback_window).mean()
        frame["ema_short"] = frame["close"].ewm(span=8, adjust=False).mean()
        frame["ema_long"] = frame["close"].ewm(span=21, adjust=False).mean()

        last = frame.iloc[-1]
        prev = frame.iloc[-2]

        trend_up = (
            last["close"] > last["trend_ma"]
            and last["ema_short"] > last["ema_long"]
            and last["trend_ma"] > frame["trend_ma"].iloc[-5]
        )
        pullback_active = prev["close"] <= prev["pullback_ma"] * 1.01
        resuming_higher = last["close"] > last["pullback_ma"] and last["close"] > prev["close"]

        if trend_up and pullback_active and resuming_higher:
            distance = max(last["close"] - last["trend_ma"], 0.01)
            confidence = min(distance / last["close"] * 8, 0.9)
            entry = float(last["close"])
            stop = float(last["trend_ma"] * 0.98)
            target = float(last["close"] * 1.12)
            risk = max(entry - stop, 0.01)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale=(
                    "Broad trend remains positive and price is resuming higher after "
                    "a controlled pullback toward the short-term average."
                ),
                confidence=confidence,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "swing",
                    "signal_role": "entry_long",
                    "trend_ma": float(last["trend_ma"]),
                    "pullback_ma": float(last["pullback_ma"]),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        trend_broken = last["close"] < last["trend_ma"] or last["ema_short"] < last["ema_long"]
        if trend_broken:
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Trend filter failed, so existing long exposure should be reduced or closed.",
                confidence=0.5,
                price=float(last["close"]),
                metadata={
                    "style": "trend_following",
                    "signal_role": "entry_short_watch",
                    "trend_ma": float(last["trend_ma"]),
                    "ema_short": float(last["ema_short"]),
                    "ema_long": float(last["ema_long"]),
                },
            )

        return None
