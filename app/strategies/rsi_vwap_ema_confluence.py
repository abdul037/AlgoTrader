"""High-confluence RSI + VWAP + EMA strategy."""

from __future__ import annotations

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class RSIVWAPEMAConfluenceStrategy(BaseStrategy):
    """Only trigger when RSI, VWAP, EMA, and volume conditions align tightly."""

    name = "rsi_vwap_ema_confluence"
    required_bars = 80

    def __init__(self, *, timeframe: str = "5m", minimum_relative_volume: float = 1.2):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if len(data) < self.required_bars:
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.006, 0.01))
        rv = float(last.get("relative_volume") or 0.0)

        long_conditions = (
            float(last["close"]) > float(last["vwap"])
            and float(last["ema_9"]) > float(last["ema_20"]) > float(last["ema_50"])
            and float(last.get("rsi_14") or 0.0) >= 54.0
            and float(last.get("rsi_14") or 100.0) <= 68.0
            and float(last.get("macd_hist") or 0.0) > 0.0
            and rv >= self.minimum_relative_volume
            and float(last["close"]) > float(last.get("range_high_20") or prev["high"])
            and float(last.get("adx_14") or 0.0) >= 18.0
        )
        if long_conditions:
            entry = float(last["close"])
            stop = float(min(last.get("vwap") or last["low"], last.get("ema_20") or last["low"], entry - atr))
            risk = max(entry - stop, atr * 0.9, 0.01)
            target = entry + (risk * 2.5)
            confluence = compute_confluence_score(last, is_short=False)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="RSI, VWAP, EMA stack, breakout structure, and relative volume all aligned for a high-conviction long.",
                confidence=round(min(0.95, 0.66 + confluence * 0.22), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_long",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "indicator_confluence_score": round(max(confluence, 0.8), 4),
                    "trend_quality": round(min(1.0, confluence + 0.18), 4),
                    "momentum_quality": round(min(1.0, (float(last.get("rsi_14") or 50.0) - 50.0) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        short_conditions = (
            float(last["close"]) < float(last["vwap"])
            and float(last["ema_9"]) < float(last["ema_20"]) < float(last["ema_50"])
            and float(last.get("rsi_14") or 100.0) <= 46.0
            and float(last.get("rsi_14") or 0.0) >= 32.0
            and float(last.get("macd_hist") or 0.0) < 0.0
            and rv >= self.minimum_relative_volume
            and float(last["close"]) < float(last.get("range_low_20") or prev["low"])
            and float(last.get("adx_14") or 0.0) >= 18.0
        )
        if short_conditions:
            entry = float(last["close"])
            stop = float(max(last.get("vwap") or last["high"], last.get("ema_20") or last["high"], entry + atr))
            risk = max(stop - entry, atr * 0.9, 0.01)
            target = entry - (risk * 2.5)
            confluence = compute_confluence_score(last, is_short=True)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="RSI, VWAP, EMA stack, breakdown structure, and relative volume all aligned for a high-conviction short.",
                confidence=round(min(0.95, 0.66 + confluence * 0.22), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_short",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "indicator_confluence_score": round(max(confluence, 0.8), 4),
                    "trend_quality": round(min(1.0, confluence + 0.18), 4),
                    "momentum_quality": round(min(1.0, (50.0 - float(last.get("rsi_14") or 50.0)) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        return None
