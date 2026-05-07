"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        enable_decoding=False,
    )

    app_name: str = "eToro Approval Trading Bot"
    environment: str = "development"
    database_url: str = "sqlite:///./etoro_bot.db"

    etoro_api_key: str = ""
    etoro_user_key: str = Field(
        default="",
        validation_alias=AliasChoices("ETORO_USER_KEY", "ETORO_GENERATED_KEY"),
    )
    etoro_base_url: str = "https://public-api.etoro.com"
    etoro_account_mode: Literal["demo", "real"] = "demo"
    etoro_request_min_interval_seconds: float = 1.25
    etoro_rate_limit_cooldown_seconds: int = 300
    enable_real_trading: bool = False

    require_approval: bool = True
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str | None = None
    telegram_webhook_auto_register: bool = True
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_rate_limit_per_minute: int = 30
    telegram_polling_enabled: bool = False
    telegram_poll_interval_seconds: int = 5
    telegram_command_timeout_seconds: int = 20
    telegram_scan_stale_after_seconds: int = 180
    telegram_scan_default_universe_limit: int = 10
    telegram_propose_top_default_universe_limit: int = 10
    telegram_hourly_alerts_enabled: bool = False
    telegram_alert_interval_minutes: int = 60
    telegram_alert_symbols: list[str] = Field(default_factory=lambda: ["NVDA"])
    market_universe_name: str = "top100_us"
    market_universe_tier: str = "large_cap_leaders"
    market_universe_limit: int = 100
    market_universe_symbols: list[str] = Field(default_factory=list)
    primary_market_data_provider: str = "auto"
    fallback_market_data_provider: str = "none"
    market_data_retry_attempts: int = 2
    market_data_retry_backoff_seconds: float = 0.75
    market_data_cache_dir: str = ".cache/market_data"
    market_data_cache_ttl_seconds: int = 900
    require_verified_market_data_for_alerts: bool = False
    require_primary_provider_for_alerts: bool = False
    require_direct_quote_for_alerts: bool = False
    require_uncached_market_data_for_alerts: bool = False
    max_market_data_age_seconds: int = 120
    screener_default_timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "1d"])
    screener_intraday_timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "10m", "15m"])
    intelligent_scan_timeframes: list[str] = Field(default_factory=lambda: ["5m", "15m", "1h", "1d", "1w"])
    single_symbol_analysis_timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m", "1h", "1d", "1w"])
    screener_active_strategy_names: list[str] = Field(default_factory=lambda: ["rsi_vwap_ema_confluence"])
    screener_primary_strategy_name: str = "rsi_vwap_ema_confluence"
    screener_top_k: int = 20
    screener_min_confidence: float = 0.45
    screener_scheduler_enabled: bool = True
    workflow_scan_default_universe_limit: int = 10
    schedule_timezone: str = "America/New_York"
    premarket_scan_enabled: bool = True
    premarket_scan_time_local: str = "08:30"
    market_open_scan_enabled: bool = True
    market_open_scan_time_local: str = "09:35"
    intraday_repeated_scan_enabled: bool = True
    intraday_scan_start_local: str = "10:00"
    intraday_scan_end_local: str = "15:30"
    intelligent_scan_enabled: bool = True
    intelligent_scan_start_local: str = "09:45"
    intelligent_scan_end_local: str = "15:45"
    intelligent_scan_interval_minutes: int = 120
    end_of_day_scan_enabled: bool = True
    end_of_day_scan_time_local: str = "15:50"
    workflow_lock_timeout_minutes: int = 45
    swing_scan_timeframes: list[str] = Field(default_factory=lambda: ["1d", "1w"])
    swing_scan_interval_minutes: int = 60
    intraday_scan_interval_minutes: int = 15
    scalp_scan_batch_size: int = 20
    intraday_active_shortlist_size: int = 20
    open_signal_check_interval_minutes: int = 5
    daily_summary_hour_utc: int = 20
    track_alerted_signals: bool = True
    track_watchlist_signals: bool = True
    require_backtest_validation_for_alerts: bool = True
    min_backtest_trades_for_alerts: int = 10
    min_backtest_profit_factor: float = 1.2
    min_backtest_annualized_return_pct: float = 5.0
    max_backtest_drawdown_pct: float = 35.0
    screener_weak_backtest_action: Literal["block", "watchlist", "rank_only"] = "watchlist"
    screener_top_alerts_per_run: int = 5
    screener_alert_mode: Literal["digest", "single"] = "digest"
    screener_min_final_score_to_alert: float = 65.0
    screener_duplicate_alert_window_minutes: int = 240
    screener_min_score_improvement_for_repeat: float = 6.0
    screener_min_price: float = 5.0
    screener_max_price: float = 10000.0
    screener_min_last_volume: int = 250_000
    screener_min_average_volume: int = 750_000
    screener_min_average_dollar_volume: float = 20_000_000.0
    screener_min_relative_volume: float = 1.05
    screener_max_spread_bps: float = 50.0
    screener_min_atr_pct: float = 0.35
    screener_max_atr_pct: float = 8.5
    screener_scalp_min_confidence: float = 0.72
    screener_scalp_min_relative_volume: float = 1.25
    screener_scalp_max_spread_bps: float = 18.0
    screener_min_trend_strength_pct: float = 0.25
    screener_min_efficiency_ratio: float = 0.22
    screener_min_reward_to_risk: float = 1.4
    screener_min_indicator_confluence: float = 0.45
    screener_min_execution_quality: float = 0.5
    screener_min_accuracy_score: float = 0.52
    screener_min_confirmation_score: float = 0.45
    screener_max_false_positive_risk: float = 0.68
    screener_min_resistance_atr_distance: float = 0.35
    screener_max_late_entry_atr_multiple: float = 2.4
    screener_min_market_regime_score: float = 0.5
    screener_min_timeframe_alignment_score: float = 0.5
    screener_min_relative_strength_vs_market: float = 0.0
    screener_min_relative_strength_vs_sector: float = -0.25
    screener_min_sector_strength_score: float = 0.45
    screener_min_benchmark_strength_score: float = 0.45
    screener_max_extension_atr_multiple: float = 3.0
    screener_min_backtest_credibility_score: float = 0.35
    screener_min_recent_backtest_consistency: float = 0.35
    screener_min_final_score_to_keep: float = 55.0
    screener_score_weight_setup_quality: float = 18.0
    screener_score_weight_trend_strength: float = 12.0
    screener_score_weight_momentum_confirmation: float = 10.0
    screener_score_weight_liquidity_quality: float = 10.0
    screener_score_weight_volatility_suitability: float = 8.0
    screener_score_weight_reward_to_risk: float = 10.0
    screener_score_weight_execution_quality: float = 8.0
    screener_score_weight_market_regime: float = 10.0
    screener_score_weight_higher_timeframe_alignment: float = 10.0
    screener_score_weight_relative_strength_market: float = 8.0
    screener_score_weight_relative_strength_sector: float = 6.0
    screener_score_weight_time_of_day: float = 4.0
    screener_score_weight_signal_freshness: float = 4.0
    screener_score_weight_backtest_win_rate: float = 8.0
    screener_score_weight_backtest_profit_factor: float = 10.0
    screener_score_weight_backtest_sample_size: float = 6.0
    screener_score_weight_backtest_recent_consistency: float = 8.0
    screener_score_weight_backtest_credibility: float = 8.0
    screener_score_weight_regime_alignment: float = 10.0
    screener_score_weight_indicator_confluence: float = 10.0
    screener_score_weight_accuracy_quality: float = 12.0
    confluence_minimum_score: float = 0.84
    confluence_minimum_relative_volume: float = 1.25
    confluence_minimum_adx: float = 20.0
    confluence_rsi_long_min: float = 54.0
    confluence_rsi_long_max: float = 66.0
    confluence_rsi_short_min: float = 34.0
    confluence_rsi_short_max: float = 46.0
    confluence_max_extension_atr: float = 1.6
    confluence_min_body_to_range: float = 0.32
    confluence_min_close_location: float = 0.62

    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_usd: float = 100.0
    max_weekly_loss_usd: float = 300.0
    max_open_positions: int = 3
    max_trades_per_day: int = 5
    per_symbol_position_limit: int = 1
    max_consecutive_losses_before_cooldown: int = 2
    kill_switch_enabled: bool = False
    automation_paused_default: bool = False
    auto_propose_enabled: bool = False
    auto_execute_after_approval: bool = False
    execution_recheck_quote_before_order: bool = True
    execution_max_entry_drift_bps: float = 35.0
    execution_queue_enabled: bool = True
    execution_mode: Literal["paper", "live"] = "paper"
    paper_trading_enabled: bool = True
    paper_account_balance_usd: float = 100000.0
    paper_slippage_bps: float = 3.0
    backtest_annual_financing_rate_by_asset_class: dict[str, float] = Field(
        default_factory=lambda: {"equity": 0.07, "gold": 0.02, "fx": 0.02}
    )
    backtest_default_half_spread_bps: float = 5.0
    backtest_slippage_impact_k: float = 1.0
    paper_max_hold_minutes_scalp: int = 90
    paper_max_hold_minutes_intraday: int = 480
    paper_max_hold_days_swing: int = 20
    ledger_enabled: bool = True
    ledger_record_alerts_enabled: bool = True
    ledger_cycle_enabled: bool = True
    ledger_cycle_interval_minutes: int = 15
    ledger_match_window_minutes: int = 120
    ledger_pending_expiry_hours: int = 48
    ledger_track_manual_positions_enabled: bool = False
    model_deployment_mode: Literal["shadow", "advisory", "gating"] = "shadow"
    meta_model_path: str = ""

    allowed_instruments: list[str] = Field(
        default_factory=lambda: ["NVDA", "GOOG", "GOOGL", "AMD", "MU", "GOLD"]
    )
    blocked_instruments: list[str] = Field(
        default_factory=lambda: ["OIL", "NATGAS", "SILVER"]
    )

    default_equity_leverage: int = 1
    max_equity_leverage: int = 5
    max_gold_leverage: int = 10

    default_trade_amount_usd: float = 1000.0
    proposal_expiry_minutes: int = 240
    live_signal_interval: str = "OneDay"
    live_signal_candles_count: int = 250
    live_signal_trend_window: int = 100
    live_signal_pullback_window: int = 10
    signal_scan_limit: int = 20
    notify_on_none_signal_change: bool = True
    signal_scan_universe: list[str] = Field(
        default_factory=lambda: [
            "NVDA",
            "AMD",
            "MU",
            "GOOG",
            "GOOGL",
            "AAPL",
            "MSFT",
            "AMZN",
            "META",
            "AVGO",
            "TSLA",
            "CRM",
            "ORCL",
            "NFLX",
            "TSM",
            "ASML",
            "SHOP",
            "UBER",
            "PLTR",
            "INTC",
            "SMCI",
            "ARM",
            "QCOM",
            "ADBE",
            "AMAT",
            "PANW",
            "CRWD",
            "MRVL",
            "ANET",
            "LRCX",
        ]
    )

    @field_validator(
        "allowed_instruments",
        "blocked_instruments",
        "signal_scan_universe",
        "telegram_alert_symbols",
        "market_universe_symbols",
        "screener_default_timeframes",
        "screener_intraday_timeframes",
        "intelligent_scan_timeframes",
        "single_symbol_analysis_timeframes",
        "swing_scan_timeframes",
        "screener_active_strategy_names",
        mode="before",
    )
    @classmethod
    def _parse_csv_list(cls, value: Any, info: ValidationInfo) -> list[str]:
        lowercase_fields = {
            "screener_default_timeframes",
            "screener_intraday_timeframes",
            "intelligent_scan_timeframes",
            "single_symbol_analysis_timeframes",
            "swing_scan_timeframes",
            "screener_active_strategy_names",
        }
        normalize = str.lower if info.field_name in lowercase_fields else str.upper
        if value is None:
            return []
        if isinstance(value, str):
            return [normalize(item.strip()) for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [normalize(str(item).strip()) for item in value if str(item).strip()]
        raise TypeError("Expected a comma-separated string or iterable")

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _parse_chat_id_list(cls, value: Any) -> list[int]:
        def parse_item(item: Any) -> int:
            return int(str(item).strip())

        if value is None:
            return []
        if isinstance(value, str):
            return [parse_item(item) for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [parse_item(item) for item in value if str(item).strip()]
        raise TypeError("Expected a comma-separated string or iterable")

    @field_validator("etoro_account_mode", mode="before")
    @classmethod
    def _normalize_account_mode(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip().lower()
        return str(value).strip().lower()

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Only sqlite:/// URLs are supported in this version.")
        raw_path = self.database_url[len(prefix) :]
        return Path(raw_path).expanduser().resolve()

    @property
    def real_mode_requested(self) -> bool:
        return self.etoro_account_mode == "real"

    @property
    def broker_simulation_enabled(self) -> bool:
        return (
            not self.etoro_api_key
            or not self.etoro_user_key
            or self.etoro_base_url.endswith(".example")
        )

    @property
    def telegram_mode(self) -> str:
        if not self.telegram_enabled:
            return "disabled"
        if self.telegram_polling_enabled:
            return "polling"
        if self.telegram_webhook_url:
            return "webhook"
        return "send-only"


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return a cached settings object."""

    return AppSettings()
