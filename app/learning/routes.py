"""Continuous-learning operational API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter(prefix="/learning", tags=["learning"])


class SignedAction(BaseModel):
    signed_by: str


class PromoteAction(BaseModel):
    target_mode: str = "paper"
    signed_by: str = ""


def _service(request: Request):
    return request.app.state.learning_service


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
