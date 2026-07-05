"""Protected production strategy qualification routes."""

from __future__ import annotations

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.models.institutional import StrategyAudit, StrategyVersion

router = APIRouter(prefix="/strategies/qualification", tags=["strategies"])


class QualificationRunRequest(BaseModel):
    """Request for a protected strategy qualification batch."""

    strategy_names: list[str] | None = None
    timeframes: list[str] | None = None
    symbols: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=500)
    walk_forward: bool = True
    force_refresh: bool = False
    decided_by: str = "qualification_api"


def _require_control_token(request: Request) -> None:
    expected = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not expected:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Control token required")


@router.get("/status")
def qualification_status(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
):
    _require_control_token(request)
    repository = request.app.state.strategy_governance_repository
    versions = repository.list_versions(limit=limit)
    audits = repository.list_audits(limit=limit)
    decisions = repository.list_decisions(limit=limit)
    latest_audit_by_version = {audit.strategy_version_id: audit for audit in audits}
    latest_decision_by_version = {decision.strategy_version_id: decision for decision in decisions}
    approved_versions = repository.approved_production_versions()
    items = []
    for version in versions:
        audit = latest_audit_by_version.get(version.id)
        decision = latest_decision_by_version.get(version.id)
        items.append(
            {
                "version": version.model_dump(),
                "latest_audit": audit.model_dump() if audit else None,
                "latest_decision": decision.model_dump() if decision else None,
            }
        )
    return {
        "production_qualified_count": len(approved_versions),
        "production_qualified_strategy_versions": approved_versions,
        "items": items,
    }


@router.post("/run")
def qualification_run(request: Request, payload: QualificationRunRequest):
    _require_control_token(request)
    summary = request.app.state.batch_backtest_service.run(
        symbols=payload.symbols,
        timeframes=payload.timeframes,
        strategy_names=payload.strategy_names,
        limit=payload.limit,
        force_refresh=payload.force_refresh,
        walk_forward=payload.walk_forward,
    )
    repository = request.app.state.strategy_governance_repository
    institutional = request.app.state.institutional_service
    errors = list(getattr(summary, "errors", []) or [])
    decisions: list[dict[str, Any]] = []
    for ranking in list(getattr(summary, "audit_rankings", []) or []):
        strategy_name = str(ranking.get("strategy_name") or "").strip()
        timeframe = str(ranking.get("timeframe") or "").strip()
        if not strategy_name or not timeframe:
            continue
        dataset_version = _dataset_version(summary, ranking)
        version = repository.create_version(
            StrategyVersion(
                strategy_name=strategy_name,
                code_version="runtime-current",
                parameters={
                    "timeframe": timeframe,
                    "qualification_source": "batch_backtest",
                    "ranking": ranking,
                },
                dataset_version=dataset_version,
                timeframe=timeframe,
                status="qualification_audit",
            )
        )
        matching_errors = _matching_errors(errors, strategy_name=strategy_name, timeframe=timeframe)
        audit = repository.record_audit(
            StrategyAudit(
                strategy_version_id=version.id,
                dataset_version=dataset_version,
                timeframe=timeframe,
                out_of_sample_trades=int(ranking.get("total_trades") or 0),
                deflated_sharpe=float(ranking.get("average_sharpe_like") or 0.0),
                rolling_sharpe=float(ranking.get("average_sharpe_like") or 0.0),
                profit_factor=float(ranking.get("average_profit_factor") or 0.0),
                expectancy_after_costs=float(ranking.get("average_expectancy_usd") or 0.0),
                max_drawdown_pct=float(ranking.get("average_max_drawdown_pct") or 0.0),
                strategy_drawdown_pct=float(ranking.get("average_max_drawdown_pct") or 0.0),
                unexplained_errors=len(matching_errors),
                protected_exit_coverage_pct=0.0 if _has_invalid_protection_error(matching_errors) else 100.0,
                metrics={
                    "ranking": ranking,
                    "matching_errors": matching_errors,
                    "batch_generated_at": getattr(summary, "generated_at", ""),
                    "walk_forward": payload.walk_forward,
                    "protected_exit_coverage_source": "strategy_signal_contract",
                },
            )
        )
        decision = institutional.assess_strategy(version.id, decided_by=payload.decided_by)
        decisions.append(
            {
                "strategy_name": strategy_name,
                "timeframe": timeframe,
                "version": version.model_dump(),
                "audit": audit.model_dump(),
                "decision": decision.model_dump(),
            }
        )
    return {
        "generated_at": getattr(summary, "generated_at", ""),
        "symbols_evaluated": getattr(summary, "symbols_evaluated", 0),
        "strategy_runs": getattr(summary, "strategy_runs", 0),
        "errors": errors,
        "decisions": decisions,
        "approved_count": sum(1 for item in decisions if item["decision"]["approved"]),
        "failed_count": sum(1 for item in decisions if not item["decision"]["approved"]),
    }


def _dataset_version(summary: Any, ranking: dict[str, Any]) -> str:
    generated_at = str(getattr(summary, "generated_at", "unknown") or "unknown").replace(":", "").replace("+", "")
    provider = str(getattr(summary, "provider", "unknown") or "unknown")
    timeframe = str(ranking.get("timeframe") or "unknown")
    return f"qualification:{provider}:{timeframe}:{generated_at}"


def _matching_errors(errors: list[str], *, strategy_name: str, timeframe: str) -> list[str]:
    strategy_lower = strategy_name.lower()
    timeframe_lower = timeframe.lower()
    return [
        error
        for error in errors
        if strategy_lower in str(error).lower() and timeframe_lower in str(error).lower()
    ]


def _has_invalid_protection_error(errors: list[str]) -> bool:
    haystack = " ".join(errors).lower()
    invalid_tokens = (
        "invalid stop",
        "invalid target",
        "negative take-profit",
        "negative take profit",
    )
    return any(token in haystack for token in invalid_tokens)
