from __future__ import annotations

from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.models.screener import ScreenerRunResponse
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings


class FakeMarketScreener:
    def __init__(self, candidates):
        self.candidates = candidates

    def scan_universe(self, **kwargs):
        return ScreenerRunResponse(
            generated_at="2026-04-11T00:00:00+00:00",
            universe_name="top100_us",
            timeframes=list(kwargs.get("timeframes") or ["1d"]),
            evaluated_symbols=1,
            evaluated_strategy_runs=1,
            candidates=self.candidates,
            suppressed=0,
            alerts_sent=0,
            errors=[],
        )


class FakeMarketDataEngine:
    def __init__(self, quote: MarketQuote):
        self.quote = quote

    def get_quote(self, symbol: str, *, timeframe: str = "1d", force_refresh: bool = False):
        return self.quote


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, message: str, *, chat_id: str | None = None):
        self.messages.append(message)
        return True

    @staticmethod
    def format_screener_summary(response):
        return f"screener:{len(response.candidates)}"

    @staticmethod
    def format_tracked_signal_update(record, *, event_type: str):
        return f"tracked:{record.symbol}:{event_type}"

    @staticmethod
    def format_daily_summary(*, open_signals, recent_alerts):
        return f"summary:{len(open_signals)}:{len(recent_alerts)}"


class FakeTrackedSignals:
    def __init__(self):
        self.items = []
        self.next_id = 1

    def list(self, *, status: str | None = None, limit: int = 100):
        items = self.items
        if status is not None:
            items = [item for item in items if item.status == status]
        return items[:limit]

    def upsert_open(self, snapshot, *, origin: str):
        existing = next(
            (
                item
                for item in self.items
                if item.symbol == snapshot.symbol
                and item.strategy_name == snapshot.strategy_name
                and item.timeframe == snapshot.timeframe
                and item.status == "open"
            ),
            None,
        )
        if existing is not None:
            existing.snapshot = snapshot
            existing.last_price = snapshot.current_price
            existing.updated_at = "2026-04-11T00:05:00+00:00"
            return existing
        from app.models.workflow import TrackedSignalRecord

        item = TrackedSignalRecord(
            id=self.next_id,
            symbol=snapshot.symbol,
            strategy_name=snapshot.strategy_name,
            timeframe=snapshot.timeframe,
            status="open",
            origin=origin,
            opened_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            entry_price=snapshot.entry_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            last_price=snapshot.current_price,
            snapshot=snapshot,
        )
        self.next_id += 1
        self.items.append(item)
        return item

    def update_price(self, record_id: int, *, last_price: float, snapshot=None):
        item = self.get_by_id(record_id)
        item.last_price = last_price
        if snapshot is not None:
            item.snapshot = snapshot
        item.updated_at = "2026-04-11T00:10:00+00:00"
        return item

    def close(self, record_id: int, *, status: str, last_price: float, snapshot=None):
        item = self.get_by_id(record_id)
        item.status = status
        item.last_price = last_price
        item.closed_at = "2026-04-11T00:10:00+00:00"
        if snapshot is not None:
            item.snapshot = snapshot
        return item

    def get_by_id(self, record_id: int):
        return next(item for item in self.items if item.id == record_id)


class FakeAlertHistory:
    def __init__(self):
        self.items = []

    def create(self, **kwargs):
        self.items.append(kwargs)
        return kwargs

    def list(self, *, limit: int = 50, category: str | None = None):
        items = self.items
        if category is not None:
            items = [item for item in items if item["category"] == category]
        return items[:limit]

    def count(self):
        return len(self.items)


class FakeState:
    def __init__(self):
        self.values = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str):
        self.values[key] = value


class FakeLogs:
    def __init__(self):
        self.items = []

    def log(self, event_type: str, payload: dict):
        self.items.append((event_type, payload))


def _snapshot() -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="momentum_breakout",
        state=SignalState.BUY,
        timeframe="1d",
        current_price=101.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        signal_role="entry_long",
        rationale="test",
        score=90.0,
        confidence=0.7,
        metadata={"backtest_validated": True, "data_source": "etoro", "data_source_verified": True},
    )


def test_run_swing_scan_tracks_and_records_alert(tmp_path) -> None:
    snapshot = _snapshot()
    tracked = FakeTrackedSignals()
    alerts = FakeAlertHistory()
    notifier = FakeNotifier()
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener([snapshot]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=notifier,
        tracked_signals=tracked,
        alert_history=alerts,
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    result = workflow.run_swing_scan(notify=True)

    assert result.candidates == 1
    assert result.alerts_sent == 1
    assert len(tracked.list(status="open")) == 1
    assert alerts.count() == 1


def test_open_signal_check_closes_target_hit(tmp_path) -> None:
    snapshot = _snapshot()
    tracked = FakeTrackedSignals()
    tracked.upsert_open(snapshot, origin="swing_scan")
    alerts = FakeAlertHistory()
    notifier = FakeNotifier()
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener([snapshot]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=111.0)),
        notifier=notifier,
        tracked_signals=tracked,
        alert_history=alerts,
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    result = workflow.check_open_signals(notify=True)

    assert result.closed_signals == 1
    assert tracked.list(status="open") == []
    assert tracked.items[0].status == "target_hit"
    assert alerts.count() == 1
