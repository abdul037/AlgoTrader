"""RSI trend-continuation and pullback strategy."""

from __future__ import annotations

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class RSITrendContinuationStrategy(BaseStrategy):
    """Trade RSI-supported continuation inside aligned EMA trends."""

    name = "rsi_trend_continuation"
    required_bars = 80

    def __init__(self, *, timeframe: str = "1h", relative_volume_floor: float = 1.05):
        self.timeframe = timeframe
        self.relative_volume_floor = relative_volume_floor

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if len(data) < self.required_bars:
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.01, 0.01))
        rv = float(last.get("relative_volume") or 0.0)
        confluence_long = compute_confluence_score(last, is_short=False)
        confluence_short = compute_confluence_score(last, is_short=True)

        bullish_trend = (
            last["close"] > last["ema_20"] > last["ema_50"]
            and last["ema_9"] > last["ema_20"]
            and float(last.get("ema_20_slope") or 0.0) > 0.0
            and 52.0 <= float(last.get("rsi_14") or 0.0) <= 68.0
            and float(last.get("macd_hist") or 0.0) > 0.0
            and rv >= self.relative_volume_floor
        )
        bullish_pullback = prev["close"] <= prev["ema_20"] and last["close"] > last["ema_20"]

        if bullish_trend and bullish_pullback:
            entry = float(last["close"])
            stop = float(min(last.get("swing_low_10") or last["low"], last["ema_50"], entry - atr))
            risk = max(entry - stop, atr * 0.9, entry * 0.004, 0.01)
            target = entry + (risk * 2.3)
            confidence = min(0.9, 0.58 + (confluence_long * 0.28))
            metadata = {
                "style": "rsi_trend",
                "signal_role": "entry_long",
                "setup_type": "rsi_pullback_continuation",
                "indicator_confluence_score": round(confluence_long, 4),
                "trend_quality": round(min(1.0, confluence_long + 0.15), 4),
                "momentum_quality": round(min(1.0, max(float(last.get("rsi_14") or 0.0) - 50.0, 0.0) / 20.0), 4),
                "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                "execution_quality": round(1.0 if entry <= float(last["ema_9"]) + atr * 0.35 else 0.72, 4),
                "risk_reward_ratio": round((target - entry) / risk, 2),
                **indicator_summary(last),
            }
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="RSI held in a bullish regime and price resumed higher from EMA support with momentum confirmation.",
                confidence=round(confidence, 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata=metadata,
            )

        bearish_trend = (
            last["close"] < last["ema_20"] < last["ema_50"]
            and last["ema_9"] < last["ema_20"]
            and float(last.get("ema_20_slope") or 0.0) < 0.0
            and 32.0 <= float(last.get("rsi_14") or 100.0) <= 48.0
            and float(last.get("macd_hist") or 0.0) < 0.0
            and rv >= self.relative_volume_floor
        )
        bearish_pullback = prev["close"] >= prev["ema_20"] and last["close"] < last["ema_20"]
        if bearish_trend and bearish_pullback:
            entry = float(last["close"])
            stop = float(max(last.get("swing_high_10") or last["high"], last["ema_50"], entry + atr))
            risk = max(stop - entry, atr * 0.9, entry * 0.004, 0.01)
            target = entry - (risk * 2.3)
            confidence = min(0.88, 0.56 + (confluence_short * 0.28))
            metadata = {
                "style": "rsi_trend",
                "signal_role": "entry_short",
                "setup_type": "rsi_pullback_continuation",
                "indicator_confluence_score": round(confluence_short, 4),
                "trend_quality": round(min(1.0, confluence_short + 0.15), 4),
                "momentum_quality": round(min(1.0, max(50.0 - float(last.get("rsi_14") or 50.0), 0.0) / 20.0), 4),
                "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                "execution_quality": round(1.0 if entry >= float(last["ema_9"]) - atr * 0.35 else 0.72, 4),
                "risk_reward_ratio": round((entry - target) / risk, 2),
                **indicator_summary(last),
            }
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="RSI stayed in a bearish regime and price rejected EMA support inside a downtrend.",
                confidence=round(confidence, 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata=metadata,
            )

        return None
