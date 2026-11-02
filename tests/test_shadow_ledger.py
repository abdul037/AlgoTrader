"""Shadow forward ledger (phase 2): every pre-market watchlist setup is followed on the
completed bars after it with the backtest engine's fill rules. Nothing is ever traded."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd

import app.workflow.shadow_ledger as shadow
from app.backtesting.cost_model import CostModel
from app.workflow.shadow_ledger import (
    LEDGER_KEY,
    STATE_KEY,
    refresh_shadow_ledger,
    score_entry,
    shadow_ledger_report,
)
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

COST = CostModel()
FRIDAY_1000_NY = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)  # 10:00 EDT


def _buy(raw: float) -> float:
    return COST.entry_fill_price(raw, side="buy", extended_hours=False)


def _sell(raw: float) -> float:
    return COST.exit_fill_price(raw, side="buy", extended_hours=False)


def _entry(signal_bar="2026-10-09", *, stop=95.0, target=110.0, spec="trend_following:1d"):
    return {
        "signal_date": "2026-10-12",
        "signal_bar": signal_bar,
        "symbol": "S00",
        "spec_key": spec,
        "kind": "strict",
        "tradeable": True,
        "evidence_passing": True,
        "expected_r": 0.05,
        "signal_price": 100.0,
        "stop": stop,
        "target": target,
        "status": "pending",
    }


def _bars(*rows):
    """(session date, open, high, low, close) rows."""

    return [(day, *(float(v) for v in values)) for day, *values in rows]


SIGNAL = ("2026-10-09", 100, 101, 99, 100)


def test_entry_at_the_next_open_and_the_target_at_the_target() -> None:
    entry = _entry()
    bars = _bars(
        SIGNAL,
        ("2026-10-12", 100.5, 102, 99.5, 101),
        ("2026-10-13", 101, 104, 100, 103),
        ("2026-10-14", 103, 111, 102, 109),
    )
    score_entry(entry, bars, date(2026, 10, 15))
    fill = _buy(100.5)
    assert entry["entry_date"] == "2026-10-12" and entry["entry_price"] == round(fill, 4)
    leg = entry["stop_target"]
    assert (leg["status"], leg["reason"], leg["exit_date"], leg["bars"]) == (
        "closed",
        "target",
        "2026-10-14",
        3,
    )
    assert leg["r"] == round((_sell(110) - fill) / (fill - 95), 3)
    assert entry["time_14d"] == leg and entry["status"] == "closed"


def test_stop_wins_a_bar_that_touches_both_and_a_gap_fills_at_the_open() -> None:
    both = _entry()
    score_entry(both, _bars(SIGNAL, ("2026-10-12", 100.5, 111, 94, 100)), date(2026, 10, 13))
    assert both["stop_target"]["reason"] == "stop"
    assert both["stop_target"]["exit_price"] == round(_sell(95), 4)
    gap = _entry()
    bars = _bars(SIGNAL, ("2026-10-12", 100.5, 101, 99, 100), ("2026-10-13", 93, 94, 92, 93))
    score_entry(gap, bars, date(2026, 10, 14))
    fill = _buy(100.5)
    assert gap["stop_target"]["exit_price"] == round(_sell(93), 4)  # the worse of stop and open
    assert gap["stop_target"]["r"] == round((_sell(93) - fill) / (fill - 95), 3) < -1


def _quiet(start: str, end: str) -> list[tuple]:
    days = pd.bdate_range(start, end).strftime("%Y-%m-%d")
    return _bars(*((day, 100, 102, 99, 101) for day in days))


def test_a_quiet_trade_gets_the_fold_length_time_exit_and_stays_open_otherwise() -> None:
    entry = _entry()
    score_entry(entry, [_bars(SIGNAL)[0], *_quiet("2026-10-12", "2026-10-30")], date(2026, 10, 31))
    fill = _buy(100)  # the quiet bars open at 100
    timed = entry["time_14d"]
    # Entry Mon 10-12; the 14-day fold ends before Mon 10-26: last close Fri 10-23.
    assert (timed["status"], timed["reason"], timed["exit_date"]) == (
        "closed",
        "time_exit",
        "2026-10-23",
    )
    assert timed["r"] == round((_sell(101) - fill) / (fill - 95), 3)
    held = entry["stop_target"]
    assert (held["status"], held["reason"], held["exit_date"]) == ("open", "marked", "2026-10-30")
    assert entry["status"] == "open"
    early = _entry()  # day 6 of 14, no exit yet: the time exit is still open
    score_entry(early, [_bars(SIGNAL)[0], *_quiet("2026-10-12", "2026-10-16")], date(2026, 10, 17))
    assert early["time_14d"] == {"status": "open"} and early["status"] == "open"
    late = _entry()  # data ends 10-23 but the horizon has passed: every bar before it is in
    score_entry(late, [_bars(SIGNAL)[0], *_quiet("2026-10-12", "2026-10-23")], date(2026, 10, 26))
    assert late["time_14d"]["exit_date"] == "2026-10-23"


def test_waits_for_the_next_session_and_skips_a_stop_at_the_fill() -> None:
    entry = _entry()
    score_entry(entry, _bars(SIGNAL), date(2026, 10, 12))
    assert entry["status"] == "pending" and "entry_date" not in entry
    flat = _entry(stop=_buy(100.5))
    score_entry(flat, _bars(SIGNAL, ("2026-10-12", 100.5, 102, 99, 101)), date(2026, 10, 13))
    assert flat["status"] == "no_risk"


def test_one_trade_per_symbol_and_strategy_like_the_backtest() -> None:
    first, second = _entry(), _entry("2026-10-13")
    second["signal_date"] = "2026-10-14"
    other = _entry("2026-10-13", spec="ema_trend_stack:1d")
    bars = [_bars(SIGNAL)[0], *_quiet("2026-10-12", "2026-10-16")]
    shadow._score_symbol([first, other, second], bars, date(2026, 10, 17))
    assert first["status"] == "open" and second["status"] == "skipped_open"
    assert other["status"] == "open"  # another strategy is its own backtest run
    first, second = _entry(), _entry("2026-10-12")
    bars = _bars(SIGNAL, ("2026-10-12", 100.5, 101, 94, 96), ("2026-10-13", 96, 97, 95.5, 96.5))
    shadow._score_symbol([first, second], bars, date(2026, 10, 14))
    assert first["stop_target"]["exit_date"] == "2026-10-12"  # stopped on the signal bar
    assert second["status"] == "open"  # so the next signal can enter


class _Market:
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def get_history(self, symbol, *, timeframe="1d", bars=250, force_refresh=False):
        self.calls.append((symbol, timeframe, bars, force_refresh))
        return self.frames[symbol]


def _frame(rows):
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([f"{r[0]} 04:00" for r in rows], utc=True),
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [1e6] * len(rows),
        }
    )


def _signal(symbol, spec="trend_following:1d", *, bar="2026-10-09", stop=95.0, target=110.0):
    return {
        "symbol": symbol,
        "spec_key": spec,
        "strategy": spec.split(":")[0],
        "kind": "strict",
        "signal_bar_ts": f"{bar} 04:00:00+00:00",
        "entry": 100.0,
        "stop": stop,
        "target": target,
        "oos_expectancy_r": 0.046,
        "tradeable": True,
        "evidence_passing": True,
    }


def _service(tmp_path, monkeypatch, *, now=FRIDAY_1000_NY, enabled=True):
    monkeypatch.setattr(shadow, "utc_now", lambda: now)
    state = FakeState()
    watchlists = {
        "2026-10-12": ("complete", [_signal("S00"), _signal("S01", stop=99.5)]),
        "2026-10-13": ("complete_with_gaps", [_signal("S00", bar="2026-10-12")]),  # S00 still open
        "2026-10-14": ("partial", [_signal("S01", bar="2026-10-13")]),  # never finished: ignored
    }
    for day, (status, signals) in watchlists.items():
        state.set(f"premarket:watchlist:{day}", json.dumps({"status": status, "signals": signals}))
    session = [("2026-10-09", 100, 101, 99, 100), *_quiet("2026-10-12", "2026-10-16")]
    session.append(("2026-10-16", 100, 102, 99, 101))  # today's forming bar: never used
    frames = {"S00": _frame(session), "S01": _frame(session)}
    market = _Market(frames)
    return SimpleNamespace(
        settings=make_settings(tmp_path, premarket_deep_scan_enabled=enabled),
        market_data=market,
        market_screener=SimpleNamespace(_bars_for_timeframe=lambda timeframe: 400),
        runtime_state=state,
        run_logs=FakeLogs(),
    )


def test_daily_refresh_adds_finished_watchlists_and_scores_them(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    before = set(service.runtime_state.values)
    summary = refresh_shadow_ledger(service)
    assert summary["added"] == 3 and summary["done_for_today"] and summary["shadow_only"]
    entries = json.loads(service.runtime_state.get(LEDGER_KEY))["entries"]
    assert set(entries) == {
        "2026-10-12|S00|trend_following:1d",
        "2026-10-12|S01|trend_following:1d",
        "2026-10-13|S00|trend_following:1d",
    }
    assert entries["2026-10-12|S00|trend_following:1d"]["status"] == "open"
    assert entries["2026-10-13|S00|trend_following:1d"]["status"] == "skipped_open"
    stopped = entries["2026-10-12|S01|trend_following:1d"]
    assert stopped["stop_target"]["reason"] == "stop"  # the quiet bars dip to 99 < 99.5
    assert all(call[3] is False for call in service.market_data.calls)  # the cache may serve
    assert set(service.runtime_state.values) - before == {LEDGER_KEY, STATE_KEY}
    assert [e for e, _ in service.run_logs.items] == ["premarket_shadow_ledger_updated"]
    assert refresh_shadow_ledger(service) is None  # once a day
    assert len(service.run_logs.items) == 1


def test_runs_only_after_the_scan_cutoff_and_with_the_flag(tmp_path, monkeypatch) -> None:
    early = _service(tmp_path, monkeypatch, now=datetime(2026, 10, 16, 13, 30, tzinfo=UTC))
    assert refresh_shadow_ledger(early) is None and early.market_data.calls == []
    off = _service(tmp_path, monkeypatch, enabled=False)
    assert refresh_shadow_ledger(off) is None and off.market_data.calls == []


def test_a_run_out_of_time_leaves_the_rest_for_the_next_one(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    summary = refresh_shadow_ledger(service, budget_seconds=0)
    assert summary["scored_symbols"] == 0 and not summary["done_for_today"]
    entries = json.loads(service.runtime_state.get(LEDGER_KEY))["entries"]
    assert {e["status"] for e in entries.values()} == {"pending"}
    assert refresh_shadow_ledger(service)["done_for_today"]  # the next maintenance run
    assert refresh_shadow_ledger(service) is None


def test_report_compares_shadow_r_with_the_backtest(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    refresh_shadow_ledger(service)
    report = shadow_ledger_report(service.runtime_state)
    assert (report["signals"], report["taken"], report["skipped_open"]) == (3, 2, 1)
    trend = report["strategies"]["trend_following:1d"]
    assert trend["stop_target"]["closed"] == 1 and trend["open"] == 1
    assert trend["stop_target"]["win_rate"] == 0.0 and trend["backtest_expected_r"] == 0.046
    assert report["cohorts"]["strict"]["taken"] == 2 and report["cohorts"]["weak"]["taken"] == 0
    assert shadow_ledger_report(FakeState())["signals"] == 0


def test_scorecard_endpoint_carries_the_shadow_ledger(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import MockBroker

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    body = (
        TestClient(app)
        .get("/performance/live-scorecard", headers={"X-Control-Token": "secret"})
        .json()
    )
    assert body["shadow_ledger"]["signals"] == 0 and "rules" in body["shadow_ledger"]


def test_maintenance_runs_it_with_the_phase_gates(tmp_path, monkeypatch) -> None:
    from app.workflow.operations import refresh_phase_gates

    calls = []
    monkeypatch.setattr(shadow, "refresh_shadow_ledger", lambda service: calls.append(1) or {})
    service = SimpleNamespace(
        settings=make_settings(tmp_path),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        market_screener=SimpleNamespace(),
        auto_trading=SimpleNamespace(),
        _is_due=lambda key, minutes: True,
    )
    completed, errors = [], []
    refresh_phase_gates(service, completed, errors)
    assert calls == [1] and "premarket_shadow_ledger" in completed


def test_old_entries_are_dropped_and_bad_signals_never_enter(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    bad = [_signal("S02", stop=101.0), {**_signal("S03"), "signal_bar_ts": None}]
    service.runtime_state.set(
        "premarket:watchlist:2026-10-15", json.dumps({"status": "complete", "signals": bad})
    )
    refresh_shadow_ledger(service)
    ledger = json.loads(service.runtime_state.get(LEDGER_KEY))
    assert not any(e["symbol"] in {"S02", "S03"} for e in ledger["entries"].values())
    old = {**_entry(), "signal_date": "2026-08-01"}
    ledger["entries"]["2026-08-01|S00|trend_following:1d"] = old
    service.runtime_state.set(LEDGER_KEY, json.dumps(ledger))
    service.runtime_state.set(STATE_KEY, json.dumps({"ingested_through": "2026-10-16"}))
    refresh_shadow_ledger(service)
    entries = json.loads(service.runtime_state.get(LEDGER_KEY))["entries"]
    assert "2026-08-01|S00|trend_following:1d" not in entries and len(entries) == 3
