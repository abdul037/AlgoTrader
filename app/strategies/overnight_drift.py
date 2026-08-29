"""Overnight / close-to-open drift.

Captures the well-documented tendency of strong names in an uptrend to drift
higher into the next session. It buys a firm-closing name that is above its
200-EMA and holds it briefly, exiting via the engine's time-stop (max_hold_bars)
rather than a price target — the edge is the short holding-period drift, not a
big move.

The backtest engine fills entries at the next bar's open and honors
``metadata['max_hold_bars']`` for a time-based exit, so a max_hold_bars of 1
gives a one-bar hold — the closest the bar model expresses to a close-to-open
capture. Long-only; self-contained via ``app.indicators``.
"""

from __future__ import annotations

import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class OvernightDriftStrategy(BaseStrategy):
    """Buy a firm-closing uptrend name for a short (overnight) drift hold."""

    name = "overnight_drift"
    required_bars = 210

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        trend_ema: int = 200,
        min_close_location: float = 0.6,
        max_hold_bars: int = 1,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 1.0,
    ):
        self.timeframe = timeframe
        self.trend_ema = trend_ema
        self.min_close_location = min_close_location
        self.max_hold_bars = max_hold_bars
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]

        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        ema_trend = last.get("ema_200")
        atr = last.get("atr_14")
        if pd.isna(ema_trend) or pd.isna(atr):
            return None
        atr = max(float(atr), close * 0.005, 0.01)

        bar_range = max(high - low, 1e-9)
        close_location = (close - low) / bar_range  # 1.0 == closed on the high
        firm_close = close > float(last.get("open", close)) and close_location >= self.min_close_location
        in_uptrend = close > float(ema_trend)
        if not (in_uptrend and firm_close):
            return None

        entry = close
        stop = entry - atr * self.stop_atr_mult
        risk = max(entry - stop, atr, entry * 0.01, 0.01)
        target = entry + atr * self.target_atr_mult
        if target <= entry:
            target = entry + risk * 1.0

        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale=(
                "Firm close in an uptrend (closed near the high, above the 200-EMA); "
                "holding briefly to capture the overnight drift."
            ),
            confidence=round(min(0.52 + 0.15 * (close_location - self.min_close_location), 0.72), 4),
            price=entry,
            stop_loss=stop,
            take_profit=target,
            metadata={
                "style": "momentum",
                "signal_role": "entry_long",
                "setup_type": "overnight_drift",
                "hold_style": "overnight",
                "max_hold_bars": self.max_hold_bars,
                "close_location": round(close_location, 3),
                "ema_200": round(float(ema_trend), 4),
                "atr_14": round(atr, 4),
                "risk_reward_ratio": round((target - entry) / risk, 2),
            },
        )
