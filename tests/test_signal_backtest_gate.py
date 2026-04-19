from __future__ import annotations

from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.runtime_settings import AppSettings
from app.signals.service import LiveSignalService


class DummyMarketData:
    pass


class DummySignalRepo:
    def __init__(self) -> None:
        self.created = []

    def create(self, signal):
        self.created.append(signal)
        return signal


class DummySignalStateRepo:
    def __init__(self) -> None:
        self.snapshot = None

    def get(self, symbol: str, strategy_name: str, timeframe: str):
        return None

    def upsert(self, snapshot):
        self.snapshot = snapshot
        return snapshot


class DummyRunLogRepo:
    def __init__(self) -> None:
        self.events = []

    def log(self, event_type: str, payload: dict):
        self.events.append((event_type, payload))


class DummyBacktestRepo:
    def __init__(self, summary=None) -> None:
        self.summary = summary
        self.calls = []

    def get_latest_summary(self, symbol: str, strategy_name: str | None = None):
        self.calls.append((symbol, strategy_name))
        return self.summary


class DummyNotifier:
    def __init__(self) -> None:
        self.calls = []

    def send_signal_change(self, snapshot, *, previous_state=None):
        self.calls.append((snapshot, previous_state))
        return True


def build_service(backtest_summary=None) -> LiveSignalService:
    settings = AppSettings(
        etoro_account_mode="demo",
        require_backtest_validation_for_alerts=True,
        allowed_instruments=["NVDA", "AMD", "MU", "GOOG", "GOOGL", "GOLD"],
    )
    return LiveSignalService(
        settings=settings,
        market_data_client=DummyMarketData(),
        signal_repository=DummySignalRepo(),
        signal_state_repository=DummySignalStateRepo(),
        run_log_repository=DummyRunLogRepo(),
        backtest_repository=DummyBacktestRepo(backtest_summary),
        telegram_notifier=DummyNotifier(),
    )


def sample_snapshot() -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="pullback_trend_100_10",
        state=SignalState.BUY,
        timeframe="OneDay",
        generated_at="2026-04-11T00:00:00Z",
        current_price=100.0,
        entry_price=101.0,
        exit_price=95.0,
        stop_loss=96.0,
        take_profit=110.0,
        rationale="test",
        score=120.0,
        confidence=0.8,
        tradable=True,
        supported=True,
        asset_class="equity",
        metadata={"data_source": "eToro", "data_source_verified": True},
    )


def test_attach_backtest_context_marks_validated_summary() -> None:
    service = build_service(
        {
            "symbol": "NVDA",
            "strategy_name": "pullback_trend",
            "completed_at": "2026-04-10T00:00:00Z",
            "metrics": {
                "number_of_trades": 25,
                "profit_factor": 1.8,
                "annualized_return_pct": 18.0,
                "max_drawdown_pct": 20.0,
                "win_rate": 55.0,
            },
            "trades": [],
        }
    )
    enriched = service._attach_backtest_context(sample_snapshot())
    assert enriched.metadata["backtest_validated"] is True
    assert enriched.metadata["backtest_strategy_name"] == "pullback_trend"
    assert enriched.metadata["backtest_profit_factor"] == 1.8


def test_send_signal_alert_suppressed_when_backtest_gate_fails() -> None:
    service = build_service(
        {
            "symbol": "NVDA",
            "strategy_name": "pullback_trend",
            "completed_at": "2026-04-10T00:00:00Z",
            "metrics": {
                "number_of_trades": 2,
                "profit_factor": 0.8,
                "annualized_return_pct": -5.0,
                "max_drawdown_pct": 60.0,
                "win_rate": 30.0,
            },
            "trades": [],
        }
    )

    service.get_latest_signal = lambda symbol, commit=True, notify=False: service._attach_backtest_context(sample_snapshot())  # type: ignore[method-assign]
    response = service.send_signal_alert_with_label("NVDA", previous_state="scheduled")

    assert response.sent is False
    assert "backtest gate" in response.detail
    assert service.notifier.calls == []
