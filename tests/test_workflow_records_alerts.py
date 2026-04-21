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
            generated_at="2026-04-21T00:00:00+00:00",
            universe_name="top100_us",
            timeframes=list(kwargs.get("timeframes") or ["1d"]),
            evaluated_symbols=1,
            evaluated_strategy_runs=1,
            candidates=self.candidates,
            suppressed=0,
            alerts_sent=0,
            errors=[],
        )


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, message: str, *, chat_id: str | None = None):
        self.messages.append(message)
        return True

    def format_screener_candidate(self, snapshot, *, rank=None):
        outcome_id = snapshot.metadata.get("ledger_outcome_id")
        assert outcome_id is not None
        return f"candidate outcome #{outcome_id}"

    def format_screener_summary(self, response, *, task_label=None):
        ids = [item.metadata.get("ledger_outcome_id") for item in response.candidates]
        assert all(ids)
        return "summary " + ",".join(str(item) for item in ids)


class FakeLedgerService:
    def __init__(self):
        self.alerts = []

    def record_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return 100 + len(self.alerts)


class FakeTrackedSignals:
    def __init__(self):
        self.items = []

    def list(self, *, status=None, limit=100):
        return []

    def upsert_open(self, snapshot, *, origin):
        self.items.append((snapshot, origin))


class FakeAlertHistory:
    def __init__(self):
        self.items = []

    def create(self, **kwargs):
        self.items.append(kwargs)

    def list(self, *, limit=50, category=None):
        return self.items[:limit]

    def count(self):
        return len(self.items)


class FakeState:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeLogs:
    def __init__(self):
        self.items = []

    def log(self, event_type, payload):
        self.items.append((event_type, payload))


class FakeMarketData:
    def get_quote(self, symbol, *, timeframe="1d", force_refresh=False):
        return MarketQuote(symbol=symbol, last_execution=101.0)


def _candidate() -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="rsi_vwap_ema_confluence",
        state=SignalState.BUY,
        timeframe="1d",
        generated_at="2026-04-21T00:00:00+00:00",
        current_price=101.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=112.0,
        direction_label="long",
        score=88.0,
        metadata={"alert_eligible": True, "strategy_diagnostics": {"rsi": 61.0}},
        indicators={"rsi": 61.0},
        score_breakdown={"setup": 20.0},
    )


def _workflow(tmp_path, *, alert_mode: str):
    return SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            screener_alert_mode=alert_mode,
            screener_top_alerts_per_run=1,
            ledger_enabled=True,
            ledger_record_alerts_enabled=True,
        ),
        market_screener=FakeMarketScreener([_candidate()]),
        market_data_engine=FakeMarketData(),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        ledger_service=FakeLedgerService(),
    )


def test_single_alert_records_ledger_before_telegram_formatting(tmp_path) -> None:
    workflow = _workflow(tmp_path, alert_mode="single")

    result = workflow.run_swing_scan(notify=True)

    assert result.alerts_sent == 1
    assert len(workflow.ledger_service.alerts) == 1
    assert workflow.notifier.messages == ["candidate outcome #101"]
    payload = workflow.ledger_service.alerts[0]["alert_payload"]
    assert payload["direction"] == "long"
    assert payload["confluence_vector"]["strategy_diagnostics"] == {"rsi": 61.0}


def test_digest_alert_records_every_candidate_before_telegram_formatting(tmp_path) -> None:
    workflow = _workflow(tmp_path, alert_mode="digest")

    result = workflow.run_swing_scan(notify=True)

    assert result.alerts_sent == 1
    assert len(workflow.ledger_service.alerts) == 1
    assert workflow.notifier.messages == ["summary 101"]
    assert workflow.alert_history.items[0]["payload"]["candidates"][0]["metadata"]["ledger_outcome_id"] == 101
