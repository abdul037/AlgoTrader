"""Mean reversion strategy for stretched moves."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Fade statistically stretched moves back toward the mean."""

    name = "mean_reversion"
    required_bars = 30

    def __init__(self, lookback: int = 20, zscore_threshold: float = 1.8):
        self.lookback = lookback
        self.zscore_threshold = zscore_threshold
        self.required_bars = lookback + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy()
        frame["mean"] = frame["close"].rolling(self.lookback).mean()
        frame["std"] = frame["close"].rolling(self.lookback).std()
        frame["zscore"] = (frame["close"] - frame["mean"]) / frame["std"].replace(0, pd.NA)
        frame["trend_ma"] = frame["close"].rolling(self.lookback * 2).mean()
        frame["recent_low"] = frame["low"].rolling(5).min()
        frame["recent_high"] = frame["high"].rolling(5).max()
        last = frame.iloc[-1]

        if pd.isna(last["zscore"]) or pd.isna(last["trend_ma"]):
            return None

        if last["zscore"] <= -self.zscore_threshold and last["close"] >= last["trend_ma"]:
            entry = float(last["close"])
            stop = float(last["recent_low"] * 0.99)
            risk = max(entry - stop, entry * 0.01, 0.01)
            target = float(last["mean"])
            if target <= entry:
                target = entry + (risk * 1.8)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Price is stretched below its rolling mean while the higher-timeframe trend still points up.",
                confidence=0.58,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "mean_reversion",
                    "signal_role": "entry_long",
                    "zscore": round(float(last["zscore"]), 3),
                    "rolling_mean": float(last["mean"]),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        if last["zscore"] >= self.zscore_threshold and last["close"] <= last["trend_ma"]:
            entry = float(last["close"])
            stop = float(last["recent_high"] * 1.01)
            risk = max(stop - entry, entry * 0.01, 0.01)
            target = float(last["mean"])
            if target >= entry:
                target = entry - (risk * 1.8)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Price is stretched above its rolling mean while the higher-timeframe trend is soft.",
                confidence=0.56,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "mean_reversion",
                    "signal_role": "entry_short",
                    "zscore": round(float(last["zscore"]), 3),
                    "rolling_mean": float(last["mean"]),
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                },
            )

        return None
