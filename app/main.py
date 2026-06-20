"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from hmac import compare_digest
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.approvals.service import ProposalService
from app.automation.etoro_reconciliation import EToroDemoReconciliationService
from app.automation.reconciliation import AlpacaReconciliationService
from app.automation.routes import router as automation_router
from app.automation.service import AutomationService
from app.automation.unattended import PaperAutoTradingService
from app.broker.comparison import ParallelBrokerComparisonService
from app.execution.coordinator import ExecutionCoordinator
from app.execution.routes import router as execution_router
from app.execution.trader import TraderService
from app.experiments.extended_hours import ExtendedHoursExperimentService
from app.experiments.routes import router as extended_hours_router
from app.institutional.routes import router as institutional_router
from app.institutional.service import InstitutionalGovernanceService
from app.learning.artifacts import build_artifact_store
from app.learning.critic import build_trade_review_client
from app.learning.modeling import LearningModelService
from app.learning.repository import LearningRepository
from app.learning.routes import router as learning_router
from app.learning.service import LearningService
from app.ledger.repository import LedgerRepository
from app.ledger.service import LedgerService
from app.logging_config import configure_logging
from app.metrics import router as metrics_router
from app.notifications.routes import router as telegram_router
from app.notifications.scheduler import TelegramAlertScheduler
from app.notifications.telegram_bot import TelegramBotService
from app.paper.routes import router as paper_router
from app.paper.service import PaperTradingService
from app.risk.guardrails import RiskManager
from app.runtime_settings import AppSettings, get_settings
from app.screener.routes import router as screener_router
from app.screener.service import BatchBacktestService, MarketScreenerService
from app.signals.routes import router as signal_router
from app.signals.service import LiveSignalService
from app.storage.db import Database
from app.storage.repositories import (
    AlertHistoryRepository,
    BacktestRepository,
    BrokerGovernanceRepository,
    BrokerOrderSnapshotRepository,
    BrokerPositionSnapshotRepository,
    EToroDemoIdempotencyRepository,
    ExecutionQueueRepository,
    ExecutionRepository,
    ExtendedHoursExperimentRepository,
    PaperPositionRepository,
    PaperTradeRepository,
    PortfolioRiskRepository,
    ProposalRepository,
    RolloutGateRepository,
    RunLogRepository,
    RuntimeStateRepository,
    SafetyStateRepository,
    ScanDecisionRepository,
    SignalRepository,
    SignalStateRepository,
    StrategyGovernanceRepository,
    TrackedSignalRepository,
)
from app.telegram_notify import TelegramNotifier
from app.workflow.routes import router as workflow_router
from app.workflow.service import SignalWorkflowService

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Health response."""

    status: str
    account_mode: str
    require_approval: bool
    reason: str = "healthy"
    last_successful_screener_run_at: str | None = None
    last_successful_ledger_cycle_at: str | None = None
    pending_match_count: int = 0
    pending_match_older_than_24h_count: int = 0
    active_meta_model_version: str | None = None
    model_deployment_mode: str = "shadow"
    current_regime_label: str | None = None
    last_etoro_api_error: str | None = None
    last_etoro_api_error_at: str | None = None


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
    max_trades_per_day: int
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
    auto_propose_enabled: bool
    auto_execute_after_approval: bool
    paper_auto_operation_mode: str
    institutional_portfolio_controls_enabled: bool
    etoro_demo_v2_enabled: bool
    etoro_parallel_comparison_enabled: bool
    rollout_stage: str
    learning_capture_enabled: bool
    learning_worker_enabled: bool
    learning_reviews_enabled: bool
    learning_training_enabled: bool
    learning_auto_promote_paper_enabled: bool
    model_deployment_mode: str
    automation_paused: bool
    automation_kill_switch_enabled: bool


def create_app(
    settings: AppSettings | None = None,
    *,
    broker: Any | None = None,
    alpaca_client: Any | None = None,
    broker_router: Any | None = None,
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

    @app.middleware("http")
    async def require_control_token_for_mutations(request: Request, call_next):
        token = app_settings.control_api_token
        is_mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        uses_separate_auth = request.url.path == "/telegram/webhook"
        if token and is_mutation and not uses_separate_auth:
            supplied = request.headers.get("X-Control-Token", "")
            if not supplied or not compare_digest(supplied, token):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "invalid_control_token"},
                )
        return await call_next(request)

    if broker is None:
        from app.broker.etoro_client import EToroClient

        broker = EToroClient(app_settings)
    if alpaca_client is None and app_settings.alpaca_enabled:
        from app.broker.alpaca_client import AlpacaClient

        alpaca_is_paper = app_settings.execution_mode != "live"
        if not alpaca_is_paper:
            if not app_settings.alpaca_live_api_key or not app_settings.alpaca_live_secret_key:
                raise RuntimeError("Live Alpaca mode requires separate ALPACA_LIVE_API_KEY credentials")
            if "paper" in app_settings.alpaca_live_base_url.lower():
                raise RuntimeError("Live Alpaca mode refuses a paper API base URL")
        alpaca_client = AlpacaClient(
            api_key=app_settings.alpaca_api_key if alpaca_is_paper else app_settings.alpaca_live_api_key,
            secret_key=(
                app_settings.alpaca_secret_key
                if alpaca_is_paper
                else app_settings.alpaca_live_secret_key
            ),
            base_url=app_settings.alpaca_base_url if alpaca_is_paper else app_settings.alpaca_live_base_url,
            data_url=app_settings.alpaca_data_url,
            paper=alpaca_is_paper,
            data_feed=app_settings.alpaca_data_feed,
        )
    if broker_router is None:
        from app.broker.router import BrokerRouter

        broker_router = BrokerRouter(
            alpaca_client=alpaca_client,
            etoro_client=broker,
            broker_for_equities=app_settings.broker_for_equities,
            broker_for_non_equities=app_settings.broker_for_non_equities,
        )
    if market_data_client is None:
        from app.broker.etoro_market_data import EtoroMarketDataClient

        market_data_client = EtoroMarketDataClient(app_settings)
    app.state.broker = broker
    app.state.alpaca_client = alpaca_client
    app.state.broker_router = broker_router
    app.state.market_data_client = market_data_client
    app.state.telegram_notifier = telegram_notifier or TelegramNotifier(app_settings)
    from app.data.engine import MarketDataEngine
    from app.data.providers.alpaca_data import AlpacaDataProvider

    alpaca_provider = AlpacaDataProvider(alpaca_client) if alpaca_client is not None else None
    app.state.market_data_engine = MarketDataEngine(
        app_settings,
        etoro_client=app.state.market_data_client,
        alpaca_provider=alpaca_provider,
    )
    signal_repository = SignalRepository(database)
    signal_state_repository = SignalStateRepository(database)
    run_log_repository = RunLogRepository(database)
    backtest_repository = BacktestRepository(database)
    runtime_state_repository = RuntimeStateRepository(database)
    tracked_signal_repository = TrackedSignalRepository(database)
    alert_history_repository = AlertHistoryRepository(database)
    scan_decision_repository = ScanDecisionRepository(database)
    strategy_governance_repository = StrategyGovernanceRepository(database)
    broker_governance_repository = BrokerGovernanceRepository(database)
    portfolio_risk_repository = PortfolioRiskRepository(database)
    rollout_gate_repository = RolloutGateRepository(database)
    etoro_demo_idempotency_repository = EToroDemoIdempotencyRepository(database)
    execution_repository = ExecutionRepository(database)
    proposal_repository = ProposalRepository(database)
    learning_repository = LearningRepository(database)
    app.state.strategy_governance_repository = strategy_governance_repository
    app.state.broker_governance_repository = broker_governance_repository
    app.state.portfolio_risk_repository = portfolio_risk_repository
    app.state.rollout_gate_repository = rollout_gate_repository
    app.state.etoro_demo_idempotency_repository = etoro_demo_idempotency_repository
    app.state.learning_repository = learning_repository
    app.state.learning_model_service = LearningModelService(
        settings=app_settings,
        repository=learning_repository,
        artifact_store=build_artifact_store(app_settings),
        run_logs=run_log_repository,
    )
    app.state.learning_service = LearningService(
        settings=app_settings,
        repository=learning_repository,
        executions=execution_repository,
        proposals=proposal_repository,
        market_data=app.state.market_data_engine,
        model_service=app.state.learning_model_service,
        critic=build_trade_review_client(app_settings),
        run_logs=run_log_repository,
        notifier=app.state.telegram_notifier,
    )
    app.state.institutional_service = InstitutionalGovernanceService(
        settings=app_settings,
        strategies=strategy_governance_repository,
        brokers=broker_governance_repository,
        portfolio_risk=portfolio_risk_repository,
        rollout_gates=rollout_gate_repository,
    )
    app.state.learning_model_service.institutional = app.state.institutional_service
    app.state.institutional_service.initialize_known_capabilities()
    app.state.etoro_demo_v2_client = None
    if app_settings.etoro_demo_v2_enabled:
        from app.broker.etoro_demo_v2 import EToroDemoV2Client

        app.state.etoro_demo_v2_client = EToroDemoV2Client(
            app_settings,
            idempotency_repository=etoro_demo_idempotency_repository,
        )
    ledger_repository = LedgerRepository(database)
    app.state.ledger_repository = ledger_repository
    app.state.ledger_service = LedgerService(
        settings=app_settings,
        broker=app.state.broker,
        repository=ledger_repository,
        database=database,
    )
    app.state.automation_service = AutomationService(
        settings=app_settings,
        runtime_state=runtime_state_repository,
        run_logs=run_log_repository,
        broker_router=app.state.broker_router,
    )
    app.state.institutional_service.automation = app.state.automation_service
    app.state.etoro_demo_reconciliation_service = None
    if app.state.etoro_demo_v2_client is not None:
        app.state.broker_router.add_emergency_client(app.state.etoro_demo_v2_client)
        app.state.etoro_demo_reconciliation_service = EToroDemoReconciliationService(
            settings=app_settings,
            client=app.state.etoro_demo_v2_client,
            idempotency=etoro_demo_idempotency_repository,
            broker_governance=broker_governance_repository,
            runtime_state=runtime_state_repository,
            run_logs=run_log_repository,
            automation=app.state.automation_service,
        )
    app.state.live_signal_service = LiveSignalService(
        settings=app_settings,
        market_data_client=app.state.market_data_client,
        signal_repository=signal_repository,
        signal_state_repository=signal_state_repository,
        run_log_repository=run_log_repository,
        backtest_repository=backtest_repository,
        telegram_notifier=app.state.telegram_notifier,
        ledger_service=app.state.ledger_service,
    )
    app.state.market_screener_service = MarketScreenerService(
        settings=app_settings,
        market_data_engine=app.state.market_data_engine,
        signal_state_repository=signal_state_repository,
        run_log_repository=run_log_repository,
        backtest_repository=backtest_repository,
        scan_decision_repository=scan_decision_repository,
        telegram_notifier=app.state.telegram_notifier,
        learning_service=app.state.learning_service,
    )
    app.state.batch_backtest_service = BatchBacktestService(
        settings=app_settings,
        market_data_engine=app.state.market_data_engine,
        backtest_repository=backtest_repository,
        run_log_repository=run_log_repository,
    )
    broker_order_repository = BrokerOrderSnapshotRepository(database)
    broker_position_repository = BrokerPositionSnapshotRepository(database)
    safety_state_repository = SafetyStateRepository(database)
    extended_hours_repository = ExtendedHoursExperimentRepository(database)
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
        broker=(
            alpaca_client
            if app_settings.execution_mode == "paper"
            and app_settings.paper_broker == "alpaca"
            and alpaca_client is not None
            else app.state.broker
        ),
        risk_manager=risk_manager,
    )
    app.state.trader_service = TraderService(
        settings=app_settings,
        proposal_service=app.state.proposal_service,
        execution_repository=execution_repository,
        run_log_repository=run_log_repository,
        broker=app.state.broker,
        risk_manager=risk_manager,
        learning_service=app.state.learning_service,
    )
    app.state.parallel_broker_comparison_service = ParallelBrokerComparisonService(
        settings=app_settings,
        etoro_demo_client=app.state.etoro_demo_v2_client,
        broker_governance=broker_governance_repository,
        automation=app.state.automation_service,
        run_logs=run_log_repository,
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
        automation_service=app.state.automation_service,
        broker_router=app.state.broker_router,
        risk_manager=risk_manager,
        parallel_broker_service=app.state.parallel_broker_comparison_service,
        learning_service=app.state.learning_service,
    )
    app.state.safety_state_repository = safety_state_repository
    app.state.broker_order_repository = broker_order_repository
    app.state.broker_position_repository = broker_position_repository
    app.state.extended_hours_experiment_repository = extended_hours_repository
    app.state.reconciliation_service = AlpacaReconciliationService(
        settings=app_settings,
        alpaca_client=alpaca_client,
        executions=execution_repository,
        broker_orders=broker_order_repository,
        broker_positions=broker_position_repository,
        safety_state=safety_state_repository,
        runtime_state=runtime_state_repository,
        run_logs=run_log_repository,
        automation=app.state.automation_service,
        broker_governance=broker_governance_repository,
        learning_service=app.state.learning_service,
    )
    app.state.extended_hours_experiment_service = ExtendedHoursExperimentService(
        settings=app_settings,
        alpaca_client=alpaca_client,
        etoro_demo_client=app.state.etoro_demo_v2_client,
        repository=extended_hours_repository,
        safety_state=safety_state_repository,
        automation=app.state.automation_service,
        run_logs=run_log_repository,
    )
    app.state.auto_trading_service = PaperAutoTradingService(
        settings=app_settings,
        proposal_service=app.state.proposal_service,
        execution_coordinator=app.state.execution_coordinator,
        automation=app.state.automation_service,
        reconciliation=app.state.reconciliation_service,
        safety_state=safety_state_repository,
        executions=execution_repository,
        run_logs=run_log_repository,
        notifier=app.state.telegram_notifier,
        alpaca_client=alpaca_client,
        strategy_governance=strategy_governance_repository,
        institutional_governance=app.state.institutional_service,
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
        ledger_service=app.state.ledger_service,
        proposal_service=app.state.proposal_service,
        automation_service=app.state.automation_service,
        reconciliation_service=app.state.reconciliation_service,
        etoro_reconciliation_service=app.state.etoro_demo_reconciliation_service,
        auto_trading_service=app.state.auto_trading_service,
        learning_service=app.state.learning_service,
    )
    app.state.telegram_command_service = TelegramBotService(
        settings=app_settings,
        notifier=app.state.telegram_notifier,
        live_signals=app.state.live_signal_service,
        market_screener=app.state.market_screener_service,
        workflow_service=app.state.workflow_service,
        proposal_service=app.state.proposal_service,
        execution_coordinator=app.state.execution_coordinator,
        execution_queue_repository=execution_queue_repository,
        paper_trading_service=app.state.paper_trading_service,
        automation_service=app.state.automation_service,
        runtime_state_repository=runtime_state_repository,
        run_log_repository=run_log_repository,
        learning_service=app.state.learning_service,
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
    app.include_router(automation_router)
    app.include_router(execution_router)
    app.include_router(extended_hours_router)
    app.include_router(paper_router)
    app.include_router(institutional_router)
    app.include_router(learning_router)
    app.include_router(metrics_router)

    @app.on_event("startup")
    def startup_tasks() -> None:
        if not enable_background_jobs:
            return
        if app_settings.alpaca_enabled and app_settings.alpaca_reconciliation_enabled:
            app.state.reconciliation_service.reconcile()
        if app.state.etoro_demo_reconciliation_service is not None:
            app.state.etoro_demo_reconciliation_service.reconcile()
        if (
            app_settings.telegram_enabled
            and app_settings.telegram_mode == "webhook"
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
        if not (
            app_settings.telegram_hourly_alerts_enabled
            or app_settings.screener_scheduler_enabled
            or app_settings.ledger_cycle_enabled
            or (app_settings.alpaca_enabled and app_settings.alpaca_reconciliation_enabled)
            or app_settings.etoro_demo_v2_enabled
            or app_settings.auto_execution_worker_enabled
            or app_settings.learning_worker_enabled
        ):
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
            reason="process_ready",
            model_deployment_mode=app_settings.model_deployment_mode,
        )

    @app.get("/health/details", response_model=HealthResponse, tags=["system"])
    def detailed_health() -> HealthResponse:
        workflow_health = app.state.workflow_service.health_summary()
        return HealthResponse(
            status=str(workflow_health.get("status") or "ok"),
            account_mode=app_settings.etoro_account_mode,
            require_approval=app_settings.require_approval,
            reason=str(workflow_health.get("reason") or "healthy"),
            last_successful_screener_run_at=workflow_health.get("last_successful_screener_run_at"),
            last_successful_ledger_cycle_at=workflow_health.get("last_successful_ledger_cycle_at"),
            pending_match_count=int(workflow_health.get("pending_match_count") or 0),
            pending_match_older_than_24h_count=int(
                workflow_health.get("pending_match_older_than_24h_count") or 0
            ),
            active_meta_model_version=workflow_health.get("active_meta_model_version") or None,
            model_deployment_mode=str(workflow_health.get("model_deployment_mode") or "shadow"),
            current_regime_label=workflow_health.get("current_regime_label"),
            last_etoro_api_error=workflow_health.get("last_etoro_api_error"),
            last_etoro_api_error_at=workflow_health.get("last_etoro_api_error_at"),
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
            max_trades_per_day=app_settings.max_trades_per_day,
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
            auto_propose_enabled=app_settings.auto_propose_enabled,
            auto_execute_after_approval=app_settings.auto_execute_after_approval,
            paper_auto_operation_mode=app_settings.paper_auto_operation_mode,
            institutional_portfolio_controls_enabled=(
                app_settings.institutional_portfolio_controls_enabled
            ),
            etoro_demo_v2_enabled=app_settings.etoro_demo_v2_enabled,
            etoro_parallel_comparison_enabled=app_settings.etoro_parallel_comparison_enabled,
            rollout_stage=app_settings.rollout_stage,
            learning_capture_enabled=app_settings.learning_capture_enabled,
            learning_worker_enabled=app_settings.learning_worker_enabled,
            learning_reviews_enabled=app_settings.learning_reviews_enabled,
            learning_training_enabled=app_settings.learning_training_enabled,
            learning_auto_promote_paper_enabled=app_settings.learning_auto_promote_paper_enabled,
            model_deployment_mode=app_settings.model_deployment_mode,
            automation_paused=app.state.automation_service.is_paused(),
            automation_kill_switch_enabled=app.state.automation_service.kill_switch_enabled(),
        )

    return app


app = create_app()
