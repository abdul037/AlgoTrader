"""Protected RL paper policy API."""

from __future__ import annotations

from hmac import compare_digest

from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(prefix="/rl-policy", tags=["rl-policy"])


def _require_control_token(request: Request) -> None:
    token = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not token:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_control_token")


def _service(request: Request):
    return request.app.state.rl_policy_service


@router.get("/status")
def rl_policy_status(request: Request):
    _require_control_token(request)
    return _service(request).status()


@router.post("/train")
def rl_policy_train(request: Request):
    _require_control_token(request)
    try:
        return _service(request).train()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/propose")
def rl_policy_propose(request: Request):
    _require_control_token(request)
    return _service(request).propose()
