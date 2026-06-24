"""Automation control routes."""

from __future__ import annotations

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models.automation import AutomationStateChange, AutomationStatus
from app.models.execution_queue import ExecutionQueueStatus

router = APIRouter(prefix="/automation", tags=["automation"])


class BlacklistChange(BaseModel):
    symbol: str
    reason: str = "manual operator blacklist"


def _automation(request: Request):
    return request.app.state.automation_service


def _require_control_token(request: Request) -> None:
    expected = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not expected:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Control token required")


def _json_or_empty(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        import json

        return json.loads(str(raw))
    except Exception:
        return default


@router.get("/status", response_model=AutomationStatus)
def automation_status(request: Request) -> AutomationStatus:
    return _automation(request).status()


@router.get("/continuous-readiness")
def continuous_readiness(request: Request):
    _require_control_token(request)
    settings = request.app.state.settings
    automation = _automation(request)
    latest_reconciliation = (
        request.app.state.safety_state_repository.latest_reconciliation()
        or {"status": "never_run", "issues_json": "[]", "positions_seen": 0}
    )
    reconciliation_issues = _json_or_empty(latest_reconciliation.get("issues_json"), [])
    learning_status = request.app.state.learning_service.status()
    workflow_status = request.app.state.workflow_service.status()
    institutional_readiness = request.app.state.institutional_service.readiness()
    approved_versions = list(institutional_readiness.get("approved_strategy_versions") or [])
    approved_exploration_strategies = (
        request.app.state.strategy_governance_repository.approved_paper_exploration_strategies()
    )
    queued = request.app.state.execution_queue_repository.list(
        status=ExecutionQueueStatus.QUEUED,
        limit=200,
    )
    processing = request.app.state.execution_queue_repository.list(
        status=ExecutionQueueStatus.PROCESSING,
        limit=200,
    )
    strategy_health = request.app.state.safety_state_repository.list_strategy_health()

    blockers: list[str] = []
    blockers.extend(automation.execution_blockers())
    if str(settings.execution_mode) != "paper":
        blockers.append("execution_mode_not_paper")
    if bool(settings.enable_real_trading):
        blockers.append("real_trading_enabled")
    if not bool(settings.alpaca_expected_account_number):
        blockers.append("alpaca_expected_account_missing")
    if latest_reconciliation.get("status") != "ok":
        blockers.append("reconciliation_not_ok")
    if int(latest_reconciliation.get("positions_seen") or 0) > int(settings.max_open_positions):
        blockers.append("open_position_limit_exceeded")
    if reconciliation_issues:
        blockers.append("reconciliation_issues_present")
    if not approved_versions:
        blockers.append("no_production_approved_strategy")
    if int(learning_status.get("failed_jobs") or 0) > 0:
        blockers.append("learning_failed_jobs_present")
    if (
        str(settings.model_deployment_mode) == "gating"
        and not learning_status.get("active_model_version")
    ):
        blockers.append("model_gating_without_champion")
    if bool(settings.extended_hours_experiment_submit_enabled):
        blockers.append("extended_hours_submit_enabled")

    auto_flags_ready = (
        bool(settings.auto_propose_enabled)
        and bool(settings.paper_auto_approve_proposals)
        and bool(settings.auto_execution_worker_enabled)
        and str(settings.paper_auto_operation_mode) == "unattended"
    )
    exploration_blockers = [
        item
        for item in blockers
        if item
        not in {
            "no_production_approved_strategy",
            "learning_failed_jobs_present",
            "model_gating_without_champion",
        }
    ]
    if not bool(settings.paper_scanner_exploration_enabled):
        exploration_blockers.append("paper_scanner_exploration_disabled")
    if not bool(settings.paper_scanner_bypass_production_approval):
        exploration_blockers.append("paper_scanner_bypass_disabled")
    if not approved_exploration_strategies:
        exploration_blockers.append("no_paper_exploration_approved_strategy")
    if not auto_flags_ready:
        exploration_blockers.append("auto_flags_not_ready")
    if not bool(settings.alpaca_require_bracket_orders):
        exploration_blockers.append("bracket_orders_not_required")
    if not bool(settings.paper_exploration_require_regular_hours):
        exploration_blockers.append("paper_exploration_regular_hours_not_required")
    return {
        "mode": "continuous_paper",
        "ready_for_unattended": not blockers and auto_flags_ready,
        "ready_for_paper_exploration": not exploration_blockers,
        "shadow_ready": not [
            item
            for item in blockers
            if item
            not in {
                "no_production_approved_strategy",
                "learning_failed_jobs_present",
            }
        ],
        "blockers": sorted(set(blockers)),
        "trading": {
            "execution_mode": settings.execution_mode,
            "enable_real_trading": settings.enable_real_trading,
            "paper_broker": settings.paper_broker,
            "alpaca_expected_account_number": settings.alpaca_expected_account_number,
            "auto_propose_enabled": settings.auto_propose_enabled,
            "paper_auto_approve_proposals": settings.paper_auto_approve_proposals,
            "auto_execution_worker_enabled": settings.auto_execution_worker_enabled,
            "paper_auto_operation_mode": settings.paper_auto_operation_mode,
        },
        "paper_exploration": {
            "enabled": settings.paper_scanner_exploration_enabled,
            "bypass_production_approval": settings.paper_scanner_bypass_production_approval,
            "allowed_strategies": list(settings.paper_scanner_allowed_strategies),
            "require_backtest_validated": settings.paper_exploration_require_backtest_validated,
            "require_regular_hours": settings.paper_exploration_require_regular_hours,
            "ready": not exploration_blockers,
            "blockers": sorted(set(exploration_blockers)),
            "approved_strategies": approved_exploration_strategies,
        },
        "risk_caps": {
            "default_trade_amount_usd": settings.default_trade_amount_usd,
            "max_trade_amount_usd": settings.max_trade_amount_usd,
            "max_open_positions": settings.max_open_positions,
            "max_trades_per_day": settings.max_trades_per_day,
            "max_daily_loss_usd": settings.max_daily_loss_usd,
            "max_weekly_loss_usd": settings.max_weekly_loss_usd,
            "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
        },
        "regular_hours": {
            "bracket_orders_required": settings.alpaca_require_bracket_orders,
            "regular_hours_only": settings.auto_execution_regular_hours_only,
        },
        "extended_hours": {
            "mode": "supervised",
            "enabled": settings.extended_hours_experiment_enabled,
            "submit_enabled": settings.extended_hours_experiment_submit_enabled,
            "whitelist": list(settings.extended_hours_whitelist),
            "max_notional_usd": settings.extended_hours_max_notional_usd,
        },
        "queue": {
            "queued": len(queued),
            "processing": len(processing),
        },
        "scan_health": {
            "scheduler_enabled": workflow_status.scheduler_enabled,
            "last_premarket_scan_at": workflow_status.last_premarket_scan_at,
            "last_market_open_scan_at": workflow_status.last_market_open_scan_at,
            "last_intraday_scan_at": workflow_status.last_intraday_scan_at,
            "last_swing_scan_at": workflow_status.last_swing_scan_at,
            "last_end_of_day_scan_at": workflow_status.last_end_of_day_scan_at,
            "market_data_timeout_seconds": settings.screener_market_data_timeout_seconds,
            "batch_deadline_seconds": settings.screener_batch_deadline_seconds,
        },
        "reconciliation": {
            "status": latest_reconciliation.get("status"),
            "account_number": latest_reconciliation.get("account_number"),
            "orders_seen": latest_reconciliation.get("orders_seen"),
            "positions_seen": latest_reconciliation.get("positions_seen"),
            "issues": reconciliation_issues,
            "created_at": latest_reconciliation.get("created_at"),
        },
        "strategies": {
            "approved_production_versions": approved_versions,
            "approved_paper_exploration_strategies": approved_exploration_strategies,
            "strategy_health": strategy_health,
        },
        "learning": learning_status,
    }


@router.post("/pause", response_model=AutomationStatus)
def automation_pause(request: Request, payload: AutomationStateChange | None = None) -> AutomationStatus:
    return _automation(request).pause(reason=(payload.reason if payload else ""))


@router.post("/resume", response_model=AutomationStatus)
def automation_resume(request: Request, payload: AutomationStateChange | None = None) -> AutomationStatus:
    return _automation(request).resume(reason=(payload.reason if payload else ""))


@router.post("/kill-switch", response_model=AutomationStatus)
def automation_kill_switch(request: Request, payload: AutomationStateChange | None = None) -> AutomationStatus:
    return _automation(request).enable_kill_switch(reason=(payload.reason if payload else ""))


@router.get("/reconciliation")
def reconciliation_status(request: Request):
    return request.app.state.safety_state_repository.latest_reconciliation() or {"status": "never_run"}


@router.post("/reconciliation/run")
def reconciliation_run(request: Request):
    return request.app.state.reconciliation_service.reconcile()


@router.get("/blacklist")
def blacklist_status(request: Request):
    return request.app.state.safety_state_repository.list_blacklist()


@router.post("/blacklist")
def blacklist_add(request: Request, payload: BlacklistChange):
    request.app.state.safety_state_repository.blacklist(payload.symbol, reason=payload.reason)
    return {"symbol": payload.symbol.upper(), "active": True, "reason": payload.reason}


@router.delete("/blacklist/{symbol}")
def blacklist_remove(symbol: str, request: Request):
    request.app.state.safety_state_repository.unblacklist(symbol)
    return {"symbol": symbol.upper(), "active": False}


@router.get("/strategy-health")
def strategy_health(request: Request):
    request.app.state.auto_trading_service.refresh_strategy_health()
    return request.app.state.safety_state_repository.list_strategy_health()


@router.post("/circuit-breaker/clear")
def circuit_breaker_clear(request: Request):
    result = request.app.state.reconciliation_service.reconcile()
    if result.get("status") != "ok":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Circuit clear requires clean reconciliation", "issues": result.get("issues") or []},
        )
    return _automation(request).clear_circuit_breaker()
