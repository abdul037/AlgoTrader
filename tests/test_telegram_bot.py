from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from types import SimpleNamespace
import threading

from app.live_signal_schema import LiveSignalSnapshot, SignalScanResponse, SignalState
from app.models.approval import ApprovalStatus, TradeProposal
from app.models.execution_queue import ExecutionQueueRecord
from app.models.paper import PaperPerformanceSummary
from app.notifications.telegram_bot import TelegramBotService
from app.universe import DEFAULT_TOP_100_US
from app.utils.time import utc_now


class FakeNotifier:
    def __init__(self):
        self.enabled = True
        self.sent: list[tuple[str, str | None]] = []
        self.updates: list[dict] = []

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return True

    def get_updates(self, *, offset=None, timeout=0, limit=20):
        if offset is None:
            return self.updates
        return [update for update in self.updates if update["update_id"] >= offset]

    def send_text(self, message: str, *, chat_id: str | None = None):
        self.sent.append((message, chat_id))
        return True

    def send_signal_change(self, snapshot, *, previous_state=None, chat_id=None):
        self.sent.append((f"signal:{snapshot.symbol}:{previous_state}", chat_id))
        return True

    @staticmethod
    def format_signal_message(snapshot, previous_state=None):
        return f"signal {snapshot.symbol} {snapshot.state.value} {previous_state}"

    @staticmethod
    def format_price_message(snapshot):
        return f"price {snapshot.symbol} {snapshot.current_price}"

    @staticmethod
    def format_scan_message(response):
        return f"scan {len(response.candidates)}"

    @staticmethod
    def format_screener_summary(response, *, task_label=None, include_other_watches=False):
        details = "details" if include_other_watches else "simple"
        return f"screener {len(response.candidates)} {','.join(response.timeframes)} {details}"


class FakeStateRepo:
    def __init__(self):
        self.state: dict[str, str] = {}

    def get(self, key: str):
        return self.state.get(key)

    def set(self, key: str, value: str):
        self.state[key] = value


class FakeRunLogRepo:
    def __init__(self):
        self.items: list[tuple[str, dict]] = []

    def log(self, event_type: str, payload: dict):
        self.items.append((event_type, payload))


class FakeLiveSignals:
    def get_latest_signal(self, symbol: str, *, commit=False, notify=False):
        return LiveSignalSnapshot(
            symbol=symbol.upper(),
            strategy_name="pullback_trend_100_10",
            state=SignalState.BUY,
            current_price=123.45,
            entry_price=122.0,
            exit_price=118.0,
            stop_loss=115.0,
            take_profit=130.0,
            rationale="test rationale",
            score=95.0,
        )

    def scan_market(self, *, limit=None, supported_only=False, commit=False, notify=False):
        return SignalScanResponse(
            evaluated_count=2,
            limit=limit or 5,
            candidates=[
                LiveSignalSnapshot(
                    symbol="NVDA",
                    strategy_name="pullback_trend_100_10",
                    state=SignalState.BUY,
                    current_price=123.45,
                    entry_price=122.0,
                    rationale="test rationale",
                    score=95.0,
                )
            ],
        )

    def send_signal_alert_with_label(self, symbol: str, *, previous_state: str):
        class Response:
            sent = True
            detail = "ok"

        return Response()


class FailingLiveSignals(FakeLiveSignals):
    def get_latest_signal(self, symbol: str, *, commit=False, notify=False):
        raise RuntimeError("signal lookup failed")


class FakeSettings:
    telegram_allowed_chat_ids = ["7329410595"]
    telegram_chat_id = "7329410595"
    telegram_hourly_alerts_enabled = True
    telegram_alert_interval_minutes = 60
    telegram_alert_symbols = ["NVDA"]
    telegram_poll_interval_seconds = 0
    telegram_command_timeout_seconds = 5
    telegram_scan_stale_after_seconds = 30
    telegram_scan_default_universe_limit = 10
    allowed_instruments = ["NVDA", "AMD"]
    market_universe_symbols = ["NVDA", "AMD"]
    market_universe_tier = "broad_top100"
    market_universe_limit = 100
    screener_intraday_timeframes = ["15m", "1h"]
    screener_default_timeframes = ["1d", "1h"]
    default_trade_amount_usd = 1000.0
    execution_mode = "paper"
    enable_real_trading = False
    require_approval = True
    auto_propose_enabled = False
    auto_execute_after_approval = False


