"""Paper-only entries in the auto-propose pass and the swing scan (operator option B, 2026-10-21).

* an eToro-eligible candidate is considered before a paper-only one: never loses its symbol;
* at most one paper-only proposal a pass, none once a cadence tick is near its scan stop;
* a paper-only entry never opens in a symbol the eToro account holds or has in flight (read
  once a pass in one round trip, as the mirror's room check reads it; unreadable refuses);
* it leaves 2 of the day's paper trades and 1 of its group's daily entries;
* it leaves room for 2 more positions in paper's open-position and heat capacity, never opens
  where paper holds or has an unfilled entry, and at most MAX_OPEN_PAPER_ONLY (3) are open at once;
* it is tagged; nothing else is, and with the switch off nothing changes and nothing is read;
* its safety blocks are logged once a day; its notices say it is not copied;
* the swing scan scans paper-only specs only after a run's own entries were proposed, in a
  second scan inside the same run's deadline.

No wall clock: the pass's clock and the scan's monotonic clock are patched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
from app.models.approval import ApprovalStatus
from app.models.screener import ScreenerRunResponse
from app.performance.strategy_evidence import VERDICTS_KEY
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import (
    BOOK_ROWS,
    LEAVE_FOR_ETORO,
    LEAVE_IN_GROUP,
    MAX_OPEN_PAPER_ONLY,
    MAX_PER_PASS,
    PROPOSAL_SECONDS,
    PaperOnlyPass,
)
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
    import app.risk.context as risk_context

    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(risk_context, "utc_now", lambda: NOW)
    monkeypatch.setattr(paper_only_module, "_LOGGED", {})


class _State(dict):
    """A runtime_state that counts its reads."""

    def __init__(self, values=None):
        super().__init__(values or {})
        self.reads: list[str] = []
        self.many: list[list[str]] = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def get_many(self, keys):
        self.many.append(list(keys))
        return {key: super(_State, self).get(key) for key in keys}

    def set(self, key, value):
        self[key] = value


def _row(symbol, *, status="filled", tagged=False, created_at=NOW):
    metadata = dict(TAGS) if tagged else {}
    return SimpleNamespace(
        created_at=created_at.isoformat(),
        status=status,
        request_payload={"symbol": symbol, "metadata": metadata},
        response_payload={},
        realized_pnl_usd=0.0,
    )


class _Executions:
    """Paper execution rows (newest first), as paper's risk context and day counts read them."""

    def __init__(self, symbols=(), fail=False, rows=()):
        self.rows = [*rows, *(_row(s) for s in symbols)]
        self.fail = fail
        self.counts: list[datetime] = []
        self.loss_reads = 0  # one a build_risk_context
        self.limits: list[int] = []  # every list() read's limit

    def count_since(self, since):
        if self.fail:
            raise RuntimeError("database down")
        self.counts.append(since)
        return len(self.rows)

    @property
    def paper_reads(self):
        """(risk contexts built, newest-row lists of BOOK_ROWS read)."""

        return self.loss_reads, self.limits.count(BOOK_ROWS)

    def daily_loss_stats(self):
        self.loss_reads += 1
        return 0.0, 0

    def period_realized_pnl(self, *, days):
        return 0.0

    def list(self, limit=200):
        if self.fail:
            raise RuntimeError("database down")
        self.limits.append(limit)
        return list(self.rows)[:limit]


class _Broker:
    """Alpaca paper's portfolio, as build_risk_context reads it."""

    def __init__(self, symbols=(), fail=False):
        self.symbols, self.fail, self.calls = list(symbols), fail, 0

    def get_portfolio(self):
        from app.models.execution import AccountSummary, PortfolioPosition, PortfolioSummary

        self.calls += 1
        if self.fail:
            raise RuntimeError("broker down")
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=100_000.0, equity=100_000.0),
            positions=[PortfolioPosition(symbol=s, market_value=5_000.0) for s in self.symbols],
        )


BOOK = dict(  # production's paper room: 12 positions, 6% heat at 0.5% a position
    paper_broker="alpaca",
    max_open_positions=12,
    portfolio_max_heat_pct=6.0,
    institutional_portfolio_controls_enabled=True,
    portfolio_future_max_risk_per_trade_pct=0.5,
)


EXECUTED = SimpleNamespace(status="executed", validation_reason="ready")  # a placed order


