"""Paper trading inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models.paper import BotPerformanceDashboard, PaperPerformanceSummary, PaperPositionRecord, PaperTradeRecord

router = APIRouter(prefix="/paper", tags=["paper"])


def _paper(request: Request):
    return request.app.state.paper_trading_service


@router.get("/summary", response_model=PaperPerformanceSummary)
def paper_summary(request: Request) -> PaperPerformanceSummary:
    return _paper(request).summary()


@router.get("/dashboard", response_model=BotPerformanceDashboard)
def paper_dashboard(request: Request) -> BotPerformanceDashboard:
    return _paper(request).dashboard()


@router.get("/positions", response_model=list[PaperPositionRecord])
def paper_positions(request: Request, status: str | None = None) -> list[PaperPositionRecord]:
    return request.app.state.paper_position_repository.list(status=status, limit=500)


@router.get("/trades", response_model=list[PaperTradeRecord])
def paper_trades(request: Request) -> list[PaperTradeRecord]:
    return request.app.state.paper_trade_repository.list(limit=500)


@router.post("/refresh")
def refresh_paper_positions(request: Request) -> dict[str, int]:
    return _paper(request).refresh_open_positions(
        market_data_engine=request.app.state.market_data_engine,
        force_refresh=True,
    )