def test_poll_once_handles_signal_and_scan_commands() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/signal NVDA"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 7329410595}, "text": "/scan 3"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )
    processed = bot.poll_once(timeout_seconds=0)
    assert processed == 2
    assert notifier.sent[0][0].startswith("signal NVDA")
    assert notifier.sent[1][0] == "scan 1"


def test_scan_in_progress_message_includes_task_and_elapsed() -> None:
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=FakeNotifier(),
        live_signals=FakeLiveSignals(),
        market_screener=FakeMarketScreener(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )
    bot._active_scan_future = Future()
    bot._active_scan_started_at = utc_now() - timedelta(seconds=7)
    bot._active_scan_label = "manual_scan"

    message = bot._scan_in_progress_message()

    assert "A screener scan is already running." in message
    assert "Task: manual_scan" in message
    assert "Elapsed:" in message
    assert "/scan_status" in message


def test_stale_scan_status_recovers_lock() -> None:
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=FakeNotifier(),
        live_signals=FakeLiveSignals(),
        market_screener=FakeMarketScreener(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )
    bot._active_scan_future = Future()
    bot._active_scan_cancel_event = threading.Event()
    bot._active_scan_started_at = utc_now() - timedelta(seconds=90)
    bot._active_scan_label = "manual_scan"
    assert bot._scan_lock.acquire(blocking=False)

    message = bot._scan_status_message()

    assert "Recovered a stale screener scan" in message
    assert not bot._scan_lock.locked()


class FakeMarketScreener:
    def __init__(self):
        self.calls = []

    def analyze_symbol(self, symbol: str, *, force_refresh: bool = False):
        return LiveSignalSnapshot(
            symbol=symbol.upper(),
            strategy_name="momentum_breakout",
            state=SignalState.BUY,
            timeframe="15m",
            current_price=100.0,
            entry_price=101.0,
            stop_loss=98.0,
            take_profit=107.0,
            targets=[107.0],
            signal_role="entry_long",
            direction_label="long",
            rationale="actionable test setup",
            score=82.0,
            execution_ready=True,
            asset_class="equity",
        )

    def scan_universe(
        self,
        *,
        symbols=None,
        timeframes=None,
        limit=None,
        validated_only=False,
        notify=False,
        force_refresh=False,
    ):
        from app.models.screener import ScreenerRunResponse

        self.calls.append(
            {
                "symbols": symbols,
                "timeframes": timeframes,
                "limit": limit,
                "validated_only": validated_only,
            }
        )
        return ScreenerRunResponse(
            generated_at="2026-04-11T00:00:00+00:00",
            universe_name="top100_us",
            timeframes=timeframes or ["1d"],
            evaluated_symbols=len(symbols or ["NVDA", "AMD"]),
            evaluated_strategy_runs=4,
            candidates=[
                LiveSignalSnapshot(
                    symbol="NVDA",
                    strategy_name="momentum_breakout",
                    state=SignalState.BUY,
                    timeframe=(timeframes or ["1d"])[0],
                    current_price=123.45,
                    entry_price=124.0,
                    stop_loss=120.0,
                    take_profit=132.0,
                    targets=[132.0],
                    rationale="test rationale",
                    score=95.0,
                    execution_ready=True,
                    metadata={"alert_eligible": True},
                )
            ],
        )


