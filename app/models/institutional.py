"""Persistent institutional governance and multi-broker models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class StrategyVersion(BaseModel):
    """Versioned strategy code, parameters, and research dataset identity."""

    id: str = Field(default_factory=lambda: generate_id("stratver"))
    strategy_name: str
    code_version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    dataset_version: str
    timeframe: str
    status: str = "draft"
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class StrategyAudit(BaseModel):
    """Production-qualification evidence for one strategy version."""

    id: str = Field(default_factory=lambda: generate_id("audit"))
    strategy_version_id: str
    dataset_version: str
    timeframe: str
    out_of_sample_trades: int = Field(default=0, ge=0)
    deflated_sharpe: float = 0.0
    rolling_sharpe: float = 0.0
    profit_factor: float = 0.0
    expectancy_after_costs: float = 0.0
    max_drawdown_pct: float = Field(default=0.0, ge=0.0)
    strategy_drawdown_pct: float = Field(default=0.0, ge=0.0)
    unexplained_errors: int = Field(default=0, ge=0)
    protected_exit_coverage_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class PromotionDecision(BaseModel):
    """Signed strategy promotion decision and its blockers."""

    id: str = Field(default_factory=lambda: generate_id("promotion"))
    strategy_version_id: str
    strategy_audit_id: str | None = None
    target_stage: str = "production_candidate"
    approved: bool = False
    blockers: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    decided_by: str = "system"
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class BrokerCapability(BaseModel):
    """Observed capability contract for a broker and account mode."""

    id: str = Field(default_factory=lambda: generate_id("broker_cap"))
    broker: str
    account_mode: str
    supports_equities: bool = False
    supports_native_protection: bool = False
    supports_client_idempotency: bool = False
    supports_shorting: bool = False
    supports_borrow_checks: bool = False
    supports_financing_costs: bool = False
    verified: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class BrokerAccountIdentity(BaseModel):
    """Non-secret broker account identity and expected-account check."""

    id: str = Field(default_factory=lambda: generate_id("broker_identity"))
    broker: str
    account_mode: str
    account_id: str = ""
    account_number: str = ""
    expected_account_number: str = ""
    verified: bool = False
    status: str = "unknown"
    details: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class BrokerReconciliationResult(BaseModel):
    """Normalized reconciliation outcome for any broker."""

    id: str = Field(default_factory=lambda: generate_id("broker_recon"))
    broker: str
    account_id: str = ""
    status: str
    orders_seen: int = Field(default=0, ge=0)
    positions_seen: int = Field(default=0, ge=0)
    unknown_positions: int = Field(default=0, ge=0)
    unprotected_positions: int = Field(default=0, ge=0)
    issues: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class BrokerComparison(BaseModel):
    """Matched lifecycle execution-quality comparison between brokers."""

    id: str = Field(default_factory=lambda: generate_id("broker_cmp"))
    signal_id: str | None = None
    symbol: str
    strategy_name: str
    primary_broker: str = "alpaca"
    comparison_broker: str = "etoro"
    primary_order_id: str = ""
    comparison_order_id: str = ""
    status: str = "pending"
    primary_fill_price: float | None = None
    comparison_fill_price: float | None = None
    primary_cost_usd: float = 0.0
    comparison_cost_usd: float = 0.0
    primary_slippage_bps: float | None = None
    comparison_slippage_bps: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class PortfolioRiskSnapshot(BaseModel):
    """Point-in-time portfolio risk and exposure state."""

    id: str = Field(default_factory=lambda: generate_id("portfolio_risk"))
    broker: str = "aggregate"
    equity_usd: float = Field(gt=0.0)
    peak_equity_usd: float = Field(gt=0.0)
    drawdown_pct: float = Field(default=0.0, ge=0.0)
    gross_exposure_pct: float = Field(default=0.0, ge=0.0)
    largest_symbol_exposure_pct: float = Field(default=0.0, ge=0.0)
    largest_sector_exposure_pct: float = Field(default=0.0, ge=0.0)
    largest_correlated_exposure_pct: float = Field(default=0.0, ge=0.0)
    open_positions: int = Field(default=0, ge=0)
    status: str = "ok"
    blockers: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class RolloutGateEvidence(BaseModel):
    """Auditable evidence for a rollout-stage gate."""

    id: str = Field(default_factory=lambda: generate_id("gate"))
    stage: str
    gate_name: str
    status: str = "pending"
    evidence: dict[str, Any] = Field(default_factory=dict)
    signed_by: str = ""
    observed_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
