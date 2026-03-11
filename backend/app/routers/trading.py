from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.schemas import BacktestRequest, SupportedInterval, TradingConfig
from app.services.backtester import run_backtest
from app.services.market_data import fetch_market_data, get_market_data_status
from app.services.signal_engine import generate_signal_report

router = APIRouter()


@router.get("/config", response_model=TradingConfig, response_model_by_alias=True)
async def trading_config() -> TradingConfig:
    settings = get_settings()
    return TradingConfig(
        appName=settings.app_name.replace(" API", ""),
        marketData=get_market_data_status(),
        analysisMode={
            "executionEnabled": False,
            "note": "Execution is disabled in this build. Use the live signal and backtest outputs to make the trade manually.",
        },
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
                "area": "Workflow",
                "choice": "Signal-only trading desk",
                "reason": "Keep the stack focused on analysis quality and backtest confidence before adding broker automation.",
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