def _auto(
    tmp_path,
    *,
    verdicts=VERDICTS,
    eto=None,
    executions=None,
    broker=None,
    auto_blockers=(),
    **settings,
):
    values = dict(require_strategy_oos_evidence=True, max_trades_per_day=15)
    values.update(settings)
    state = _State({VERDICTS_KEY: json.dumps({"verdicts": verdicts}), **(eto or {})})
    auto = PaperAutoTradingService(
        settings=make_settings(tmp_path, **values),
        proposal_service=SimpleNamespace(broker=broker if broker is not None else _Broker()),
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
    # The unattended path's own checks (rule 6): none by default, so it would execute at once.
    auto.candidate_blockers = lambda _candidate: list(auto_blockers)
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
    auto.approve_enqueue_execute = lambda *_a, **_k: EXECUTED
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


def test_the_pass_counts_its_own_entries_before_its_paper_only_one(tmp_path) -> None:
    executions = _Executions(["X"] * 12)
    auto, _state = _auto(tmp_path, executions=executions)
    workflow, proposals = _workflow(tmp_path, auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: executions.rows.append(_row("X")) or EXECUTED
    response = _response(  # every entry is placed: one more execution row today
        _scan_candidate("UBER", "momentum_breakout", 95.0),  # 13 + 1 + 2 > 15: refused
        _scan_candidate("ADP", "inside_bar_narrow_range_breakout", 90.0),  # first: 13 after it
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 1
    assert [r.symbol for r in proposals.created] == ["ADP"]
    eleven = _Executions(["X"] * 11)
    auto, _state = _auto(tmp_path / "11", executions=eleven)
    workflow, proposals = _workflow(tmp_path / "11", auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: eleven.rows.append(_row("X")) or EXECUTED
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 2
    assert [r.symbol for r in proposals.created] == ["ADP", "UBER"]


def test_at_most_one_paper_only_proposal_a_pass(tmp_path) -> None:
    assert MAX_PER_PASS == 1
    auto, _state = _auto(tmp_path)
    workflow, proposals = _workflow(tmp_path, auto)
    response = _response(
        _scan_candidate("UBER", "momentum_breakout", 95.0),
        _scan_candidate("V", "momentum_breakout", 90.0),
        _scan_candidate("ADP", "inside_bar_narrow_range_breakout", 80.0),
        _scan_candidate("MA", "inside_bar_narrow_range_breakout", 70.0),
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 3
    assert [r.symbol for r in proposals.created] == ["ADP", "MA", "UBER"]  # V: the pass's limit
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert blocked == [
        {"origin": "swing_scan", "symbol": "V", "blockers": ["paper_only_pass_limit"]}
    ]
    paper_pass = PaperOnlyPass(auto)  # an eToro-eligible proposal never uses the allowance
    paper_pass.created(_candidate("ADP", "inside_bar_narrow_range_breakout"))
    assert _blockers(auto, _candidate("UBER", "momentum_breakout"), paper_pass) == []
    paper_pass.created(_candidate("UBER", "momentum_breakout"))
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == [
        "paper_only_pass_limit"
    ]


def test_no_paper_only_proposal_once_the_cadence_tick_nears_its_scan_stop(
    tmp_path, monkeypatch
) -> None:
    from app.workflow.cadence_budget import CadenceBudget, tick_budget

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(paper_only_module.time, "monotonic", lambda: clock.now)
    auto, _state = _auto(tmp_path)
    budget = CadenceBudget(started_at=1_000.0, job_timeout=240.0)  # scan stop at 1_180
    assert PROPOSAL_SECONDS == 35.0  # the pass's reads and one proposal, measured 20-35 s
    with tick_budget(budget):
        clock.now = 1_180.0 - PROPOSAL_SECONDS - 0.1
        assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []
        clock.now = 1_180.0 - PROPOSAL_SECONDS  # the reserve is the run's own
        assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
            "paper_only_cadence_budget_spent"
        ]
        assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []
    legacy = CadenceBudget(started_at=1_000.0, job_timeout=240.0, legacy_soft_budget=110.0)
    with tick_budget(legacy):  # no scan stop in the legacy mode: nothing to keep
        assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []  # no tick: manual run


def test_a_paper_only_proposal_and_its_execution_say_they_are_not_copied(tmp_path) -> None:
    auto, _state = _auto(tmp_path)
    workflow, proposals = _workflow(tmp_path, auto)
    response = _response(
        _scan_candidate("UBER", "momentum_breakout", 95.0),
        _scan_candidate("ADP", "inside_bar_narrow_range_breakout", 80.0),
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=True) == 2
    notices = [m for m in workflow.notifier.messages if m.startswith("Auto proposal created")]
    assert [("Paper-only (not copied to eToro)" in m) for m in notices] == [False, True]
    assert notices[1].splitlines()[:3] == [
        "Auto proposal created",
        "Paper-only (not copied to eToro)",
        "Automatic only: /approve is refused for it",
    ]
    for metadata, expected in ((dict(TAGS), True), ({}, False)):
        sent = []
        auto.notifier = SimpleNamespace(send_text=sent.append)
        auto.logs = FakeLogs()
        auto.candidate_blockers = lambda _c: []
        order = SimpleNamespace(symbol="UBER", metadata=metadata)
        auto.proposals = SimpleNamespace(
            approve_proposal=lambda *_a, order=order: SimpleNamespace(id="p1", order=order)
        )
        processed = SimpleNamespace(id="q1", symbol="UBER", status="executed", validation_reason="")
        auto.execution = SimpleNamespace(
            enqueue_approved_proposal=lambda _id: SimpleNamespace(id="q1"),
            process_queue_item=lambda _id, processed=processed: processed,
        )
        PaperAutoTradingService.approve_enqueue_execute(  # the real one: _workflow stubs it
            auto, SimpleNamespace(id="p1", order=order), None
        )
        assert ("Paper-only (not copied to eToro)" in sent[-1]) is expected


def test_a_paper_only_safety_block_is_logged_once_a_day(tmp_path, monkeypatch) -> None:
    executions = _Executions(["X"] * 13)  # every paper-only entry is refused for headroom
    auto, _state = _auto(tmp_path, executions=executions)
    workflow, _proposals = _workflow(tmp_path, auto)
    auto.safety = SimpleNamespace(
        is_blacklisted=lambda s: s == "MA", strategy_active=lambda _s: True
    )
    response = _response(
        _scan_candidate("UBER", "momentum_breakout", 95.0),
        _scan_candidate("MA", "inside_bar_narrow_range_breakout", 80.0),  # blacklisted
    )

    def blocked():
        return [
            p["symbol"] for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"
        ]

    for _ in range(3):
        auto_propose_candidates(workflow, response, origin="swing_scan", notify=False)
    assert blocked() == ["MA", "UBER", "MA", "MA"]  # the eToro-eligible one each time
    funnels = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"]
    assert [f["safety_blocked"] for f in funnels] == [2, 2, 2]  # the funnel still counts it
    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW + timedelta(days=1))
    auto_propose_candidates(workflow, response, origin="swing_scan", notify=False)
    assert blocked()[-2:] == ["MA", "UBER"]  # a new day


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


@pytest.mark.parametrize(
    "settings", [{"paper_oos_bar_enabled": False}, {"require_strategy_oos_evidence": False}]
)
def test_off_restores_the_old_gate_reads_nothing_and_tags_nothing(tmp_path, settings) -> None:
    auto, state = _auto(tmp_path, eto=_etoro(), **settings)
    paper_pass = PaperOnlyPass(auto)
    assert paper_pass.state is state  # the gate reads the runtime state itself, as before
    candidates = [_candidate(s, k) for s in ("UBER", "V") for k in VERDICTS]
    assert [c.symbol for c in paper_pass.order(candidates)] == [c.symbol for c in candidates]
    for candidate in candidates:
        assert paper_pass.tags(candidate) == {} and paper_pass.notice(candidate) == []
        assert paper_pass.log_blocked(candidate, ["x"]) and paper_pass.log_blocked(candidate, ["x"])
        assert paper_pass.blockers(candidate) == []
        paper_pass.created(candidate)
    assert state.reads == [] and state.many == [] and auto.executions.counts == []
    # The gate's own reads: one a candidate, as at 822e46e.
    candidate = _candidate("UBER", "momentum_breakout")
    blockers = [_blockers(auto, candidate, paper_pass) for _ in range(3)]
    if "paper_oos_bar_enabled" in settings:  # the old gate: refused, one read a candidate
        assert blockers == [["strategy_lacks_oos_evidence"]] * 3
        assert state.reads == [VERDICTS_KEY] * 3
    else:  # no gate at all: admitted, nothing read
        assert blockers == [[]] * 3 and state.reads == []
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
    assert state.many == [[STATE_KEY, SCORECARD_KEY, TEST_STATE_KEY]]  # one round trip a pass
    assert not {STATE_KEY, SCORECARD_KEY, TEST_STATE_KEY} & set(state.reads)


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
    assert state.many == [] and auto.executions.counts == []


def test_the_execution_coordinator_checks_the_etoro_symbols_again_for_a_tagged_order() -> None:
    from app.broker.etoro_live_room import paper_only_held_reasons

    state = _State(_etoro())
    mirror = SimpleNamespace(state=state)

    def order(symbol, tagged=True):
        return SimpleNamespace(symbol=symbol, metadata=dict(TAGS) if tagged else {})

    assert paper_only_held_reasons(mirror, order("UBER")) == ["paper_only_etoro_live_symbol_held"]
    assert paper_only_held_reasons(mirror, order("adp")) == ["paper_only_etoro_live_symbol_held"]
    assert paper_only_held_reasons(mirror, order("V")) == []
    assert paper_only_held_reasons(mirror, order("UBER", tagged=False)) == []  # untagged: never
    assert paper_only_held_reasons(None, order("UBER")) == []  # no mirror: nothing is copied
    state[SCORECARD_KEY] = "[]"
    assert paper_only_held_reasons(mirror, order("V")) == ["paper_only_etoro_live_state_unreadable"]


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


def test_one_read_of_paper_a_pass_serves_the_day_and_the_book_and_again_after_a_proposal(
    tmp_path,
) -> None:
    executions = _Executions(["X"] * 12)
    broker = _Broker()
    auto, _ = _auto(tmp_path, executions=executions, broker=broker, **BOOK)
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, _candidate("UBER", "momentum_breakout"), paper_pass) == []
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == []
    # One risk context and one list of the newest rows for both, and one broker read.
    assert (executions.paper_reads, broker.calls) == ((1, 1), 1)
    executions.rows.append(_row("X"))  # the pass's entry was placed
    paper_pass.created(_candidate("ADP", "inside_bar_narrow_range_breakout"))
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == [
        "paper_only_headroom_trades_per_day"
    ]
    assert executions.paper_reads == (2, 2)
    paper_pass = PaperOnlyPass(auto)
    executions.rows.pop()
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == []
    broker.symbols = ["V"]  # its entry filled
    paper_pass.created(_candidate("ADP", "inside_bar_narrow_range_breakout"))
    assert _blockers(auto, _candidate("V", "momentum_breakout"), paper_pass) == [
        "paper_only_paper_symbol_held"
    ]
    assert broker.calls == 4  # once a pass, and again after each of its proposals


def test_unreadable_counts_refuse(tmp_path) -> None:
    auto, _ = _auto(tmp_path, executions=_Executions(fail=True))
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_paper_book_unreadable"
    ]
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []


# --- guard: paper's open positions and heat ----------------------------------------------------

HELD = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "CVX", "KO", "PEP", "WMT", "HD", "LLY", "UNH"]


