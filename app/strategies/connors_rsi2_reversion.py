"""Connors RSI(2) short-term mean reversion.

A well-documented, robust edge distinct from everything already in the library:
buy a *deeply* oversold 2-period RSI while the instrument is in a long-term
uptrend (above its 200-period EMA), and exit on the snap back toward the
short-term mean. The existing mean-reversion strategies use a z-score
(``mean_reversion``) or RSI(14) divergence (``rsi_reversal``); none use the
2-period RSI, which is the defining feature of the Connors approach and fires on
much shorter, sharper dips.

Long-only, which suits the paper bot's long-biased execution path. Self-contained:
it enriches the frame for the trend/vol context and computes RSI(2) inline
(``indicators.enrich_technical_indicators`` only provides RSI(14)).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


class ConnorsRSI2ReversionStrategy(BaseStrategy):
    """Buy a sharp oversold dip (RSI2) inside a confirmed uptrend; revert to the mean."""

    name = "connors_rsi2_reversion"
    required_bars = 210

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        rsi_period: int = 2,
        rsi_entry: float = 10.0,
        trend_ema: int = 200,
        mean_window: int = 5,
        stop_atr_mult: float = 2.5,
    ):
        self.timeframe = timeframe
        self.rsi_period = rsi_period
        self.rsi_entry = rsi_entry
        self.trend_ema = trend_ema
        self.mean_window = mean_window
        self.stop_atr_mult = stop_atr_mult

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        frame["rsi_fast"] = _rsi(frame["close"].astype("float64"), self.rsi_period)
        frame["mean_target"] = frame["close"].rolling(self.mean_window).mean()
        last = frame.iloc[-1]

        rsi_fast = last.get("rsi_fast")
        ema_trend = last.get("ema_200")
        atr = last.get("atr_14")
        mean_target = last.get("mean_target")
        if pd.isna(rsi_fast) or pd.isna(ema_trend) or pd.isna(atr) or pd.isna(mean_target):
            return None

        close = float(last["close"])
        atr = max(float(atr), close * 0.005, 0.01)

        # Uptrend (above long EMA) + a sharp short-term oversold dip.
        in_uptrend = close > float(ema_trend)
        oversold = float(rsi_fast) <= self.rsi_entry
        if not (in_uptrend and oversold):
            return None

        entry = close
        swing_low = last.get("swing_low_10")
        floor = float(swing_low) if not pd.isna(swing_low) else entry - atr
        stop = min(floor, entry - atr * self.stop_atr_mult)
        stop = min(stop, entry - atr)  # ensure a non-trivial stop below entry
        risk = max(entry - stop, atr, entry * 0.01, 0.01)

        # Target the reversion to the short-term mean; keep a floor reward.
        target = float(mean_target)
        if target <= entry:
            target = entry + risk * 1.5
        target = max(target, entry + risk * 1.2)

        oversold_depth = max(0.0, (self.rsi_entry - float(rsi_fast)) / max(self.rsi_entry, 1.0))
        confidence = round(min(0.55 + 0.20 * oversold_depth, 0.80), 4)

        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale=(
                "RSI(2) is deeply oversold while price holds above its 200-period trend EMA — "
                "a sharp dip inside an uptrend, expected to revert toward the short-term mean."
            ),
            confidence=confidence,
            price=entry,
            stop_loss=stop,
            take_profit=target,
            metadata={
                "style": "mean_reversion",
                "signal_role": "entry_long",
                "setup_type": "connors_rsi2_reversion",
                "rsi_2": round(float(rsi_fast), 2),
                "ema_200": round(float(ema_trend), 4),
                "mean_target": round(float(mean_target), 4),
                "atr_14": round(atr, 4),
                "risk_reward_ratio": round((target - entry) / risk, 2),
            },
        )
