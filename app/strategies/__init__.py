"""Strategy registry and lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.strategies.base import BaseStrategy
from app.strategies.gold_momentum import GoldMomentumStrategy
from app.strategies.intraday_vwap_trend import IntradayVWAPTrendStrategy
from app.strategies.ma_crossover import MACrossoverStrategy
from app.strategies.ema_trend_stack import EMATrendStackStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.momentum_breakout import MomentumBreakoutStrategy
from app.strategies.pullback_trend import PullbackTrendStrategy
from app.strategies.rsi_reversal import RSIReversalStrategy
from app.strategies.rsi_trend_continuation import RSITrendContinuationStrategy
from app.strategies.rsi_vwap_ema_confluence import RSIVWAPEMAConfluenceStrategy
from app.strategies.trend_following import TrendFollowingStrategy
from app.strategies.vwap_reclaim import VWAPReclaimStrategy


@dataclass(frozen=True)
class StrategySpec:
    """Descriptor used by the screener and batch backtester."""

    name: str
    timeframe: str
    style: str
    default_kwargs: dict[str, object] = field(default_factory=dict)


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ma_crossover": MACrossoverStrategy,
    "pullback_trend": PullbackTrendStrategy,
    "gold_momentum": GoldMomentumStrategy,
    "trend_following": TrendFollowingStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
    "mean_reversion": MeanReversionStrategy,
    "intraday_vwap_trend": IntradayVWAPTrendStrategy,
    "ema_trend_stack": EMATrendStackStrategy,
    "rsi_trend_continuation": RSITrendContinuationStrategy,
    "rsi_reversal": RSIReversalStrategy,
    "vwap_reclaim": VWAPReclaimStrategy,
    "rsi_vwap_ema_confluence": RSIVWAPEMAConfluenceStrategy,
}


STRATEGY_SPECS: list[StrategySpec] = [
    StrategySpec("pullback_trend", timeframe="1d", style="swing", default_kwargs={"trend_window": 100, "pullback_window": 10}),
    StrategySpec("ma_crossover", timeframe="1d", style="trend", default_kwargs={"fast_window": 5, "slow_window": 20}),
    StrategySpec("trend_following", timeframe="1d", style="trend", default_kwargs={"fast_span": 20, "slow_span": 50, "pullback_window": 5}),
    StrategySpec("momentum_breakout", timeframe="1d", style="momentum", default_kwargs={"breakout_window": 20, "volume_window": 20}),
    StrategySpec("mean_reversion", timeframe="1d", style="mean_reversion", default_kwargs={"lookback": 20, "zscore_threshold": 1.8}),
    StrategySpec("trend_following", timeframe="1h", style="trend", default_kwargs={"fast_span": 20, "slow_span": 50, "pullback_window": 5}),
    StrategySpec("ema_trend_stack", timeframe="1h", style="trend", default_kwargs={"timeframe": "1h"}),
    StrategySpec("momentum_breakout", timeframe="1h", style="momentum", default_kwargs={"breakout_window": 20, "volume_window": 20}),
    StrategySpec("mean_reversion", timeframe="1h", style="mean_reversion", default_kwargs={"lookback": 20, "zscore_threshold": 1.8}),
    StrategySpec("rsi_trend_continuation", timeframe="1h", style="trend", default_kwargs={"timeframe": "1h"}),
    StrategySpec("intraday_vwap_trend", timeframe="15m", style="intraday", default_kwargs={"lookback_bars": 8}),
    StrategySpec("vwap_reclaim", timeframe="15m", style="intraday", default_kwargs={"timeframe": "15m"}),
    StrategySpec("momentum_breakout", timeframe="15m", style="momentum", default_kwargs={"breakout_window": 20, "volume_window": 20}),
    StrategySpec("rsi_reversal", timeframe="15m", style="reversal", default_kwargs={"timeframe": "15m"}),
    StrategySpec("ema_trend_stack", timeframe="15m", style="trend", default_kwargs={"timeframe": "15m"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="15m", style="confluence", default_kwargs={"timeframe": "15m"}),
    StrategySpec("vwap_reclaim", timeframe="5m", style="intraday", default_kwargs={"timeframe": "5m"}),
    StrategySpec("rsi_reversal", timeframe="5m", style="reversal", default_kwargs={"timeframe": "5m"}),
    StrategySpec("ema_trend_stack", timeframe="5m", style="trend", default_kwargs={"timeframe": "5m"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="5m", style="confluence", default_kwargs={"timeframe": "5m"}),
    StrategySpec("vwap_reclaim", timeframe="10m", style="intraday", default_kwargs={"timeframe": "10m"}),
    StrategySpec("rsi_reversal", timeframe="10m", style="reversal", default_kwargs={"timeframe": "10m"}),
    StrategySpec("ema_trend_stack", timeframe="10m", style="trend", default_kwargs={"timeframe": "10m"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="10m", style="confluence", default_kwargs={"timeframe": "10m"}),
    StrategySpec("vwap_reclaim", timeframe="1m", style="scalp", default_kwargs={"timeframe": "1m", "relative_volume_floor": 1.25}),
    StrategySpec("rsi_reversal", timeframe="1m", style="scalp", default_kwargs={"timeframe": "1m"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="1m", style="scalp", default_kwargs={"timeframe": "1m", "minimum_relative_volume": 1.35}),
    StrategySpec("ema_trend_stack", timeframe="1d", style="position", default_kwargs={"timeframe": "1d"}),
    StrategySpec("rsi_trend_continuation", timeframe="1d", style="position", default_kwargs={"timeframe": "1d"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="1h", style="confluence", default_kwargs={"timeframe": "1h"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="1d", style="confluence", default_kwargs={"timeframe": "1d", "minimum_relative_volume": 1.15}),
    StrategySpec("trend_following", timeframe="1w", style="position", default_kwargs={"fast_span": 10, "slow_span": 30, "pullback_window": 4}),
    StrategySpec("momentum_breakout", timeframe="1w", style="position", default_kwargs={"breakout_window": 12, "volume_window": 12}),
    StrategySpec("ema_trend_stack", timeframe="1w", style="position", default_kwargs={"timeframe": "1w"}),
    StrategySpec("rsi_trend_continuation", timeframe="1w", style="position", default_kwargs={"timeframe": "1w"}),
    StrategySpec("rsi_vwap_ema_confluence", timeframe="1w", style="confluence", default_kwargs={"timeframe": "1w", "minimum_relative_volume": 1.0}),
]


def get_strategy(name: str, **kwargs) -> BaseStrategy:
    """Instantiate a strategy by registered name."""

    normalized = name.strip().lower()
    strategy_cls = STRATEGY_REGISTRY.get(normalized)
    if strategy_cls is None:
        available = ", ".join(sorted(STRATEGY_REGISTRY))
        raise ValueError(f"Unknown strategy '{name}'. Available strategies: {available}")
    return strategy_cls(**kwargs)


def get_strategy_specs(
    *,
    timeframe: str | None = None,
    styles: list[str] | None = None,
) -> list[StrategySpec]:
    """Return strategy specs filtered by timeframe and optional style."""

    specs = STRATEGY_SPECS
    if timeframe is not None:
        normalized_timeframe = timeframe.strip().lower()
        specs = [spec for spec in specs if spec.timeframe == normalized_timeframe]
    if styles:
        style_set = {item.strip().lower() for item in styles}
        specs = [spec for spec in specs if spec.style.lower() in style_set]
    return list(specs)


__all__ = [
    "BaseStrategy",
    "GoldMomentumStrategy",
    "IntradayVWAPTrendStrategy",
    "EMATrendStackStrategy",
    "MeanReversionStrategy",
    "MomentumBreakoutStrategy",
    "MACrossoverStrategy",
    "PullbackTrendStrategy",
    "RSIReversalStrategy",
    "RSITrendContinuationStrategy",
    "RSIVWAPEMAConfluenceStrategy",
    "StrategySpec",
    "TrendFollowingStrategy",
    "VWAPReclaimStrategy",
    "get_strategy",
    "get_strategy_specs",
]
