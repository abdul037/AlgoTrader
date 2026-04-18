"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.runtime_settings import AppSettings, get_settings
from app.logging_config import configure_logging
from app.notifications.routes import router as telegram_router
from app.notifications.scheduler import TelegramAlertScheduler
from app.telegram_notify import TelegramNotifier
from app.notifications.telegram_bot import TelegramBotService
from app.execution.routes import router as execution_router
from app.execution.coordinator import ExecutionCoordinator
from app.execution.trader import TraderService
from app.paper.routes import router as paper_router
from app.paper.service import PaperTradingService
from app.approvals.service import ProposalService
from app.risk.guardrails import RiskManager
from app.signals.service import LiveSignalService
from app.signals.routes import router as signal_router
from app.screener.routes import router as screener_router
from app.screener.service import BatchBacktestService, MarketScreenerService
from app.workflow.routes import router as workflow_router
from app.workflow.service import SignalWorkflowService
from app.storage.db import Database
from app.storage.repositories import (
    AlertHistoryRepository,
    BacktestRepository,
    ExecutionQueueRepository,
    ExecutionRepository,
    PaperPositionRepository,
    PaperTradeRepository,
    ProposalRepository,
    RunLogRepository,
    RuntimeStateRepository,
    ScanDecisionRepository,
    SignalRepository,
    SignalStateRepository,
    TrackedSignalRepository,
)

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Health response."""

    status: str
    account_mode: str
    require_approval: bool


class BacktestRunRequest(BaseModel):
    """Backtest trigger request."""

    symbol: str
    strategy: str
    file_path: str
    initial_cash: float = Field(default=10000.0, gt=0)


class ConfigSummary(BaseModel):
    """Safe redacted configuration summary."""

    account_mode: str
    enable_real_trading: bool
    require_approval: bool
    allowed_instruments: list[str]
    blocked_instruments: list[str]
    max_risk_per_trade_pct: float
    max_daily_loss_usd: float
    max_open_positions: int
    default_equity_leverage: int
    max_equity_leverage: int
    max_gold_leverage: int
    broker_simulation_enabled: bool
    market_universe_name: str
    market_universe_limit: int
    primary_market_data_provider: str
    fallback_market_data_provider: str
    screener_default_timeframes: list[str]
    screener_top_k: int


def create_app(
    settings: AppSettings | None = None,
    *,
    broker: Any | None = None,
    market_data_client: Any | None = None,
    telegram_notifier: Any | None = None,
    enable_background_jobs: bool = True,
) -> FastAPI:
    """Create the FastAPI application."""

    configure_logging()
    app_settings = settings or get_settings()
    database = Database(app_settings)
    database.initialize()

    app = FastAPI(
        title="eToro Approval Trading Bot",
        version="0.1.0",
        description=(
            "Backtest strategies, generate proposals, require approval, and execute "
            "through a safe-first broker integration."
        ),
    )
    app.state.settings = app_settings
    app.state.db = database
    if broker is None:
        from app.broker.etoro_client import EToroClient

        broker = EToroClient(app_settings)
    if market_data_client is None:
        from app.broker.etoro_market_data import EtoroMarketDataClient

        market_data_client = EtoroMarketDataClient(app_settings)
    app.state.broker = broker
    app.state.market_data_client = market_data_client
    app.state.telegram_notifier = telegram_notifier or TelegramNotifier(app_settings)
    from app.data.engine import MarketDataEngine

    app.state.market_data_engine = MarketDataEngine(
        app_settings,
        etoro_client=app.state.market_data_client,
    )
    signal_repository = SignalRepository(database)
    signal_state_repository = SignalStateRepository(database)
    run_log_repository = RunLogRepository(database)
    backtest_repository = BacktestRepository(database)
    runtime_state_repository = RuntimeStateRepository(database)
    tracked_signal_repository = TrackedSignalRepository(database)
    alert_history_repository = AlertHistoryRepository(database)
    scan_decision_repository = ScanDecisionRepository(database)
    app.state.live_signal_service = LiveSignalService(
        settings=app_settings,
        market_data_client=app.state.market_data_client,
        signal_repository=signal_repository,
        signal_state_repository=signal_state_repository,
        run_log_repository=run_log_repository,
        backtest_repository=backtest_repository,
        telegram_notifier=app.state.telegram_notifier,
    )
    app.state.market_screener_service = MarketScreenerService(
        settings=app_settings,
        market_data_engine=app.state.market_data_engine,
        signal_state_repository=signal_state_repository,
        run_log_repository=run_log_repository,
        backtest_repository=backtest_repository,
        scan_decision_repository=scan_decision_repository,
        telegram_notifier=app.state.telegram_notifier,
    )
    app.state.batch_backtest_service = BatchBacktestService(
        settings=app_settings,
        market_data_engine=app.state.market_data_engine,
        backtest_repository=backtest_repository,
        run_log_repository=run_log_repository,
    )
    execution_repository = ExecutionRepository(database)
    proposal_repository = ProposalRepository(database)
    execution_queue_repository = ExecutionQueueRepository(database)
    paper_position_repository = PaperPositionRepository(database)
    paper_trade_repository = PaperTradeRepository(database)
    risk_manager = RiskManager(app_settings)
    app.state.execution_queue_repository = execution_queue_repository
    app.state.paper_position_repository = paper_position_repository
    app.state.paper_trade_repository = paper_trade_repository
    app.state.proposal_service = ProposalService(
        settings=app_settings,
        proposal_repository=proposal_repository,
        signal_repository=signal_repository,
        execution_repository=execution_repository,
        run_log_repository=run_log_repository,
        broker=app.state.broker,
        risk_manager=risk_manager,
    )
    app.state.trader_service = TraderService(
        settings=app_settings,
        proposal_service=app.state.proposal_service,
        execution_repository=execution_repository,
        run_log_repository=run_log_repository,
        broker=app.state.broker,
        risk_manager=risk_manager,
    )
    app.state.paper_trading_service = PaperTradingService(
        settings=app_settings,
        positions=paper_position_repository,
        trades=paper_trade_repository,
        run_logs=run_log_repository,
        scan_decisions=scan_decision_repository,
    )
    app.state.execution_coordinator = ExecutionCoordinator(
        settings=app_settings,
        proposal_service=app.state.proposal_service,
        queue_repository=execution_queue_repository,
        execution_repository=execution_repository,
        trader_service=app.state.trader_service,
        paper_trading_service=app.state.paper_trading_service,
        market_data_engine=app.state.market_data_engine,
        run_logs=run_log_repository,
    )
    app.state.tracked_signal_repository = tracked_signal_repository
    app.state.alert_history_repository = alert_history_repository
    app.state.scan_decision_repository = scan_decision_repository
    app.state.workflow_service = SignalWorkflowService(
        settings=app_settings,
        market_screener=app.state.market_screener_service,
        market_data_engine=app.state.market_data_engine,
        notifier=app.state.telegram_notifier,
        tracked_signals=tracked_signal_repository,
        alert_history=alert_history_repository,
        runtime_state=runtime_state_repository,
        run_logs=run_log_repository,
    )
    app.state.telegram_command_service = TelegramBotService(
        settings=app_settings,
        notifier=app.state.telegram_notifier,
        live_signals=app.state.live_signal_service,
        market_screener=app.state.market_screener_service,
        workflow_service=app.state.workflow_service,
        runtime_state_repository=runtime_state_repository,
        run_log_repository=run_log_repository,
    )
    app.state.telegram_alert_scheduler = None

    try:
        from app.approvals.routes import router as proposal_router

        app.include_router(proposal_router)
    except Exception:
        pass
    app.include_router(signal_router)
    app.include_router(screener_router)
    app.include_router(telegram_router)
    app.include_router(workflow_router)
    app.include_router(execution_router)
    app.include_router(paper_router)

    @app.on_event("startup")
    def startup_tasks() -> None:
        if not enable_background_jobs:
            return
        if not app_settings.telegram_enabled:
            return
        if (
            app_settings.telegram_mode == "webhook"
            and app_settings.telegram_webhook_auto_register
            and app_settings.telegram_webhook_url
        ):
            try:
                app.state.telegram_notifier.ensure_webhook(
                    app_settings.telegram_webhook_url,
                    secret_token=app_settings.telegram_webhook_secret or None,
                    drop_pending_updates=False,
                )
                logger.info("Telegram webhook ensured for %s", app_settings.telegram_webhook_url)
            except Exception as exc:
                logger.exception("Telegram webhook registration failed: %s", exc)
        if app_settings.telegram_polling_enabled:
            return
        if not (app_settings.telegram_hourly_alerts_enabled or app_settings.screener_scheduler_enabled):
            return
        scheduler = TelegramAlertScheduler(app.state.telegram_command_service)
        scheduler.start()
        app.state.telegram_alert_scheduler = scheduler

    @app.on_event("shutdown")
    def shutdown_tasks() -> None:
        scheduler = getattr(app.state, "telegram_alert_scheduler", None)
        if scheduler is not None:
            scheduler.stop()

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            account_mode=app_settings.etoro_account_mode,
            require_approval=app_settings.require_approval,
        )

    @app.post("/backtests/run", tags=["backtests"])
    def run_backtest(payload: BacktestRunRequest, request: Request) -> Any:
        from app.backtesting.engine import BacktestEngine
        from app.data.market_data import MarketDataService
        from app.storage.repositories import BacktestRepository
        from app.strategies import get_strategy

        try:
            strategy = get_strategy(payload.strategy)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        market_data = MarketDataService()
        data = market_data.load_csv(payload.file_path)
        engine = BacktestEngine(BacktestRepository(request.app.state.db))
        return engine.run(
            symbol=payload.symbol,
            strategy=strategy,
            data=data,
            file_path=payload.file_path,
            initial_cash=payload.initial_cash,
        )

    @app.get("/portfolio/summary", tags=["broker"])
    def portfolio_summary(request: Request) -> Any:
        try:
            return request.app.state.broker.get_portfolio()
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    @app.get("/config/summary", response_model=ConfigSummary, tags=["system"])
    def config_summary() -> ConfigSummary:
        return ConfigSummary(
            account_mode=app_settings.etoro_account_mode,
            enable_real_trading=app_settings.enable_real_trading,
            require_approval=app_settings.require_approval,
            allowed_instruments=app_settings.allowed_instruments,
            blocked_instruments=app_settings.blocked_instruments,
            max_risk_per_trade_pct=app_settings.max_risk_per_trade_pct,
            max_daily_loss_usd=app_settings.max_daily_loss_usd,
            max_open_positions=app_settings.max_open_positions,
            default_equity_leverage=app_settings.default_equity_leverage,
            max_equity_leverage=app_settings.max_equity_leverage,
            max_gold_leverage=app_settings.max_gold_leverage,
            broker_simulation_enabled=app_settings.broker_simulation_enabled,
            market_universe_name=app_settings.market_universe_name,
            market_universe_limit=app_settings.market_universe_limit,
            primary_market_data_provider=app_settings.primary_market_data_provider,
            fallback_market_data_provider=app_settings.fallback_market_data_provider,
            screener_default_timeframes=app_settings.screener_default_timeframes,
            screener_top_k=app_settings.screener_top_k,
        )

    return app


app = create_app()