def _held(count, rows=()):
    """Paper holds the first ``count`` of HELD, each an eToro-eligible entry from last week."""

    old = [_row(s, created_at=NOW - timedelta(days=7)) for s in HELD[:count]]
    return {"broker": _Broker(HELD[:count]), "executions": _Executions(rows=[*rows, *old])}


def test_a_paper_only_entry_leaves_room_for_two_more_positions(tmp_path) -> None:
    candidate = _candidate("UBER", "momentum_breakout")
    nine, _ = _auto(tmp_path / "9", **_held(9), **BOOK)
    assert _blockers(nine, candidate) == []  # 9 + 1 + 2 = 12
    ten, _ = _auto(tmp_path / "10", **_held(10), **BOOK)
    assert _blockers(ten, candidate) == ["paper_only_headroom_open_positions"]
    assert _blockers(ten, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []
    # An entry placed today but not filled yet counts too (paper's own check does not).
    unfilled = _held(9, rows=[_row("ADP", status="submitted")])
    pending, _ = _auto(tmp_path / "p", **unfilled, **BOOK)
    assert _blockers(pending, candidate) == ["paper_only_headroom_open_positions"]
    yesterday = _held(9, rows=[_row("ADP", status="submitted", created_at=NOW - timedelta(days=1))])
    stale, _ = _auto(tmp_path / "y", **yesterday, **BOOK)
    assert _blockers(stale, candidate) == []  # not today's: not an unfilled entry


def test_a_paper_only_entry_leaves_room_for_two_more_positions_in_portfolio_heat(tmp_path) -> None:
    candidate = _candidate("UBER", "momentum_breakout")
    roomy = {**BOOK, "max_open_positions": 20}  # heat binds: 6% / 0.5% = 12 positions
    nine, _ = _auto(tmp_path / "9", **_held(9), **roomy)
    assert _blockers(nine, candidate) == []
    ten, _ = _auto(tmp_path / "10", **_held(10), **roomy)
    assert _blockers(ten, candidate) == ["paper_only_headroom_portfolio_heat"]
    whole = {**roomy, "institutional_portfolio_controls_enabled": False}  # 1% a trade: 6 positions
    three, _ = _auto(tmp_path / "3", **_held(3), **whole)
    assert _blockers(three, candidate) == []
    four, _ = _auto(tmp_path / "4", **_held(4), **whole)
    assert _blockers(four, candidate) == ["paper_only_headroom_portfolio_heat"]
    no_cap, _ = _auto(tmp_path / "0", **_held(10), **{**roomy, "portfolio_max_heat_pct": 0.0})
    assert _blockers(no_cap, candidate) == []  # no heat cap: paper checks none


def test_at_most_max_open_paper_only_positions_and_never_where_paper_holds(tmp_path) -> None:
    assert MAX_OPEN_PAPER_ONLY == 3  # the one place to change it; the rest reads the name
    candidate = _candidate("UBER", "momentum_breakout")

    def book(rows, held):
        auto, _ = _auto(
            tmp_path / str(len(rows)) / "-".join(held),
            broker=_Broker(held),
            executions=_Executions(rows=rows),
            **BOOK,
        )
        return _blockers(auto, candidate)

    old = NOW - timedelta(days=5)
    names = HELD[: MAX_OPEN_PAPER_ONLY - 1]  # one short of the cap
    short = [_row(s, tagged=True, created_at=old) for s in names]
    assert book(short, names) == []
    full = [_row("ADP", status="submitted", tagged=True), *short]  # placed, not filled yet
    assert book(full, names) == ["paper_only_positions_cap"]
    closed = [_row("KO", tagged=True, created_at=old), *short]  # KO no longer held: closed
    assert book(closed, names) == []
    newer = [_row(names[-1], created_at=NOW), *short]  # its newest entry is eToro-eligible
    assert book([*newer, _row("KO", tagged=True)], [*names, "KO"]) == []
    unknown = book(short, [*names, "XOM"])  # XOM's entry is not in the rows
    assert unknown == ["paper_only_positions_cap"]
    assert book([], ["UBER"]) == ["paper_only_paper_symbol_held"]
    assert book([_row("UBER", status="accepted")], []) == ["paper_only_paper_symbol_held"]


def test_an_unreadable_book_refuses_paper_only_entries_only(tmp_path) -> None:
    auto, _ = _auto(tmp_path, broker=_Broker(fail=True), **BOOK)
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_paper_book_unreadable"
    ]
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []


