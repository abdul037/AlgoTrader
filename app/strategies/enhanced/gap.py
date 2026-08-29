"""Enhanced research strategies: gap."""

from __future__ import annotations


import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _liquidity_ok,
    _long_signal,
    _metadata,
    _reject,
    _safe_float,
)


class GapContinuationFadeStrategy(BaseStrategy):
    """Long-only gap continuation or gap-fade setup with liquidity guardrails."""

    name = "gap_continuation_fade"
    required_bars = 35

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        minimum_gap_pct: float = 0.8,
        minimum_relative_volume: float = 1.25,
        minimum_dollar_volume: float = 5_000_000.0,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.minimum_gap_pct = minimum_gap_pct
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_dollar_volume = minimum_dollar_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        open_price = _safe_float(last.get("open"))
        prev_close = _safe_float(prev.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or open_price is None or prev_close is None or atr is None or prev_close <= 0:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        gap_pct = ((open_price - prev_close) / prev_close) * 100.0
        day_range = max((_safe_float(last.get("high"), price) or price) - (_safe_float(last.get("low"), price) or price), 0.01)
        close_location = (price - (_safe_float(last.get("low"), price) or price)) / day_range
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        liquidity_ok = _liquidity_ok(last, self.minimum_dollar_volume)
        continuation = gap_pct >= self.minimum_gap_pct and price > open_price and close_location >= 0.65
        fade = gap_pct <= -self.minimum_gap_pct and price > open_price and close_location >= 0.75
        checks = {
            "relative_volume_too_low": volume_ok,
            "average_dollar_volume_below_threshold": liquidity_ok,
            "gap_setup_not_confirmed": continuation or fade,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=54.0 if continuation or fade else 40.0,
                measurements={"gap_pct": gap_pct, "close_location": close_location},
            )
            return None
        setup = "gap_up_continuation" if continuation else "gap_down_fade"
        stop = min(_safe_float(last.get("low"), price - atr) or price - atr, price - (1.2 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale=f"{setup.replace('_', ' ').title()} confirmed by relative volume and close location.",
            confidence=0.62,
            metadata=_metadata(
                row=last,
                style="gap",
                setup_type=setup,
                risk_reward=self.risk_multiple,
                extra={"gap_pct": round(gap_pct, 4), "close_location": round(close_location, 4)},
            ),
        )


