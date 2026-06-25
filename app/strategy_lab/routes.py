"""Protected Strategy Lab API."""

from __future__ import annotations

from hmac import compare_digest

from fastapi import APIRouter, HTTPException, Request, status

from app.models.strategy_lab import StrategyBacktestRequest, StrategyGenerationRequest, StrategyPromotionRequest

router = APIRouter(prefix="/strategy-lab", tags=["strategy-lab"])


def _require_control_token(request: Request) -> None:
    token = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not token:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_control_token")


def _service(request: Request):
    return request.app.state.strategy_lab_service


@router.get("/status")
def strategy_lab_status(request: Request):
    _require_control_token(request)
    return _service(request).status()


@router.get("/generated")
def generated_strategies(request: Request, status_filter: str | None = None, limit: int = 100):
    _require_control_token(request)
    return request.app.state.strategy_lab_repository.list_generated(status=status_filter, limit=limit)


@router.post("/generate")
def generate_strategy(request: Request, payload: StrategyGenerationRequest):
    _require_control_token(request)
    try:
        return _service(request).generate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{id}/backtest")
def backtest_strategy(id: str, request: Request, payload: StrategyBacktestRequest | None = None):
    _require_control_token(request)
    try:
        return _service(request).backtest(id, payload or StrategyBacktestRequest())
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{id}/promote-paper")
def promote_strategy_paper(id: str, request: Request, payload: StrategyPromotionRequest | None = None):
    _require_control_token(request)
    try:
        return _service(request).promote_paper(id, payload or StrategyPromotionRequest())
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