def test_paper_s_per_symbol_limit_keeps_untagged_entries_out_of_a_held_paper_only_symbol(
    tmp_path,
) -> None:
    from app.models.trade import TradeOrder
    from app.risk.context import build_risk_context
    from app.risk.guardrails import RiskManager

    settings = make_settings(tmp_path, **BOOK)
    order = TradeOrder(
        symbol="NVDA",
        amount_usd=1_000,
        proposed_price=100.0,
        stop_loss=99.5,
        take_profit=110.0,
        strategy_name="inside_bar_narrow_range_breakout",
    )
    limit = "Per-symbol position limit reached"
    filled = build_risk_context(
        settings, _Broker(["NVDA"]), _Executions(rows=[_row("NVDA", tagged=True)])
    )
    assert limit in RiskManager(settings).validate_order(order, filled).reasons
    # Placed but not filled: the broker shows no position, so paper's check cannot see it
    # (the seconds-long window the module docstring states).
    unfilled = build_risk_context(
        settings, _Broker(), _Executions(rows=[_row("NVDA", status="submitted", tagged=True)])
    )
    assert limit not in RiskManager(settings).validate_order(order, unfilled).reasons


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
    fail_after_create=False,
    **settings,
):
    """The run's own scan, then ``paper_only_scan`` (its auto-propose pass is recorded)."""

    monkeypatch.setattr(swing_focus.time, "monotonic", screener.clock)
    passes, tracked, origins = [], [], []

    def propose(_service, extra, *, origin, notify, paper_only):
        passes.append(extra)
        origins.append(origin)
        assert paper_only.only  # the second pass: paper-only candidates alone
        for candidate in extra.candidates:
            paper_only.created(candidate)
            if fail_after_create:
                raise RuntimeError("broker down mid-pass")
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
        response=response,
        service=service,
        created=created,
        passes=passes,
        tracked=tracked,
        origins=origins,
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

    assert PAPER_ONLY_PASS_SECONDS == 10.0 + PROPOSAL_SECONDS  # overshoot + one proposal
    tick_stop = 1_020.0 + 60.0 + PAPER_ONLY_PASS_SECONDS  # the first scan ends at 1_020
    screener = _Screener(_Clock())
    _scan(
        tmp_path,
        monkeypatch,
        screener,
        cancel_event=ScanDeadline(tick_stop),
        screener_batch_deadline_seconds=500.0,
    )
    assert screener.calls[1]["cancel_event"].stop_at == 1_080.0  # the tick's stop less it
    early = _Screener(_Clock())
    run = _scan(
        tmp_path,
        monkeypatch,
        early,
        cancel_event=ScanDeadline(1_020.0 + 44.9 + PAPER_ONLY_PASS_SECONDS),
        screener_batch_deadline_seconds=500.0,
    )
    assert len(early.calls) == 1  # 44.9 s left before the stop less it: under 45 s
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
        cancel_event=ScanDeadline(1_080.0 + PAPER_ONLY_PASS_SECONDS),
        screener_batch_deadline_seconds=0.0,
    )
    assert screener.calls[1]["cancel_event"].stop_at == 1_080.0
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


