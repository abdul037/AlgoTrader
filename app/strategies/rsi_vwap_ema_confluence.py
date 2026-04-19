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

    def __init__(
        self,
        *,
        timeframe: str = "5m",
        minimum_relative_volume: float = 1.25,
        minimum_confluence_score: float = 0.84,
        minimum_adx: float = 20.0,
        rsi_long_min: float = 54.0,
        rsi_long_max: float = 66.0,
        rsi_short_min: float = 34.0,
        rsi_short_max: float = 46.0,
        max_extension_atr: float = 1.6,
        minimum_body_to_range: float = 0.32,
        minimum_close_location: float = 0.62,
    ):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_confluence_score = minimum_confluence_score
        self.minimum_adx = minimum_adx
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.max_extension_atr = max_extension_atr
        self.minimum_body_to_range = minimum_body_to_range
        self.minimum_close_location = minimum_close_location

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if len(data) < self.required_bars:
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.006, 0.01))
        rv = float(last.get("relative_volume") or 0.0)
        rsi = float(last.get("rsi_14") or 50.0)
        adx = float(last.get("adx_14") or 0.0)
        confluence_long = compute_confluence_score(last, is_short=False)
        confluence_short = compute_confluence_score(last, is_short=True)
        quality = self._quality_metrics(last, atr=atr)

        long_conditions = (
            float(last["close"]) > float(last["vwap"])
            and float(last["ema_9"]) > float(last["ema_20"]) > float(last["ema_50"])
            and self.rsi_long_min <= rsi <= self.rsi_long_max
            and float(last.get("macd_hist") or 0.0) > 0.0
            and rv >= self.minimum_relative_volume
            and float(last["close"]) > float(last.get("range_high_20") or prev["high"])
            and adx >= self.minimum_adx
            and confluence_long >= self.minimum_confluence_score
            and quality["extension_atr"] <= self.max_extension_atr
            and quality["body_to_range"] >= self.minimum_body_to_range
            and quality["close_location"] >= self.minimum_close_location
            and float(last.get("ema_9_slope") or 0.0) > 0.0
            and float(last.get("ema_20_slope") or 0.0) > 0.0
        )
        if long_conditions:
            entry = float(last["close"])
            stop = float(min(last.get("vwap") or last["low"], last.get("ema_20") or last["low"], entry - atr))
            risk = max(entry - stop, atr * 0.9, 0.01)
            target = entry + (risk * 2.5)
            quality_score = self._quality_score(confluence_long, rv=rv, adx=adx, quality=quality)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="A+ RSI/VWAP/EMA confluence long: trend stack, VWAP support, controlled RSI, breakout, RVOL, ADX, candle quality, and entry extension all aligned.",
                confidence=round(min(0.96, 0.70 + quality_score * 0.24), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_long",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "primary_strategy": True,
                    "a_plus_setup": True,
                    "indicator_confluence_score": round(confluence_long, 4),
                    "confluence_quality_score": round(quality_score, 4),
                    "trend_quality": round(min(1.0, confluence_long + 0.18), 4),
                    "momentum_quality": round(min(1.0, (rsi - 50.0) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    **quality,
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        short_conditions = (
            float(last["close"]) < float(last["vwap"])
            and float(last["ema_9"]) < float(last["ema_20"]) < float(last["ema_50"])
            and self.rsi_short_min <= rsi <= self.rsi_short_max
            and float(last.get("macd_hist") or 0.0) < 0.0
            and rv >= self.minimum_relative_volume
            and float(last["close"]) < float(last.get("range_low_20") or prev["low"])
            and adx >= self.minimum_adx
            and confluence_short >= self.minimum_confluence_score
            and quality["extension_atr"] <= self.max_extension_atr
            and quality["body_to_range"] >= self.minimum_body_to_range
            and quality["close_location_short"] >= self.minimum_close_location
            and float(last.get("ema_9_slope") or 0.0) < 0.0
            and float(last.get("ema_20_slope") or 0.0) < 0.0
        )
        if short_conditions:
            entry = float(last["close"])
            stop = float(max(last.get("vwap") or last["high"], last.get("ema_20") or last["high"], entry + atr))
            risk = max(stop - entry, atr * 0.9, 0.01)
            target = entry - (risk * 2.5)
            quality_score = self._quality_score(confluence_short, rv=rv, adx=adx, quality=quality)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="A+ RSI/VWAP/EMA confluence short: trend stack, VWAP rejection, controlled RSI, breakdown, RVOL, ADX, candle quality, and entry extension all aligned.",
                confidence=round(min(0.96, 0.70 + quality_score * 0.24), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_short",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "primary_strategy": True,
                    "a_plus_setup": True,
                    "indicator_confluence_score": round(confluence_short, 4),
                    "confluence_quality_score": round(quality_score, 4),
                    "trend_quality": round(min(1.0, confluence_short + 0.18), 4),
                    "momentum_quality": round(min(1.0, (50.0 - rsi) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    **quality,
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        return None

    @staticmethod
    def _quality_metrics(last: pd.Series, *, atr: float) -> dict[str, float]:
        high = float(last.get("high") or last["close"])
        low = float(last.get("low") or last["close"])
        close = float(last["close"])
        open_price = float(last.get("open") or close)
        candle_range = max(high - low, 0.01)
        anchor_levels = [
            float(last.get("ema_9") or close),
            float(last.get("ema_20") or close),
            float(last.get("vwap") or close),
        ]
        extension = min(abs(close - level) for level in anchor_levels) / max(atr, 0.01)
        return {
            "extension_atr": round(extension, 4),
            "body_to_range": round(abs(close - open_price) / candle_range, 4),
            "close_location": round((close - low) / candle_range, 4),
            "close_location_short": round((high - close) / candle_range, 4),
        }

    @staticmethod
    def _quality_score(confluence: float, *, rv: float, adx: float, quality: dict[str, float]) -> float:
        extension_score = max(0.0, min(1.0, 1.0 - (quality["extension_atr"] / 2.2)))
        return round(
            min(
                1.0,
                (confluence * 0.38)
                + (min(rv / 2.2, 1.0) * 0.20)
                + (min(adx / 35.0, 1.0) * 0.18)
                + (quality["body_to_range"] * 0.12)
                + (extension_score * 0.12),
            ),
            4,
        )
