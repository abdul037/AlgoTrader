"""Market and symbol intelligence used by scans and single-symbol analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.utils.time import utc_now


SECTOR_ETF_BY_SYMBOL: dict[str, str] = {
    "AAPL": "XLK",
    "MSFT": "XLK",
    "NVDA": "SMH",
    "AMD": "SMH",
    "MU": "SMH",
    "AVGO": "SMH",
    "QCOM": "SMH",
    "AMAT": "SMH",
    "LRCX": "SMH",
    "KLAC": "SMH",
    "ANET": "IGV",
    "CRM": "IGV",
    "NOW": "IGV",
    "ORCL": "IGV",
    "ADBE": "IGV",
    "PANW": "HACK",
    "CRWD": "HACK",
    "SHOP": "XLY",
    "AMZN": "XLY",
    "TSLA": "XLY",
    "MCD": "XLY",
    "BKNG": "XLY",
    "NFLX": "XLC",
    "GOOG": "XLC",
    "GOOGL": "XLC",
    "META": "XLC",
    "DIS": "XLC",
    "JPM": "XLF",
    "BAC": "XLF",
    "WFC": "XLF",
    "GS": "XLF",
    "MS": "XLF",
    "AXP": "XLF",
    "BLK": "XLF",
    "UNH": "XLV",
    "LLY": "XLV",
    "JNJ": "XLV",
    "MRK": "XLV",
    "ABBV": "XLV",
    "ABT": "XLV",
    "PFE": "XLV",
    "AMGN": "XLV",
    "ISRG": "XLV",
    "XOM": "XLE",
    "CVX": "XLE",
    "COP": "XLE",
    "SLB": "XLE",
    "RTX": "XLI",
    "CAT": "XLI",
    "GE": "XLI",
    "DE": "XLI",
    "HON": "XLI",
    "UNP": "XLI",
    "BA": "XLI",
    "WMT": "XLP",
    "PG": "XLP",
    "KO": "XLP",
    "PEP": "XLP",
    "COST": "XLP",
    "HD": "XLY",
    "LOW": "XLY",
    "LIN": "XLB",
    "TMO": "XLV",
    "DHR": "XLV",
    "INTC": "SMH",
    "PLTR": "IWF",
    "SNOW": "IGV",
}


@dataclass(slots=True)
class MarketIntelligenceSnapshot:
    """Market-aware intelligence layered on top of a raw setup."""

    benchmark_symbol: str
    sector_symbol: str | None
    market_regime_label: str
    risk_mode: str
    volatility_environment: str
    momentum_state: str
    market_regime_score: float
    market_trend_score: float
    market_breadth_score: float
    sector_strength_score: float
    benchmark_strength_score: float
    relative_strength_vs_market: float
    relative_strength_vs_sector: float
    higher_timeframe_alignment_score: float
    lower_timeframe_alignment_score: float
    timeframe_alignment_score: float
    time_of_day_score: float
    volatility_suitability_score: float
    momentum_state_score: float
    extension_atr_multiple: float
    summary: str
    measurements: dict[str, float | str | None] = field(default_factory=dict)


class MarketIntelligenceService:
    """Compute benchmark, sector, regime, and timeframe-alignment signals."""

    def __init__(self, settings: Any, market_data_engine: Any):
        self.settings = settings
        self.market_data = market_data_engine

    def analyze(
        self,
        *,
        symbol: str,
        timeframe: str,
        history: pd.DataFrame,
        quote: Any,
        signal: Any,
        force_refresh: bool = False,
    ) -> MarketIntelligenceSnapshot:
        normalized_symbol = symbol.upper().strip()
        benchmark_symbol = self._benchmark_for_symbol(normalized_symbol)
        sector_symbol = SECTOR_ETF_BY_SYMBOL.get(normalized_symbol)
        is_short = str(signal.metadata.get("signal_role") or "entry_long") == "entry_short"

        benchmark_daily = self._safe_history(benchmark_symbol, timeframe="1d", bars=180, force_refresh=force_refresh)
        growth_daily = self._safe_history("QQQ", timeframe="1d", bars=180, force_refresh=force_refresh)
        breadth_daily = self._safe_history("IWM", timeframe="1d", bars=180, force_refresh=force_refresh)
        sector_history = (
            self._safe_history(sector_symbol, timeframe=timeframe, bars=max(len(history), 80), force_refresh=force_refresh)
            if sector_symbol
            else None
        )
        higher_history = self._safe_history(
            normalized_symbol,
            timeframe=self._higher_timeframe(timeframe),
            bars=180,
            force_refresh=force_refresh,
        )
        lower_history = self._safe_history(
            normalized_symbol,
            timeframe=self._lower_timeframe(timeframe),
            bars=180,
            force_refresh=force_refresh,
        )

        market_trend_score = self._market_trend_score(
            benchmark_daily=benchmark_daily,
            growth_daily=growth_daily,
            is_short=is_short,
        )
        market_breadth_score = self._breadth_score(
            benchmark_daily=benchmark_daily,
            breadth_daily=breadth_daily,
            is_short=is_short,
        )
        benchmark_strength_score = self._trend_score(benchmark_daily, is_short=is_short)
        sector_strength_score = self._trend_score(sector_history, is_short=is_short)
        market_regime_score = round(
            (market_trend_score + market_breadth_score + benchmark_strength_score) / 3.0,
            4,
        )
        relative_strength_market = round(
            self._relative_strength(history, benchmark_daily, is_short=is_short),
            4,
        )
        relative_strength_sector = round(
            self._relative_strength(history, sector_history, is_short=is_short),
            4,
        )
        higher_alignment = self._trend_score(higher_history, is_short=is_short)
        lower_alignment = self._trend_score(lower_history, is_short=is_short)
        timeframe_alignment_score = round((higher_alignment + lower_alignment) / 2.0, 4)
        time_of_day_score = round(self._time_of_day_score(timeframe), 4)
        volatility_suitability_score, volatility_environment = self._volatility_environment(benchmark_daily)
        momentum_state_score, momentum_state, extension_atr_multiple = self._momentum_state(history, is_short=is_short)
        market_regime_label = self._regime_label(market_regime_score, is_short=is_short)
        risk_mode = self._risk_mode(market_breadth_score, volatility_suitability_score)

        summary_parts = [
            market_regime_label,
            f"sector {'strong' if sector_strength_score >= 0.6 else 'mixed' if sector_strength_score >= 0.4 else 'weak'}",
            f"RS vs {benchmark_symbol} {relative_strength_market:+.2f}%",
            f"alignment {timeframe_alignment_score:.2f}",
            momentum_state,
        ]
        measurements: dict[str, float | str | None] = {
            "benchmark_symbol": benchmark_symbol,
            "sector_symbol": sector_symbol,
            "market_regime_score": round(market_regime_score, 4),
            "market_trend_score": round(market_trend_score, 4),
            "market_breadth_score": round(market_breadth_score, 4),
            "benchmark_strength_score": round(benchmark_strength_score, 4),
            "sector_strength_score": round(sector_strength_score, 4),
            "relative_strength_vs_market": round(relative_strength_market, 4),
            "relative_strength_vs_sector": round(relative_strength_sector, 4),
            "higher_timeframe_alignment_score": round(higher_alignment, 4),
            "lower_timeframe_alignment_score": round(lower_alignment, 4),
            "timeframe_alignment_score": round(timeframe_alignment_score, 4),
            "time_of_day_score": round(time_of_day_score, 4),
            "volatility_suitability_score": round(volatility_suitability_score, 4),
            "momentum_state_score": round(momentum_state_score, 4),
            "extension_atr_multiple": round(extension_atr_multiple, 4),
            "market_regime_label": market_regime_label,
            "risk_mode": risk_mode,
            "volatility_environment": volatility_environment,
            "momentum_state": momentum_state,
        }
        return MarketIntelligenceSnapshot(
            benchmark_symbol=benchmark_symbol,
            sector_symbol=sector_symbol,
            market_regime_label=market_regime_label,
            risk_mode=risk_mode,
            volatility_environment=volatility_environment,
            momentum_state=momentum_state,
            market_regime_score=market_regime_score,
            market_trend_score=market_trend_score,
            market_breadth_score=market_breadth_score,
            sector_strength_score=sector_strength_score,
            benchmark_strength_score=benchmark_strength_score,
            relative_strength_vs_market=relative_strength_market,
            relative_strength_vs_sector=relative_strength_sector,
            higher_timeframe_alignment_score=higher_alignment,
            lower_timeframe_alignment_score=lower_alignment,
            timeframe_alignment_score=timeframe_alignment_score,
            time_of_day_score=time_of_day_score,
            volatility_suitability_score=volatility_suitability_score,
            momentum_state_score=momentum_state_score,
            extension_atr_multiple=extension_atr_multiple,
            summary=", ".join(summary_parts),
            measurements=measurements,
        )

    def _safe_history(
        self,
        symbol: str | None,
        *,
        timeframe: str,
        bars: int,
        force_refresh: bool,
    ) -> pd.DataFrame | None:
        if not symbol:
            return None
        try:
            return self.market_data.get_history(symbol, timeframe=timeframe, bars=bars, force_refresh=force_refresh)
        except Exception:
            return None

    @staticmethod
    def _benchmark_for_symbol(symbol: str) -> str:
        if symbol in {"NVDA", "AMD", "MU", "AVGO", "QCOM", "AMAT", "LRCX", "KLAC", "INTC", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "ADBE", "ORCL"}:
            return "QQQ"
        return "SPY"

    @staticmethod
    def _higher_timeframe(timeframe: str) -> str:
        mapping = {"1m": "5m", "5m": "10m", "10m": "15m", "15m": "1h", "1h": "1d", "1d": "1w", "1w": "1w"}
        return mapping.get(timeframe.lower(), "1d")

    @staticmethod
    def _lower_timeframe(timeframe: str) -> str:
        mapping = {"1w": "1d", "1d": "1h", "1h": "15m", "15m": "10m", "10m": "5m", "5m": "1m", "1m": "1m"}
        return mapping.get(timeframe.lower(), "15m")

    def _time_of_day_score(self, timeframe: str) -> float:
        if timeframe in {"1d", "1w"}:
            return 0.75
        now_local = utc_now().astimezone(ZoneInfo(self.settings.schedule_timezone))
        minutes = (now_local.hour * 60) + now_local.minute
        if minutes < 570 or minutes > 960:
            return 0.35
        if timeframe == "1m":
            if 570 <= minutes <= 615:
                return 1.0
            if 616 <= minutes <= 720:
                return 0.62
            if 721 <= minutes <= 840:
                return 0.3
            return 0.82
        if 570 <= minutes <= 630:
            return 1.0
        if 631 <= minutes <= 720:
            return 0.78
        if 721 <= minutes <= 810:
            return 0.48
        if 811 <= minutes <= 930:
            return 0.9
        return 0.6

    def _volatility_environment(self, history: pd.DataFrame | None) -> tuple[float, str]:
        if history is None or history.empty:
            return 0.5, "unknown"
        frame = history.copy().reset_index(drop=True)
        frame["prev_close"] = frame["close"].shift(1)
        frame["tr"] = (
            pd.concat(
                [
                    frame["high"] - frame["low"],
                    (frame["high"] - frame["prev_close"]).abs(),
                    (frame["low"] - frame["prev_close"]).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .fillna(0.0)
        )
        atr = float(frame["tr"].rolling(14).mean().iloc[-1] or 0.0)
        close = max(float(frame["close"].iloc[-1] or 0.0), 0.01)
        atr_pct = (atr / close) * 100.0
        if atr_pct < 0.6:
            return 0.45, "compressed"
        if atr_pct <= 2.8:
            return 1.0, "healthy"
        if atr_pct <= 4.5:
            return 0.72, "elevated"
        return 0.35, "volatile"

    def _momentum_state(self, history: pd.DataFrame, *, is_short: bool) -> tuple[float, str, float]:
        frame = history.copy().reset_index(drop=True)
        frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
        frame["prev_close"] = frame["close"].shift(1)
        frame["tr"] = (
            pd.concat(
                [
                    frame["high"] - frame["low"],
                    (frame["high"] - frame["prev_close"]).abs(),
                    (frame["low"] - frame["prev_close"]).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .fillna(0.0)
        )
        atr = float(frame["tr"].rolling(14).mean().iloc[-1] or 0.0)
        close = float(frame["close"].iloc[-1])
        ema20 = float(frame["ema20"].iloc[-1] or close)
        extension_atr_multiple = ((close - ema20) / max(atr, 0.01)) * (-1.0 if is_short else 1.0)
        momentum_pct = self._return_pct(frame, bars=5)
        directional_momentum = -momentum_pct if is_short else momentum_pct
        if extension_atr_multiple >= 3.2:
            return 0.2, "exhausted", extension_atr_multiple
        if directional_momentum >= 1.2 and extension_atr_multiple >= 0.8:
            return 1.0, "expanding", extension_atr_multiple
        if directional_momentum >= 0.2:
            return 0.72, "constructive", extension_atr_multiple
        return 0.42, "mixed", extension_atr_multiple

    def _market_trend_score(
        self,
        *,
        benchmark_daily: pd.DataFrame | None,
        growth_daily: pd.DataFrame | None,
        is_short: bool,
    ) -> float:
        benchmark_score = self._trend_score(benchmark_daily, is_short=is_short)
        growth_score = self._trend_score(growth_daily, is_short=is_short)
        return round((benchmark_score + growth_score) / 2.0, 4)

    def _breadth_score(
        self,
        *,
        benchmark_daily: pd.DataFrame | None,
        breadth_daily: pd.DataFrame | None,
        is_short: bool,
    ) -> float:
        if benchmark_daily is None or breadth_daily is None:
            return 0.5
        relative = self._relative_strength(breadth_daily, benchmark_daily, is_short=is_short)
        if relative >= 1.5:
            return 1.0
        if relative >= 0.3:
            return 0.7
        if relative >= -0.8:
            return 0.45
        return 0.2

    def _trend_score(self, history: pd.DataFrame | None, *, is_short: bool) -> float:
        if history is None or history.empty:
            return 0.5
        frame = history.copy().reset_index(drop=True)
        frame["ema_fast"] = frame["close"].ewm(span=20, adjust=False).mean()
        frame["ema_slow"] = frame["close"].ewm(span=50, adjust=False).mean()
        last = frame.iloc[-1]
        close = float(last["close"])
        ema_fast = float(last["ema_fast"] or close)
        ema_slow = float(last["ema_slow"] or close)
        slope = 0.0
        if len(frame) >= 6:
            slope = (float(frame["ema_fast"].iloc[-1]) - float(frame["ema_fast"].iloc[-6])) / max(close, 0.01) * 100.0
        if is_short:
            aligned = close < ema_fast < ema_slow
            if aligned and slope < 0:
                return 1.0
            if close < ema_slow:
                return 0.68
            return 0.2
        aligned = close > ema_fast > ema_slow
        if aligned and slope > 0:
            return 1.0
        if close > ema_slow:
            return 0.68
        return 0.2

    def _relative_strength(self, asset_history: pd.DataFrame | None, benchmark_history: pd.DataFrame | None, *, is_short: bool) -> float:
        if asset_history is None or benchmark_history is None:
            return 0.0
        asset_return = self._return_pct(asset_history, bars=20)
        benchmark_return = self._return_pct(benchmark_history, bars=20)
        relative = asset_return - benchmark_return
        return -relative if is_short else relative

    @staticmethod
    def _return_pct(history: pd.DataFrame, *, bars: int) -> float:
        frame = history.reset_index(drop=True)
        if frame.empty:
            return 0.0
        anchor_index = max(0, len(frame) - bars - 1)
        anchor = max(float(frame["close"].iloc[anchor_index] or 0.0), 0.01)
        current = float(frame["close"].iloc[-1] or anchor)
        return ((current - anchor) / anchor) * 100.0

    @staticmethod
    def _regime_label(score: float, *, is_short: bool) -> str:
        if score >= 0.72:
            return "risk-off trend" if is_short else "risk-on trend"
        if score >= 0.5:
            return "mixed tape"
        return "countertrend risk" if not is_short else "squeeze risk"

    @staticmethod
    def _risk_mode(breadth_score: float, volatility_score: float) -> str:
        composite = (breadth_score + volatility_score) / 2.0
        if composite >= 0.72:
            return "risk_on"
        if composite >= 0.5:
            return "balanced"
        return "risk_off"