AUTOMATIC = dict(paper_auto_approve_proposals=True, paper_auto_operation_mode="unattended")


def _swing_workflow(tmp_path, screener, state, *, auto=None, **settings):
    values = dict(
        require_strategy_oos_evidence=True,
        screener_spec_coverage_mode="scheduled_all",
        market_universe_symbols=SYMBOLS,
        allowed_instruments=SYMBOLS,
    )
    values.update(settings)
    workflow_settings = make_settings(tmp_path, **values)
    if auto is not None:
        auto.settings = workflow_settings  # one settings object, as in production
    return SignalWorkflowService(
        settings=workflow_settings,
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
        auto_trading_service=auto,
    )


def test_the_focused_swing_run_lists_the_paper_only_specs_after_the_etoro_passing_ones(
    tmp_path, monkeypatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(swing_focus.time, "monotonic", clock)

    def run(with_auto=True, **settings):
        screener = _Screener(clock, took=(1.0, 1.0))
        auto, state = _auto(tmp_path / "auto")
        auto.runtime_state = state
        workflow = _swing_workflow(
            tmp_path, screener, state, auto=auto if with_auto else None, **settings
        )
        workflow.run_swing_scan(notify=False)
        return [call["keys"] for call in screener.calls]

    assert run(**AUTOMATIC) == [["inside_bar_narrow_range_breakout:1d"], ["momentum_breakout:1d"]]
    # No paper-only scan unless a paper-only entry could be proposed and executed at once.
    first_only = [["inside_bar_narrow_range_breakout:1d"]]
    assert run(**AUTOMATIC, paper_oos_bar_enabled=False) == first_only
    # Without the gate there is no focus at all: one scan, the normal rotation.
    assert len(run(**AUTOMATIC, require_strategy_oos_evidence=False)) == 1
    assert (
        run(
            auto_propose_enabled=True,
            paper_auto_approve_proposals=False,
            paper_auto_operation_mode="unattended",
        )
        == first_only
    )  # proposals would wait for a person: paper-only entries are automatic only
    assert run(paper_auto_approve_proposals=True, paper_auto_operation_mode="shadow") == first_only
    assert run(with_auto=False, **AUTOMATIC) == first_only


def test_the_paper_only_specs_are_swing_keys_never_in_the_first_scan(tmp_path) -> None:
    from app.workflow.swing_focus import focused_spec_batch

    minute_oos_only = {  # passes eToro's full check, fails paper's minute-bar costs
        "passed": True,
        "paper_passed": False,
        "paper_oos_passed": True,
        "oos_expectancy_r": 0.05,
    }
    verdicts = {
        **VERDICTS,
        "rsi_reversal:15m": minute_oos_only,
        "vwap_reversion:5m": {"passed": False, "paper_oos_passed": True},
        "momentum_breakout:1w": OOS_ONLY,
    }
    auto, state = _auto(tmp_path, verdicts=verdicts, **AUTOMATIC)
    keys = [*verdicts]
    service = SimpleNamespace(
        settings=auto.settings,
        runtime_state=state,
        auto_trading=auto,
        market_screener=SimpleNamespace(strategy_spec_keys_for_timeframes=lambda _tf: keys),
    )
    batch = focused_spec_batch(service, task="swing_scan", timeframes=["1d", "1w", "15m", "5m"])
    assert "rsi_reversal:15m" in batch["strategy_spec_keys"]  # eToro-passing: the first scan
    assert batch["paper_only_spec_keys"] == ["momentum_breakout:1d", "momentum_breakout:1w"]


def test_the_runs_own_entries_are_proposed_and_executed_before_the_paper_only_scan_starts(
    tmp_path, monkeypatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(swing_focus.time, "monotonic", clock)
    events: list[tuple] = []
    screener = _Screener(clock, took=(1.0, 1.0), events=events)
    auto, state = _auto(tmp_path / "auto")
    auto.approve_enqueue_execute = (
        lambda proposal, candidate, **_k: events.append(
            ("execute", candidate.symbol, candidate.strategy_name)
        )
        or EXECUTED
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
            **AUTOMATIC,
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


def test_the_queue_refuses_a_tagged_order_once_the_etoro_account_holds_its_symbol(
    tmp_path,
) -> None:
    from app.models.execution_queue import ExecutionQueueStatus
    from tests.test_execution_coordinator_gates import approved_queue

    for tagged, held in ((True, True), (True, False), (False, True)):
        app, _data, proposal, queued = approved_queue(tmp_path / f"{tagged}{held}")
        stored = app.state.proposal_service.proposals.get(proposal.id)
        if tagged:  # as the auto-propose pass tags it
            stored.order.metadata.update(TAGS)
            app.state.proposal_service.proposals.update(stored)
        state = _State(_etoro(open_symbols=("NVDA",) if held else (), cards={}))
        coordinator = app.state.execution_coordinator
        coordinator.etoro_live_mirror = SimpleNamespace(  # bought while it waited for approval
            state=state, settings=SimpleNamespace(etoro_live_room_authority_enabled=False)
        )
        result = coordinator.process_queue_item(queued.id)
        refused = "paper_only_etoro_live_symbol_held" in str(result.validation_reason)
        assert refused is (tagged and held), (tagged, held, result.validation_reason)
        assert (result.status == ExecutionQueueStatus.BLOCKED) is refused
        assert bool(state.many) is tagged  # an untagged order reads nothing more


def test_a_crypto_position_is_told_apart_however_its_symbol_is_spelled(tmp_path) -> None:
    old = NOW - timedelta(days=5)
    names = HELD[: MAX_OPEN_PAPER_ONLY - 1]
    rows = [
        _row("BTC/USD", created_at=old),  # the order's spelling; the broker's is BTCUSD
        *(_row(s, tagged=True, created_at=old) for s in names),
    ]
    auto, _ = _auto(
        tmp_path,
        broker=_Broker(["BTCUSD", *names]),
        executions=_Executions(rows=rows),
        **BOOK,
    )
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == []  # one short of the cap


# --- round 2 (2026-10-22): paper-only entries are automatic only ------------------------------


class _Proposals:
    """A proposal service that keeps its proposals and their status (pending -> ...)."""

    def __init__(self):
        self.items: dict[str, SimpleNamespace] = {}
        self.created = []

    def list_proposals(self, *, status):
        return [p for p in self.items.values() if p.status == status]

    def create_proposal(self, request):
        self.created.append(request)
        ident = f"p{len(self.created)}"
        order = SimpleNamespace(
            symbol=request.symbol,
            proposed_price=request.proposed_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            metadata=dict(request.metadata or {}),
        )
        self.items[ident] = SimpleNamespace(id=ident, order=order, status=ApprovalStatus.PENDING)
        return self.items[ident]

    def reject_proposal(self, proposal_id, decision):
        proposal = self.items[proposal_id]
        if proposal.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
            raise ValueError("Only pending or approved proposals can be rejected")
        proposal.status, proposal.decision = ApprovalStatus.REJECTED, decision
        return proposal


def _stateful(tmp_path, auto):
    workflow, _ = _workflow(tmp_path, auto)
    workflow.proposal_service = proposals = _Proposals()
    return workflow, proposals


def test_a_paper_only_candidate_is_proposed_only_when_it_would_execute_at_once(tmp_path) -> None:
    calls = []
    auto, _ = _auto(tmp_path, auto_blockers=["outside_regular_market_hours"])
    blockers = auto.candidate_blockers
    auto.candidate_blockers = lambda c: calls.append(c.symbol) or blockers(c)
    assert _blockers(auto, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_not_automatic",
        "paper_only_auto_outside_regular_market_hours",
    ]
    assert _blockers(auto, _candidate("UBER", "inside_bar_narrow_range_breakout")) == []
    assert calls == ["UBER"]  # never for an eToro-eligible candidate
    full, _ = _auto(tmp_path / "full", executions=_Executions(["X"] * 13))
    full.candidate_blockers = lambda c: calls.append(c.symbol) or []
    assert _blockers(full, _candidate("V", "momentum_breakout")) == [
        "paper_only_headroom_trades_per_day"
    ]
    assert calls == ["UBER"]  # checked last: not read for one refused before it
    broken, _ = _auto(tmp_path / "broken")
    broken.candidate_blockers = lambda _c: 1 / 0
    assert _blockers(broken, _candidate("UBER", "momentum_breakout")) == [
        "paper_only_not_automatic",
        "paper_only_auto_candidate_blockers_unreadable",
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        None,  # refused by candidate_blockers after all
        SimpleNamespace(status="blocked", validation_reason="entry_drift_too_large"),
        RuntimeError("enqueue failed"),
    ],
)
def test_a_paper_only_proposal_not_placed_in_its_pass_is_rejected_at_once(
    tmp_path, outcome
) -> None:
    auto, _ = _auto(tmp_path)
    workflow, proposals = _stateful(tmp_path, auto)

    def execute(*_a, **_k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    auto.approve_enqueue_execute = execute
    response = _response(_scan_candidate("UBER", "momentum_breakout", 95.0))
    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError):
            auto_propose_candidates(workflow, response, origin="swing_scan", notify=False)
    else:
        auto_propose_candidates(workflow, response, origin="swing_scan", notify=False)
    (proposal,) = proposals.items.values()
    assert proposal.status == ApprovalStatus.REJECTED
    assert proposal.decision.reviewer == "paper_auto"
    (withdrawn,) = [p for e, p in workflow.run_logs.items if e == "paper_only_proposal_withdrawn"]
    assert withdrawn["rejected"] is True and withdrawn["symbol"] == "UBER"


def test_an_executed_paper_only_proposal_and_any_untagged_one_are_left_as_they_are(
    tmp_path,
) -> None:
    auto, _ = _auto(tmp_path)
    workflow, proposals = _stateful(tmp_path, auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: EXECUTED
    auto_propose_candidates(
        workflow,
        _response(_scan_candidate("UBER", "momentum_breakout", 95.0)),
        origin="swing_scan",
        notify=False,
    )
    auto.approve_enqueue_execute = lambda *_a, **_k: None  # an untagged one waits, as before
    auto_propose_candidates(
        workflow,
        _response(_scan_candidate("ADP", "inside_bar_narrow_range_breakout", 95.0)),
        origin="swing_scan",
        notify=False,
    )
    assert [p.status for p in proposals.items.values()] == [ApprovalStatus.PENDING] * 2
    assert not [e for e, _p in workflow.run_logs.items if e == "paper_only_proposal_withdrawn"]


def test_a_paper_only_uber_left_pending_never_stops_a_later_etoro_eligible_uber(tmp_path) -> None:
    # The reviewer's demo (round 2): the unattended path refuses the paper-only UBER.
    auto, _ = _auto(tmp_path)
    workflow, proposals = _stateful(tmp_path, auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: None
    auto_propose_candidates(
        workflow,
        _response(_scan_candidate("UBER", "momentum_breakout", 95.0)),
        origin="swing_scan",
        notify=False,
    )
    auto.approve_enqueue_execute = lambda *_a, **_k: EXECUTED
    later = _response(_scan_candidate("UBER", "inside_bar_narrow_range_breakout", 80.0))
    assert auto_propose_candidates(workflow, later, origin="swing_scan", notify=False) == 1
    assert [r.strategy_name for r in proposals.created] == [
        "momentum_breakout",
        "inside_bar_narrow_range_breakout",
    ]
    # And when the unattended path would refuse it up front, it is never proposed at all.
    auto, _ = _auto(tmp_path / "up", auto_blockers=["candidate_not_backtest_validated"])
    workflow, proposals = _stateful(tmp_path / "up", auto)
    auto_propose_candidates(
        workflow,
        _response(_scan_candidate("UBER", "momentum_breakout", 95.0)),
        origin="swing_scan",
        notify=False,
    )
    assert proposals.created == []


def test_a_person_cannot_approve_a_paper_only_proposal(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.models.approval import ApprovalDecisionRequest
    from app.performance.paper_only import AUTO_REVIEWER, AUTOMATIC_ONLY
    from tests.test_execution_coordinator_gates import approved_queue, proposal_payload

    app, *_ = approved_queue(tmp_path)
    service = app.state.proposal_service
    tagged = proposal_payload("NVDA")
    tagged.metadata.update(TAGS)
    plain = service.create_proposal(proposal_payload("GOOGL"))
    by_hand = service.create_proposal(tagged)
    with pytest.raises(ValueError, match="automatic only"):
        service.approve_proposal(
            by_hand.id, ApprovalDecisionRequest(reviewer="telegram:1", notes="x")
        )
    response = TestClient(app).post(
        f"/proposals/{by_hand.id}/approve", json={"reviewer": "operator", "notes": "x"}
    )
    assert (response.status_code, response.json()["detail"]) == (409, AUTOMATIC_ONLY)
    assert service.get_proposal(by_hand.id).status == ApprovalStatus.PENDING
    assert (
        service.approve_proposal(
            plain.id, ApprovalDecisionRequest(reviewer="telegram:1", notes="x")
        ).status
        == ApprovalStatus.APPROVED
    )  # an untagged one, as before
    assert (
        service.approve_proposal(
            by_hand.id, ApprovalDecisionRequest(reviewer=AUTO_REVIEWER, notes="x")
        ).status
        == ApprovalStatus.APPROVED
    )  # the unattended path in its own pass


def test_the_second_pass_considers_only_candidates_paper_only_by_its_own_read(tmp_path) -> None:
    auto, state = _auto(tmp_path)
    candidates = [
        _candidate("A", "momentum_breakout"),
        _candidate("B", "inside_bar_narrow_range_breakout"),
        _candidate("C", "trend_following"),
    ]
    assert [c.symbol for c in PaperOnlyPass(auto, only=True).order(candidates)] == ["A"]
    flipped = {**VERDICTS, "momentum_breakout:1d": FULL}  # it passed the full check since
    state[VERDICTS_KEY] = json.dumps({"verdicts": flipped})
    assert PaperOnlyPass(auto, only=True).order(candidates) == []
    assert [c.symbol for c in PaperOnlyPass(auto).order(candidates)] == ["A", "B", "C"]


def test_the_second_pass_counts_what_it_created_even_when_it_raises(tmp_path, monkeypatch) -> None:
    screener = _Screener(_Clock())
    run = _scan(
        tmp_path,
        monkeypatch,
        screener,
        fail_after_create=True,
        screener_batch_deadline_seconds=120.0,
    )
    assert run.created == 1
    coverage = run.response.coverage["paper_only_scan"]
    assert (coverage["proposals_created"], coverage["error"]) == (1, "broker down mid-pass")
    assert coverage["symbols_evaluated"] == 4  # the scan's own counts are kept
    assert run.origins == ["swing_scan:paper_only"]  # its own funnel and log origin


def test_the_cadence_is_stamped_before_the_paper_only_scan(tmp_path, monkeypatch) -> None:
    from app.workflow.schedule import is_due

    clock = _Clock()
    monkeypatch.setattr(swing_focus.time, "monotonic", clock)
    monkeypatch.setattr(operations, "utc_now", lambda: NOW + timedelta(seconds=clock.now - 1_000))
    screener = _Screener(clock, took=(1.0, 300.0))  # a very long paper-only scan
    auto, state = _auto(tmp_path / "auto")
    auto.runtime_state = state
    workflow = _swing_workflow(tmp_path, screener, state, auto=auto, **AUTOMATIC)
    workflow.run_swing_scan(notify=False)
    assert len(screener.calls) == 2
    stamped = datetime.fromisoformat(state["workflow:last_swing_scan_at"])
    assert stamped == NOW + timedelta(seconds=1)  # after the run's own pass, not 301 s
    import app.workflow.schedule as schedule

    monkeypatch.setattr(schedule, "utc_now", lambda: NOW + timedelta(minutes=10, seconds=1))
    assert is_due(workflow, "workflow:last_swing_scan_at", 10)  # 10 min after its own pass


def test_a_tagged_order_is_never_mirrored_to_the_etoro_demo_comparison(tmp_path) -> None:
    from tests.test_broker_comparison import _proposal as demo_proposal
    from tests.test_broker_comparison import _service as demo_service

    service, etoro, automation, brokers = demo_service(tmp_path)
    tagged = demo_proposal()
    tagged.order.metadata.update(TAGS)
    primary = SimpleNamespace(broker_order_id="alpaca-1")
    assert (
        service.mirror(proposal=tagged, primary_execution=primary, primary_broker="alpaca") is None
    )
    assert etoro.orders == [] and brokers.list_comparisons() == [] and automation.reasons == []
    untagged = service.mirror(
        proposal=demo_proposal(), primary_execution=primary, primary_broker="alpaca"
    )
    assert untagged.status == "submitted" and len(etoro.orders) == 1  # as before
