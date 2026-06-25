"""Models for generated paper-only strategy lab."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.ids import generate_id
from app.utils.time import utc_now

IndicatorKind = Literal["sma", "ema", "rsi", "volume_sma"]
ConditionKind = Literal["above", "below", "crosses_above", "crosses_below"]
Timeframe = Literal["1m", "5m", "10m", "15m", "1h", "1d", "1w"]


class StrategyLabIndicator(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: IndicatorKind
    source: Literal["close", "volume"] = "close"
    period: int = Field(ge=2, le=250)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("_", "").isalnum():
            raise ValueError("indicator name must be alphanumeric or underscore")
        return normalized


class StrategyLabCondition(BaseModel):
    kind: ConditionKind
    left: str = Field(min_length=1, max_length=40)
    right: str | float = Field()

    @field_validator("left")
    @classmethod
    def _safe_left(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.replace("_", "").isalnum():
            raise ValueError("condition left operand must be safe")
        return normalized

    @field_validator("right")
    @classmethod
    def _safe_right(cls, value: str | float) -> str | float:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized.replace("_", "").isalnum():
                raise ValueError("condition right operand must be safe")
            return normalized
        return value


class StrategyLabDsl(BaseModel):
    name: str = Field(min_length=3, max_length=64)
    description: str = Field(default="", max_length=500)
    timeframe: Timeframe
    indicators: list[StrategyLabIndicator] = Field(min_length=1, max_length=8)
    entry_conditions: list[StrategyLabCondition] = Field(min_length=1, max_length=8)
    stop_loss_pct: float = Field(gt=0.0, le=20.0)
    take_profit_pct: float = Field(gt=0.0, le=50.0)
    max_hold_bars: int = Field(default=20, ge=1, le=500)
    confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    parameter_ranges: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _safe_strategy_name(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not normalized.startswith("generated_"):
            normalized = f"generated_{normalized}"
        if not normalized.replace("_", "").isalnum():
            raise ValueError("strategy name must be alphanumeric or underscore")
        return normalized[:64]

    @model_validator(mode="after")
    def _validate_condition_operands(self) -> "StrategyLabDsl":
        available = {"open", "high", "low", "close", "volume"}
        available.update(indicator.name for indicator in self.indicators)
        for condition in self.entry_conditions:
            if condition.left not in available:
                raise ValueError(f"condition left operand '{condition.left}' is not available")
            if isinstance(condition.right, str) and condition.right not in available:
                raise ValueError(f"condition right operand '{condition.right}' is not available")
        return self


class GeneratedStrategyRecord(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("genstrat"))
    name: str
    status: Literal["generated_shadow", "paper_generated", "rejected", "retired"] = "generated_shadow"
    dsl: StrategyLabDsl
    source: str = "operator"
    latest_backtest_id: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class StrategyLabBacktestRecord(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("genbt"))
    generated_strategy_id: str
    status: Literal["passed", "failed"] = "failed"
    metrics: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class StrategyGenerationRequest(BaseModel):
    prompt: str = Field(default="", max_length=1000)
    dsl: StrategyLabDsl | None = None
    source: str = "operator"


class StrategyBacktestRequest(BaseModel):
    symbols: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=50)
    force_refresh: bool = False


class StrategyPromotionRequest(BaseModel):
    decided_by: str = "strategy_lab"
