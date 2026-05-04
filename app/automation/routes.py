"""Automation control routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models.automation import AutomationStateChange, AutomationStatus

router = APIRouter(prefix="/automation", tags=["automation"])


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
