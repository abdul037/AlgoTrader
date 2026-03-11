from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.schemas import BacktestRequest, ExecuteRequest, SupportedInterval, TradingConfig
from app.services.backtester import run_backtest
from app.services.execution import execute_command, get_execution_status
from app.services.market_data import fetch_market_data, get_market_data_status
from app.services.signal_engine import generate_signal_report

router = APIRouter()


@router.get("/config", response_model=TradingConfig, response_model_by_alias=True)
async def trading_config() -> TradingConfig:
    settings = get_settings()
    return TradingConfig(
        appName=settings.app_name.replace(" API", ""),
        marketData=get_market_data_status(),
        execution=get_execution_status(),
        recommendations=[
            {
                "area": "Open-source engine",
                "choice": "QuantConnect LEAN",
                "reason": "Best upgrade path when you outgrow a lightweight in-app backtest engine.",
            },
            {
                "area": "Free market data",
                "choice": "Alpha Vantage",
                "reason": "Simple stock API with free access for prototyping and indicator-driven dashboards.",
            },
            {
                "area": "Broker execution",
                "choice": "Alpaca paper trading",
                "reason": "Free paper environment with the same API shape you can later promote to live trading.",
            },
        ],
    )


@router.get("/analyze", response_model_by_alias=True)
async def analyze_symbol(
    symbol: str = Query(..., min_length=1),
    interval: SupportedInterval = Query("15min"),
    lookback: int = Query(220, ge=60, le=600),
):
    try:
        provider, mode, bars = await fetch_market_data(symbol.upper(), interval, lookback)
        return generate_signal_report(symbol.upper(), interval, provider, mode, bars)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/backtest", response_model_by_alias=True)
async def backtest_strategy(payload: BacktestRequest):
    try:
        provider, mode, bars = await fetch_market_data(
            payload.symbol.upper(), payload.interval, payload.lookback
        )
        return run_backtest(
            payload.symbol.upper(),
            payload.interval,
            provider,
            mode,
            bars,
            payload.starting_capital,
        )
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/execute", response_model_by_alias=True)
async def execute_trade(payload: ExecuteRequest):
    try:
        return await execute_command(payload.model_dump())
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(error)) from error