class FakeProposalService:
    def __init__(self):
        self.proposal: TradeProposal | None = None

    def create_proposal(self, request):
        proposal = TradeProposal(
            order=request.to_order(),
            signal=request.signal,
            notes=request.notes,
        )
        proposal.id = "prop_test"
        self.proposal = proposal
        return proposal

    def list_proposals(self, status=None):
        if self.proposal is None:
            self.create_proposal(
                type(
                    "Request",
                    (),
                    {
                        "to_order": lambda _self: FakeProposalService._sample_order(),
                        "signal": None,
                        "notes": "",
                    },
                )()
            )
        return [self.proposal]

    def approve_proposal(self, proposal_id, decision):
        assert proposal_id == "prop_test"
        assert self.proposal is not None
        self.proposal.status = ApprovalStatus.APPROVED
        self.proposal.approved_by = decision.reviewer
        return self.proposal

    def reject_proposal(self, proposal_id, decision):
        assert proposal_id == "prop_test"
        assert self.proposal is not None
        self.proposal.status = ApprovalStatus.REJECTED
        self.proposal.approved_by = decision.reviewer
        return self.proposal

    @staticmethod
    def _sample_order():
        from app.models.trade import OrderSide, TradeOrder

        return TradeOrder(
            symbol="NVDA",
            side=OrderSide.BUY,
            amount_usd=20.0,
            leverage=1,
            proposed_price=101.0,
            stop_loss=98.0,
            take_profit=107.0,
            strategy_name="momentum_breakout",
        )


class FakeExecutionCoordinator:
    def __init__(self):
        self.enqueued: list[str] = []
        self.processed: list[str] = []
        self.queue_record = ExecutionQueueRecord(
            id="queue_test",
            proposal_id="prop_test",
            symbol="NVDA",
            strategy_name="momentum_breakout",
            mode="paper",
        )
        self.queue = self

    def enqueue_approved_proposal(self, proposal_id):
        assert proposal_id == "prop_test"
        self.enqueued.append(proposal_id)
        return self.queue_record

    def process_queue_item(self, queue_id):
        assert queue_id == "queue_test"
        self.processed.append(queue_id)
        self.queue_record.status = "executed"
        self.queue_record.ready_for_execution = True
        self.queue_record.latest_quote_price = 101.1
        self.queue_record.validation_reason = "ready"
        return self.queue_record

    def process_ready_queue(self):
        return [self.process_queue_item("queue_test")]

    def list(self, *, status=None, limit=100):
        return [self.queue_record]


class FakePaperService:
    def dashboard(self):
        return {
            "paper": PaperPerformanceSummary(
                total_trades=4,
                open_positions=1,
                win_rate=50.0,
                realized_pnl_usd=120.0,
                unrealized_pnl_usd=15.0,
                expectancy_usd=30.0,
            ),
            "provider_health": {
                "history_provider": "yfinance",
                "quote_provider": "etoro",
                "freshness_status": "fresh",
            },
            "calibration_suggestions": ["Review near-miss outcomes before lowering relative-volume thresholds."],
        }


class FakeAutomation:
    def __init__(self):
        self.paused = False
        self.kill_switch = False
        self.reason = ""

    def status(self):
        return SimpleNamespace(
            paused=self.paused,
            kill_switch_enabled=self.kill_switch,
            auto_propose_enabled=False,
            auto_execute_after_approval=False,
            execution_mode="paper",
            require_approval=True,
            enable_real_trading=False,
            reason=self.reason,
        )

    def pause(self, *, reason: str = ""):
        self.paused = True
        self.reason = reason
        return self.status()

    def resume(self, *, reason: str = ""):
        self.paused = False
        self.kill_switch = False
        self.reason = reason
        return self.status()

    def enable_kill_switch(self, *, reason: str = ""):
        self.paused = True
        self.kill_switch = True
        self.reason = reason
        return self.status()


class FakeWorkflowSchedule:
    def schedule_statuses(self):
        return [
            SimpleNamespace(
                name="intraday_rotation",
                enabled=True,
                paused=False,
                last_run_at="2026-05-04T10:00:00-04:00",
                last_success_at="2026-05-04T10:00:00-04:00",
                next_due_at="2026-05-04T10:15:00-04:00",
                last_status="ok",
                last_error=None,
            ),
            SimpleNamespace(
                name="swing_hourly",
                enabled=True,
                paused=False,
                last_run_at="2026-05-04T09:35:00-04:00",
                last_success_at=None,
                next_due_at="2026-05-04T10:35:00-04:00",
                last_status="error",
                last_error="provider unavailable",
            ),
        ]

    @staticmethod
    def health_summary():
        return {"last_successful_screener_run_at": "2026-05-04T10:00:00-04:00"}


