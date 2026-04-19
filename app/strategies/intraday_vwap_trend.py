"""Intraday VWAP trend strategy."""

from __future__ import annotations

import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class IntradayVWAPTrendStrategy(BaseStrategy):
    """Look for intraday VWAP and EMA alignment with local range breaks."""

    name = "intraday_vwap_trend"
    required_bars = 40

    def __init__(self, lookback_bars: int = 8):
        self.lookback_bars = lookback_bars
        self.required_bars = max(lookback_bars + 10, 30)

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None

        frame = data.copy().reset_index(drop=True)
        frame["session_date"] = pd.to_datetime(frame["timestamp"], utc=True).dt.date
        latest_session = frame[frame["session_date"] == frame.iloc[-1]["session_date"]].copy()
        if len(latest_session) < max(self.lookback_bars + 2, 8):
            latest_session = frame.tail(max(self.lookback_bars + 8, 20)).copy()

        latest_session["ema_fast"] = latest_session["close"].ewm(span=9, adjust=False).mean()
        latest_session["ema_slow"] = latest_session["close"].ewm(span=21, adjust=False).mean()
        cumulative_volume = latest_session["volume"].cumsum().replace(0, pd.NA)
        latest_session["vwap"] = (latest_session["close"] * latest_session["volume"]).cumsum() / cumulative_volume
        latest_session["recent_high"] = latest_session["high"].rolling(self.lookback_bars).max().shift(1)
        latest_session["recent_low"] = latest_session["low"].rolling(self.lookback_bars).min().shift(1)

        last = latest_session.iloc[-1]
        if pd.isna(last["vwap"]) or pd.isna(last["recent_high"]) or pd.isna(last["recent_low"]):
            return None

        bullish = last["close"] > last["vwap"] and last["ema_fast"] > last["ema_slow"] and last["close"] > last["recent_high"]
        bearish = last["close"] < last["vwap"] and last["ema_fast"] < last["ema_slow"] and last["close"] < last["recent_low"]

        if bullish:
            entry = float(last["close"])
            stop = float(min(last["vwap"], last["recent_low"]))
            risk = max(entry - stop, entry * 0.003, 0.01)
            target = entry + (risk * 2.0)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Intraday price is above VWAP and EMA support while breaking the recent local range.",
                confidence=0.62,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "intraday",
                    "signal_role": "entry_long",
                    "vwap": round(float(last["vwap"]), 4),
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                },
            )

        if bearish:
            entry = float(last["close"])
            stop = float(max(last["vwap"], last["recent_high"]))
            risk = max(stop - entry, entry * 0.003, 0.01)
            target = entry - (risk * 2.0)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Intraday price is below VWAP and EMA resistance while breaking local support.",
                confidence=0.6,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "intraday",
                    "signal_role": "entry_short",
                    "vwap": round(float(last["vwap"]), 4),
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                },
            )

        return None
