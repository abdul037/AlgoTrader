"""Execution queue routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.models.execution_queue import ExecutionQueueRecord

router = APIRouter(prefix="/execution", tags=["execution"])


def _execution(request: Request):
    return request.app.state.execution_coordinator


@router.get("/queue", response_model=list[ExecutionQueueRecord])
def execution_queue(request: Request, status_filter: str | None = Query(default=None, alias="status")) -> list[ExecutionQueueRecord]:
    return request.app.state.execution_queue_repository.list(status=status_filter, limit=200)


@router.post("/queue/{proposal_id}/enqueue", response_model=ExecutionQueueRecord, status_code=status.HTTP_201_CREATED)
def enqueue_execution(proposal_id: str, request: Request) -> ExecutionQueueRecord:
    try:
        return _execution(request).enqueue_approved_proposal(proposal_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/queue/{queue_id}/process", response_model=ExecutionQueueRecord)
def process_execution(queue_id: str, request: Request) -> ExecutionQueueRecord:
    try:
        return _execution(request).process_queue_item(queue_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/queue/process", response_model=list[ExecutionQueueRecord])
def process_execution_queue(request: Request) -> list[ExecutionQueueRecord]:
    return _execution(request).process_ready_queue()
