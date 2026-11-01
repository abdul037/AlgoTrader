"""Item C (operator 2026-11-01): SPY's daily closes for the scorecard's "vs holding SPY" line."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import app.workflow.benchmark as benchmark
from app.utils.time import utc_now
from tests.test_workflow_service import FakeLogs, FakeState


class _Market:
    def __init__(self, frame=None, error=None):
        self.frame, self.error, self.calls = frame, error, []

    def get_history(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if self.error:
            raise self.error
        return self.frame


def _frame(rows):
    return pd.DataFrame({"timestamp": [pd.Timestamp(t) for t, _ in rows], "close": [c for _, c in rows]})


def _service(market):
    return SimpleNamespace(runtime_state=FakeState(), run_logs=FakeLogs(), market_data=market)


ROWS = [
    ("2026-05-29T04:00:00Z", 590.0),  # before FROM_DATE: left out
    ("2026-06-10T04:00:00Z", 600.0),
    ("2026-06-11T04:00:00Z", 601.5),  # Alpaca stamps daily bars at midnight New York
    ("2026-10-30", 680.25),  # a naive stamp is read as the date itself
]


def test_stores_new_york_dated_closes_from_june() -> None:
    market = _Market(_frame(ROWS))
    service = _service(market)
    payload = benchmark.refresh_benchmark(service)
    assert payload["closes"] == {"2026-06-10": 600.0, "2026-06-11": 601.5, "2026-10-30": 680.25}
    assert json.loads(service.runtime_state.get(benchmark.BENCHMARK_KEY)) == payload
    symbol, kwargs = market.calls[0]
    assert symbol == "SPY" and kwargs["timeframe"] == "1d" and kwargs["write_cache"] is False


def test_refreshes_at_most_every_six_hours() -> None:
    service = _service(_Market(_frame(ROWS)))
    assert benchmark.refresh_benchmark(service) is not None
    assert benchmark.refresh_benchmark(service) is None  # just stored
    stored = json.loads(service.runtime_state.get(benchmark.BENCHMARK_KEY))
    stored["updated_at"] = (utc_now() - timedelta(hours=6, minutes=1)).isoformat()
    service.runtime_state.set(benchmark.BENCHMARK_KEY, json.dumps(stored))
    assert benchmark.refresh_benchmark(service) is not None


@pytest.mark.parametrize("close", [float("nan"), float("inf"), 0.0, -1.0])
def test_bad_closes_are_left_out(close) -> None:
    closes = benchmark.daily_closes(_frame([("2026-10-29", close), ("2026-10-30", 680.0)]))
    assert closes == {"2026-10-30": 680.0}


def test_a_failed_read_raises_and_keeps_the_stored_closes() -> None:
    service = _service(_Market(error=RuntimeError("feed down")))
    service.runtime_state.set(benchmark.BENCHMARK_KEY, json.dumps({"updated_at": "x", "closes": {"a": 1}}))
    with pytest.raises(RuntimeError):
        benchmark.refresh_benchmark(service)
    assert json.loads(service.runtime_state.get(benchmark.BENCHMARK_KEY))["closes"] == {"a": 1}
    empty = _service(_Market(_frame([("2026-05-01", 500.0)])))
    with pytest.raises(ValueError):
        benchmark.refresh_benchmark(empty)
    assert empty.runtime_state.get(benchmark.BENCHMARK_KEY) is None


def test_the_phase_gate_step_runs_it_and_a_failure_never_stops_the_others(monkeypatch) -> None:
    from app.workflow import operations

    ran: list[str] = []
    monkeypatch.setattr(benchmark, "refresh_benchmark", lambda service: ran.append("spy") or {"ok": 1})
    service = SimpleNamespace(
        runtime_state=FakeState(), run_logs=FakeLogs(), settings=SimpleNamespace(),
        market_screener=SimpleNamespace(), auto_trading=None, _is_due=lambda key, minutes: True,
    )
    completed: list[str] = []
    operations.refresh_phase_gates(service, completed, [])
    assert ran == ["spy"] and "benchmark_refresh" in completed
    monkeypatch.setattr(benchmark, "refresh_benchmark", lambda service: (_ for _ in ()).throw(RuntimeError("boom")))
    completed, errors = [], []
    operations.refresh_phase_gates(service, completed, errors)
    assert "phase_gates_refresh:benchmark_refresh:boom" in errors
