"""Governed AI/generated strategy lab service."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

import httpx

from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import bars_per_year_for, leakage_tripwire_triggered
from app.learning.critic import _response_text
from app.models.institutional import PromotionDecision, StrategyVersion
from app.models.strategy_lab import (
    GeneratedStrategyRecord,
    StrategyBacktestRequest,
    StrategyConceptPackRequest,
    StrategyGenerationRequest,
    StrategyLabBacktestRecord,
    StrategyLabCondition,
    StrategyLabDsl,
    StrategyLabIndicator,
    StrategyPromotionRequest,
)
from app.strategies import StrategySpec
from app.strategy_lab.dsl import GeneratedRuleStrategy
from app.universe import resolve_universe
from app.utils.time import utc_now


STRATEGY_DSL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "timeframe": {"type": "string", "enum": ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]},
        "indicators": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "sma",
                            "ema",
                            "rsi",
                            "volume_sma",
                            "atr",
                            "roc",
                            "bb_upper",
                            "bb_lower",
                            "bb_width",
                            "donchian_high",
                            "donchian_low",
                            "relative_volume",
                            "vwap",
                        ],
                    },
                    "source": {"type": "string", "enum": ["open", "high", "low", "close", "volume"]},
                    "period": {"type": "integer", "minimum": 2, "maximum": 250},
                },
                "required": ["name", "kind", "source", "period"],
            },
        },
        "entry_conditions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["above", "below", "crosses_above", "crosses_below"]},
                    "left": {"type": "string"},
                    "right": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                },
                "required": ["kind", "left", "right"],
            },
        },
        "stop_loss_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 20},
        "take_profit_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 50},
        "max_hold_bars": {"type": "integer", "minimum": 1, "maximum": 500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "parameter_ranges": {"type": "object"},
    },
    "required": [
        "name",
        "description",
        "timeframe",
        "indicators",
        "entry_conditions",
        "stop_loss_pct",
        "take_profit_pct",
        "max_hold_bars",
        "confidence",
        "parameter_ranges",
    ],
}


class StrategyLabService:
    """Create, audit, and paper-promote generated long-only strategy specs."""

    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        market_data_engine: Any,
        backtest_repository: Any,
        run_log_repository: Any,
        strategy_governance: Any,
    ):
        self.settings = settings
        self.repository = repository
        self.market_data = market_data_engine
        self.backtests = backtest_repository
        self.logs = run_log_repository
        self.strategy_governance = strategy_governance

    def status(self) -> dict[str, Any]:
        generated = self.repository.list_generated(limit=1000)
        counts: dict[str, int] = {}
        active_by_timeframe: dict[str, int] = {}
        for item in generated:
            counts[item.status] = counts.get(item.status, 0) + 1
            if item.status == "paper_generated":
                active_by_timeframe[item.dsl.timeframe] = active_by_timeframe.get(item.dsl.timeframe, 0) + 1
        return {
            "enabled": bool(getattr(self.settings, "strategy_lab_enabled", False)),
            "generation_enabled": bool(getattr(self.settings, "strategy_lab_generation_enabled", False)),
            "paper_trading_enabled": bool(getattr(self.settings, "strategy_lab_paper_trading_enabled", False)),
            "max_generations_per_day": int(getattr(self.settings, "strategy_lab_max_generations_per_day", 3) or 0),
            "gates": {
                "min_backtest_trades": int(getattr(self.settings, "strategy_lab_min_backtest_trades", 100) or 100),
                "min_profit_factor": float(getattr(self.settings, "strategy_lab_min_profit_factor", 1.15) or 1.15),
                "max_drawdown_pct": float(getattr(self.settings, "strategy_lab_max_drawdown_pct", 12.0) or 12.0),
                "positive_expectancy_after_costs": True,
            },
            "counts": counts,
            "active_specs_by_timeframe": active_by_timeframe,
        }

    def active_specs(self, *, timeframe: str) -> list[StrategySpec]:
        if not (
            bool(getattr(self.settings, "strategy_lab_enabled", False))
            and bool(getattr(self.settings, "strategy_lab_paper_trading_enabled", False))
        ):
            return []
        normalized = timeframe.strip().lower()
        specs: list[StrategySpec] = []
        for item in self.repository.list_generated(status="paper_generated", limit=500):
            if item.dsl.timeframe != normalized:
                continue
            specs.append(
                StrategySpec(
                    item.name,
                    timeframe=item.dsl.timeframe,
                    style="generated",
                    default_kwargs={"generated_strategy_id": item.id},
                )
            )
        return specs

    def build_strategy_for_spec(self, spec: Any) -> GeneratedRuleStrategy | None:
        generated_id = (getattr(spec, "default_kwargs", {}) or {}).get("generated_strategy_id")
        item = self.repository.get_generated(str(generated_id)) if generated_id else self.repository.get_generated_by_name(spec.name)
        if item is None or item.status != "paper_generated":
            return None
        return GeneratedRuleStrategy(item.dsl)

    def generate(self, request: StrategyGenerationRequest) -> GeneratedStrategyRecord:
        if not bool(getattr(self.settings, "strategy_lab_enabled", False)):
            raise ValueError("strategy_lab_disabled")
        if not bool(getattr(self.settings, "strategy_lab_generation_enabled", False)):
            raise ValueError("strategy_lab_generation_disabled")
        self._enforce_daily_generation_limit()
        dsl = request.dsl or self._generate_dsl_from_prompt(request.prompt)
        item = GeneratedStrategyRecord(name=dsl.name, dsl=dsl, source=request.source or "strategy_lab")
        created = self.repository.create_generated(item)
        self.logs.log("strategy_lab_generated", {"id": created.id, "name": created.name, "source": created.source})
        return created

    def generate_concept_pack(self, request: StrategyConceptPackRequest) -> dict[str, Any]:
        if not bool(getattr(self.settings, "strategy_lab_enabled", False)):
            raise ValueError("strategy_lab_disabled")
        if not bool(getattr(self.settings, "strategy_lab_generation_enabled", False)):
            raise ValueError("strategy_lab_generation_disabled")
        created: list[GeneratedStrategyRecord] = []
        existing: list[GeneratedStrategyRecord] = []
        for dsl in _concept_pack_dsls()[: request.count]:
            prior = self.repository.get_generated_by_name(dsl.name)
            if prior is not None:
                existing.append(prior)
                continue
            item = self.repository.create_generated(
                GeneratedStrategyRecord(name=dsl.name, dsl=dsl, source=request.source)
            )
            created.append(item)
        self.logs.log(
            "strategy_lab_concept_pack_generated",
            {"requested": request.count, "created": len(created), "existing": len(existing)},
        )
        return {"created": created, "existing": existing, "requested": request.count}

    def backtest(self, generated_id: str, request: StrategyBacktestRequest) -> StrategyLabBacktestRecord:
        if not bool(getattr(self.settings, "strategy_lab_enabled", False)):
            raise ValueError("strategy_lab_disabled")
        item = self.repository.get_generated(generated_id)
        if item is None:
            raise KeyError(f"Generated strategy {generated_id} not found")
        symbols = [symbol.upper() for symbol in (request.symbols or resolve_universe(self.settings, limit=request.limit))]
        strategy = GeneratedRuleStrategy(item.dsl)
        engine = BacktestEngine(
            self.backtests,
            config=EngineConfig(
                initial_cash=10_000.0,
                risk_per_trade_pct=float(getattr(self.settings, "max_risk_per_trade_pct", 1.0) or 1.0),
                bars_per_year=bars_per_year_for(item.dsl.timeframe),
            ),
        )
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        all_trades: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                history = self.market_data.get_history(
                    symbol,
                    timeframe=item.dsl.timeframe,
                    bars=_history_bars(item.dsl.timeframe),
                    force_refresh=request.force_refresh,
                )
                result = engine.run(
                    symbol=symbol,
                    strategy=strategy,
                    data=history,
                    file_path=f"strategy_lab:{item.dsl.timeframe}:{symbol}",
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not hide all evidence
                errors.append(f"{symbol}: {exc}")
                continue
            summary = {
                "symbol": symbol,
                "strategy_name": result.strategy_name,
                "timeframe": item.dsl.timeframe,
                **_safe_numbers(result.metrics),
                "warnings": list(result.warnings),
            }
            triggered, reason = leakage_tripwire_triggered(summary)
            if triggered:
                errors.append(f"{symbol}: leakage_tripwire:{reason}")
            results.append(summary)
            all_trades.extend(result.trades)
        metrics = _aggregate_trade_metrics(results, all_trades, errors)
        blockers = self._backtest_blockers(metrics=metrics)
        record = StrategyLabBacktestRecord(
            generated_strategy_id=item.id,
            status="passed" if not blockers else "failed",
            metrics=metrics,
            blockers=blockers,
            results=results,
        )
        saved = self.repository.record_backtest(record)
        self.repository.update_generated_status(item.id, status=item.status, latest_backtest_id=saved.id)
        self.logs.log(
            "strategy_lab_backtest_completed",
            {"id": saved.id, "generated_strategy_id": item.id, "status": saved.status, "blockers": blockers},
        )
        return saved

    def promote_paper(self, generated_id: str, request: StrategyPromotionRequest) -> dict[str, Any]:
        if not (
            bool(getattr(self.settings, "strategy_lab_enabled", False))
            and bool(getattr(self.settings, "strategy_lab_paper_trading_enabled", False))
        ):
            raise ValueError("strategy_lab_paper_trading_disabled")
        item = self.repository.get_generated(generated_id)
        if item is None:
            raise KeyError(f"Generated strategy {generated_id} not found")
        backtest = self.repository.latest_backtest(item.id)
        if backtest is None:
            raise ValueError("strategy_lab_backtest_missing")
        if backtest.status != "passed" or backtest.blockers:
            raise ValueError("strategy_lab_backtest_gates_not_passed")

        version = self.strategy_governance.create_version(
            StrategyVersion(
                strategy_name=item.name,
                code_version="strategy_lab_dsl_v1",
                parameters={"generated_strategy_id": item.id, "dsl": item.dsl.model_dump()},
                dataset_version=f"strategy_lab_backtest:{backtest.id}",
                timeframe=item.dsl.timeframe,
                status="paper_exploration",
            )
        )
        decision = self.strategy_governance.record_decision(
            PromotionDecision(
                strategy_version_id=version.id,
                target_stage="paper_exploration",
                approved=True,
                evidence={"strategy_lab_backtest_id": backtest.id, "metrics": backtest.metrics},
                decided_by=request.decided_by,
            )
        )
        promoted = self.repository.update_generated_status(item.id, status="paper_generated", latest_backtest_id=backtest.id)
        self.logs.log(
            "strategy_lab_promoted_paper",
            {"generated_strategy_id": item.id, "version_id": version.id, "promotion_id": decision.id},
        )
        return {"strategy": promoted, "strategy_version": version, "promotion_decision": decision}

    def _generate_dsl_from_prompt(self, prompt: str) -> StrategyLabDsl:
        if bool(getattr(self.settings, "learning_openai_enabled", False)) and str(
            getattr(self.settings, "learning_openai_api_key", "") or ""
        ).strip():
            try:
                return self._generate_dsl_with_openai(prompt)
            except Exception as exc:  # noqa: BLE001 - generation failure should be visible, not unsafe
                self.logs.log("strategy_lab_openai_generation_failed", {"error": str(exc)})
                raise ValueError(f"strategy_lab_openai_generation_failed:{exc}") from exc
        return self._template_dsl(prompt)

    def _generate_dsl_with_openai(self, prompt: str) -> StrategyLabDsl:
        payload = {
            "model": getattr(self.settings, "learning_trade_critic_model", "gpt-5.4-mini"),
            "store": False,
            "tools": [],
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Generate one long-only US-equity strategy spec as constrained JSON. "
                        "Use only the provided DSL fields. Do not include code, broker access, "
                        "secrets, files, network access, shorts, crypto, or live trading."
                    ),
                },
                {"role": "user", "content": (prompt or "Create a conservative trend-following paper strategy.")[:1000]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "strategy_lab_dsl",
                    "strict": True,
                    "schema": STRATEGY_DSL_SCHEMA,
                }
            },
        }
        last_error: Exception | None = None
        for attempt in range(int(getattr(self.settings, "learning_openai_max_retries", 2) or 0) + 1):
            try:
                response = httpx.post(
                    f"{str(getattr(self.settings, 'learning_openai_base_url', 'https://api.openai.com/v1')).rstrip('/')}/responses",
                    headers={
                        "Authorization": f"Bearer {str(getattr(self.settings, 'learning_openai_api_key', '')).strip()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=int(getattr(self.settings, "learning_openai_timeout_seconds", 30) or 30),
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"OpenAI Responses API failed: HTTP {response.status_code}")
                return StrategyLabDsl.model_validate_json(_response_text(response.json()))
            except Exception as exc:
                last_error = exc
                if attempt < int(getattr(self.settings, "learning_openai_max_retries", 2) or 0):
                    time.sleep(min(2**attempt, 4))
        raise RuntimeError(str(last_error or "strategy generation failed"))

    def _template_dsl(self, prompt: str) -> StrategyLabDsl:
        digest = hashlib.sha1((prompt or "strategy_lab").encode("utf-8")).hexdigest()[:10]
        return StrategyLabDsl(
            name=f"generated_ai_template_{digest}",
            description=(prompt or "Template generated strategy lab idea")[:200],
            timeframe="15m",
            indicators=[
                StrategyLabIndicator(name="ema_fast", kind="ema", source="close", period=12),
                StrategyLabIndicator(name="ema_slow", kind="ema", source="close", period=26),
                StrategyLabIndicator(name="rsi_14", kind="rsi", source="close", period=14),
                StrategyLabIndicator(name="vol_sma_20", kind="volume_sma", source="volume", period=20),
            ],
            entry_conditions=[
                StrategyLabCondition(kind="above", left="ema_fast", right="ema_slow"),
                StrategyLabCondition(kind="above", left="rsi_14", right=50.0),
                StrategyLabCondition(kind="above", left="volume", right="vol_sma_20"),
            ],
            stop_loss_pct=1.5,
            take_profit_pct=3.0,
            max_hold_bars=20,
            confidence=0.58,
            parameter_ranges={"note": "template_fallback_when_openai_not_configured"},
        )

    def _enforce_daily_generation_limit(self) -> None:
        limit = int(getattr(self.settings, "strategy_lab_max_generations_per_day", 3) or 0)
        if limit <= 0:
            raise ValueError("strategy_lab_generation_daily_limit_zero")
        today = utc_now().date().isoformat()
        generated_today = [
            item
            for item in self.repository.list_generated(limit=1000)
            if str(item.created_at).startswith(today)
        ]
        if len(generated_today) >= limit:
            raise ValueError("strategy_lab_generation_daily_limit_reached")

    def _backtest_blockers(self, *, metrics: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if int(metrics.get("number_of_trades") or 0) < int(getattr(self.settings, "strategy_lab_min_backtest_trades", 100) or 100):
            blockers.append("insufficient_valid_historical_trades")
        if float(metrics.get("expectancy_usd") or 0.0) <= 0:
            blockers.append("non_positive_expectancy_after_costs")
        if float(metrics.get("profit_factor") or 0.0) < float(getattr(self.settings, "strategy_lab_min_profit_factor", 1.15) or 1.15):
            blockers.append("profit_factor_below_strategy_lab_gate")
        if float(metrics.get("max_drawdown_pct") or 0.0) > float(getattr(self.settings, "strategy_lab_max_drawdown_pct", 12.0) or 12.0):
            blockers.append("drawdown_above_strategy_lab_gate")
        if int(metrics.get("data_error_count") or 0) > 0:
            blockers.append("unexplained_data_or_leakage_errors")
        if not bool(metrics.get("valid_stop_target_coverage")):
            blockers.append("invalid_stop_or_target_generation")
        return blockers


def _history_bars(timeframe: str) -> int:
    if timeframe == "1w":
        return 520
    if timeframe == "1d":
        return 1000
    if timeframe == "1h":
        return 1200
    return 1500


def _concept_pack_dsls() -> list[StrategyLabDsl]:
    raw: list[dict[str, Any]] = [
        {
            "name": "generated_trend_breakout_donchian_20",
            "description": "Donchian trend breakout with relative volume confirmation.",
            "timeframe": "1d",
            "indicators": [
                StrategyLabIndicator(name="donchian_high_20", kind="donchian_high", source="high", period=20),
                StrategyLabIndicator(name="ema_50", kind="ema", source="close", period=50),
                StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="close", right="donchian_high_20"),
                StrategyLabCondition(kind="above", left="close", right="ema_50"),
                StrategyLabCondition(kind="above", left="rv_20", right=1.2),
            ],
            "stop_loss_pct": 4.0,
            "take_profit_pct": 9.0,
            "max_hold_bars": 20,
        },
        {
            "name": "generated_hourly_atr_breakout_momentum",
            "description": "Hourly momentum breakout with ATR and ROC confirmation.",
            "timeframe": "1h",
            "indicators": [
                StrategyLabIndicator(name="donchian_high_24", kind="donchian_high", source="high", period=24),
                StrategyLabIndicator(name="roc_12", kind="roc", source="close", period=12),
                StrategyLabIndicator(name="atr_14", kind="atr", source="close", period=14),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="close", right="donchian_high_24"),
                StrategyLabCondition(kind="above", left="roc_12", right=1.0),
                StrategyLabCondition(kind="above", left="atr_14", right=0.1),
            ],
            "stop_loss_pct": 2.0,
            "take_profit_pct": 4.5,
            "max_hold_bars": 16,
        },
        {
            "name": "generated_vwap_pullback_reclaim_15m",
            "description": "Intraday VWAP pullback reclaim with fast EMA support.",
            "timeframe": "15m",
            "indicators": [
                StrategyLabIndicator(name="vwap_line", kind="vwap", source="close", period=20),
                StrategyLabIndicator(name="ema_20", kind="ema", source="close", period=20),
                StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="crosses_above", left="close", right="vwap_line"),
                StrategyLabCondition(kind="above", left="close", right="ema_20"),
                StrategyLabCondition(kind="above", left="rv_20", right=0.9),
            ],
            "stop_loss_pct": 1.2,
            "take_profit_pct": 2.5,
            "max_hold_bars": 12,
        },
        {
            "name": "generated_bb_squeeze_breakout_1h",
            "description": "Bollinger upper-band breakout after compression.",
            "timeframe": "1h",
            "indicators": [
                StrategyLabIndicator(name="bb_upper_20", kind="bb_upper", source="close", period=20),
                StrategyLabIndicator(name="bb_width_20", kind="bb_width", source="close", period=20),
                StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="close", right="bb_upper_20"),
                StrategyLabCondition(kind="below", left="bb_width_20", right=8.0),
                StrategyLabCondition(kind="above", left="rv_20", right=1.1),
            ],
            "stop_loss_pct": 2.0,
            "take_profit_pct": 4.0,
            "max_hold_bars": 18,
        },
        {
            "name": "generated_relative_strength_momentum_daily",
            "description": "Daily momentum continuation using ROC, medium trend, and relative volume.",
            "timeframe": "1d",
            "indicators": [
                StrategyLabIndicator(name="roc_20", kind="roc", source="close", period=20),
                StrategyLabIndicator(name="ema_50", kind="ema", source="close", period=50),
                StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="roc_20", right=3.0),
                StrategyLabCondition(kind="above", left="close", right="ema_50"),
                StrategyLabCondition(kind="above", left="rv_20", right=1.0),
            ],
            "stop_loss_pct": 4.0,
            "take_profit_pct": 8.0,
            "max_hold_bars": 18,
        },
        {
            "name": "generated_volume_expansion_breakout_15m",
            "description": "Intraday volume expansion breakout above short Donchian resistance.",
            "timeframe": "15m",
            "indicators": [
                StrategyLabIndicator(name="donchian_high_12", kind="donchian_high", source="high", period=12),
                StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
                StrategyLabIndicator(name="ema_20", kind="ema", source="close", period=20),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="close", right="donchian_high_12"),
                StrategyLabCondition(kind="above", left="rv_20", right=1.5),
                StrategyLabCondition(kind="above", left="close", right="ema_20"),
            ],
            "stop_loss_pct": 1.1,
            "take_profit_pct": 2.3,
            "max_hold_bars": 10,
        },
        {
            "name": "generated_regime_mean_reversion_daily",
            "description": "Daily mean reversion while price remains above long-term trend.",
            "timeframe": "1d",
            "indicators": [
                StrategyLabIndicator(name="bb_lower_20", kind="bb_lower", source="close", period=20),
                StrategyLabIndicator(name="rsi_14", kind="rsi", source="close", period=14),
                StrategyLabIndicator(name="ema_200", kind="ema", source="close", period=200),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="below", left="close", right="bb_lower_20"),
                StrategyLabCondition(kind="below", left="rsi_14", right=38.0),
                StrategyLabCondition(kind="above", left="close", right="ema_200"),
            ],
            "stop_loss_pct": 5.0,
            "take_profit_pct": 7.5,
            "max_hold_bars": 12,
        },
        {
            "name": "generated_regime_filtered_breakout_weekly",
            "description": "Weekly trend breakout filtered by long-term regime and momentum.",
            "timeframe": "1w",
            "indicators": [
                StrategyLabIndicator(name="donchian_high_26", kind="donchian_high", source="high", period=26),
                StrategyLabIndicator(name="ema_40", kind="ema", source="close", period=40),
                StrategyLabIndicator(name="roc_12", kind="roc", source="close", period=12),
            ],
            "entry_conditions": [
                StrategyLabCondition(kind="above", left="close", right="donchian_high_26"),
                StrategyLabCondition(kind="above", left="close", right="ema_40"),
                StrategyLabCondition(kind="above", left="roc_12", right=4.0),
            ],
            "stop_loss_pct": 6.0,
            "take_profit_pct": 13.0,
            "max_hold_bars": 14,
        },
    ]
    families = list(raw)
    while len(families) < 25:
        base = raw[len(families) % len(raw)]
        variant = dict(base)
        variant_index = len(families) + 1
        variant["name"] = f"{base['name']}_v{variant_index}"
        variant["description"] = f"{base['description']} Variant {variant_index}."
        variant["stop_loss_pct"] = round(float(base["stop_loss_pct"]) * (1.0 + ((variant_index % 3) * 0.1)), 2)
        variant["take_profit_pct"] = round(float(base["take_profit_pct"]) * (1.0 + ((variant_index % 4) * 0.08)), 2)
        variant["confidence"] = 0.55 + ((variant_index % 5) * 0.02)
        families.append(variant)
    dsls: list[StrategyLabDsl] = []
    for item in families:
        payload = dict(item)
        confidence = float(payload.pop("confidence", 0.58))
        dsls.append(
            StrategyLabDsl(
                confidence=confidence,
                parameter_ranges={"concept_pack": True, "family": str(payload["name"]).removeprefix("generated_")},
                **payload,
            )
        )
    return dsls


def _aggregate_trade_metrics(
    results: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    pnl_values = [float(trade.get("pnl_usd", 0.0) or 0.0) for trade in trades]
    winners = [value for value in pnl_values if value > 0]
    losers = [value for value in pnl_values if value < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0
    expectancy = sum(pnl_values) / len(pnl_values) if pnl_values else 0.0
    return {
        "symbols_evaluated": len(results),
        "number_of_trades": len(trades),
        "expectancy_usd": _safe_float(expectancy),
        "profit_factor": _safe_float(profit_factor),
        "max_drawdown_pct": _safe_float(max((float(item.get("max_drawdown_pct", 0.0) or 0.0) for item in results), default=0.0)),
        "win_rate": _safe_float((len(winners) / len(trades)) * 100.0 if trades else 0.0),
        "data_error_count": len(errors),
        "errors": errors[:25],
        "valid_stop_target_coverage": True,
    }


def _safe_numbers(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: _safe_float(value) for key, value in metrics.items()}


def _safe_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(numeric):
        return 0.0
    if math.isinf(numeric):
        return 999.0 if numeric > 0 else -999.0
    return float(numeric)