def test_send_due_alerts_respects_runtime_state() -> None:
    notifier = FakeNotifier()
    state = FakeStateRepo()
    logs = FakeRunLogRepo()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        runtime_state_repository=state,
        run_log_repository=logs,
    )
    assert bot.send_due_alerts() == 1
    assert bot.send_due_alerts() == 0


def test_performance_command_formats_dashboard() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/performance"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        paper_trading_service=FakePaperService(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert "AlgoBot performance" in notifier.sent[0][0]
    assert "Paper P&L: realized 120.00" in notifier.sent[0][0]


def test_poll_once_uses_market_screener_when_available() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/scan 3"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 7329410595}, "text": "/intraday_scan 2"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=FakeMarketScreener(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 2
    assert notifier.sent[0][0].startswith("screener 1 1d,1h")
    assert "best setup shown" in notifier.sent[0][0]
    assert notifier.sent[1][0].startswith("screener 1 15m,1h")
    assert "best setup shown" in notifier.sent[1][0]


def test_scan_command_can_scope_specific_symbols() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/scan 2 NVDA,AAPL MSFT"},
        },
    ]
    screener = FakeMarketScreener()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=screener,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert screener.calls[0]["symbols"] == ["NVDA", "AAPL", "MSFT"]
    assert screener.calls[0]["limit"] == 2
    assert "Scope: requested symbols NVDA, AAPL, MSFT" in notifier.sent[0][0]


def test_scan_command_can_override_timeframes() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/scan 2 NVDA AAPL 1m 10m"},
        },
    ]
    screener = FakeMarketScreener()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=screener,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert screener.calls[0]["symbols"] == ["NVDA", "AAPL"]
    assert screener.calls[0]["timeframes"] == ["1m", "10m"]


def test_scan_details_command_includes_lower_priority_watches() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/scan details 2 NVDA AAPL"},
        },
    ]
    screener = FakeMarketScreener()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=screener,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert screener.calls[0]["symbols"] == ["NVDA", "AAPL"]
    assert screener.calls[0]["limit"] == 2
    assert notifier.sent[0][0].startswith("screener 1 1d,1h details")


def test_scan_args_support_top100_and_custom_timeframes() -> None:
    limit, symbols, details, universe_limit, timeframes = TelegramBotService._parse_scan_args(
        ["details", "top100", "tf=1m,5m,10m,15m,1h,1d,1w", "4"]
    )

    assert limit == 4
    assert symbols is None
    assert details is True
    assert universe_limit == 100
    assert timeframes == ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]


def test_top100_scan_ignores_small_configured_quick_universe() -> None:
    notifier = FakeNotifier()
    screener = FakeMarketScreener()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=screener,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    bot._scan_message(
        limit=3,
        supported_only=False,
        validated_only=False,
        intraday=False,
        universe_limit=100,
        requested_timeframes=["1m"],
    )

    assert screener.calls[0]["symbols"] == DEFAULT_TOP_100_US
    assert screener.calls[0]["timeframes"] == ["1m"]


def test_scan_args_allow_symbol_scan_with_timeframe_tokens() -> None:
    limit, symbols, details, universe_limit, timeframes = TelegramBotService._parse_scan_args(
        ["3", "NVDA", "AAPL", "1m", "10m"]
    )

    assert limit == 3
    assert symbols == ["NVDA", "AAPL"]
    assert details is False
    assert universe_limit is None
    assert timeframes == ["1m", "10m"]


