"""Paper trading inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models.paper import (
    BotPerformanceDashboard,
    PaperBrokerExecutionRecord,
    PaperPerformanceSummary,
    PaperPositionRecord,
    PaperTradeLifecycleRecord,
    PaperTradeRecord,
)

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


@router.get("/broker-executions", response_model=list[PaperBrokerExecutionRecord])
def paper_broker_executions(request: Request, limit: int = 100) -> list[PaperBrokerExecutionRecord]:
    return _paper(request).broker_executions(limit=min(max(limit, 1), 500))


@router.get("/lifecycles", response_model=list[PaperTradeLifecycleRecord])
def paper_lifecycles(
    request: Request,
    limit: int = 100,
    source: str | None = None,
    autonomous_only: bool = False,
    complete_only: bool = False,
) -> list[PaperTradeLifecycleRecord]:
    return _paper(request).lifecycles(
        limit=min(max(limit, 1), 500),
        source=source,
        autonomous_only=autonomous_only,
        complete_only=complete_only,
    )


@router.get("/lifecycles/{execution_id}", response_model=PaperTradeLifecycleRecord)
def paper_lifecycle(request: Request, execution_id: str) -> PaperTradeLifecycleRecord:
    from fastapi import HTTPException, status

    record = _paper(request).lifecycle(execution_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper lifecycle not found")
    return record


@router.post("/refresh")
def refresh_paper_positions(request: Request) -> dict[str, int]:
    return _paper(request).refresh_open_positions(
        market_data_engine=request.app.state.market_data_engine,
        force_refresh=True,
    )
