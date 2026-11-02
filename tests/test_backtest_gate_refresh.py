"""Item A (operator 2026-11-01): the daily evidence of every symbol is refreshed first.

From 10-27 a pass ran daily and minute-bar strategies symbol by symbol and the minute-bar
walk-forwards took most of it: only 10-16 of 25 symbols had fresh daily evidence a day.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.backtesting.gate_refresh as gate
from app.utils.time import utc_now
from tests.test_workflow_service import FakeState

UNIVERSE = 25
BOTH = ["1d", "15m", "5m", "10m"]


class _Batch:
    def __init__(self, covered=5, mid_symbol=False, resume=0):
        self.calls: list[dict] = []
        self.shorts = 0
        self.summary = SimpleNamespace(
            symbols_evaluated=covered, stopped_mid_symbol=mid_symbol, resume_unit=resume
        )

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.summary

    def run_short_research(self, runtime_state):
        self.shorts += 1


@pytest.fixture(autouse=True)
def _universe(monkeypatch):
    import app.universe as universe

    monkeypatch.setattr(universe, "resolve_universe", lambda settings, limit=None: ["S"] * UNIVERSE)


def _settings(timeframes=BOTH):
    return SimpleNamespace(
        backtest_scheduler_symbol_limit=0,
        backtest_scheduler_timeframes=timeframes,
        backtest_scheduler_deadline_seconds=180.0,
    )


def test_daily_passes_run_first_until_every_symbol_is_covered() -> None:
    state, batch = FakeState(), _Batch(covered=6)  # 6 whole symbols a pass
    kinds = [gate.refresh_backtest_gate(batch, _settings(), state)["pass"] for _ in range(6)]
    assert kinds == ["daily"] * 5 + ["minute"]  # 5 x 6 = 30 >= 25: the cycle completed
    assert [c["timeframes"] for c in batch.calls[:5]] == [["1d"]] * 5
    assert batch.calls[5]["timeframes"] == ["15m", "5m", "10m"]
    assert [c["start_offset"] for c in batch.calls[:5]] == [0, 6, 12, 18, 24]
    assert state.get(gate.DAILY_FULL_AT_KEY) and state.get(gate.DAILY_COVERED_KEY) == "0"
    assert batch.shorts == 6  # the short research still follows every pass


def test_minute_passes_keep_the_original_cursor_and_unit() -> None:
    state = FakeState()
    state.set(gate.DAILY_FULL_AT_KEY, utc_now().isoformat())  # the daily cycle is fresh
    state.set(gate.CURSOR_KEY, "7")
    state.set(gate.UNIT_KEY, "31")
    batch = _Batch(covered=2, mid_symbol=True, resume=12)
    assert gate.refresh_backtest_gate(batch, _settings(), state)["pass"] == "minute"
    assert (batch.calls[0]["start_offset"], batch.calls[0]["start_unit"]) == (7, 31)
    assert (state.get(gate.CURSOR_KEY), state.get(gate.UNIT_KEY)) == ("8", "12")  # stays mid-symbol
    assert state.get(gate.DAILY_CURSOR_KEY) is None  # the daily cursor is untouched


def test_the_daily_cycle_comes_round_again_after_twelve_hours() -> None:
    state = FakeState()
    state.set(gate.DAILY_FULL_AT_KEY, (utc_now() - timedelta(hours=11)).isoformat())
    assert gate.refresh_backtest_gate(_Batch(), _settings(), state)["pass"] == "minute"
    state.set(gate.DAILY_FULL_AT_KEY, (utc_now() - timedelta(hours=12, minutes=1)).isoformat())
    assert gate.refresh_backtest_gate(_Batch(), _settings(), state)["pass"] == "daily"


def test_a_daily_pass_stopped_inside_a_symbol_resumes_there() -> None:
    state, batch = FakeState(), _Batch(covered=3, mid_symbol=True, resume=9)
    gate.refresh_backtest_gate(batch, _settings(), state)
    assert (state.get(gate.DAILY_CURSOR_KEY), state.get(gate.DAILY_UNIT_KEY)) == ("2", "9")
    assert state.get(gate.DAILY_COVERED_KEY) == "2"  # the unfinished symbol is not counted
    gate.refresh_backtest_gate(batch, _settings(), state)
    assert (batch.calls[1]["start_offset"], batch.calls[1]["start_unit"]) == (2, 9)


@pytest.mark.parametrize("bad", ["", "not a time", None])
def test_an_unreadable_cycle_time_refreshes_the_daily_evidence(bad) -> None:
    state = FakeState()
    if bad is not None:
        state.set(gate.DAILY_FULL_AT_KEY, bad)
    assert gate.refresh_backtest_gate(_Batch(), _settings(), state)["pass"] == "daily"


@pytest.mark.parametrize("timeframes", [["1d"], ["15m", "5m"], ["1d", "1w"]])
def test_one_kind_runs_every_pass_on_the_original_cursor(timeframes) -> None:
    state, batch = FakeState(), _Batch(covered=4)
    for _ in range(3):
        assert gate.refresh_backtest_gate(batch, _settings(timeframes), state)["pass"] == "all"
    assert all(c["timeframes"] == timeframes for c in batch.calls)
    assert state.get(gate.CURSOR_KEY) == "12" and state.get(gate.DAILY_CURSOR_KEY) is None
