"""The open-signal check stays inside its time budget (2026-10-06: ~130 s in market hours
pushed maintenance past 240 s) and skips price writes for tiny moves."""

from __future__ import annotations

import app.workflow.open_signals as open_signals
from app.live_signal_schema import MarketQuote
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings
from tests.test_workflow_service import (
    FakeAlertHistory,
    FakeLogs,
    FakeMarketScreener,
    FakeNotifier,
    FakeState,
    FakeTrackedSignals,
    _snapshot,
)


class _CountingSignals(FakeTrackedSignals):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def update_price(self, record_id, *, last_price, snapshot=None):
        self.writes += 1
        return super().update_price(record_id, last_price=last_price, snapshot=snapshot)


class _Quotes:
    def __init__(self, price):
        self.price, self.calls = price, []

    def get_quote(self, symbol, *, timeframe="1d", force_refresh=False):
        self.calls.append(symbol)
        return MarketQuote(symbol=symbol, last_execution=self.price)


def _workflow(tmp_path, tracked, quotes, state=None):
    return SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener([]),
        market_data_engine=quotes,
        notifier=FakeNotifier(),
        tracked_signals=tracked,
        alert_history=FakeAlertHistory(),
        runtime_state=state or FakeState(),
        run_logs=FakeLogs(),
    )


def _track(tracked, symbols):
    for symbol in symbols:
        tracked.upsert_open(_snapshot().model_copy(update={"symbol": symbol}), origin="swing_scan")


def test_tiny_price_moves_are_not_written(tmp_path) -> None:
    tracked = _CountingSignals()
    _track(tracked, ["NVDA"])  # stored last price 101.0
    _workflow(tmp_path, tracked, _Quotes(101.05)).check_open_signals(notify=False)
    assert tracked.writes == 0  # 0.05% move
    _workflow(tmp_path, tracked, _Quotes(101.5)).check_open_signals(notify=False)
    assert tracked.writes == 1  # 0.5% move


def test_budget_stops_the_run_and_the_next_run_resumes(tmp_path, monkeypatch) -> None:
    clock = iter(range(1000))
    monkeypatch.setattr(open_signals.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(open_signals, "OPEN_SIGNAL_BUDGET_SECONDS", 2.5)
    tracked = _CountingSignals()
    _track(tracked, ["AAA", "BBB", "CCC", "DDD"])
    state = FakeState()
    quotes = _Quotes(102.0)
    _workflow(tmp_path, tracked, quotes, state).check_open_signals(notify=False)
    first = list(quotes.calls)
    assert 0 < len(first) < 4  # stopped at the budget
    _workflow(tmp_path, tracked, quotes, state).check_open_signals(notify=False)
    assert quotes.calls[len(first)] not in first  # resumed with an unchecked signal
    assert set(quotes.calls) == {"AAA", "BBB", "CCC", "DDD"}
