"""API routes for market universe screening and batch backtests."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.live_signal_schema import LiveSignalSnapshot
from app.models.screener import BatchBacktestSummary, MarketUniverseResponse, ScreenerRunResponse

router = APIRouter(prefix="/screener", tags=["screener"])


class BatchBacktestRequest(BaseModel):
    """Batch backtest request payload."""

    symbols: list[str] | None = None
    timeframes: list[str] = Field(default_factory=lambda: ["1d"])
    strategy_names: list[str] | None = None
    provider: str | None = None
    initial_cash: float = Field(default=10000.0, gt=0)
    limit: int | None = Field(default=None, ge=1, le=100)
    force_refresh: bool = False


def _screener(request: Request):
    return request.app.state.market_screener_service


def _batch_backtests(request: Request):
    return request.app.state.batch_backtest_service


@router.get("/universe", response_model=MarketUniverseResponse)
def screener_universe(request: Request, limit: int | None = Query(default=None, ge=1, le=100)) -> MarketUniverseResponse:
    return _screener(request).get_universe(limit=limit)


@router.get("/scan", response_model=ScreenerRunResponse)
def screener_scan(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=100),
    symbols: str | None = Query(default=None, description="Comma-separated symbol list"),
    timeframes: str | None = Query(default=None, description="Comma-separated timeframes like 1d,1h,15m"),
    validated_only: bool = Query(default=False),
    notify: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
) -> ScreenerRunResponse:
    parsed_symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()] if symbols else None
    parsed_timeframes = [item.strip().lower() for item in timeframes.split(",") if item.strip()] if timeframes else None
    try:
        return _screener(request).scan_universe(
            symbols=parsed_symbols,
            timeframes=parsed_timeframes,
            limit=limit,
            validated_only=validated_only,
            notify=notify,
            force_refresh=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/analyze", response_model=LiveSignalSnapshot)
def screener_analyze_symbol(
    request: Request,
    symbol: str = Query(..., min_length=1),
    force_refresh: bool = Query(default=False),
) -> LiveSignalSnapshot:
    try:
        return _screener(request).analyze_symbol(symbol, force_refresh=force_refresh)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/backtests/run", response_model=BatchBacktestSummary)
def screener_batch_backtest(payload: BatchBacktestRequest, request: Request) -> BatchBacktestSummary:
    try:
        return _batch_backtests(request).run(
            symbols=payload.symbols,
            timeframes=payload.timeframes,
            strategy_names=payload.strategy_names,
            provider=payload.provider,
            initial_cash=payload.initial_cash,
            limit=payload.limit,
            force_refresh=payload.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
