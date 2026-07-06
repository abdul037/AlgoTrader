from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd

from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.models.screener import ScreenerRunResponse
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings


class FakeMarketScreener:
    def __init__(self, candidates, *, spec_keys=None):
        self.candidates = candidates
        self.spec_keys = list(spec_keys or [])
        self.calls = []

    def scan_universe(self, **kwargs):
        self.calls.append(kwargs)
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

    def strategy_spec_keys_for_timeframes(self, timeframes):
        requested = {str(item).lower() for item in timeframes}
        return [key for key in self.spec_keys if key.rsplit(":", 1)[-1] in requested]


class FailingMarketScreener:
    def scan_universe(self, **kwargs):
        raise RuntimeError("scan provider failed")


class FakeMarketDataEngine:
    def __init__(self, quote: MarketQuote):
        self.quote = quote

    def get_quote(self, symbol: str, *, timeframe: str = "1d", force_refresh: bool = False):
        return self.quote


class ActiveMoverMarketData:
    def __init__(self):
        self.frames = {}
        self.quotes = {}

    def add(self, symbol: str, *, last_volume_multiplier: float, quote: MarketQuote):
        timestamps = pd.date_range("2026-07-06T14:00:00Z", periods=40, freq="5min", tz="UTC")
        rows = []
        for index, timestamp in enumerate(timestamps):
            price = 100.0 + index * 0.1
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": price - 0.05,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "close": price,
                    "volume": 1_000_000,
                }
            )
        frame = pd.DataFrame(rows)
        frame.loc[frame.index[-1], "volume"] = 1_000_000 * last_volume_multiplier
        frame.attrs.update({"provider": "alpaca", "used_fallback": False, "from_cache": False, "data_age_seconds": 0.0})
        self.frames[symbol.upper()] = frame
        self.quotes[symbol.upper()] = quote

    def get_history(self, symbol: str, *, timeframe: str = "5m", bars: int = 40, force_refresh: bool = False):
        return self.frames[symbol.upper()].copy()

    def get_quote(self, symbol: str, *, timeframe: str = "5m", force_refresh: bool = False):
        return self.quotes[symbol.upper()]


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


class BlockingAutomation:
    def scan_blockers(self):
        return ["automation_paused"]


class FakeLedgerService:
    def __init__(self):
        self.alerts = []
        self.cycles = 0

    def record_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return len(self.alerts)

    def run_cycle(self):
        self.cycles += 1
        return {
            "snapshot_ts": "2026-04-11T00:00:00+00:00",
            "positions_seen": 2,
            "matched_new": 1,
            "closed_new": 0,
            "expired_pending": 0,
        }


class FakeRLPolicy:
    def __init__(self):
        self.trained = 0
        self.proposed = 0

    def train(self):
        self.trained += 1
        return type("Policy", (), {"blockers": []})()

    def propose(self):
        self.proposed += 1
        return type("Proposal", (), {"status": "queued"})()


def _snapshot() -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="momentum_breakout",
        state=SignalState.BUY,
        timeframe="1d",
        generated_at="2026-04-11T00:00:00+00:00",
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
    ledger = FakeLedgerService()
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener([snapshot]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=notifier,
        tracked_signals=tracked,
        alert_history=alerts,
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        ledger_service=ledger,
    )

    result = workflow.run_swing_scan(notify=True)

    assert result.candidates == 1
    assert result.alerts_sent == 1
    assert len(tracked.list(status="open")) == 1
    assert alerts.count() == 1
    assert len(ledger.alerts) == 1
    assert ledger.alerts[0]["symbol"] == "NVDA"
    assert ledger.alerts[0]["alert_source"] == "swing_scan"
    assert ledger.alerts[0]["alert_entry_price"] == 100.0


def test_run_swing_scan_tracks_watchlist_candidates(tmp_path) -> None:
    snapshot = _snapshot().model_copy(
        update={
            "metadata": {
                "alert_eligible": False,
                "signal_classification": "watchlist",
                "backtest_validated": True,
                "data_source": "etoro",
                "data_source_verified": True,
            }
        }
    )
    tracked = FakeTrackedSignals()
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, track_watchlist_signals=True),
        market_screener=FakeMarketScreener([snapshot]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=tracked,
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    result = workflow.run_swing_scan(notify=False)

    assert result.candidates == 1
    assert len(tracked.list(status="open")) == 1
    assert tracked.items[0].origin == "swing_scan"


def test_scheduled_tasks_runs_ledger_cycle_when_due_even_without_screener_scheduler(tmp_path) -> None:
    ledger = FakeLedgerService()
    state = FakeState()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            screener_scheduler_enabled=False,
            ledger_enabled=True,
            ledger_cycle_enabled=True,
            ledger_cycle_interval_minutes=15,
        ),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
        ledger_service=ledger,
    )

    summary = workflow.run_scheduled_tasks()

    assert summary["ledger_cycles"] == 1
    assert ledger.cycles == 1
    assert state.get("workflow:last_ledger_cycle_at") is not None


