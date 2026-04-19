"""FastAPI approval routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.approvals.service import ProposalService
from app.config import AppSettings
from app.execution.trader import TraderService
from app.models.approval import ApprovalDecisionRequest, ApprovalStatus, TradeProposal, TradeProposalCreate
from app.models.execution import ExecutionRecord
from app.risk.guardrails import RiskManager
from app.storage.db import Database
from app.storage.repositories import ExecutionRepository, ProposalRepository, RunLogRepository, SignalRepository

router = APIRouter(tags=["proposals"])


def _proposal_service(request: Request) -> ProposalService:
    existing = getattr(request.app.state, "proposal_service", None)
    if existing is not None:
        return existing
    settings: AppSettings = request.app.state.settings
    db: Database = request.app.state.db
    return ProposalService(
        settings=settings,
        proposal_repository=ProposalRepository(db),
        signal_repository=SignalRepository(db),
        execution_repository=ExecutionRepository(db),
        run_log_repository=RunLogRepository(db),
        broker=request.app.state.broker,
        risk_manager=RiskManager(settings),
    )


def _trader_service(request: Request) -> TraderService:
    existing = getattr(request.app.state, "trader_service", None)
    if existing is not None:
        return existing
    settings: AppSettings = request.app.state.settings
    db: Database = request.app.state.db
    return TraderService(
        settings=settings,
        proposal_service=_proposal_service(request),
        execution_repository=ExecutionRepository(db),
        run_log_repository=RunLogRepository(db),
        broker=request.app.state.broker,
        risk_manager=RiskManager(settings),
    )


@router.post("/proposals/create", response_model=TradeProposal, status_code=status.HTTP_201_CREATED)
def create_proposal(payload: TradeProposalCreate, request: Request) -> TradeProposal:
    try:
        return _proposal_service(request).create_proposal(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/proposals", response_model=list[TradeProposal])
def list_proposals(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[TradeProposal]:
    try:
        parsed_status = ApprovalStatus(status_filter) if status_filter else None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid proposal status") from exc
    return _proposal_service(request).list_proposals(status=parsed_status)


@router.post("/proposals/{proposal_id}/approve", response_model=TradeProposal)
def approve_proposal(
    proposal_id: str,
    decision: ApprovalDecisionRequest,
    request: Request,
) -> TradeProposal:
    try:
        return _proposal_service(request).approve_proposal(proposal_id, decision)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/reject", response_model=TradeProposal)
def reject_proposal(
    proposal_id: str,
    decision: ApprovalDecisionRequest,
    request: Request,
) -> TradeProposal:
    try:
        return _proposal_service(request).reject_proposal(proposal_id, decision)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/execute", response_model=ExecutionRecord)
def execute_proposal(proposal_id: str, request: Request) -> ExecutionRecord:
    try:
        return _trader_service(request).execute_proposal(proposal_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
