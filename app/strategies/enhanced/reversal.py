"""Enhanced research strategies: reversal."""

from __future__ import annotations


import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _long_signal,
    _metadata,
    _recent_low,
    _reject,
    _safe_float,
    _weak_long_signal,
)


class RegimeFilteredMeanReversionStrategy(BaseStrategy):
    """Long mean reversion only when the higher-level regime is not bearish."""

    name = "regime_filtered_mean_reversion"
    required_bars = 70

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        max_rsi: float = 38.0,
        max_adx: float = 32.0,
        risk_multiple: float = 1.8,
    ):
        self.timeframe = timeframe
        self.max_rsi = max_rsi
        self.max_adx = max_adx
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or atr is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_50 = _safe_float(last.get("ema_50"), price) or price
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        rsi = _safe_float(last.get("rsi_14"), 50.0) or 50.0
        adx = _safe_float(last.get("adx_14"), 0.0) or 0.0
        lower_band = _safe_float(last.get("bb_lower"), price) or price
        mean_target = max(_safe_float(last.get("bb_mid"), price) or price, _safe_float(last.get("vwap"), price) or price)
        regime_ok = ema_50 >= ema_200 * 0.98 and price >= ema_200 * 0.94
        oversold = price <= lower_band * 1.01 and rsi <= self.max_rsi
        reversal_bar = price > (_safe_float(last.get("open"), price) or price) or (_safe_float(last.get("stoch_rsi"), 1.0) or 1.0) <= 0.25
        checks = {
            "regime_alignment_too_low": regime_ok,
            "mean_reversion_not_oversold": oversold,
            "reversal_bar_not_confirmed": reversal_bar,
            "adx_too_high_for_mean_reversion": adx <= self.max_adx,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            stop = min(_recent_low(frame, 8) or price - atr, price - (1.25 * atr))
            weak_risk = price - stop
            weak_multiple = self.risk_multiple
            if weak_risk > 0 and mean_target > price:
                weak_multiple = max((mean_target - price) / weak_risk, 1.0)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=stop,
                risk_multiple=weak_multiple,
                rationale="Supervised weak-valid regime-filtered mean reversion with real oversold anchor but incomplete confirmation.",
                confidence=0.50,
                style="mean_reversion",
                setup_type="regime_filtered_mean_reversion",
                rejection_reasons=rejection_reasons,
                setup_anchor=oversold,
                extra={"regime_filter": "ema50_vs_ema200_non_bearish", "weak_signal_kind": "oversold_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if oversold or reversal_bar else 42.0,
                measurements={"lower_band": lower_band, "mean_target": mean_target},
            )
            return None
        stop = min(_recent_low(frame, 8) or price - atr, price - (1.25 * atr))
        risk = price - stop
        if mean_target <= price and risk > 0:
            mean_target = price + (risk * self.risk_multiple)
        adjusted_multiple = max((mean_target - price) / risk, 1.2) if risk > 0 else self.risk_multiple
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=adjusted_multiple,
            rationale="Mean-reversion entry is oversold but still inside a constructive higher-level regime.",
            confidence=0.63,
            metadata=_metadata(
                row=last,
                style="mean_reversion",
                setup_type="regime_filtered_mean_reversion",
                risk_reward=adjusted_multiple,
                extra={"regime_filter": "ema50_vs_ema200_non_bearish"},
            ),
        )


class FailedBreakdownReversalStrategy(BaseStrategy):
    """Long reversal after a support break fails and price reclaims the level."""

    name = "failed_breakdown_reversal"
    required_bars = 55

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        support_window: int = 20,
        minimum_relative_volume: float = 0.90,
        risk_multiple: float = 1.8,
    ):
        self.timeframe = timeframe
        self.support_window = support_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prior = frame.iloc[:-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        support = _recent_low(prior, self.support_window)
        low = _safe_float(last.get("low"))
        open_price = _safe_float(last.get("open"))
        if price is None or atr is None or support is None or low is None or open_price is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        broke_support = low < support - (0.15 * atr)
        reclaimed = price > support and price > open_price
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        regime_ok = price >= (_safe_float(last.get("ema_200"), price) or price) * 0.94
        checks = {
            "support_break_not_observed": broke_support,
            "support_reclaim_not_confirmed": reclaimed,
            "relative_volume_too_low": volume_ok,
            "regime_alignment_too_low": regime_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(low, price - (1.0 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid failed-breakdown reversal with real support break/reclaim but incomplete confirmation.",
                confidence=0.50,
                style="reversal",
                setup_type="failed_breakdown_reversal",
                rejection_reasons=rejection_reasons,
                setup_anchor=broke_support and reclaimed,
                extra={"support": support, "weak_signal_kind": "support_reclaim_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if broke_support or reclaimed else 41.0,
                measurements={"support": support, "low": low},
            )
            return None
        stop = min(low, price - (1.0 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Failed breakdown reclaimed support with reversal confirmation.",
            confidence=0.62,
            metadata=_metadata(
                row=last,
                style="reversal",
                setup_type="failed_breakdown_reversal",
                risk_reward=self.risk_multiple,
                extra={"support": support},
            ),
        )


