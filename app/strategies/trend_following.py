"""Trend-following strategy for swing and hourly scans."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class TrendFollowingStrategy(BaseStrategy):
    """Trade pullbacks inside established EMA trends."""

    name = "trend_following"
    required_bars = 60

    def __init__(self, fast_span: int = 20, slow_span: int = 50, pullback_window: int = 5):
        self.fast_span = fast_span
        self.slow_span = slow_span
        self.pullback_window = pullback_window
        self.required_bars = max(fast_span, slow_span) + pullback_window + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["ema_fast"] = frame["close"].ewm(span=self.fast_span, adjust=False).mean()
        frame["ema_slow"] = frame["close"].ewm(span=self.slow_span, adjust=False).mean()
        frame["swing_low"] = frame["low"].rolling(self.pullback_window).min()
        frame["swing_high"] = frame["high"].rolling(self.pullback_window).max()

        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        trend_up = last["close"] > last["ema_fast"] > last["ema_slow"]
        trend_down = last["close"] < last["ema_fast"] < last["ema_slow"]
        reclaimed_fast = prev["close"] <= prev["ema_fast"] and last["close"] > last["ema_fast"]
        lost_fast = prev["close"] >= prev["ema_fast"] and last["close"] < last["ema_fast"]

        if trend_up and reclaimed_fast:
            entry = float(last["close"])
            stop = float(min(last["swing_low"], last["ema_slow"]))
            risk = max(entry - stop, entry * 0.01, 0.01)
            target = entry + (risk * 2.2)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Price reclaimed the fast EMA while the broader EMA trend remains positive.",
                confidence=0.67,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "trend_following",
                    "signal_role": "entry_long",
                    "ema_fast": float(last["ema_fast"]),
                    "ema_slow": float(last["ema_slow"]),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        if trend_down and lost_fast:
            entry = float(last["close"])
            stop = float(max(last["swing_high"], last["ema_slow"]))
            risk = max(stop - entry, entry * 0.01, 0.01)
            target = entry - (risk * 2.0)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Price lost the fast EMA inside a confirmed downtrend; short entry watch is active.",
                confidence=0.64,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "trend_following",
                    "signal_role": "entry_short",
                    "ema_fast": float(last["ema_fast"]),
                    "ema_slow": float(last["ema_slow"]),
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                },
            )

        return None
