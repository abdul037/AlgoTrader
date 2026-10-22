"""Paper-only entries in the auto-propose pass and the swing scan (operator option B, 2026-10-21).

* an eToro-eligible candidate is considered before a paper-only one: never loses its symbol;
* a paper-only entry never opens in a symbol the eToro account holds or has in flight (read
  once a pass, as the mirror's room check reads it; unreadable refuses);
* it leaves 2 of the day's paper trades and 2 of its group's daily entries;
* it is tagged; nothing else is, and with the switch off nothing changes;
* the swing scan scans paper-only specs only after a run's eToro-passing specs reached every
  symbol, in a second scan inside the same run's deadline.

No wall clock: the pass's clock and the scan's monotonic clock are patched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.workflow.operations as operations
import app.workflow.paper_only as paper_only_module
import app.workflow.swing_focus as swing_focus
from app.automation.unattended import PaperAutoTradingService
from app.broker.etoro_live_mirror import STATE_KEY
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from app.broker.etoro_live_test_order import TEST_STATE_KEY
from app.live_signal_schema import MarketQuote
from app.models.screener import ScreenerRunResponse
from app.performance.strategy_evidence import VERDICTS_KEY
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import LEAVE_FOR_ETORO, LEAVE_IN_GROUP, PaperOnlyPass
from app.workflow.service import SignalWorkflowService
from app.workflow.swing_focus import (
    PAPER_ONLY_CURSOR_KEY,
    PAPER_ONLY_PASS_SECONDS,
    PAPER_ONLY_TOP_K,
    paper_only_scan,
)
from tests.conftest import make_settings
from tests.test_workflow_service import (
    FakeAlertHistory,
    FakeLogs,
    FakeMarketDataEngine,
    FakeNotifier,
    FakeProposalService,
    FakeState,
    FakeTrackedSignals,
    _snapshot,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
FULL = {"passed": True, "paper_oos_passed": True, "oos_expectancy_r": 0.04}
OOS_ONLY = {"passed": False, "paper_oos_passed": True, "oos_expectancy_r": 0.068}
VERDICTS = {
    "inside_bar_narrow_range_breakout:1d": FULL,
    "momentum_breakout:1d": OOS_ONLY,
    "trend_following:1d": {"passed": False, "paper_oos_passed": False},
}
TAGS = {"paper_only": True, "paper_only_reason": "oos_only"}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW)


class _State(dict):
    """A runtime_state that counts its reads."""

    def __init__(self, values=None):
        super().__init__(values or {})
        self.reads: list[str] = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Executions:
    """Today's paper execution rows, as count_since and entries_today_by_bucket read them."""

    def __init__(self, symbols=(), fail=False):
        self.rows = [
            SimpleNamespace(
                created_at=NOW.isoformat(), status="filled", request_payload={"symbol": s}
            )
            for s in symbols
        ]
        self.fail = fail
        self.counts: list[datetime] = []

    def count_since(self, since):
        if self.fail:
            raise RuntimeError("database down")
        self.counts.append(since)
        return len(self.rows)

    def list(self, limit=200):
        return list(self.rows)


def _auto(tmp_path, *, verdicts=VERDICTS, eto=None, executions=None, **settings):
    values = dict(require_strategy_oos_evidence=True, max_trades_per_day=15)
    values.update(settings)
    state = _State({VERDICTS_KEY: json.dumps({"verdicts": verdicts}), **(eto or {})})
    auto = PaperAutoTradingService(
        settings=make_settings(tmp_path, **values),
        proposal_service=None,
        execution_coordinator=SimpleNamespace(etoro_live_mirror=SimpleNamespace(state=state)),
        automation=None,
        reconciliation=None,
        safety_state=SimpleNamespace(
            is_blacklisted=lambda _s: False, strategy_active=lambda _s: True
        ),
        executions=executions if executions is not None else _Executions(),
        run_logs=None,
        notifier=None,
        alpaca_client=None,
        runtime_state=state,
    )
    return auto, state


def _candidate(symbol, strategy, *, ready=True, timeframe="1d"):
    return SimpleNamespace(
        symbol=symbol,
        strategy_name=strategy,
        timeframe=timeframe,
        execution_ready=ready,
        metadata={"alert_eligible": True},
        signal_role="entry_long",
        stop_loss=95.0,
    )


