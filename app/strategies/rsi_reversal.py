"""RSI reversal strategy with optional divergence confirmation."""

from __future__ import annotations

import pandas as pd

from app.indicators import detect_rsi_divergence, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class RSIReversalStrategy(BaseStrategy):
    """Trade oversold/overbought reversals when price confirms the turn."""

    name = "rsi_reversal"
    required_bars = 60

    def __init__(self, *, timeframe: str = "15m"):
        self.timeframe = timeframe

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if len(data) < self.required_bars:
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        divergence = detect_rsi_divergence(frame)
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.01, 0.01))
        rv = float(last.get("relative_volume") or 0.0)

        bullish = (
            float(prev.get("rsi_14") or 100.0) < 34.0
            and float(last.get("rsi_14") or 0.0) > float(prev.get("rsi_14") or 0.0)
            and float(last["close"]) > float(prev["high"])
            and (divergence["bullish"] or float(last["close"]) <= float(last.get("bb_lower") or last["close"]))
        )
        if bullish:
            entry = float(last["close"])
            stop = float(min(last["low"], last.get("swing_low_10") or last["low"]) - (atr * 0.25))
            risk = max(entry - stop, atr * 0.8, 0.01)
            target = entry + (risk * 1.9)
            confidence = 0.57 + (0.12 if divergence["bullish"] else 0.0) + min(rv / 6.0, 0.12)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="RSI emerged from an oversold pocket and price confirmed the reversal with a higher close.",
                confidence=round(min(confidence, 0.82), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "rsi_reversal",
                    "signal_role": "entry_long",
                    "setup_type": "rsi_reversal_confirmation",
                    "indicator_confluence_score": round(0.62 + (0.18 if divergence["bullish"] else 0.0), 4),
                    "trend_quality": 0.45,
                    "momentum_quality": round(min(1.0, max(float(last.get("rsi_14") or 30.0) - 30.0, 0.0) / 25.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.5), 4),
                    "execution_quality": 0.76,
                    "rsi_divergence_bullish": divergence["bullish"],
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        bearish = (
            float(prev.get("rsi_14") or 0.0) > 66.0
            and float(last.get("rsi_14") or 100.0) < float(prev.get("rsi_14") or 100.0)
            and float(last["close"]) < float(prev["low"])
            and (divergence["bearish"] or float(last["close"]) >= float(last.get("bb_upper") or last["close"]))
        )
        if bearish:
            entry = float(last["close"])
            stop = float(max(last["high"], last.get("swing_high_10") or last["high"]) + (atr * 0.25))
            risk = max(stop - entry, atr * 0.8, 0.01)
            target = entry - (risk * 1.9)
            confidence = 0.57 + (0.12 if divergence["bearish"] else 0.0) + min(rv / 6.0, 0.12)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="RSI rolled over from an overbought pocket and price confirmed the reversal with a lower close.",
                confidence=round(min(confidence, 0.82), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "rsi_reversal",
                    "signal_role": "entry_short",
                    "setup_type": "rsi_reversal_confirmation",
                    "indicator_confluence_score": round(0.62 + (0.18 if divergence["bearish"] else 0.0), 4),
                    "trend_quality": 0.45,
                    "momentum_quality": round(min(1.0, max(70.0 - float(last.get("rsi_14") or 70.0), 0.0) / 25.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.5), 4),
                    "execution_quality": 0.76,
                    "rsi_divergence_bearish": divergence["bearish"],
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        return None
