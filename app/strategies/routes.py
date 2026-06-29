"""Protected strategy enhancement diagnostics."""

from __future__ import annotations

from hmac import compare_digest

from fastapi import APIRouter, HTTPException, Query, Request, status

router = APIRouter(prefix="/strategies/enhancement", tags=["strategies"])


def _require_control_token(request: Request) -> None:
    token = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not token:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_control_token")


def _service(request: Request):
    return request.app.state.strategy_enhancement_service


@router.get("/status")
def strategy_enhancement_status(request: Request):
    _require_control_token(request)
    return _service(request).status()


@router.get("/near-misses")
def strategy_enhancement_near_misses(
    request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
):
    _require_control_token(request)
    return _service(request).near_misses(limit=limit)


@router.post("/run-paper-tuning")
def strategy_enhancement_run_paper_tuning(
    request: Request,
    limit: int = Query(default=1000, ge=1, le=5000),
):
    _require_control_token(request)
    return _service(request).run_paper_tuning(limit=limit)