def _blockers(auto, candidate, paper_pass=None):
    return auto.candidate_proposal_blockers(candidate, paper_only=paper_pass)


# --- the auto-propose pass -------------------------------------------------------------------


def _workflow(tmp_path, auto):
    proposals = FakeProposalService()
    auto.approve_enqueue_execute = lambda *_a, **_k: None
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, auto_propose_enabled=True),
        market_screener=None,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="UBER", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        proposal_service=proposals,
        auto_trading_service=auto,
    )
    return workflow, proposals


def _response(*candidates):
    return ScreenerRunResponse(
        generated_at=NOW.isoformat(),
        universe_name="t",
        timeframes=["1d"],
        evaluated_symbols=1,
        evaluated_strategy_runs=1,
        candidates=list(candidates),
    )


def _scan_candidate(symbol, strategy, score):
    return _snapshot().model_copy(
        update={
            "symbol": symbol,
            "strategy_name": strategy,
            "score": score,
            "execution_ready": True,
            "metadata": {"alert_eligible": True, "timeframe": "1d"},
        }
    )


def test_an_etoro_eligible_candidate_takes_its_symbol_before_a_paper_only_one(tmp_path) -> None:
    auto, _state = _auto(tmp_path)
    workflow, proposals = _workflow(tmp_path, auto)
    ranked = [  # the paper-only candidate ranks first; UBER is in no correlation group
        _scan_candidate("UBER", "momentum_breakout", 95.0),
        _scan_candidate("UBER", "inside_bar_narrow_range_breakout", 80.0),
    ]
    assert (
        auto_propose_candidates(workflow, _response(*ranked), origin="swing_scan", notify=False)
        == 1
    )
    (request,) = proposals.created
    assert request.strategy_name == "inside_bar_narrow_range_breakout"
    assert not {k for k in request.metadata if k.startswith("paper_only")}


