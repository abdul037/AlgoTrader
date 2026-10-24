"""Response and request models of the system endpoints in ``app.main`` (moved out of it, unchanged)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health response."""

    status: str
    account_mode: str
    require_approval: bool
    reason: str = "healthy"
    last_successful_screener_run_at: str | None = None
    last_successful_ledger_cycle_at: str | None = None
    pending_match_count: int = 0
    pending_match_older_than_24h_count: int = 0
    active_meta_model_version: str | None = None
    model_deployment_mode: str = "shadow"
    current_regime_label: str | None = None
    last_etoro_api_error: str | None = None
    last_etoro_api_error_at: str | None = None


class BacktestRunRequest(BaseModel):
    """Backtest trigger request."""

    symbol: str
    strategy: str
    file_path: str
    initial_cash: float = Field(default=10000.0, gt=0)


class ConfigSummary(BaseModel):
    """Safe redacted configuration summary."""

    account_mode: str
    enable_real_trading: bool
    require_approval: bool
    allowed_instruments: list[str]
    blocked_instruments: list[str]
    max_risk_per_trade_pct: float
    max_daily_loss_usd: float
    max_trades_per_day: int
    max_open_positions: int
    default_equity_leverage: int
    max_equity_leverage: int
    max_gold_leverage: int
    broker_simulation_enabled: bool
    market_universe_name: str
    market_universe_limit: int
    primary_market_data_provider: str
    fallback_market_data_provider: str
    screener_default_timeframes: list[str]
    screener_top_k: int
    auto_propose_enabled: bool
    auto_execute_after_approval: bool
    paper_auto_operation_mode: str
    institutional_portfolio_controls_enabled: bool
    etoro_demo_v2_enabled: bool
    etoro_parallel_comparison_enabled: bool
    rollout_stage: str
    learning_capture_enabled: bool
    learning_worker_enabled: bool
    learning_reviews_enabled: bool
    learning_training_enabled: bool
    learning_auto_promote_paper_enabled: bool
    model_deployment_mode: str
    automation_paused: bool
    automation_kill_switch_enabled: bool
