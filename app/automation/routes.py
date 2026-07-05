"""Automation control routes."""

from __future__ import annotations

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models.automation import AutomationStateChange, AutomationStatus
from app.models.execution_queue import ExecutionQueueStatus
from app.strategies.catalog import build_strategy_catalog_report
from app.utils.time import utc_now

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


def _date_part(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    return str(timestamp)[:10]


def _success_rate(values: list[bool]) -> float:
    if not values:
        return 0.0
    return round(sum(1 for item in values if item) / len(values), 4)


def _paper_performance_metrics(pnl_values: list[float]) -> dict[str, float | None]:
    if not pnl_values:
        return {
            "gross_pnl_usd": 0.0,
            "net_pnl_usd": 0.0,
            "profit_factor": None,
            "expectancy_usd": 0.0,
            "max_drawdown_usd": 0.0,
        }
    gains = sum(value for value in pnl_values if value > 0)
    losses = abs(sum(value for value in pnl_values if value < 0))
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnl_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    total = round(sum(pnl_values), 4)
    return {
        "gross_pnl_usd": total,
        "net_pnl_usd": total,
        "profit_factor": None if losses == 0 else round(gains / losses, 4),
        "expectancy_usd": round(total / len(pnl_values), 4),
        "max_drawdown_usd": round(max_drawdown, 4),
    }


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
    rl_policy_status = request.app.state.rl_policy_service.status()
    workflow_status = request.app.state.workflow_service.status()
    institutional_readiness = request.app.state.institutional_service.readiness()
    approved_versions = list(institutional_readiness.get("approved_strategy_versions") or [])
    approved_exploration_strategies = (
        request.app.state.strategy_governance_repository.approved_paper_exploration_strategies()
    )
    strategy_catalog = build_strategy_catalog_report(
        settings=settings,
        governance=request.app.state.strategy_governance_repository,
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
            "catalog": strategy_catalog,
        },
        "learning": learning_status,
        "rl_policy": rl_policy_status,
    }