def test_a_paper_only_entry_is_tagged_and_an_etoro_eligible_one_is_not(tmp_path) -> None:
    auto, _state = _auto(tmp_path)
    workflow, proposals = _workflow(tmp_path, auto)
    response = _response(
        _scan_candidate("UBER", "momentum_breakout", 95.0),
        _scan_candidate("ADP", "inside_bar_narrow_range_breakout", 80.0),
        _scan_candidate("V", "trend_following", 99.0),  # fails both bars: refused
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 2
    tagged = {
        r.symbol: {k: v for k, v in r.metadata.items() if k.startswith("paper_only")}
        for r in proposals.created
    }
    assert tagged == {"ADP": {}, "UBER": TAGS}
    assert [r.symbol for r in proposals.created] == ["ADP", "UBER"]  # eToro-eligible first


def test_the_pass_counts_its_own_entries_before_the_next_paper_only_one(tmp_path) -> None:
    executions = _Executions(["X"] * 10)
    auto, _state = _auto(tmp_path, executions=executions)
    workflow, proposals = _workflow(tmp_path, auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: executions.rows.append(executions.rows[0])
    response = _response(  # every entry is placed: one more execution row today
        _scan_candidate("UBER", "momentum_breakout", 95.0),  # 12 after it
        _scan_candidate("ADP", "inside_bar_narrow_range_breakout", 90.0),  # first: 11 after it
        _scan_candidate("V", "momentum_breakout", 85.0),  # 13 after it: 2 of 15 left
        _scan_candidate("MA", "momentum_breakout", 80.0),  # would take one of those 2
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 3
    assert [r.symbol for r in proposals.created] == ["ADP", "UBER", "V"]
    assert len(executions.counts) == 3  # read once, then again after each of the 2 entries


def test_the_order_is_stable_and_only_paper_only_candidates_move(tmp_path) -> None:
    auto, _state = _auto(tmp_path)
    candidates = [
        _candidate("A", "momentum_breakout"),
        _candidate("B", "trend_following"),
        _candidate("C", "inside_bar_narrow_range_breakout"),
        _candidate("D", "momentum_breakout"),
        _candidate("BTCUSD", "momentum_breakout"),  # crypto: never paper-only
    ]
    ordered = PaperOnlyPass(auto).order(candidates)
    assert [c.symbol for c in ordered] == ["B", "C", "BTCUSD", "A", "D"]


def test_off_restores_the_old_gate_and_tags_nothing(tmp_path) -> None:
    auto, _state = _auto(tmp_path, paper_oos_bar_enabled=False)
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", "momentum_breakout")
    assert _blockers(auto, candidate, paper_pass) == ["strategy_lacks_oos_evidence"]
    assert paper_pass.tags(candidate) == {} and not paper_pass.is_paper_only(candidate)
    gate_off, _ = _auto(tmp_path / "gate", require_strategy_oos_evidence=False)
    assert not PaperOnlyPass(gate_off).is_paper_only(candidate)  # no gate: nothing is paper-only
    assert not PaperOnlyPass(None).is_paper_only(candidate)


def test_one_verdicts_read_serves_the_whole_pass(tmp_path) -> None:
    auto, state = _auto(tmp_path)
    paper_pass = PaperOnlyPass(auto)
    for symbol in ("UBER", "ADP", "V"):
        for strategy in VERDICTS:
            _blockers(auto, _candidate(symbol, strategy), paper_pass)
            paper_pass.tags(_candidate(symbol, strategy))
    assert state.reads.count(VERDICTS_KEY) == 1


# --- guard: the eToro account's symbols ------------------------------------------------------


def _etoro(open_symbols=("UBER", "100001"), cards=None, test=None):
    cards = (
        cards
        if cards is not None
        else {
            "t1": {"symbol": "ADP", "status": "pending_fill"},
            "t2": {"symbol": "V", "status": "closed"},
            "t3": {"symbol": "MA", "instrument_id": 100001, "status": "closed"},
        }
    )
    return {
        STATE_KEY: json.dumps({"open_symbols": list(open_symbols)}),
        SCORECARD_KEY: json.dumps(cards),
        TEST_STATE_KEY: json.dumps(test or {}),
    }


def test_a_paper_only_entry_never_opens_where_the_etoro_account_holds_or_has_in_flight(
    tmp_path,
) -> None:
    auto, state = _auto(tmp_path, eto=_etoro())
    paper_pass = PaperOnlyPass(auto)
    held = ["paper_only_etoro_live_symbol_held"]
    assert _blockers(auto, _candidate("UBER", "momentum_breakout"), paper_pass) == held  # open
    assert _blockers(auto, _candidate("ADP", "momentum_breakout"), paper_pass) == held  # in flight
    assert _blockers(auto, _candidate("MA", "momentum_breakout"), paper_pass) == held  # by its id
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == []  # closed
    # An eToro-eligible entry is not paper-only: this guard never touches it.
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout"), paper_pass) == []
    assert [state.reads.count(key) for key in (STATE_KEY, SCORECARD_KEY, TEST_STATE_KEY)] == [
        1,
        1,
        1,
    ]


@pytest.mark.parametrize(
    "broken",
    [{STATE_KEY: "not json"}, {SCORECARD_KEY: "[]"}, {TEST_STATE_KEY: "{"}],
)
def test_an_unreadable_etoro_record_refuses_the_pass_paper_only_entries(tmp_path, broken) -> None:
    auto, _state = _auto(tmp_path, eto={**_etoro(open_symbols=()), **broken})
    paper_pass = PaperOnlyPass(auto)
    for symbol in ("UBER", "V"):
        assert _blockers(auto, _candidate(symbol, "momentum_breakout"), paper_pass) == [
            "paper_only_etoro_live_state_unreadable"
        ]
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout"), paper_pass) == []


def test_without_a_mirror_the_records_are_read_from_the_runtime_state(tmp_path) -> None:
    auto, state = _auto(tmp_path, eto=_etoro())
    auto.execution = None
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_etoro_live_symbol_held"
    ]
    auto.runtime_state = None  # nothing to read: no gate at all, as before
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []


def test_a_candidate_the_loop_drops_anyway_reads_nothing(tmp_path) -> None:
    auto, state = _auto(tmp_path, eto=_etoro())
    assert _blockers(auto, _candidate("UBER", "momentum_breakout", ready=False)) == []
    assert STATE_KEY not in state.reads and auto.executions.counts == []


# --- guard: headroom for eToro-eligible entries ------------------------------------------------


def test_a_paper_only_entry_leaves_two_of_the_days_paper_trades(tmp_path) -> None:
    assert LEAVE_FOR_ETORO == 2
    candidate = _candidate("UBER", "momentum_breakout")
    twelve, _ = _auto(tmp_path / "12", executions=_Executions(["X"] * 12))
    assert _blockers(twelve, candidate) == []  # 13 after it: 2 of 15 left
    thirteen, _ = _auto(tmp_path / "13", executions=_Executions(["X"] * 13))
    assert _blockers(thirteen, candidate) == ["paper_only_headroom_trades_per_day"]
    assert _blockers(thirteen, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []
    assert thirteen.executions.counts == [
        datetime(2026, 10, 21, tzinfo=UTC)
    ]  # today, as paper counts


def test_a_paper_only_entry_leaves_one_of_its_groups_daily_entries(tmp_path) -> None:
    # Production's group cap is 2: a group takes a paper-only entry only while it has none today.
    assert LEAVE_IN_GROUP == 1
    auto, _ = _auto(tmp_path)
    assert _blockers(auto, _candidate("AAPL", "momentum_breakout")) == []
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []  # unclassified
    used, _ = _auto(tmp_path / "u", executions=_Executions(["MSFT"]))
    assert _blockers(used, _candidate("AAPL", "momentum_breakout")) == [
        "paper_only_headroom_correlation_bucket"
    ]
    assert _blockers(used, _candidate("AAPL", "inside_bar_narrow_range_breakout")) == []
    assert _blockers(used, _candidate("JPM", "momentum_breakout")) == []  # another group
    assert _blockers(used, _candidate("UBER", "momentum_breakout")) == []  # unclassified
    three, _ = _auto(
        tmp_path / "3", max_daily_entries_per_correlation_bucket=3, executions=_Executions(["MSFT"])
    )
    assert _blockers(three, _candidate("AAPL", "momentum_breakout")) == []
    two_used, _ = _auto(
        tmp_path / "2u",
        max_daily_entries_per_correlation_bucket=3,
        executions=_Executions(["MSFT", "NVDA"]),
    )
    assert _blockers(two_used, _candidate("AAPL", "momentum_breakout")) == [
        "paper_only_headroom_correlation_bucket"
    ]
    off, _ = _auto(
        tmp_path / "off",
        max_daily_entries_per_correlation_bucket=0,
        executions=_Executions(["MSFT", "NVDA"]),
    )
    assert _blockers(off, _candidate("AAPL", "momentum_breakout")) == []


def test_the_days_counts_are_read_once_and_again_after_each_proposal(tmp_path) -> None:
    executions = _Executions(["X"] * 12)
    auto, _ = _auto(tmp_path, executions=executions)
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, _candidate("UBER", "momentum_breakout"), paper_pass) == []
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == []
    assert len(executions.counts) == 1
    executions.rows.append(executions.rows[0])  # the pass's entry was placed
    paper_pass.created()
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == [
        "paper_only_headroom_trades_per_day"
    ]
    assert len(executions.counts) == 2


def test_unreadable_counts_refuse(tmp_path) -> None:
    auto, _ = _auto(tmp_path, executions=_Executions(fail=True))
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_paper_day_unreadable"
    ]
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []


# --- the swing scan --------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class _Screener:
    """scan_universe with a cancel hook; each call takes ``took`` seconds of the fake clock."""

    def __init__(
        self,
        clock,
        *,
        evaluated=None,
        took=(20.0, 20.0),
        errors=(),
        fail_second=False,
        second_evaluated=None,
        events=None,
    ):
        self.clock, self.evaluated, self.took = clock, evaluated, list(took)
        self.second_evaluated = second_evaluated
        self.errors, self.fail_second = list(errors), fail_second
        self.events = events if events is not None else []
        self.calls: list[dict] = []

    def scan_universe(
        self,
        *,
        symbols,
        timeframes,
        limit=None,
        validated_only=False,
        notify=False,
        force_refresh=False,
        scan_task="manual_scan",
        strategy_spec_keys=None,
        cancel_event=None,
    ):
        self.calls.append(
            {
                "symbols": list(symbols),
                "timeframes": list(timeframes),
                "keys": list(strategy_spec_keys or []),
                "limit": limit,
                "force_refresh": force_refresh,
                "cancel_event": cancel_event,
            }
        )
        self.events.append(("scan", list(strategy_spec_keys or [])))
        self.clock.now += self.took[len(self.calls) - 1]
        if len(self.calls) == 2 and self.fail_second:
            raise RuntimeError("provider down")
        first = len(self.calls) == 1
        count = self.evaluated if first else self.second_evaluated
        count = len(symbols) if count is None else count
        candidate = _scan_candidate(
            symbols[0], (strategy_spec_keys or ["x:1d"])[0].split(":")[0], 50.0
        )
        return ScreenerRunResponse(
            generated_at=NOW.isoformat(),
            universe_name="t",
            timeframes=list(timeframes),
            evaluated_symbols=count,
            evaluated_strategy_runs=count,
            candidates=[candidate],
            errors=self.errors if first else [],
        )

    def strategy_spec_keys_for_timeframes(self, timeframes):
        keys = [
            "momentum_breakout:1h",
            "inside_bar_narrow_range_breakout:1d",
            "momentum_breakout:1d",
            "trend_following:1d",
        ]
        return [key for key in keys if key.rsplit(":", 1)[-1] in set(timeframes)]


SYMBOLS = ["NVDA", "AAPL", "UBER", "ADP"]
BATCH = {"paper_only_spec_keys": ["momentum_breakout:1d"], "paper_only_timeframes": ["1d"]}


def _scan(
    tmp_path,
    monkeypatch,
    screener,
    *,
    batch=BATCH,
    cancel_event=None,
    first=None,
    state=None,
    **settings,
):
    """The run's own scan, then ``paper_only_scan`` (its auto-propose pass is recorded)."""

    monkeypatch.setattr(swing_focus.time, "monotonic", screener.clock)
    passes, tracked = [], []

    def propose(_service, extra, *, origin, notify):
        passes.append(extra)
        return len(extra.candidates)

    monkeypatch.setattr(operations, "auto_propose_candidates", propose)
    service = SimpleNamespace(
        market_screener=screener,
        runtime_state=state if state is not None else FakeState(),
        settings=make_settings(tmp_path, **settings),
        _track_candidates=lambda extra, *, origin: tracked.append(extra),
    )
    kwargs = {
        "symbols": list(SYMBOLS),
        "timeframes": ["1d"],
        "limit": 10,
        "force_refresh": True,
        "strategy_spec_keys": ["inside_bar_narrow_range_breakout:1d"],
    }
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    started = screener.clock()
    response = screener.scan_universe(**kwargs) if first is None else first
    created = paper_only_scan(
        service, kwargs, response, batch, started, origin="swing_scan", notify=False
    )
    return SimpleNamespace(
        response=response, service=service, created=created, passes=passes, tracked=tracked
    )


def test_paper_only_specs_scan_after_the_runs_own_pass_with_its_time_left(
    tmp_path, monkeypatch
) -> None:
    screener = _Screener(_Clock())
    batch = {
        "paper_only_spec_keys": ["momentum_breakout:1d"],
        "paper_only_timeframes": ["1h", "1d"],
    }
    run = _scan(tmp_path, monkeypatch, screener, batch=batch, screener_batch_deadline_seconds=120.0)
    first, second = screener.calls
    assert first["keys"] == ["inside_bar_narrow_range_breakout:1d"] and first["limit"] == 10
    assert second["keys"] == ["momentum_breakout:1d"] and second["force_refresh"] is False
    assert second["limit"] == PAPER_ONLY_TOP_K == 3  # not the run's top-k
    assert first["timeframes"] == ["1d"] and second["timeframes"] == ["1h", "1d"]
    assert second["symbols"] == sorted(SYMBOLS)
    assert second["cancel_event"].stop_at == 1_000.0 + 120.0  # what is left of the same deadline
    # The run's own candidates stay as they were; the paper-only ones get their own pass.
    assert [c.strategy_name for c in run.response.candidates] == [
        "inside_bar_narrow_range_breakout"
    ]
    (extra,) = run.passes
    assert run.tracked == [extra] and [c.strategy_name for c in extra.candidates] == [
        "momentum_breakout"
    ]
    assert run.created == 1
    assert run.response.coverage["paper_only_scan"] == {
        "specs": 1,  # a count: the keys are in the verdicts
        "symbols_evaluated": 4,
        "candidates": 1,
        "errors": [],
        "proposals_created": 1,
    }
    assert run.service.runtime_state.get(PAPER_ONLY_CURSOR_KEY) == "0"  # all 4 of 4: wrapped


def test_no_paper_only_scan_when_the_etoro_passing_specs_did_not_finish(
    tmp_path, monkeypatch
) -> None:
    for kwargs, reason in (
        ({"evaluated": 3}, "etoro_passing_specs_unfinished"),
        ({"errors": ["scan_deadline_exceeded"]}, "etoro_passing_specs_unfinished"),
        ({"errors": ["scan_cancelled"]}, "etoro_passing_specs_unfinished"),
        ({"took": (76.0, 1.0)}, "run_deadline_too_close"),  # 44 s left of 120: under 45 s
    ):
        screener = _Screener(_Clock(), **kwargs)
        run = _scan(tmp_path, monkeypatch, screener, screener_batch_deadline_seconds=120.0)
        assert len(screener.calls) == 1 and run.passes == [] and run.created == 0, kwargs
        assert run.response.coverage["paper_only_scan"] == {"specs": 1, "skipped": reason}
    screener = _Screener(_Clock(), took=(75.0, 1.0))  # exactly 45 s left: it runs
    _scan(tmp_path, monkeypatch, screener, screener_batch_deadline_seconds=120.0)
    assert len(screener.calls) == 2


def test_the_cadence_ticks_scan_stop_less_one_proposal_bounds_the_paper_only_scan(
    tmp_path, monkeypatch
) -> None:
    from app.workflow.cadence_budget import ScanDeadline

    assert PAPER_ONLY_PASS_SECONDS == 30.0
    screener = _Screener(_Clock())
    _scan(
        tmp_path,
        monkeypatch,
        screener,
        cancel_event=ScanDeadline(1_120.0),
        screener_batch_deadline_seconds=200.0,
    )
    assert screener.calls[1]["cancel_event"].stop_at == 1_090.0  # the tick's stop less 30 s
    early = _Screener(_Clock())
    run = _scan(
        tmp_path,
        monkeypatch,
        early,
        cancel_event=ScanDeadline(1_090.0),
        screener_batch_deadline_seconds=200.0,
    )
    assert len(early.calls) == 1  # 40 s left before the stop less 30 s
    assert run.response.coverage["paper_only_scan"]["skipped"] == "run_deadline_too_close"


def test_without_a_batch_deadline_the_cadence_stop_bounds_it_and_without_both_it_skips(
    tmp_path, monkeypatch
) -> None:
    from app.workflow.cadence_budget import ScanDeadline

    screener = _Screener(_Clock())
    _scan(
        tmp_path,
        monkeypatch,
        screener,
        cancel_event=ScanDeadline(1_120.0),
        screener_batch_deadline_seconds=0.0,
    )
    assert screener.calls[1]["cancel_event"].stop_at == 1_090.0
    unbounded = _Screener(_Clock())
    run = _scan(tmp_path, monkeypatch, unbounded, screener_batch_deadline_seconds=0.0)
    assert len(unbounded.calls) == 1 and run.passes == []
    assert run.response.coverage["paper_only_scan"] == {"specs": 1, "skipped": "no_run_deadline"}


def test_a_failing_paper_only_scan_keeps_the_runs_own_candidates(tmp_path, monkeypatch) -> None:
    screener = _Screener(_Clock(), fail_second=True)
    run = _scan(tmp_path, monkeypatch, screener, screener_batch_deadline_seconds=120.0)
    assert [c.strategy_name for c in run.response.candidates] == [
        "inside_bar_narrow_range_breakout"
    ]
    assert run.passes == [] and run.created == 0
    assert run.response.coverage["paper_only_scan"] == {"specs": 1, "error": "provider down"}


def test_a_first_response_without_coverage_never_fails_the_run(tmp_path, monkeypatch) -> None:
    first = SimpleNamespace(evaluated_symbols=4, errors=[], candidates=[])  # no coverage field
    screener = _Screener(_Clock())
    run = _scan(tmp_path, monkeypatch, screener, first=first, screener_batch_deadline_seconds=120.0)
    assert run.created == 1 and not hasattr(first, "coverage")


def test_the_paper_only_cursor_sweeps_the_symbols(tmp_path, monkeypatch) -> None:
    state = FakeState()
    starts = []
    for _ in range(2):
        screener = _Screener(_Clock(), second_evaluated=3)  # the deadline stops it after 3
        _scan(tmp_path, monkeypatch, screener, state=state, screener_batch_deadline_seconds=120.0)
        starts.append(screener.calls[1]["symbols"])
    assert starts == [["AAPL", "ADP", "NVDA", "UBER"], ["UBER", "AAPL", "ADP", "NVDA"]]
    assert state.get(PAPER_ONLY_CURSOR_KEY) == "2"  # (0 + 3 + 3) % 4


def _swing_workflow(tmp_path, screener, state, **settings):
    return SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            require_strategy_oos_evidence=True,
            screener_spec_coverage_mode="scheduled_all",
            market_universe_symbols=SYMBOLS,
            allowed_instruments=SYMBOLS,
            **settings,
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )


