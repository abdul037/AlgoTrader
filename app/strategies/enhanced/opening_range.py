"""Enhanced research strategies: opening_range."""

from __future__ import annotations


import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _long_signal,
    _metadata,
    _recent_high,
    _recent_low,
    _reject,
    _safe_float,
    _weak_long_signal,
)


class OpeningRangeBreakoutRetestStrategy(BaseStrategy):
    """Long opening-range breakout or retest continuation."""

    name = "opening_range_breakout_retest"
    required_bars = 45

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        opening_range_bars: int = 5,
        minimum_relative_volume: float = 0.95,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.opening_range_bars = opening_range_bars
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        previous = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        opening_high = _safe_float(last.get("opening_range_high"), _recent_high(frame.iloc[:-1], self.opening_range_bars))
        opening_low = _safe_float(last.get("opening_range_low"), _recent_low(frame.iloc[:-1], self.opening_range_bars))
        if price is None or atr is None or opening_high is None or opening_low is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        prev_close = _safe_float(previous.get("close"), price) or price
        retest_zone = opening_high - (0.35 * atr)
        breakout = price > opening_high and prev_close <= opening_high * 1.01
        retest_reclaim = price > opening_high and (_safe_float(last.get("low"), price) or price) <= max(retest_zone, opening_low)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        trend_ok = price > (_safe_float(last.get("vwap"), price) or price) and price > (_safe_float(last.get("ema_20"), price) or price)
        checks = {
            "opening_range_not_cleared": breakout or retest_reclaim,
            "relative_volume_too_low": volume_ok,
            "trend_not_aligned": trend_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(opening_low, price - (1.1 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid opening-range setup with real range break/retest but incomplete confirmation.",
                confidence=0.50,
                style="opening_range",
                setup_type="opening_range_breakout_retest",
                rejection_reasons=rejection_reasons,
                setup_anchor=breakout or retest_reclaim,
                extra={
                    "opening_range_high": opening_high,
                    "opening_range_low": opening_low,
                    "weak_signal_kind": "opening_range_anchor",
                },
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=56.0 if breakout or retest_reclaim else 43.0,
                measurements={"opening_range_high": opening_high, "opening_range_low": opening_low},
            )
            return None
        stop = min(opening_low, price - (1.1 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Opening range breakout/retest reclaimed range high with volume confirmation.",
            confidence=0.64,
            metadata=_metadata(
                row=last,
                style="opening_range",
                setup_type="opening_range_breakout_retest",
                risk_reward=self.risk_multiple,
                extra={"opening_range_high": opening_high, "opening_range_low": opening_low},
            ),
        )


