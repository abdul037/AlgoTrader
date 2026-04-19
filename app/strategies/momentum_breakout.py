"""Momentum breakout strategy for daily and hourly scans."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class MomentumBreakoutStrategy(BaseStrategy):
    """Trade range expansion backed by trend and relative volume."""

    name = "momentum_breakout"
    required_bars = 40

    def __init__(self, breakout_window: int = 20, volume_window: int = 20):
        self.breakout_window = breakout_window
        self.volume_window = volume_window
        self.required_bars = max(breakout_window, volume_window) + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["breakout_high"] = frame["high"].rolling(self.breakout_window).max().shift(1)
        frame["breakdown_low"] = frame["low"].rolling(self.breakout_window).min().shift(1)
        frame["ema_fast"] = frame["close"].ewm(span=10, adjust=False).mean()
        frame["ema_slow"] = frame["close"].ewm(span=30, adjust=False).mean()
        frame["avg_volume"] = frame["volume"].rolling(self.volume_window).mean()
        frame["tr"] = (frame["high"] - frame["low"]).abs()
        frame["atr"] = frame["tr"].rolling(14).mean()
        last = frame.iloc[-1]

        if pd.isna(last["breakout_high"]) or pd.isna(last["breakdown_low"]) or pd.isna(last["avg_volume"]):
            return None

        bullish = (
            last["close"] > last["breakout_high"]
            and last["ema_fast"] > last["ema_slow"]
            and last["volume"] >= last["avg_volume"] * 1.1
        )
        bearish = (
            last["close"] < last["breakdown_low"]
            and last["ema_fast"] < last["ema_slow"]
            and last["volume"] >= last["avg_volume"] * 1.1
        )
        atr = float(last["atr"] or max(last["close"] * 0.015, 0.01))

        if bullish:
            entry = float(last["close"])
            stop = float(entry - max(atr * 1.4, entry * 0.012))
            risk = max(entry - stop, 0.01)
            target = entry + (risk * 2.5)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Price broke above the recent range with trend and volume confirmation.",
                confidence=0.72,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "momentum_breakout",
                    "signal_role": "entry_long",
                    "breakout_high": float(last["breakout_high"]),
                    "average_volume": float(last["avg_volume"]),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        if bearish:
            entry = float(last["close"])
            stop = float(entry + max(atr * 1.4, entry * 0.012))
            risk = max(stop - entry, 0.01)
            target = entry - (risk * 2.3)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Price broke below the recent range with trend and volume confirmation.",
                confidence=0.7,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "momentum_breakout",
                    "signal_role": "entry_short",
                    "breakdown_low": float(last["breakdown_low"]),
                    "average_volume": float(last["avg_volume"]),
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                },
            )

        return None