def test_the_focused_swing_run_lists_the_paper_only_specs_after_the_etoro_passing_ones(
    tmp_path, monkeypatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(swing_focus.time, "monotonic", clock)

    def run(**settings):
        screener = _Screener(clock, took=(1.0, 1.0))
        state = FakeState()
        state.set(VERDICTS_KEY, json.dumps({"verdicts": VERDICTS}))
        _swing_workflow(tmp_path, screener, state, **settings).run_swing_scan(notify=False)
        return [call["keys"] for call in screener.calls]

    assert run() == [["inside_bar_narrow_range_breakout:1d"], ["momentum_breakout:1d"]]
    assert run(paper_oos_bar_enabled=False) == [["inside_bar_narrow_range_breakout:1d"]]


def test_the_runs_own_entries_are_proposed_and_executed_before_the_paper_only_scan_starts(
    tmp_path, monkeypatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(swing_focus.time, "monotonic", clock)
    events: list[tuple] = []
    screener = _Screener(clock, took=(1.0, 1.0), events=events)
    auto, state = _auto(tmp_path / "auto")
    auto.approve_enqueue_execute = lambda proposal, candidate, **_k: events.append(
        ("execute", candidate.symbol, candidate.strategy_name)
    )
    proposals = FakeProposalService()
    create = proposals.create_proposal

    def create_proposal(request):
        events.append(("propose", request.symbol, request.strategy_name))
        return create(request)

    proposals.create_proposal = create_proposal
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            require_strategy_oos_evidence=True,
            auto_propose_enabled=True,
            screener_spec_coverage_mode="scheduled_all",
            market_universe_symbols=SYMBOLS,
            allowed_instruments=SYMBOLS,
        ),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
        proposal_service=proposals,
        auto_trading_service=auto,
    )
    result = workflow.run_swing_scan(notify=False)
    assert result.status == "ok"
    assert events == [
        ("scan", ["inside_bar_narrow_range_breakout:1d"]),
        ("propose", "NVDA", "inside_bar_narrow_range_breakout"),
        ("execute", "NVDA", "inside_bar_narrow_range_breakout"),
        ("scan", ["momentum_breakout:1d"]),  # only now
        ("propose", "AAPL", "momentum_breakout"),
        ("execute", "AAPL", "momentum_breakout"),
    ]
    # Bounded by the run's deadline counted from its first scan's start (120 s by default).
    assert screener.calls[1]["cancel_event"].stop_at == 1_000.0 + 120.0
    tags = [
        {k: v for k, v in r.metadata.items() if k.startswith("paper_only")}
        for r in proposals.created
    ]
    assert tags == [{}, TAGS]
