"""Post-Earnings Announcement Drift (PEAD).

Prices tend to keep drifting in the direction of an earnings surprise for weeks
after the report — a persistent, well-documented anomaly. This buys a positive
surprise while the drift window is still open and the trend confirms, holding for
the remaining window via the engine's time-stop.

PEAD needs earnings/estimate data the price feed doesn't carry, so — like the
pairs strategy's hedge leg — it pulls that through an injected provider. Without
a provider the strategy is inert and emits nothing, so it is safe on every path
until a real earnings source is wired in. The provider is a callable
``(symbol) -> EarningsSurprise | dict | None`` returning at least
``surprise_pct`` and ``bars_since_report``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class PEADStrategy(BaseStrategy):
    """Buy a positive earnings surprise inside its drift window; hold to time-stop."""

    name = "post_earnings_drift"
    required_bars = 60

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        drift_window_bars: int = 15,
        min_surprise_pct: float = 5.0,
        trend_ema: int = 50,
        stop_atr_mult: float = 2.0,
        target_atr_mult: float = 3.0,
    ):
        self.timeframe = timeframe
        self.drift_window_bars = drift_window_bars
        self.min_surprise_pct = min_surprise_pct
        self.trend_ema = trend_ema
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self._earnings_provider = None

    def set_earnings_provider(self, provider) -> None:
        """Inject a callable ``(symbol) -> {surprise_pct, bars_since_report} | None``."""

        self._earnings_provider = provider

    @staticmethod
    def _field(info: Any, key: str) -> Any:
        if isinstance(info, dict):
            return info.get(key)
        return getattr(info, key, None)

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data) or self._earnings_provider is None:
            return None
        try:
            info = self._earnings_provider(symbol.upper())
        except Exception:
            return None
        if info is None:
            return None

        surprise = self._field(info, "surprise_pct")
        bars_since = self._field(info, "bars_since_report")
        if surprise is None or bars_since is None:
            return None
        try:
            surprise = float(surprise)
            bars_since = int(bars_since)
        except (TypeError, ValueError):
            return None

        # Positive surprise, still inside the drift window.
        if surprise < self.min_surprise_pct or not (0 <= bars_since < self.drift_window_bars):
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        close = float(last["close"])
        ema = last.get(f"ema_{self.trend_ema}") if last.get(f"ema_{self.trend_ema}") is not None else last.get("ema_50")
        atr = last.get("atr_14")
        if pd.isna(ema) or pd.isna(atr):
            return None
        if close <= float(ema):  # only ride a surprise the trend confirms
            return None
        atr = max(float(atr), close * 0.005, 0.01)

        entry = close
        stop = entry - atr * self.stop_atr_mult
        risk = max(entry - stop, atr, entry * 0.01, 0.01)
        target = entry + atr * self.target_atr_mult
        remaining = max(self.drift_window_bars - bars_since, 1)

        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale=(
                f"Positive earnings surprise (+{surprise:.1f}%) {bars_since} bars ago, still inside "
                f"the drift window and above the {self.trend_ema}-EMA — riding the post-earnings drift."
            ),
            confidence=round(min(0.55 + 0.02 * (surprise - self.min_surprise_pct), 0.80), 4),
            price=entry,
            stop_loss=stop,
            take_profit=target,
            metadata={
                "style": "momentum",
                "signal_role": "entry_long",
                "setup_type": "post_earnings_drift",
                "earnings_surprise_pct": round(surprise, 2),
                "bars_since_report": bars_since,
                "max_hold_bars": remaining,
                "ema_trend": round(float(ema), 4),
                "atr_14": round(atr, 4),
                "risk_reward_ratio": round((target - entry) / risk, 2),
            },
        )