def test_telegram_proposal_approval_and_queue_commands() -> None:
    notifier = FakeNotifier()
    proposal_service = FakeProposalService()
    execution = FakeExecutionCoordinator()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/propose NVDA 20"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 7329410595}, "text": "/approve prop_test"},
        },
        {
            "update_id": 3,
            "message": {"chat": {"id": 7329410595}, "text": "/enqueue prop_test"},
        },
        {
            "update_id": 4,
            "message": {"chat": {"id": 7329410595}, "text": "/process_queue queue_test"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=FakeMarketScreener(),
        proposal_service=proposal_service,
        execution_coordinator=execution,
        execution_queue_repository=execution,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 4
    assert notifier.sent[0][0].startswith("Proposal created")
    assert "Order: NVDA BUY $20.00" in notifier.sent[0][0]
    assert notifier.sent[1][0].startswith("Proposal approved")
    assert notifier.sent[2][0].startswith("Proposal queued")
    assert notifier.sent[3][0].startswith("Queue processed")
    assert "Status: executed" in notifier.sent[3][0]


def test_telegram_approval_can_auto_execute_after_approval() -> None:
    class AutoExecuteSettings(FakeSettings):
        auto_execute_after_approval = True

    notifier = FakeNotifier()
    proposal_service = FakeProposalService()
    proposal_service.create_proposal(
        type(
            "Request",
            (),
            {
                "to_order": lambda _self: FakeProposalService._sample_order(),
                "signal": None,
                "notes": "",
            },
        )()
    )
    execution = FakeExecutionCoordinator()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/approve prop_test"},
        },
    ]
    bot = TelegramBotService(
        settings=AutoExecuteSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        proposal_service=proposal_service,
        execution_coordinator=execution,
        execution_queue_repository=execution,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert execution.enqueued == ["prop_test"]
    assert execution.processed == ["queue_test"]
    assert "Auto-execute after approval is enabled." in notifier.sent[0][0]


def test_telegram_automation_commands() -> None:
    notifier = FakeNotifier()
    automation = FakeAutomation()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/auto_status"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 7329410595}, "text": "/pause_auto testing"},
        },
        {
            "update_id": 3,
            "message": {"chat": {"id": 7329410595}, "text": "/resume_auto"},
        },
        {
            "update_id": 4,
            "message": {"chat": {"id": 7329410595}, "text": "/kill_switch emergency"},
        },
        {
            "update_id": 5,
            "message": {"chat": {"id": 7329410595}, "text": "/schedule_status"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        automation_service=automation,
        workflow_service=FakeWorkflowSchedule(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 5
    assert notifier.sent[0][0].startswith("Automation status")
    assert "Next due: intraday_rotation at 2026-05-04T10:15:00-04:00" in notifier.sent[0][0]
    assert "Latest failed bucket: swing_hourly" in notifier.sent[0][0]
    assert notifier.sent[1][0].startswith("Automation paused")
    assert notifier.sent[2][0].startswith("Automation resumed")
    assert notifier.sent[3][0].startswith("Kill switch enabled")
    assert notifier.sent[4][0].startswith("Schedule status")
    assert "intraday_rotation | enabled | active" in notifier.sent[4][0]


def test_telegram_propose_top_scans_and_creates_best_proposal() -> None:
    notifier = FakeNotifier()
    proposal_service = FakeProposalService()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/propose_top 20"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        market_screener=FakeMarketScreener(),
        proposal_service=proposal_service,
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert notifier.sent[0][0].startswith("Top opportunity proposal created")
    assert "Order: NVDA BUY $20.00" in notifier.sent[0][0]
    assert "Approve: /approve prop_test" in notifier.sent[0][0]


def test_poll_once_replies_with_error_when_command_fails() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/signal NVDA"},
        }
    ]
    logs = FakeRunLogRepo()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FailingLiveSignals(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=logs,
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 1
    assert notifier.sent[0][0].startswith("Command failed for")
    assert logs.items[0][0] == "telegram_command_error"


def test_signal_command_rejects_limits_and_scan_words() -> None:
    notifier = FakeNotifier()
    notifier.updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 7329410595}, "text": "/signal 5"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 7329410595}, "text": "/signal intraday 5"},
        },
    ]
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        runtime_state_repository=FakeStateRepo(),
        run_log_repository=FakeRunLogRepo(),
    )

    processed = bot.poll_once(timeout_seconds=0)

    assert processed == 2
    assert notifier.sent[0][0].startswith("Usage: /signal SYMBOL")
    assert "For ranked scans use /scan 5 or /intraday_scan 5." in notifier.sent[0][0]
    assert notifier.sent[1][0].startswith("Usage: /signal SYMBOL")
