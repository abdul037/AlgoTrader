"""EMA trend-stack, crossover, and pullback bounce strategy."""

from __future__ import annotations

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy
from app.strategies.weak_signals import build_supervised_weak_long_signal


class EMATrendStackStrategy(BaseStrategy):
    """Trade EMA stack alignment, crossovers, and pullback bounces."""

    name = "ema_trend_stack"
    required_bars = 120

    def __init__(self, *, timeframe: str = "1h"):
        self.timeframe = timeframe
        self.last_diagnostics: dict[str, object] | None = None

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        self.last_diagnostics = None
        if len(data) < self.required_bars:
            self.last_diagnostics = {"status": "no_signal", "rejection_reasons": ["insufficient_data"]}
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        atr_raw = last.get("atr_14")
        atr = float(atr_raw) if atr_raw is not None and pd.notna(atr_raw) else None
        if atr is None or atr <= 0:
            self.last_diagnostics = {
                "status": "no_signal",
                "rejection_reasons": ["atr_unavailable"],
                "measurements": {
                    "timeframe": self.timeframe,
                    "close": float(last["close"]),
                },
            }
            return None

        long_stack = (
            float(last["ema_9"]) > float(last["ema_20"]) > float(last["ema_50"])
            and float(last["ema_20_slope"] or 0.0) > 0.0
            and float(last["ema_50_slope"] or 0.0) > 0.0
        )
        bullish_bounce = long_stack and prev["close"] <= prev["ema_20"] and last["close"] > last["ema_20"]
        bullish_crossover = prev["ema_9"] <= prev["ema_20"] and last["ema_9"] > last["ema_20"] and float(last["close"]) > float(last["ema_50"])
        if bullish_bounce or bullish_crossover:
            entry = float(last["close"])
            stop = float(min(last.get("swing_low_10") or last["low"], last["ema_50"], entry - atr))
            risk = max(entry - stop, atr * 0.85, 0.01)
            target = entry + (risk * 2.2)
            confluence = compute_confluence_score(last, is_short=False)
            return self._build_signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="EMA stack stayed aligned and price resumed higher after either a pullback bounce or fresh crossover.",
                confidence=round(min(0.9, 0.6 + confluence * 0.24), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "ema_trend",
                    "signal_role": "entry_long",
                    "setup_type": "ema_pullback_bounce" if bullish_bounce else "ema_crossover",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.2), 4),
                    "momentum_quality": round(min(1.0, max(float(last.get("ema_9_slope") or 0.0), 0.0) / max(entry * 0.01, 0.01)), 4),
                    "liquidity_quality": round(min(1.0, float(last.get("relative_volume") or 0.0) / 2.0), 4),
                    "execution_quality": 0.84,
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        weak_long_anchor = (
            long_stack
            and float(last["close"]) > float(last["ema_20"])
            and float(last["low"]) <= float(last["ema_20"]) + (0.5 * atr)
        )
        if weak_long_anchor:
            entry = float(last["close"])
            stop = float(min(last.get("swing_low_10") or last["low"], last["ema_50"], entry - atr))
            risk = max(entry - stop, atr * 0.85, 0.01)
            target = entry + (risk * 1.2)
            confluence = compute_confluence_score(last, is_short=False)
            weak = build_supervised_weak_long_signal(
                self,
                symbol=symbol,
                price=entry,
                stop=stop,
                risk_multiple=round((target - entry) / risk, 4),
                rationale="Supervised weak-valid EMA trend-stack pullback with real long stack but incomplete bounce confirmation.",
                confidence=0.50,
                metadata={
                    "style": "ema_trend",
                    "signal_role": "entry_long",
                    "setup_type": "ema_trend_stack_pullback",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.2), 4),
                    "momentum_quality": round(
                        min(
                            1.0,
                            max(float(last.get("ema_9_slope") or 0.0), 0.0) / max(entry * 0.01, 0.01),
                        ),
                        4,
                    ),
                    "liquidity_quality": round(min(1.0, float(last.get("relative_volume") or 0.0) / 2.0), 4),
                    "execution_quality": 0.80,
                    "weak_signal_kind": "ema_stack_pullback_anchor",
                    **indicator_summary(last),
                },
                rejection_reasons=["confirmation_too_weak"],
                setup_anchor=True,
            )
            if weak is not None:
                return weak

        short_stack = (
            float(last["ema_9"]) < float(last["ema_20"]) < float(last["ema_50"])
            and float(last["ema_20_slope"] or 0.0) < 0.0
            and float(last["ema_50_slope"] or 0.0) < 0.0
        )
        bearish_bounce = short_stack and prev["close"] >= prev["ema_20"] and last["close"] < last["ema_20"]
        bearish_crossover = prev["ema_9"] >= prev["ema_20"] and last["ema_9"] < last["ema_20"] and float(last["close"]) < float(last["ema_50"])
        if bearish_bounce or bearish_crossover:
            entry = float(last["close"])
            stop = float(max(last.get("swing_high_10") or last["high"], last["ema_50"], entry + atr))
            risk = max(stop - entry, atr * 0.85, 0.01)
            target = entry - (risk * 2.2)
            confluence = compute_confluence_score(last, is_short=True)
            return self._build_signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="EMA stack stayed bearish and price rolled back under dynamic resistance after a bounce or crossover failure.",
                confidence=round(min(0.9, 0.6 + confluence * 0.24), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "ema_trend",
                    "signal_role": "entry_short",
                    "setup_type": "ema_pullback_bounce" if bearish_bounce else "ema_crossover",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.2), 4),
                    "momentum_quality": round(min(1.0, max(abs(float(last.get("ema_9_slope") or 0.0)), 0.0) / max(entry * 0.01, 0.01)), 4),
                    "liquidity_quality": round(min(1.0, float(last.get("relative_volume") or 0.0) / 2.0), 4),
                    "execution_quality": 0.84,
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        self.last_diagnostics = {
            "status": "no_signal",
            "rejection_reasons": ["ema_trend_stack_setup_not_confirmed"],
            "reason_codes": ["ema_trend_stack_setup_not_confirmed"],
            "score": 44.0,
            "measurements": {
                "close": float(last["close"]),
                "ema_9": float(last["ema_9"]),
                "ema_20": float(last["ema_20"]),
                "ema_50": float(last["ema_50"]),
                "long_stack": long_stack,
                "short_stack": short_stack,
            },
        }
        return None
