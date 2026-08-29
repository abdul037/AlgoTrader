"""Pairs / statistical-arbitrage: trade the mean reversion of a two-leg spread.

This is the Stage 2 uncorrelated-edge the audit flagged as the real lever for a
*steady* daily number: a market-neutral-ish spread reverts on its own schedule,
largely independent of market direction, so its P&L stream diversifies the
trend/mean-reversion book.

The single-symbol ``BaseStrategy`` interface can't see two legs, so
``PairsTradingStrategy`` pulls the hedge leg through an injected
``hedge_history`` provider (the batch backtester wires one from its market-data
engine; without a provider the strategy is inert and emits nothing — safe by
construction on any path that doesn't supply one).

Long-only, to suit the paper bot's long-biased execution: it takes the long side
of the spread when the spread is stretched *cheap* (primary undervalued vs the
hedge), and expresses the hedge leg only as metadata for reference. The math here
is pure and dependency-light (numpy only).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


@dataclass
class PairSpread:
    beta: float           # hedge ratio: primary ~= beta * hedge
    zscore: float         # latest spread z-score over the lookback
    correlation: float    # primary/hedge return-level correlation
    spread_std: float
    last_spread: float
    observations: int


def _aligned_closes(primary_df: pd.DataFrame, hedge_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Inner-join two OHLCV frames on timestamp and return aligned close arrays."""

    if "timestamp" not in primary_df.columns or "timestamp" not in hedge_df.columns:
        # Fall back to positional alignment on the shared tail length.
        n = min(len(primary_df), len(hedge_df))
        if n == 0:
            return np.array([]), np.array([])
        return (
            primary_df["close"].to_numpy(dtype="float64")[-n:],
            hedge_df["close"].to_numpy(dtype="float64")[-n:],
        )
    left = primary_df[["timestamp", "close"]].rename(columns={"close": "p"})
    right = hedge_df[["timestamp", "close"]].rename(columns={"close": "h"})
    merged = left.merge(right, on="timestamp", how="inner").dropna()
    return merged["p"].to_numpy(dtype="float64"), merged["h"].to_numpy(dtype="float64")


def compute_pair_spread(
    primary_df: pd.DataFrame, hedge_df: pd.DataFrame, *, lookback: int = 60
) -> PairSpread | None:
    """Hedge ratio, spread, and latest z-score for a primary/hedge pair.

    Returns None when there isn't enough overlapping history or the spread has no
    dispersion (a degenerate pair the z-score can't describe).
    """

    primary, hedge = _aligned_closes(primary_df, hedge_df)
    n = len(primary)
    if n < max(lookback, 20) or len(hedge) != n:
        return None
    if np.std(hedge) == 0 or np.std(primary) == 0:
        return None

    # Hedge ratio via OLS slope of primary on hedge.
    beta = float(np.polyfit(hedge, primary, 1)[0])
    spread = primary - beta * hedge
    window = spread[-lookback:]
    std = float(np.std(window))
    if std == 0:
        return None
    z = float((spread[-1] - float(np.mean(window))) / std)
    corr = float(np.corrcoef(primary, hedge)[0, 1])
    return PairSpread(
        beta=round(beta, 6),
        zscore=round(z, 4),
        correlation=round(corr, 4),
        spread_std=round(std, 6),
        last_spread=round(float(spread[-1]), 6),
        observations=n,
    )


# Curated correlated pairs (primary -> hedge). Same-industry names whose spread
# has historically mean-reverted. When a scanned symbol isn't in the map, the
# strategy falls back to a market-relative spread against the default hedge.
PAIR_MAP: dict[str, str] = {
    "KO": "PEP", "PEP": "KO",
    "HD": "LOW", "LOW": "HD",
    "V": "MA", "MA": "V",
    "MSFT": "GOOGL", "GOOGL": "MSFT",
    "XOM": "CVX", "CVX": "XOM",
    "JPM": "BAC", "BAC": "JPM",
    "UPS": "FDX", "FDX": "UPS",
    "WMT": "TGT", "TGT": "WMT",
}


class PairsTradingStrategy(BaseStrategy):
    """Long the primary leg when the pair spread is stretched cheap and reverting."""

    name = "pairs_stat_arb"
    required_bars = 80

    def __init__(
        self,
        *,
        hedge_symbol: str = "SPY",
        pair_map: dict[str, str] | None = None,
        timeframe: str = "1d",
        lookback: int = 60,
        entry_z: float = 2.0,
        min_correlation: float = 0.6,
        stop_atr_mult: float = 2.0,
        reward_risk: float = 1.5,
    ):
        self.hedge_symbol = hedge_symbol.upper()
        self.pair_map = {k.upper(): v.upper() for k, v in (pair_map or PAIR_MAP).items()}
        self.timeframe = timeframe
        self.lookback = lookback
        self.entry_z = entry_z
        self.min_correlation = min_correlation
        self.stop_atr_mult = stop_atr_mult
        self.reward_risk = reward_risk
        self.required_bars = max(lookback + 20, 80)
        self._hedge_provider = None

    def set_hedge_provider(self, provider) -> None:
        """Inject a callable ``(symbol, bars) -> OHLCV DataFrame`` for the hedge leg."""

        self._hedge_provider = provider

    @staticmethod
    def _atr(frame: pd.DataFrame, period: int = 14) -> float:
        high = frame["high"].astype("float64")
        low = frame["low"].astype("float64")
        prev_close = frame["close"].astype("float64").shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = float(tr.rolling(period).mean().iloc[-1] or 0.0)
        return atr

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data) or self._hedge_provider is None:
            return None
        hedge_symbol = self.pair_map.get(symbol.upper(), self.hedge_symbol)
        if hedge_symbol == symbol.upper():
            return None  # never pair a symbol with itself
        try:
            hedge_df = self._hedge_provider(hedge_symbol, self.required_bars + 40)
        except Exception:
            return None
        if hedge_df is None or len(hedge_df) < self.lookback:
            return None

        pair = compute_pair_spread(data, hedge_df, lookback=self.lookback)
        if pair is None or pair.correlation < self.min_correlation:
            return None
        # Long the primary only when it's stretched cheap vs the hedge (spread far
        # below its mean) and expected to revert up.
        if pair.zscore > -self.entry_z:
            return None

        last = data.iloc[-1]
        entry = float(last["close"])
        atr = max(self._atr(data), entry * 0.005, 0.01)
        stop = entry - atr * self.stop_atr_mult
        risk = max(entry - stop, atr, entry * 0.01, 0.01)
        target = entry + risk * self.reward_risk

        confidence = round(min(0.55 + 0.10 * (abs(pair.zscore) - self.entry_z), 0.80), 4)
        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale=(
                f"Pair spread vs {hedge_symbol} is stretched cheap "
                f"(z={pair.zscore:.2f}); expected mean reversion of the spread."
            ),
            confidence=max(confidence, 0.5),
            price=entry,
            stop_loss=stop,
            take_profit=target,
            metadata={
                "style": "mean_reversion",
                "signal_role": "entry_long",
                "setup_type": "pairs_stat_arb",
                "hedge_symbol": self.hedge_symbol,
                "hedge_ratio": pair.beta,
                "spread_zscore": pair.zscore,
                "pair_correlation": pair.correlation,
                "risk_reward_ratio": round((target - entry) / risk, 2),
            },
        )
