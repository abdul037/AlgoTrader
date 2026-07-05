"""Continuous-learning operational API."""

from __future__ import annotations

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/learning", tags=["learning"])


class SignedAction(BaseModel):
    signed_by: str


class ResolveJobAction(BaseModel):
    signed_by: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class PromoteAction(BaseModel):
    target_mode: str = "paper"
    signed_by: str = ""


def _service(request: Request):
    return request.app.state.learning_service


def _require_control_token(request: Request) -> None:
    expected = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not expected:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_control_token")


@router.get("/status")
def learning_status(request: Request):
    return _service(request).status()


@router.get("/reviews")
def learning_reviews(request: Request, limit: int = 100):
    return request.app.state.learning_repository.list_reviews(limit=limit)


@router.get("/models")
def learning_models(request: Request, limit: int = 100):
    return request.app.state.learning_repository.list_models(limit=limit)


@router.get("/experiments")
def learning_experiments(request: Request, limit: int = 100):
    return request.app.state.learning_repository.list_experiments(limit=limit)


@router.get("/drift")
def learning_drift(request: Request, limit: int = 100):
    return request.app.state.learning_repository.list_drift(limit=limit)


@router.get("/jobs")
def learning_jobs(request: Request, status: str | None = None, limit: int = 100):
    _require_control_token(request)
    return _service(request).list_job_summaries(status=status, limit=min(max(limit, 1), 500))


@router.get("/jobs/{job_id}")
def learning_job(job_id: str, request: Request):
    _require_control_token(request)
    try:
        return _service(request).get_job_summary(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry")
def retry_learning_job(job_id: str, request: Request):
    try:
        return _service(request).retry_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/resolve")
def resolve_learning_job(job_id: str, request: Request, payload: ResolveJobAction):
    try:
        return _service(request).resolve_job(
            job_id,
            signed_by=payload.signed_by,
            evidence=payload.evidence,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/reviews/{execution_id}/retry")
def retry_review(execution_id: str, request: Request):
    try:
        return _service(request).review_execution(execution_id, retry=True)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/jobs/process")
def process_jobs(request: Request, limit: int = 10):
    return _service(request).process_jobs(limit=limit)


@router.post("/models/{model_id}/promote")
def promote_model(model_id: str, request: Request, payload: PromoteAction):
    if payload.target_mode == "live" and not payload.signed_by.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Live model promotion requires signed_by",
        )
    try:
        return request.app.state.learning_model_service.promote(
            model_id,
            target_mode=payload.target_mode,
            signed_by=payload.signed_by,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/models/{model_id}/rollback")
def rollback_model(model_id: str, request: Request, payload: SignedAction):
    try:
        return request.app.state.learning_model_service.rollback(model_id, signed_by=payload.signed_by)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