def test_lightweight_health_does_not_mark_scan_buckets_stale_off_market(tmp_path, monkeypatch) -> None:
    import app.workflow.schedule as schedule_module
    import app.workflow.service as service_module

    sunday = datetime(2026, 7, 5, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(service_module, "utc_now", lambda: sunday)
    monkeypatch.setattr(schedule_module, "utc_now", lambda: sunday)
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, schedule_timezone="UTC"),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    health = workflow.lightweight_health()

    scan_buckets = {
        item["name"]: item
        for item in health["buckets"]
        if item["name"] in workflow.SCAN_BUCKETS
    }
    assert all(item["expected"] is False for item in scan_buckets.values())
    assert all(item["stale"] is False for item in scan_buckets.values())
    assert "scheduler_bucket_stale:maintenance" in health["blockers"]
    assert not any(blocker.startswith("scheduler_bucket_stale:intraday_rotation") for blocker in health["blockers"])


def test_maintenance_runs_rl_policy_training_and_proposal_when_enabled(tmp_path) -> None:
    state = FakeState()
    rl_policy = FakeRLPolicy()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            rl_policy_enabled=True,
            rl_policy_training_enabled=True,
            rl_policy_paper_proposals_enabled=True,
            ledger_enabled=False,
            ledger_cycle_enabled=False,
            open_signal_check_interval_minutes=9999,
        ),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
        rl_policy_service=rl_policy,
    )

    result = workflow.run_maintenance(notify=False)

    assert result.status == "ok"
    assert rl_policy.trained == 1
    assert rl_policy.proposed == 1
    assert state.get("rl_policy:last_train_at") is not None
    assert "rl_policy_training" in result.detail
    assert "rl_policy_proposal_queued" in result.detail


def test_scheduled_tasks_skip_scans_when_automation_paused(tmp_path) -> None:
    screener = FakeMarketScreener([])
    logs = FakeLogs()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            screener_scheduler_enabled=True,
            ledger_enabled=False,
            ledger_cycle_enabled=False,
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=logs,
        automation_service=BlockingAutomation(),
    )

    summary = workflow.run_scheduled_tasks()

    assert summary["alerts_sent"] == 0
    assert screener.calls == []
    assert logs.items[-1][0] == "workflow_scheduler_paused"
    paused = workflow.schedule_statuses()
    assert next(item for item in paused if item.name == "intraday_rotation").last_status == "paused"


def test_intraday_scan_rotates_top100_batches(tmp_path) -> None:
    screener = FakeMarketScreener([])
    state = FakeState()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
            market_universe_limit=4,
            scalp_scan_batch_size=2,
            intraday_active_shortlist_size=0,
            screener_intraday_timeframes=["1m", "5m", "10m", "15m"],
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )

    workflow.run_intraday_scan(notify=False)
    workflow.run_intraday_scan(notify=False)

    assert screener.calls[0]["symbols"] == ["AAPL", "MSFT"]
    assert screener.calls[0]["timeframes"] == ["1m", "5m", "10m", "15m"]
    assert screener.calls[1]["symbols"] == ["NVDA", "AMD"]


def test_intraday_scan_rotates_strategy_spec_batches(tmp_path) -> None:
    spec_keys = [f"strategy_{index}:5m" for index in range(18)]
    screener = FakeMarketScreener([], spec_keys=spec_keys)
    state = FakeState()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["AAPL", "MSFT"],
            market_universe_limit=2,
            scalp_scan_batch_size=2,
            intraday_active_shortlist_size=0,
            screener_intraday_timeframes=["5m"],
            screener_spec_batch_mode="rotating",
            screener_spec_batch_size=8,
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="AAPL", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )

    workflow.run_intraday_scan(notify=False)
    workflow.run_intraday_scan(notify=False)
    workflow.run_intraday_scan(notify=False)

    assert screener.calls[0]["strategy_spec_keys"] == spec_keys[:8]
    assert screener.calls[1]["strategy_spec_keys"] == spec_keys[8:16]
    assert screener.calls[2]["strategy_spec_keys"] == [*spec_keys[16:], *spec_keys[:6]]
    coverage = json.loads(state.get("workflow:intraday_scan:last_scan_coverage"))
    assert coverage["spec_batch_size"] == 8
    assert coverage["spec_batch_total"] == 18


def test_scheduled_all_mode_uses_bucket_specific_timeframes(tmp_path) -> None:
    screener = FakeMarketScreener([])
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            screener_spec_coverage_mode="scheduled_all",
            market_universe_symbols=["AAPL", "MSFT"],
            market_universe_limit=2,
            intraday_active_shortlist_size=0,
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="AAPL", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    workflow.run_intraday_scan(notify=False)
    workflow.run_swing_scan(notify=False)
    workflow.run_end_of_day_scan(notify=False)

    assert screener.calls[0]["timeframes"] == ["1m", "5m", "10m", "15m"]
    assert screener.calls[1]["timeframes"] == ["1h", "1d"]
    assert screener.calls[2]["timeframes"] == ["1w"]


