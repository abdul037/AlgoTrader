"""Typed, grouped views over the flat runtime settings — a back-compat shim.

``AppSettings`` is one flat surface of ~400 environment-loaded fields. That is
fine for loading but poor for reading: the ~40 knobs an operator actually tunes
are scattered across the file with no grouping. This module adds *typed
sections* (Risk / Execution / Data / Strategy) computed from the existing flat
fields, plus named *operating profiles* that bundle the default-off feature
flags into coherent modes.

It is purely additive and back-compatible: the flat fields are untouched and
``settings.max_daily_loss_usd`` keeps working exactly as before. New code may
instead read ``settings.sections.risk.max_daily_loss_usd`` for a grouped, typed,
discoverable view. Nothing here changes behavior or enables any feature — the
profiles are override *dicts* an operator applies deliberately, not auto-applied.

Fields are pulled with ``getattr(..., default)`` so a section stays valid even
if a flag is renamed or absent; the defaults here mirror the ``AppSettings``
defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _get(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


@dataclass(frozen=True)
class RiskConfig:
    """Position-, loss-, and drawdown-limit knobs."""

    max_risk_per_trade_pct: float
    max_daily_loss_usd: float
    max_weekly_loss_usd: float
    loss_limit_includes_unrealized: bool
    max_open_positions: int
    max_trades_per_day: int
    per_symbol_position_limit: int
    max_consecutive_losses_before_cooldown: int
    portfolio_soft_drawdown_pct: float
    drawdown_governor_enabled: bool
    drawdown_governor_soft_pct: float
    drawdown_governor_hard_pct: float
    drawdown_governor_floor: float

    @classmethod
    def from_settings(cls, s: Any) -> "RiskConfig":
        return cls(
            max_risk_per_trade_pct=float(_get(s, "max_risk_per_trade_pct", 1.0)),
            max_daily_loss_usd=float(_get(s, "max_daily_loss_usd", 50.0)),
            max_weekly_loss_usd=float(_get(s, "max_weekly_loss_usd", 125.0)),
            loss_limit_includes_unrealized=bool(_get(s, "loss_limit_includes_unrealized", True)),
            max_open_positions=int(_get(s, "max_open_positions", 3)),
            max_trades_per_day=int(_get(s, "max_trades_per_day", 6)),
            per_symbol_position_limit=int(_get(s, "per_symbol_position_limit", 1)),
            max_consecutive_losses_before_cooldown=int(_get(s, "max_consecutive_losses_before_cooldown", 2)),
            portfolio_soft_drawdown_pct=float(_get(s, "portfolio_soft_drawdown_pct", 5.0)),
            drawdown_governor_enabled=bool(_get(s, "drawdown_governor_enabled", False)),
            drawdown_governor_soft_pct=float(_get(s, "drawdown_governor_soft_pct", 2.0)),
            drawdown_governor_hard_pct=float(_get(s, "drawdown_governor_hard_pct", 5.0)),
            drawdown_governor_floor=float(_get(s, "drawdown_governor_floor", 0.25)),
        )


@dataclass(frozen=True)
class ExecutionConfig:
    """Order-routing, broker, and session knobs."""

    enable_real_trading: bool
    require_approval: bool
    alpaca_require_bracket_orders: bool
    alpaca_reconciliation_enabled: bool
    alpaca_trade_stream_enabled: bool
    extended_hours_experiment_enabled: bool
    extended_hours_max_notional_usd: float
    extended_hours_max_spread_bps: float

    @classmethod
    def from_settings(cls, s: Any) -> "ExecutionConfig":
        return cls(
            enable_real_trading=bool(_get(s, "enable_real_trading", False)),
            require_approval=bool(_get(s, "require_approval", True)),
            alpaca_require_bracket_orders=bool(_get(s, "alpaca_require_bracket_orders", True)),
            alpaca_reconciliation_enabled=bool(_get(s, "alpaca_reconciliation_enabled", True)),
            alpaca_trade_stream_enabled=bool(_get(s, "alpaca_trade_stream_enabled", False)),
            extended_hours_experiment_enabled=bool(_get(s, "extended_hours_experiment_enabled", False)),
            extended_hours_max_notional_usd=float(_get(s, "extended_hours_max_notional_usd", 100.0)),
            extended_hours_max_spread_bps=float(_get(s, "extended_hours_max_spread_bps", 75.0)),
        )


@dataclass(frozen=True)
class DataConfig:
    """Market-data provider and freshness knobs."""

    primary_market_data_provider: str
    fallback_market_data_provider: str
    alpaca_data_feed: str
    market_data_retry_attempts: int
    market_data_cache_ttl_seconds: int
    max_market_data_age_seconds: int
    require_verified_market_data_for_alerts: bool
    require_primary_provider_for_alerts: bool

    @classmethod
    def from_settings(cls, s: Any) -> "DataConfig":
        return cls(
            primary_market_data_provider=str(_get(s, "primary_market_data_provider", "alpaca")),
            fallback_market_data_provider=str(_get(s, "fallback_market_data_provider", "yfinance")),
            alpaca_data_feed=str(_get(s, "alpaca_data_feed", "iex")),
            market_data_retry_attempts=int(_get(s, "market_data_retry_attempts", 2)),
            market_data_cache_ttl_seconds=int(_get(s, "market_data_cache_ttl_seconds", 60)),
            max_market_data_age_seconds=int(_get(s, "max_market_data_age_seconds", 120)),
            require_verified_market_data_for_alerts=bool(_get(s, "require_verified_market_data_for_alerts", True)),
            require_primary_provider_for_alerts=bool(_get(s, "require_primary_provider_for_alerts", False)),
        )


@dataclass(frozen=True)
class StrategyConfig:
    """Selection-layer knobs: which strategies run and how the book concentrates."""

    screener_primary_strategy_name: str
    screener_active_strategy_names: tuple[str, ...]
    screener_top_k: int
    screener_min_confidence: float
    regime_router_enabled: bool
    cross_sectional_momentum_enabled: bool
    cross_sectional_momentum_top_pct: float

    @classmethod
    def from_settings(cls, s: Any) -> "StrategyConfig":
        active = _get(s, "screener_active_strategy_names", []) or []
        return cls(
            screener_primary_strategy_name=str(_get(s, "screener_primary_strategy_name", "")),
            screener_active_strategy_names=tuple(str(x) for x in active),
            screener_top_k=int(_get(s, "screener_top_k", 5)),
            screener_min_confidence=float(_get(s, "screener_min_confidence", 0.0)),
            regime_router_enabled=bool(_get(s, "regime_router_enabled", False)),
            cross_sectional_momentum_enabled=bool(_get(s, "cross_sectional_momentum_enabled", False)),
            cross_sectional_momentum_top_pct=float(_get(s, "cross_sectional_momentum_top_pct", 30.0)),
        )


@dataclass(frozen=True)
class ConfigSections:
    """Grouped, typed view over the flat runtime settings."""

    risk: RiskConfig
    execution: ExecutionConfig
    data: DataConfig
    strategy: StrategyConfig


def build_config_sections(settings: Any) -> ConfigSections:
    """Assemble the typed sections from a flat settings object."""

    return ConfigSections(
        risk=RiskConfig.from_settings(settings),
        execution=ExecutionConfig.from_settings(settings),
        data=DataConfig.from_settings(settings),
        strategy=StrategyConfig.from_settings(settings),
    )


# --------------------------------------------------------------------------- #
# Named operating profiles — coherent bundles of the default-off feature flags.
# These are override *dicts*, not applied automatically. An operator sets them
# in the environment (one flag at a time, per the Monday runbook) after the
# validation script's verdict says so. They exist to name the modes, not to
# flip anything on their own.
# --------------------------------------------------------------------------- #
OPERATING_PROFILES: dict[str, dict[str, Any]] = {
    # The current default: every gated feature off, so Stage 1 measures the base
    # strategy edge cleanly without overlays confounding the P&L.
    "measurement": {
        "regime_router_enabled": False,
        "cross_sectional_momentum_enabled": False,
        "drawdown_governor_enabled": False,
    },
    # Capital-preservation lean: gate families by regime and scale size down as
    # the day's loss deepens. Enable once the validation script rates both GO.
    "defensive": {
        "regime_router_enabled": True,
        "drawdown_governor_enabled": True,
        "cross_sectional_momentum_enabled": False,
    },
    # Leader-concentration lean: keep only the strongest names in the universe.
    "concentrated": {
        "regime_router_enabled": True,
        "cross_sectional_momentum_enabled": True,
        "drawdown_governor_enabled": True,
    },
}


def profile_overrides(name: str) -> dict[str, Any]:
    """Return the flag overrides for a named operating profile.

    Raises ``KeyError`` with the valid names when ``name`` is unknown, so a
    typo fails loudly rather than silently applying nothing.
    """

    try:
        return dict(OPERATING_PROFILES[name])
    except KeyError as exc:
        valid = ", ".join(sorted(OPERATING_PROFILES))
        raise KeyError(f"unknown operating profile {name!r}; valid: {valid}") from exc
