"""Filter pipeline and market-context helpers for the universe screener."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators


@dataclass(slots=True)
class MarketContext:
    """Derived market-quality inputs used by filters and ranking."""

    current_price: float
    last_volume: float
    average_volume: float
    average_dollar_volume: float
    relative_volume: float
    spread_bps: float | None
    atr_pct: float
    trend_strength_pct: float
    efficiency_ratio: float
    momentum_pct: float
    regime_alignment_score: float
    measurements: dict[str, float | None] = field(default_factory=dict)


@dataclass(slots=True)
class FilterOutcome:
    """Outcome for the pre-ranking filter pipeline."""

    passed: bool
    pass_reasons: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    measurements: dict[str, float | None] = field(default_factory=dict)
    watchlist_only: bool = False


def build_market_context(history: pd.DataFrame, *, quote: Any, signal: Any) -> MarketContext:
    """Build reusable market context from normalized OHLCV data."""

    timeframe = str(signal.metadata.get("timeframe") or "1d").lower()
    frame = enrich_technical_indicators(history, timeframe=timeframe)
    frame["ema_fast"] = frame["ema_20"]
    frame["ema_slow"] = frame["ema_50"]
    frame["atr"] = frame["atr_14"]
    frame["avg_volume"] = frame["avg_volume_20"]
    frame["avg_dollar_volume"] = frame["avg_dollar_volume_20"]

    last = frame.iloc[-1]
    price = float(quote.last_execution or quote.ask or quote.bid or last["close"])
    avg_volume = float(last["avg_volume"] or 0.0)
    avg_dollar_volume = float(last["avg_dollar_volume"] or 0.0)
    last_volume = float(last["volume"] or 0.0)
    relative_volume = float(last_volume / avg_volume) if avg_volume > 0 else 0.0
    atr_pct = float((last["atr"] / last["close"]) * 100) if float(last["close"] or 0.0) > 0 and pd.notna(last["atr"]) else 0.0
    trend_strength_pct = (
        abs(float(last["ema_fast"]) - float(last["ema_slow"])) / max(float(last["close"]), 0.01) * 100
        if pd.notna(last["ema_fast"]) and pd.notna(last["ema_slow"])
        else 0.0
    )

    closes = frame["close"].tail(20).astype("float64")
    net_change = abs(float(closes.iloc[-1] - closes.iloc[0])) if len(closes) > 1 else 0.0
    path_change = float(closes.diff().abs().sum()) if len(closes) > 1 else 0.0
    efficiency_ratio = float(net_change / path_change) if path_change > 0 else 0.0

    lookback_close = float(frame["close"].iloc[-6]) if len(frame) >= 6 else float(frame["close"].iloc[0])
    raw_momentum_pct = ((float(last["close"]) - lookback_close) / max(lookback_close, 0.01)) * 100
    is_short = str(signal.metadata.get("signal_role") or "entry_long") == "entry_short"
    momentum_pct = -raw_momentum_pct if is_short else raw_momentum_pct

    spread_bps: float | None = None
    if quote.bid is not None and quote.ask is not None:
        mid = (float(quote.bid) + float(quote.ask)) / 2.0
        if mid > 0:
            spread_bps = ((float(quote.ask) - float(quote.bid)) / mid) * 10_000

    style = str(signal.metadata.get("style") or signal.strategy_name)
    regime_alignment_score = _regime_alignment_score(
        style=style,
        is_short=is_short,
        close=float(last["close"]),
        ema_fast=float(last["ema_fast"]) if pd.notna(last["ema_fast"]) else float(last["close"]),
        ema_slow=float(last["ema_slow"]) if pd.notna(last["ema_slow"]) else float(last["close"]),
        momentum_pct=momentum_pct,
    )

    measurements = {
        "price": round(price, 4),
        "last_volume": round(last_volume, 2),
        "average_volume": round(avg_volume, 2),
        "average_dollar_volume": round(avg_dollar_volume, 2),
        "relative_volume": round(relative_volume, 4),
        "spread_bps": round(spread_bps, 4) if spread_bps is not None else None,
        "atr_pct": round(atr_pct, 4),
        "trend_strength_pct": round(trend_strength_pct, 4),
        "efficiency_ratio": round(efficiency_ratio, 4),
        "momentum_pct": round(momentum_pct, 4),
        "regime_alignment_score": round(regime_alignment_score, 4),
        "indicator_confluence_score": round(compute_confluence_score(last, is_short=is_short), 4),
        "execution_quality": round(
            _execution_quality_score(
                spread_bps=spread_bps,
                relative_volume=relative_volume,
                atr_pct=atr_pct,
                efficiency_ratio=efficiency_ratio,
            ),
            4,
        ),
    }
    return MarketContext(
        current_price=price,
        last_volume=last_volume,
        average_volume=avg_volume,
        average_dollar_volume=avg_dollar_volume,
        relative_volume=relative_volume,
        spread_bps=spread_bps,
        atr_pct=atr_pct,
        trend_strength_pct=trend_strength_pct,
        efficiency_ratio=efficiency_ratio,
        momentum_pct=momentum_pct,
        regime_alignment_score=regime_alignment_score,
        measurements=measurements,
    )


class ScreenerFilterPipeline:
    """Apply modular pre-ranking filters to generated strategy signals."""

    def __init__(self, settings: Any):
        self.settings = settings

    def evaluate(
        self,
        *,
        signal: Any,
        context: MarketContext,
        backtest_snapshot: dict[str, Any],
        intelligence: Any | None = None,
    ) -> FilterOutcome:
        reasons: list[str] = []
        rejections: list[str] = []
        timeframe = str(signal.metadata.get("timeframe") or "").lower()
        indicator_confluence = float(signal.metadata.get("indicator_confluence_score") or 0.0)
        execution_quality = float(signal.metadata.get("execution_quality") or 0.0)
        relative_volume_floor = float(self.settings.screener_min_relative_volume)
        spread_ceiling = float(self.settings.screener_max_spread_bps)
        confidence_floor = float(self.settings.screener_min_confidence)
        if timeframe == "1m":
            relative_volume_floor = max(relative_volume_floor, float(self.settings.screener_scalp_min_relative_volume))
            spread_ceiling = min(spread_ceiling, float(self.settings.screener_scalp_max_spread_bps))
            confidence_floor = max(confidence_floor, float(self.settings.screener_scalp_min_confidence))

        self._check(
            confidence_floor <= float(signal.confidence or 0.0),
            reasons,
            rejections,
            "confidence_ok",
            "confidence_below_threshold",
        )
        self._check(
            self.settings.screener_min_price <= context.current_price <= self.settings.screener_max_price,
            reasons,
            rejections,
            "price_ok",
            "price_out_of_range",
        )
        self._check(
            context.last_volume >= float(self.settings.screener_min_last_volume),
            reasons,
            rejections,
            "last_volume_ok",
            "last_volume_below_threshold",
        )
        self._check(
            context.average_volume >= float(self.settings.screener_min_average_volume),
            reasons,
            rejections,
            "average_volume_ok",
            "average_volume_below_threshold",
        )
        self._check(
            context.average_dollar_volume >= float(self.settings.screener_min_average_dollar_volume),
            reasons,
            rejections,
            "dollar_volume_ok",
            "dollar_volume_below_threshold",
        )
        self._check(
            context.relative_volume >= relative_volume_floor,
            reasons,
            rejections,
            "relative_volume_ok",
            "relative_volume_too_low",
        )
        if context.spread_bps is None:
            reasons.append("spread_unavailable")
        else:
            self._check(
                context.spread_bps <= spread_ceiling,
                reasons,
                rejections,
                "spread_ok",
                "spread_too_wide",
            )
        self._check(
            float(self.settings.screener_min_atr_pct) <= context.atr_pct <= float(self.settings.screener_max_atr_pct),
            reasons,
            rejections,
            "volatility_ok",
            "volatility_out_of_range",
        )
        self._check(
            context.trend_strength_pct >= float(self.settings.screener_min_trend_strength_pct),
            reasons,
            rejections,
            "trend_strength_ok",
            "trend_strength_too_low",
        )
        self._check(
            context.efficiency_ratio >= float(self.settings.screener_min_efficiency_ratio),
            reasons,
            rejections,
            "structure_clean",
            "structure_too_choppy",
        )
        rr = float(signal.metadata.get("risk_reward_ratio") or 0.0)
        self._check(
            rr >= float(self.settings.screener_min_reward_to_risk),
            reasons,
            rejections,
            "reward_to_risk_ok",
            "reward_to_risk_too_low",
        )
        self._check(
            indicator_confluence >= float(self.settings.screener_min_indicator_confluence),
            reasons,
            rejections,
            "indicator_confluence_ok",
            "indicator_confluence_too_low",
        )
        self._check(
            execution_quality >= float(self.settings.screener_min_execution_quality),
            reasons,
            rejections,
            "execution_quality_ok",
            "execution_quality_too_low",
        )
        self._check(
            context.regime_alignment_score >= 0.45,
            reasons,
            rejections,
            "regime_alignment_ok",
            "regime_alignment_too_low",
        )

        if intelligence is not None:
            self._check(
                float(intelligence.market_regime_score) >= float(self.settings.screener_min_market_regime_score),
                reasons,
                rejections,
                "market_regime_fit_ok",
                "market_regime_fit_too_low",
            )
            self._check(
                float(intelligence.timeframe_alignment_score) >= float(self.settings.screener_min_timeframe_alignment_score),
                reasons,
                rejections,
                "timeframe_alignment_ok",
                "timeframe_alignment_too_low",
            )
            self._check(
                float(intelligence.relative_strength_vs_market) >= float(self.settings.screener_min_relative_strength_vs_market),
                reasons,
                rejections,
                "relative_strength_market_ok",
                "relative_strength_market_too_low",
            )
            self._check(
                float(intelligence.relative_strength_vs_sector) >= float(self.settings.screener_min_relative_strength_vs_sector),
                reasons,
                rejections,
                "relative_strength_sector_ok",
                "relative_strength_sector_too_low",
            )
            self._check(
                float(intelligence.sector_strength_score) >= float(self.settings.screener_min_sector_strength_score),
                reasons,
                rejections,
                "sector_strength_ok",
                "sector_strength_too_low",
            )
            self._check(
                float(intelligence.benchmark_strength_score) >= float(self.settings.screener_min_benchmark_strength_score),
                reasons,
                rejections,
                "benchmark_strength_ok",
                "benchmark_strength_too_low",
            )
            self._check(
                float(intelligence.extension_atr_multiple) <= float(self.settings.screener_max_extension_atr_multiple),
                reasons,
                rejections,
                "entry_not_extended",
                "entry_too_extended",
            )
            reasons.append(f"momentum_state_{intelligence.momentum_state}")
            self._check(
                float(backtest_snapshot.get("recent_consistency_score", 0.0) or 0.0)
                >= float(self.settings.screener_min_recent_backtest_consistency),
                reasons,
                rejections,
                "recent_backtest_consistency_ok",
                "recent_backtest_consistency_too_low",
            )
            measurements = {
                **context.measurements,
                **getattr(intelligence, "measurements", {}),
            }
        else:
            measurements = dict(context.measurements)

        watchlist_only = False
        credibility_score = float(backtest_snapshot.get("credibility_score", 0.0) or 0.0)
        if not bool(backtest_snapshot.get("validated", False)):
            reasons.append("backtest_unvalidated")
            if self.settings.screener_weak_backtest_action == "block":
                rejections.append("backtest_validation_blocked")
            elif self.settings.screener_weak_backtest_action == "watchlist":
                watchlist_only = True
                reasons.append("backtest_downgraded_to_watchlist")
        elif backtest_snapshot:
            reasons.append("backtest_validated")
        if credibility_score < float(self.settings.screener_min_backtest_credibility_score):
            if self.settings.screener_weak_backtest_action == "block":
                rejections.append("backtest_credibility_too_low")
            elif self.settings.screener_weak_backtest_action == "watchlist":
                watchlist_only = True
                reasons.append("backtest_credibility_watchlist")

        return FilterOutcome(
            passed=not rejections,
            pass_reasons=reasons,
            rejection_reasons=rejections,
            reason_codes=[*reasons, *rejections],
            measurements=measurements,
            watchlist_only=watchlist_only,
        )

    @staticmethod
    def _check(
        condition: bool,
        reasons: list[str],
        rejections: list[str],
        pass_code: str,
        fail_code: str,
    ) -> None:
        if condition:
            reasons.append(pass_code)
        else:
            rejections.append(fail_code)


def _regime_alignment_score(
    *,
    style: str,
    is_short: bool,
    close: float,
    ema_fast: float,
    ema_slow: float,
    momentum_pct: float,
) -> float:
    """Score how well the signal style aligns with the current market regime."""

    trend_up = close > ema_fast > ema_slow
    trend_down = close < ema_fast < ema_slow
    if "mean_reversion" in style:
        if is_short:
            return 0.75 if close <= ema_slow else 0.45
        return 0.75 if close >= ema_slow else 0.45
    if is_short:
        if trend_down:
            return 1.0
        if close < ema_slow:
            return 0.65
        return 0.2
    if trend_up:
        return 1.0
    if close > ema_slow:
        return 0.65
    return 0.2


def _execution_quality_score(
    *,
    spread_bps: float | None,
    relative_volume: float,
    atr_pct: float,
    efficiency_ratio: float,
) -> float:
    spread_score = 0.7 if spread_bps is None else max(0.0, min(1.0, 1.0 - (spread_bps / 40.0)))
    volume_score = max(0.0, min(1.0, relative_volume / 1.5))
    volatility_score = 1.0 if 0.35 <= atr_pct <= 6.5 else 0.45
    structure_score = max(0.0, min(1.0, efficiency_ratio / 0.45))
    return (spread_score + volume_score + volatility_score + structure_score) / 4.0
