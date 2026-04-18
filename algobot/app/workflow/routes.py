"""Routes for workflow scheduling, tracked signals, and alert history."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.models.screener import ScanDecisionRecord
from app.models.workflow import AlertHistoryRecord, TrackedSignalRecord, WorkflowStatusResponse, WorkflowTaskResponse

router = APIRouter(prefix="/workflow", tags=["workflow"])


def _workflow(request: Request):
    return request.app.state.workflow_service


@router.get("/status", response_model=WorkflowStatusResponse)
def workflow_status(request: Request) -> WorkflowStatusResponse:
    return _workflow(request).status()


@router.get("/tracked-signals", response_model=list[TrackedSignalRecord])
def tracked_signals(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[TrackedSignalRecord]:
    return request.app.state.tracked_signal_repository.list(status=status, limit=limit)


@router.get("/alerts", response_model=list[AlertHistoryRecord])
def alert_history(
    request: Request,
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[AlertHistoryRecord]:
    return request.app.state.alert_history_repository.list(limit=limit, category=category)


@router.get("/scan-decisions", response_model=list[ScanDecisionRecord])
def scan_decisions(
    request: Request,
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ScanDecisionRecord]:
    return request.app.state.scan_decision_repository.list(limit=limit, status=status, symbol=symbol)


@router.post("/run/premarket-scan", response_model=WorkflowTaskResponse)
def run_premarket_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_premarket_scan(notify=True, force_refresh=True)


@router.post("/run/market-open-scan", response_model=WorkflowTaskResponse)
def run_market_open_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_market_open_scan(notify=True, force_refresh=True)


@router.post("/run/intelligent-scan", response_model=WorkflowTaskResponse)
def run_intelligent_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_intelligent_scan(notify=True, force_refresh=True)


@router.post("/run/swing-scan", response_model=WorkflowTaskResponse)
def run_swing_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_swing_scan(notify=True, force_refresh=True)


@router.post("/run/intraday-scan", response_model=WorkflowTaskResponse)
def run_intraday_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_intraday_scan(notify=True, force_refresh=True)


@router.post("/run/open-signal-check", response_model=WorkflowTaskResponse)
def run_open_signal_check(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).check_open_signals(notify=True, force_refresh=True)


@router.post("/run/daily-summary", response_model=WorkflowTaskResponse)
def run_daily_summary(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).send_daily_summary(notify=True)


@router.post("/run/end-of-day-scan", response_model=WorkflowTaskResponse)
def run_end_of_day_scan(request: Request) -> WorkflowTaskResponse:
    return _workflow(request).run_end_of_day_scan(notify=True, force_refresh=True)
