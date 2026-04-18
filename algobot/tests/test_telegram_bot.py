from __future__ import annotations

from app.live_signal_schema import LiveSignalSnapshot, SignalScanResponse, SignalState
from app.notifications.telegram_bot import TelegramBotService


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
    def format_screener_summary(response):
        return f"screener {len(response.candidates)} {','.join(response.timeframes)}"


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
    allowed_instruments = ["NVDA", "AMD"]
    screener_intraday_timeframes = ["15m", "1h"]
    screener_default_timeframes = ["1d", "1h"]


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


class FakeMarketScreener:
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
                    rationale="test rationale",
                    score=95.0,
                )
            ],
        )


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
    assert notifier.sent[0][0] == "screener 1 1d,1h"
    assert notifier.sent[1][0] == "screener 1 15m,1h"


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
