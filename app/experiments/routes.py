"""Routes for supervised experimental workflows."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/experiments/extended-hours", tags=["experiments"])


class AlpacaProbeRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    limit_price: float | None = Field(default=None, gt=0)
    client_order_id: str | None = None
    operator: str = "api"


class CancelOrderRequest(BaseModel):
    reason: str = "manual_cancel"


class ExitOrderRequest(BaseModel):
    limit_price: float | None = Field(default=None, gt=0)
    client_order_id: str | None = None
    operator: str = "api"


def _service(request: Request):
    return request.app.state.extended_hours_experiment_service


@router.get("/status")
def extended_hours_status(request: Request):
    return _service(request).status()


@router.get("/orders")
def extended_hours_orders(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    return _service(request).list_orders(limit=limit)


@router.get("/whitelist")
def extended_hours_whitelist(request: Request):
    return {"symbols": _service(request).whitelist()}


@router.get("/etoro/probes")
def etoro_probe_history(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    return _service(request).list_etoro_probes(limit=limit)


@router.post("/alpaca/probe", status_code=status.HTTP_201_CREATED)
def alpaca_extended_hours_probe(request: Request, payload: AlpacaProbeRequest):
    try:
        return _service(request).probe_alpaca(
            symbol=payload.symbol,
            side=payload.side,
            limit_price=payload.limit_price,
            client_order_id=payload.client_order_id,
            operator=payload.operator,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/orders/{order_id}/cancel")
def cancel_extended_hours_order(
    order_id: str,
    request: Request,
    payload: CancelOrderRequest | None = None,
):
    try:
        return _service(request).cancel_order(
            order_id,
            reason=payload.reason if payload else "manual_cancel",
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/orders/{order_id}/exit")
def exit_extended_hours_order(order_id: str, request: Request, payload: ExitOrderRequest):
    try:
        return _service(request).submit_exit(
            order_id,
            limit_price=payload.limit_price,
            client_order_id=payload.client_order_id,
            operator=payload.operator,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/etoro/capability-probe", status_code=status.HTTP_201_CREATED)
def etoro_capability_probe(request: Request):
    try:
        return _service(request).run_etoro_capability_probe()
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
