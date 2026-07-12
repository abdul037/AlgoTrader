"""Momentum breakout strategy for daily and hourly scans."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy
from app.strategies.weak_signals import build_supervised_weak_long_signal


class MomentumBreakoutStrategy(BaseStrategy):
    """Trade range expansion backed by trend and relative volume."""

    name = "momentum_breakout"
    required_bars = 40

    def __init__(self, breakout_window: int = 20, volume_window: int = 20):
        self.breakout_window = breakout_window
        self.volume_window = volume_window
        self.required_bars = max(breakout_window, volume_window) + 5
        self.last_diagnostics: dict[str, object] | None = None

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        self.last_diagnostics = None
        if not self._ensure_length(data):
            self.last_diagnostics = {"status": "no_signal", "rejection_reasons": ["insufficient_data"]}
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
            self.last_diagnostics = {"status": "no_signal", "rejection_reasons": ["indicator_unavailable"]}
            return None

        long_anchor = last["close"] > last["breakout_high"] and last["ema_fast"] > last["ema_slow"]
        long_volume_ok = last["volume"] >= last["avg_volume"] * 1.1
        bullish = (
            long_anchor
            and long_volume_ok
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
            return self._build_signal(
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

        if long_anchor:
            entry = float(last["close"])
            stop = float(entry - max(atr * 1.4, entry * 0.012))
            risk = max(entry - stop, 0.01)
            target = entry + (risk * 1.2)
            reasons = ["relative_volume_too_low"] if not long_volume_ok else ["confirmation_too_weak"]
            weak = build_supervised_weak_long_signal(
                self,
                symbol=symbol,
                price=entry,
                stop=stop,
                risk_multiple=round((target - entry) / risk, 4),
                rationale="Supervised weak-valid momentum breakout with real range break but incomplete volume confirmation.",
                confidence=0.50,
                metadata={
                    "style": "momentum_breakout",
                    "signal_role": "entry_long",
                    "setup_type": "momentum_breakout",
                    "breakout_high": float(last["breakout_high"]),
                    "average_volume": float(last["avg_volume"]),
                    "weak_signal_kind": "range_breakout_anchor",
                },
                rejection_reasons=reasons,
                setup_anchor=True,
            )
            if weak is not None:
                return weak

        if bearish:
            entry = float(last["close"])
            stop = float(entry + max(atr * 1.4, entry * 0.012))
            risk = max(stop - entry, 0.01)
            target = entry - (risk * 2.3)
            return self._build_signal(
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

        self.last_diagnostics = {
            "status": "no_signal",
            "rejection_reasons": ["breakout_level_not_cleared"],
            "reason_codes": ["breakout_level_not_cleared"],
            "score": 44.0,
            "measurements": {
                "breakout_high": float(last["breakout_high"]),
                "breakdown_low": float(last["breakdown_low"]),
                "average_volume": float(last["avg_volume"]),
            },
        }
        return None