@router.get("/production-readiness")
def production_readiness(request: Request):
    _require_control_token(request)
    institutional_readiness = request.app.state.institutional_service.readiness()
    approved_versions = list(institutional_readiness.get("approved_strategy_versions") or [])
    learning_status = request.app.state.learning_service.status()
    workflow_health = request.app.state.workflow_service.lightweight_health()
    reconciliation_rows = request.app.state.safety_state_repository.list_reconciliations(limit=100)
    latest_reconciliation = reconciliation_rows[0] if reconciliation_rows else None
    reconciliation_issues = _json_or_empty(
        latest_reconciliation.get("issues_json") if latest_reconciliation else None,
        [],
    )
    lifecycles = request.app.state.paper_trading_service.lifecycles(limit=1000)
    autonomous = [item for item in lifecycles if item.autonomous]
    closed = [
        item
        for item in autonomous
        if item.flags.entry_filled and item.flags.exit_filled_or_position_flat
    ]
    closed_by_strategy: dict[str, int] = {}
    for item in closed:
        strategy_name = item.strategy_name or "unknown"
        closed_by_strategy[strategy_name] = closed_by_strategy.get(strategy_name, 0) + 1
    promoted_strategy_trade_counts = []
    for version_id in approved_versions:
        try:
            version = request.app.state.strategy_governance_repository.get_version(version_id)
            strategy_name = version.strategy_name
        except Exception:
            strategy_name = "unknown"
        promoted_strategy_trade_counts.append(
            {
                "strategy_version_id": version_id,
                "strategy_name": strategy_name,
                "closed_trades": closed_by_strategy.get(strategy_name, 0),
            }
        )

    duplicate_order_count = sum(1 for item in lifecycles if not item.flags.duplicate_order_absent)
    unprotected_position_count = sum(
        1
        for item in lifecycles
        if item.flags.entry_filled
        and not item.flags.bracket_legs_verified
        and not item.flags.exit_filled_or_position_flat
    )
    unreconciled_lifecycle_count = sum(1 for item in lifecycles if item.flags.entry_filled and not item.flags.reconciled)
    session_dates = sorted(
        {
            date
            for item in closed
            if (date := _date_part(item.updated_at or item.created_at)) is not None
        }
    )
    pnl_values = [float(item.realized_pnl_usd or 0.0) for item in closed]
    performance = _paper_performance_metrics(pnl_values)
    reconciliation_success_rate = _success_rate(
        [str(row.get("status") or "") == "ok" for row in reconciliation_rows]
    )
    critical_alerts = request.app.state.safety_state_repository.list_strategy_health()

    blockers: list[str] = []
    if len(approved_versions) < 1:
        blockers.append("no_production_qualified_strategy")
    if len(closed) < 100:
        blockers.append("insufficient_autonomous_closed_trades")
    if len(session_dates) < 20:
        blockers.append("insufficient_clean_paper_sessions_initial")
    if len(session_dates) < 60:
        blockers.append("insufficient_clean_paper_sessions_production")
    for item in promoted_strategy_trade_counts:
        if int(item["closed_trades"]) < 30:
            blockers.append(f"insufficient_closed_trades_for_promoted_strategy:{item['strategy_version_id']}")
    if duplicate_order_count:
        blockers.append("duplicate_broker_orders_present")
    if unprotected_position_count:
        blockers.append("unprotected_open_positions_present")
    if unreconciled_lifecycle_count:
        blockers.append("unreconciled_lifecycles_present")
    if not latest_reconciliation or latest_reconciliation.get("status") != "ok":
        blockers.append("reconciliation_not_ok")
    if reconciliation_issues:
        blockers.append("unresolved_reconciliation_issues")
    if int(learning_status.get("failed_jobs") or 0) > 0:
        blockers.append("unresolved_failed_learning_jobs")
    blockers.extend(workflow_health.get("blockers") or [])
    if critical_alerts:
        blockers.append("unresolved_strategy_health_alerts")

    initial_validated_blockers = [
        item
        for item in blockers
        if item != "insufficient_clean_paper_sessions_production"
    ]
    return {
        "mode": "production_grade_paper_readiness",
        "ready": not blockers,
        "initial_validated_ready": not initial_validated_blockers,
        "production_grade_ready": not blockers,
        "generated_at": utc_now().isoformat(),
        "blockers": sorted(set(blockers)),
        "gates": {
            "minimum_initial_clean_sessions": 20,
            "minimum_production_clean_sessions": 60,
            "minimum_closed_autonomous_trades": 100,
            "minimum_closed_trades_per_promoted_strategy": 30,
            "zero_duplicate_orders": duplicate_order_count == 0,
            "zero_unprotected_positions": unprotected_position_count == 0,
            "no_failed_learning_jobs": int(learning_status.get("failed_jobs") or 0) == 0,
            "clean_reconciliation": bool(
                latest_reconciliation
                and latest_reconciliation.get("status") == "ok"
                and not reconciliation_issues
            ),
        },
        "metrics": {
            "production_qualified_strategy_count": len(approved_versions),
            "production_qualified_strategy_versions": approved_versions,
            "autonomous_lifecycle_count": len(autonomous),
            "autonomous_closed_trade_count": len(closed),
            "closed_trades_by_strategy": closed_by_strategy,
            "closed_trades_by_promoted_strategy": promoted_strategy_trade_counts,
            "paper_sessions_observed": len(session_dates),
            "paper_session_dates": session_dates,
            "duplicate_order_count": duplicate_order_count,
            "unprotected_position_count": unprotected_position_count,
            "unreconciled_lifecycle_count": unreconciled_lifecycle_count,
            "reconciliation_success_rate": reconciliation_success_rate,
            "gross_pnl_usd": performance["gross_pnl_usd"],
            "net_pnl_usd": performance["net_pnl_usd"],
            "profit_factor": performance["profit_factor"],
            "expectancy_usd": performance["expectancy_usd"],
            "max_drawdown_usd": performance["max_drawdown_usd"],
            "learning_failed_jobs": int(learning_status.get("failed_jobs") or 0),
            "unresolved_critical_alert_count": len(critical_alerts),
        },
        "reconciliation": {
            "latest_status": latest_reconciliation.get("status") if latest_reconciliation else "never_run",
            "latest_account_number": latest_reconciliation.get("account_number") if latest_reconciliation else None,
            "latest_created_at": latest_reconciliation.get("created_at") if latest_reconciliation else None,
            "issues": reconciliation_issues,
        },
        "scheduler": workflow_health,
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
