"""FastAPI routes for live signal evaluation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.runtime_settings import AppSettings
from app.live_signal_schema import LiveSignalSnapshot, SignalScanResponse, TelegramAlertResponse
from app.signals.service import LiveSignalService
from app.storage.db import Database
from app.storage.repositories import RunLogRepository, SignalRepository, SignalStateRepository

router = APIRouter(tags=["signals"])


class TelegramTestRequest(BaseModel):
    """Manual Telegram test message payload."""

    message: str = Field(
        default="CX Algo Bot Telegram test message.",
        min_length=1,
        max_length=1000,
    )


class SignalNotifyRequest(BaseModel):
    """Manual Telegram signal alert payload."""

    symbol: str = Field(min_length=1)


def _signal_service(request: Request) -> LiveSignalService:
    existing = getattr(request.app.state, "live_signal_service", None)
    if existing is not None:
        return existing
    settings: AppSettings = request.app.state.settings
    db: Database = request.app.state.db
    return LiveSignalService(
        settings=settings,
        market_data_client=request.app.state.market_data_client,
        signal_repository=SignalRepository(db),
        signal_state_repository=SignalStateRepository(db),
        run_log_repository=RunLogRepository(db),
        telegram_notifier=request.app.state.telegram_notifier,
    )


@router.get("/signals/latest", response_model=LiveSignalSnapshot)
def latest_signal(
    request: Request,
    symbol: str = Query(..., min_length=1),
    commit: bool = Query(default=False),
    notify: bool = Query(default=False),
) -> LiveSignalSnapshot:
    try:
        return _signal_service(request).get_latest_signal(
            symbol,
            commit=commit or notify,
            notify=notify,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/signals/scan", response_model=SignalScanResponse)
def scan_signals(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    symbols: str | None = Query(default=None, description="Comma-separated symbol list"),
    supported_only: bool = Query(default=False),
    commit: bool = Query(default=False),
    notify: bool = Query(default=False),
) -> SignalScanResponse:
    parsed_symbols = None
    if symbols:
        parsed_symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    try:
        return _signal_service(request).scan_market(
            symbols=parsed_symbols,
            limit=limit,
            supported_only=supported_only,
            commit=commit or notify,
            notify=notify,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/signals/test-telegram", response_model=TelegramAlertResponse)
def test_telegram_alert(
    payload: TelegramTestRequest,
    request: Request,
) -> TelegramAlertResponse:
    response = _signal_service(request).send_test_alert(payload.message)
    if not response.sent:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=response.detail)
    return response


@router.post("/signals/notify", response_model=TelegramAlertResponse)
def notify_signal(
    payload: SignalNotifyRequest,
    request: Request,
) -> TelegramAlertResponse:
    try:
        response = _signal_service(request).send_signal_alert(payload.symbol)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not response.sent:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=response.detail)
    return response
