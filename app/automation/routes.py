"""Automation control routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models.automation import AutomationStateChange, AutomationStatus

router = APIRouter(prefix="/automation", tags=["automation"])


class BlacklistChange(BaseModel):
    symbol: str
    reason: str = "manual operator blacklist"


def _automation(request: Request):
    return request.app.state.automation_service


@router.get("/status", response_model=AutomationStatus)
def automation_status(request: Request) -> AutomationStatus:
    return _automation(request).status()


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
