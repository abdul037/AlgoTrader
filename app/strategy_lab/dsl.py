"""Constrained generated-strategy DSL compiler."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.models.signal import SignalAction
from app.models.strategy_lab import StrategyLabCondition, StrategyLabDsl
from app.strategies.base import BaseStrategy


class GeneratedRuleStrategy(BaseStrategy):
    """Runtime strategy compiled from a validated long-only DSL."""

    def __init__(self, dsl: StrategyLabDsl):
        self.dsl = dsl
        self.name = dsl.name
        self.required_bars = max([indicator.period for indicator in dsl.indicators] + [20]) + 2
        self.last_diagnostics: dict[str, Any] = {}

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        if not self._ensure_length(data):
            self.last_diagnostics = {"status": "insufficient_data", "rejection_reasons": ["insufficient_data"]}
            return None
        frame = _with_indicators(data.copy(), self.dsl)
        if len(frame) < 2 or frame.tail(1).isna().any(axis=None):
            self.last_diagnostics = {"status": "indicator_unavailable", "rejection_reasons": ["indicator_unavailable"]}
            return None
        current = frame.iloc[-1]
        previous = frame.iloc[-2]
        failed = [
            _condition_label(condition)
            for condition in self.dsl.entry_conditions
            if not _condition_passed(condition, current=current, previous=previous)
        ]
        if failed:
            self.last_diagnostics = {
                "status": "no_signal",
                "rejection_reasons": failed,
                "score": 50.0,
            }
            return None
        price = float(current["close"])
        stop = round(price * (1.0 - self.dsl.stop_loss_pct / 100.0), 4)
        target = round(price * (1.0 + self.dsl.take_profit_pct / 100.0), 4)
        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.BUY,
            confidence=self.dsl.confidence,
            price=price,
            stop_loss=stop,
            take_profit=target,
            rationale=f"Generated long-only DSL strategy: {self.dsl.description or self.name}",
            metadata={
                "strategy_lab_generated": True,
                "timeframe": self.dsl.timeframe,
                "max_hold_bars": self.dsl.max_hold_bars,
                "dsl": self.dsl.model_dump(),
            },
        )


def _with_indicators(frame: pd.DataFrame, dsl: StrategyLabDsl) -> pd.DataFrame:
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    for indicator in dsl.indicators:
        source = frame[indicator.source].astype(float)
        if indicator.kind == "sma":
            frame[indicator.name] = source.rolling(indicator.period).mean()
        elif indicator.kind == "ema":
            frame[indicator.name] = source.ewm(span=indicator.period, adjust=False).mean()
        elif indicator.kind == "rsi":
            delta = source.diff()
            gains = delta.clip(lower=0).rolling(indicator.period).mean()
            losses = (-delta.clip(upper=0)).rolling(indicator.period).mean().replace(0, pd.NA)
            rs = gains / losses
            frame[indicator.name] = 100 - (100 / (1 + rs))
        elif indicator.kind == "volume_sma":
            frame[indicator.name] = volume.rolling(indicator.period).mean()
        elif indicator.kind == "atr":
            previous_close = close.shift(1)
            true_range = pd.concat(
                [
                    high - low,
                    (high - previous_close).abs(),
                    (low - previous_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            frame[indicator.name] = true_range.rolling(indicator.period).mean()
        elif indicator.kind == "roc":
            frame[indicator.name] = close.pct_change(indicator.period) * 100.0
        elif indicator.kind in {"bb_upper", "bb_lower", "bb_width"}:
            mid = close.rolling(indicator.period).mean()
            std = close.rolling(indicator.period).std()
            upper = mid + (std * 2.0)
            lower = mid - (std * 2.0)
            if indicator.kind == "bb_upper":
                frame[indicator.name] = upper
            elif indicator.kind == "bb_lower":
                frame[indicator.name] = lower
            else:
                frame[indicator.name] = ((upper - lower) / mid.replace(0.0, pd.NA)) * 100.0
        elif indicator.kind == "donchian_high":
            frame[indicator.name] = high.rolling(indicator.period).max().shift(1)
        elif indicator.kind == "donchian_low":
            frame[indicator.name] = low.rolling(indicator.period).min().shift(1)
        elif indicator.kind == "relative_volume":
            average_volume = volume.rolling(indicator.period).mean().replace(0.0, pd.NA)
            frame[indicator.name] = volume / average_volume
        elif indicator.kind == "vwap":
            cumulative_volume = volume.cumsum().replace(0.0, pd.NA)
            frame[indicator.name] = (close * volume).cumsum() / cumulative_volume
    return frame


def _condition_passed(condition: StrategyLabCondition, *, current: Any, previous: Any) -> bool:
    left_now = _value(condition.left, current)
    right_now = _value(condition.right, current)
    if left_now is None or right_now is None:
        return False
    if condition.kind == "above":
        return left_now > right_now
    if condition.kind == "below":
        return left_now < right_now
    left_prev = _value(condition.left, previous)
    right_prev = _value(condition.right, previous)
    if left_prev is None or right_prev is None:
        return False
    if condition.kind == "crosses_above":
        return left_prev <= right_prev and left_now > right_now
    if condition.kind == "crosses_below":
        return left_prev >= right_prev and left_now < right_now
    return False


def _value(operand: str | float, row: Any) -> float | None:
    if isinstance(operand, (float, int)):
        return float(operand)
    key = operand.lower()
    if key not in row:
        return None
    try:
        value = float(row[key])
    except (TypeError, ValueError):
        return None
    return value if pd.notna(value) else None


def _condition_label(condition: StrategyLabCondition) -> str:
    return f"{condition.left}_{condition.kind}_{condition.right}"