def test_intraday_rotation_includes_active_shortlist_before_batch(tmp_path) -> None:
    screener = FakeMarketScreener([])
    tracked = FakeTrackedSignals()
    tracked.upsert_open(_snapshot(), origin="manual")
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
            market_universe_limit=4,
            scalp_scan_batch_size=2,
            intraday_active_shortlist_size=1,
            screener_intraday_timeframes=["1m", "5m", "10m", "15m"],
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=tracked,
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    workflow.run_intraday_scan(notify=False)

    assert screener.calls[0]["symbols"] == ["NVDA", "AAPL", "MSFT"]
    assert screener.calls[0]["timeframes"] == ["1m", "5m", "10m", "15m"]


def test_intraday_rotation_prioritizes_active_movers_and_skips_bad_quotes(tmp_path) -> None:
    screener = FakeMarketScreener([])
    market_data = ActiveMoverMarketData()
    market_data.add(
        "FAST",
        last_volume_multiplier=2.4,
        quote=MarketQuote(symbol="FAST", bid=100.0, ask=100.1, last_execution=100.05, data_age_seconds=1),
    )
    market_data.add(
        "SLOW",
        last_volume_multiplier=0.8,
        quote=MarketQuote(symbol="SLOW", bid=100.0, ask=100.1, last_execution=100.05, data_age_seconds=1),
    )
    market_data.add(
        "STALE",
        last_volume_multiplier=3.0,
        quote=MarketQuote(symbol="STALE", bid=100.0, ask=100.1, last_execution=100.05, data_age_seconds=999),
    )
    market_data.add(
        "WIDE",
        last_volume_multiplier=3.0,
        quote=MarketQuote(symbol="WIDE", bid=100.0, ask=105.0, last_execution=102.5, data_age_seconds=1),
    )
    market_data.add(
        "BLK",
        last_volume_multiplier=3.0,
        quote=MarketQuote(symbol="BLK", bid=100.0, ask=100.1, last_execution=100.05, data_age_seconds=1),
    )
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["FAST", "SLOW", "STALE", "WIDE", "BLK", "AAPL"],
            market_universe_limit=6,
            scalp_scan_batch_size=2,
            intraday_active_shortlist_size=0,
            intraday_active_mover_shortlist_enabled=True,
            intraday_active_mover_shortlist_size=2,
            intraday_active_mover_scan_limit=5,
            blocked_instruments=["BLK"],
            max_market_data_age_seconds=30,
            screener_max_spread_bps=50,
        ),
        market_screener=screener,
        market_data_engine=market_data,
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    workflow.run_intraday_scan(notify=False)

    assert screener.calls[0]["symbols"][:2] == ["FAST", "SLOW"]
    assert "STALE" not in screener.calls[0]["symbols"][:2]
    assert "WIDE" not in screener.calls[0]["symbols"][:2]
    assert "BLK" not in screener.calls[0]["symbols"][:2]
    health = workflow.lightweight_health()
    assert health["scan_coverage"]["active_mover_shortlist"]["symbols"] == ["FAST", "SLOW"]


def test_scheduler_bucket_status_respects_new_york_market_weekdays(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.workflow.schedule.utc_now", lambda: datetime(2026, 5, 9, 14, 0, tzinfo=UTC))
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            schedule_timezone="America/New_York",
            screener_scheduler_enabled=True,
            market_open_scan_enabled=True,
            market_open_scan_time_local="09:35",
        ),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )

    assert workflow._bucket_due("market_open_scan") is False
    status = next(item for item in workflow.schedule_statuses() if item.name == "market_open_scan")
    assert status.enabled is True
    assert status.paused is False
    assert status.next_due_at is not None
    assert "2026-05-11T09:35:00" in status.next_due_at


def test_stale_workflow_lock_expires_and_allows_retry(tmp_path) -> None:
    state = FakeState()
    state.set("workflow:lock:swing_scan", "2000-01-01T00:00:00+00:00")
    screener = FakeMarketScreener([])
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, workflow_lock_timeout_minutes=1),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )

    result = workflow.run_swing_scan(notify=False)

    assert result.status == "ok"
    assert result.skipped is False
    assert screener.calls
    assert state.get("workflow:lock:swing_scan") == ""


def test_scheduler_records_bucket_success_and_error(tmp_path) -> None:
    state = FakeState()
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )

    success = workflow.run_intraday_scan(notify=False)
    success_status = next(item for item in workflow.schedule_statuses() if item.name == "intraday_rotation")

    workflow.market_screener = FailingMarketScreener()
    failure = workflow.run_intraday_scan(notify=False)
    failure_status = next(item for item in workflow.schedule_statuses() if item.name == "intraday_rotation")

    assert success.status == "ok"
    assert success_status.last_status == "ok"
    assert success_status.last_success_at is not None
    assert failure.status == "error"
    assert failure_status.last_status == "error"
    assert "scan provider failed" in (failure_status.last_error or "")


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
