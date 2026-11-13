"""The paper exploration lane (operator Phase 1, 2026-11-10; app/workflow/exploration.py).

* admission: a candidate refused only by the evidence gate (no verdict, failing, too few trades,
  minute-bar cost profiles, stale) becomes a tagged paper-only exploration entry; with the lane
  off, or for anything else (blacklist, retired, health, governance, crypto, other timeframes,
  a candidate the loop drops), the old refusal stands;
* every hard gate of the unattended path still refuses an exploration entry; only the
  evidence-family execution checks are bypassed, and only for a tagged exploration proposal;
* caps: one exploration proposal a pass (not taken from the OOS-only allowance), 8 open, 12 a
  New York day, one position a symbol, the eToro account's symbols, paper's day and book
  headroom, the cadence budget, a bound on checks, no late intraday entry;
* sizing at 0.25% of equity at the stop, never above the gate's cap;
* real money: every reader of the paper-only tag (the eToro mirror, its room, the coordinator's
  re-check, the demo comparison, the paper record, the evidence, the demotion, health, tier 3,
  the bootstrap, Stage 3, the go-live and weekly readiness, the ledgers) treats an exploration
  entry as it treats an OOS-only one: each reader test of option B is re-run with the
  exploration tag, and the mirror, the paper record and the verdicts are checked directly.

No wall clock: the pass's clocks are patched.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.workflow.exploration as exploration_module
import app.workflow.paper_only as paper_only_module
import tests.test_paper_only_pass as pass_tests
import tests.test_paper_only_readers as reader_tests
from app.models.approval import ApprovalStatus
from app.performance.paper_only import EXPLORATION, is_exploration, is_paper_only
from app.performance.strategy_evidence import VERDICTS_KEY
from app.risk.proposal_sizing import risk_based_proposal_notional
from app.workflow.exploration import CHECKS_PER_PASS, EXECUTION_BYPASS, PROPOSAL_BYPASS
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import PaperOnlyPass
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings
from tests.test_paper_only_pass import (
    BOOK,
    EXECUTED,
    FULL,
    NOW,
    TAGS,
    _auto,
    _Broker,
    _candidate,
    _etoro,
    _Executions,
    _held,
    _response,
    _row,
    _scan_candidate,
    _stateful,
)
from tests.test_workflow_service import (
    FakeAlertHistory,
    FakeLogs,
    FakeMarketDataEngine,
    FakeNotifier,
    FakeProposalService,
    FakeState,
    FakeTrackedSignals,
)

LANE = {"paper_exploration_lane_enabled": True}
EXPLORATION_TAGS = {"paper_only": True, "paper_only_reason": EXPLORATION}
EXPLORE = "trend_following"  # fails both bars in VERDICTS: refused by the evidence gate
OOS = "momentum_breakout"  # paper-only on the OOS bar alone
PASSING = "inside_bar_narrow_range_breakout"  # passes: eToro-eligible
BYPASSED = "paper_exploration_bypassed"


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    import app.risk.context as risk_context

    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(risk_context, "utc_now", lambda: NOW)
    monkeypatch.setattr(exploration_module, "utc_now", lambda: NOW)  # 11:00 New York
    monkeypatch.setattr(paper_only_module, "_LOGGED", {})


def _lane(tmp_path, **settings):
    return _auto(tmp_path, **{**LANE, **settings})


def _blockers(auto, candidate, paper_pass=None):
    return auto.candidate_proposal_blockers(candidate, paper_only=paper_pass)


def _xrow(symbol, *, status="filled", created_at=NOW):
    """An execution row of an exploration entry."""

    row = _row(symbol, status=status, created_at=created_at)
    row.request_payload["metadata"] = dict(EXPLORATION_TAGS)
    return row


# --- admission -------------------------------------------------------------------------------


def test_an_evidence_refused_candidate_becomes_a_tagged_exploration_entry(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    candidate = _candidate("UBER", EXPLORE)
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, candidate, paper_pass) == []
    tags = paper_pass.tags(candidate)
    assert tags == {
        **EXPLORATION_TAGS,
        BYPASSED: ["strategy_lacks_oos_evidence"],
        "timeframe": "1d",  # the candidate's metadata had none: the flatten and ledger read it
    }
    assert is_paper_only(tags) and is_exploration(tags)
    assert paper_pass.notice(candidate)[0] == "Paper-only exploration (not copied to eToro)"
    assert paper_pass.risk_pct(candidate) == 0.25
    # The same candidate with the lane off: the old gate's refusal, nothing tagged.
    off, _ = _auto(tmp_path / "off")
    off_pass = PaperOnlyPass(off)
    assert _blockers(off, candidate, off_pass) == ["strategy_lacks_oos_evidence"]
    assert off_pass.tags(candidate) == {} and off_pass.risk_pct(candidate) is None
    # An eToro-eligible and an OOS-only candidate are never exploration entries.
    for strategy, tags in ((PASSING, {}), (OOS, TAGS)):
        other = _candidate("ADP", strategy)
        assert _blockers(auto, other, paper_pass) == []
        assert paper_pass.tags(other) == tags and paper_pass.risk_pct(other) is None


@pytest.mark.parametrize(
    ("verdicts", "timeframe", "bypassed"),
    [
        ({}, "1d", "strategy_lacks_oos_evidence"),  # no verdict at all
        ({"trend_following:1d": {"passed": False, "reasons": ["too_few_oos_trades"]}}, "1d", None),
        (
            {"trend_following:1d": {"passed": False, "reasons": ["oos_expectancy_not_positive"]}},
            "1d",
            None,
        ),
        (  # a minute key failing its Alpaca / stress cost profiles
            {"trend_following:5m": {"passed": True, "paper_passed": False}},
            "5m",
            None,
        ),
        ({"trend_following:1h": {"passed": False, "paper_oos_passed": True}}, "1h", None),
    ],
)
def test_every_kind_of_missing_or_failing_evidence_is_admitted(
    tmp_path, verdicts, timeframe, bypassed
) -> None:
    auto, _ = _lane(tmp_path, verdicts=verdicts)
    candidate = _candidate("UBER", EXPLORE, timeframe=timeframe)
    paper_pass = PaperOnlyPass(auto)
    if timeframe == "1h" and verdicts:  # paper's OOS bar admits it: OOS-only, not exploration
        assert _blockers(auto, candidate, paper_pass) == []
        assert paper_pass.tags(candidate) == TAGS
        return
    assert _blockers(auto, candidate, paper_pass) == []
    assert paper_pass.tags(candidate)[BYPASSED] == [bypassed or "strategy_lacks_oos_evidence"]


def test_stale_evidence_is_admitted_too(tmp_path) -> None:
    stale = {
        VERDICTS_KEY: json.dumps(
            {"computed_at": (NOW - timedelta(days=2)).isoformat(), "verdicts": {"x:1d": FULL}}
        )
    }
    auto, _ = _lane(tmp_path, eto=stale)
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", "x")
    assert _blockers(auto, candidate, paper_pass) == []
    assert paper_pass.tags(candidate)[BYPASSED] == ["strategy_evidence_stale"]
    off, _ = _auto(tmp_path / "off", eto=stale)
    assert _blockers(off, candidate) == ["strategy_evidence_stale"]


def test_the_lane_bypasses_only_the_evidence_family() -> None:
    assert {"strategy_lacks_oos_evidence", "strategy_evidence_stale"} == PROPOSAL_BYPASS
    assert {
        "candidate_not_backtest_validated",
        "insufficient_strategy_closed_trade_evidence",
    } == EXECUTION_BYPASS


def test_a_candidate_refused_for_anything_else_keeps_its_refusal(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    auto.safety = SimpleNamespace(
        is_blacklisted=lambda s: s == "MA", strategy_active=lambda s: s != "sleepy"
    )
    assert _blockers(auto, _candidate("MA", EXPLORE)) == [
        "symbol_blacklisted",
        "strategy_lacks_oos_evidence",
    ]
    assert _blockers(auto, _candidate("UBER", "sleepy")) == [
        "strategy_inactive",  # strategy health: negative live evidence is not bypassed
        "strategy_lacks_oos_evidence",
    ]
    retired = "etf_mega_cap_relative_strength_rotation"
    assert _blockers(auto, _candidate("UBER", retired)) == ["strategy_retired"]
    auto.strategy_governance = SimpleNamespace(
        strategy_production_approved=lambda _s: False,
        strategy_paper_exploration_approved=lambda _s: False,
    )
    assert _blockers(auto, _candidate("UBER", EXPLORE)) == [
        "strategy_not_production_approved",
        "strategy_lacks_oos_evidence",
    ]


@pytest.mark.parametrize(
    ("symbol", "timeframe", "ready", "skip"),
    [
        ("BTCUSD", "1d", True, "crypto"),
        ("UBER", "1m", True, "timeframe_1m"),
        ("UBER", "1w", True, "timeframe_1w"),
        ("UBER", "1d", False, "not_ready_not_execution_ready"),
    ],
)
def test_outside_the_lane_the_evidence_refusal_stands(tmp_path, symbol, timeframe, ready, skip) -> None:
    auto, state = _lane(tmp_path, eto=_etoro())
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate(symbol, EXPLORE, timeframe=timeframe, ready=ready)
    assert _blockers(auto, candidate, paper_pass) == ["strategy_lacks_oos_evidence"]
    assert paper_pass.tags(candidate) == {}
    assert paper_pass.exploration.skip == skip  # why, for the funnel (2026-11-13)
    assert state.many == [] and auto.executions.counts == []  # nothing else was read


def test_the_lane_timeframes_are_a_setting(tmp_path) -> None:
    auto, _ = _lane(tmp_path, paper_exploration_lane_timeframes="1D, 15m")
    assert auto.settings.paper_exploration_lane_timeframes == ["1d", "15m"]
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="15m")) == []
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="5m")) == [
        "strategy_lacks_oos_evidence"
    ]
    assert make_settings(tmp_path, paper_exploration_lane_enabled=True).paper_exploration_lane_timeframes == [
        "5m",
        "10m",
        "15m",
        "1h",
        "1d",
    ]


def test_the_production_defaults(tmp_path) -> None:
    from app.runtime_settings import AppSettings

    fields = AppSettings.model_fields
    assert fields["paper_exploration_lane_enabled"].default is True
    assert fields["paper_exploration_lane_risk_pct"].default == 0.25
    assert fields["paper_exploration_lane_max_open"].default == 8
    assert fields["paper_exploration_lane_max_new_per_day"].default == 12
    assert fields["paper_exploration_lane_max_per_pass"].default == 1
    for real in ({"enable_real_trading": True}, {"execution_mode": "live"}):
        auto, _ = _lane(tmp_path / str(real), **real)
        assert _blockers(auto, _candidate("UBER", EXPLORE)) == ["strategy_lacks_oos_evidence"]


# --- every hard gate of the unattended path still refuses ------------------------------------

CLEAN = dict(
    execution_mode="paper",
    enable_real_trading=False,
    alpaca_expected_account_number="PAPER-1",
    paper_auto_approve_proposals=True,
    auto_execution_worker_enabled=True,
    paper_auto_operation_mode="unattended",
    paper_auto_approval_tier="tier2_strict_valid",
    paper_auto_min_clean_supervised_lifecycles=0,
    market_universe_symbols=["NVDA"],
)


def _real(tmp_path, *, lifecycles=(), **settings):
    """A lane whose unattended-path check is the real ``candidate_blockers`` on clean fakes."""

    auto, _ = _lane(tmp_path, **{**CLEAN, **settings})
    del auto.candidate_blockers  # _auto stubs it; the class's own check runs here
    auto.automation = SimpleNamespace(execution_blockers=lambda: [])
    auto.reconciliation = SimpleNamespace(account_verified=lambda: True)
    auto.alpaca = SimpleNamespace(
        is_regular_market_open=lambda: True, is_supported_equity=lambda _s: True
    )
    auto.paper_trading = SimpleNamespace(lifecycles=lambda limit=1000: list(lifecycles))
    return auto


def _ready(**changes):
    values = dict(
        symbol="NVDA",
        strategy_name=EXPLORE,
        timeframe="1d",
        execution_ready=True,
        signal_role="entry_long",
        score=95.0,
        stop_loss=95.0,
        take_profit=110.0,
        metadata={"alert_eligible": True, "backtest_validated": True},
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_a_clean_exploration_candidate_passes_the_real_unattended_check(tmp_path) -> None:
    auto = _real(tmp_path)
    assert auto.candidate_blockers(_ready()) == []
    assert _blockers(auto, _ready()) == []


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        (lambda a, c: setattr(c, "score", 10.0), "candidate_score_below_auto_threshold"),
        (lambda a, c: setattr(c, "take_profit", None), "bracket_prices_missing"),
        (
            lambda a, c: setattr(a.alpaca, "is_regular_market_open", lambda: False),
            "outside_regular_market_hours",
        ),
        (lambda a, c: setattr(c, "symbol", "GOOG"), "symbol_not_in_execution_universe"),
        (
            lambda a, c: setattr(a.alpaca, "is_supported_equity", lambda _s: False),
            "symbol_not_supported_by_alpaca",
        ),
        (
            lambda a, c: setattr(
                a.automation, "execution_blockers", lambda: ["automation_kill_switch_enabled"]
            ),
            "automation_kill_switch_enabled",
        ),
        (
            lambda a, c: setattr(a.reconciliation, "account_verified", lambda: False),
            "alpaca_account_not_verified",
        ),
        (
            lambda a, c: setattr(a.settings, "paper_auto_approval_tier", "tier1_supervised_only"),
            "paper_auto_tier_supervised_only",
        ),
        (
            lambda a, c: setattr(a.settings, "paper_auto_min_clean_supervised_lifecycles", 10),
            "insufficient_clean_supervised_lifecycles",
        ),
        (lambda a, c: setattr(a.settings, "paper_auto_operation_mode", "supervised"), None),
    ],
)
def test_every_hard_gate_still_refuses_an_exploration_candidate(tmp_path, change, blocker) -> None:
    auto = _real(tmp_path)
    candidate = _ready()
    change(auto, candidate)
    reasons = auto.candidate_blockers(candidate)
    assert reasons and not set(reasons) & EXECUTION_BYPASS
    paper_pass = PaperOnlyPass(auto)
    lane = _blockers(auto, candidate, paper_pass)
    assert lane[0] == "exploration_lane_not_automatic"
    assert lane[1:] == [f"exploration_lane_auto_{reason}" for reason in reasons]
    if blocker:
        assert f"exploration_lane_auto_{blocker}" in lane
    assert paper_pass.tags(candidate) == {}


def _lifecycle(pnl):
    flags = SimpleNamespace(
        entry_submitted=True,
        entry_filled=True,
        bracket_legs_verified=True,
        exit_filled_or_position_flat=True,
        reconciled=True,
        review_created=True,
        duplicate_order_absent=True,
    )
    return SimpleNamespace(
        strategy_name=EXPLORE,
        autonomous=True,
        flags=flags,
        realized_pnl_usd=pnl,
        execution=SimpleNamespace(payload={"request": {"metadata": {}}}),
        blockers=[],
    )


def test_only_the_evidence_family_of_the_unattended_check_is_bypassed(tmp_path) -> None:
    unvalidated = _ready(metadata={"alert_eligible": True, "backtest_validated": False})
    auto = _real(tmp_path)
    assert auto.candidate_blockers(unvalidated) == ["candidate_not_backtest_validated"]
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, unvalidated, paper_pass) == []
    assert paper_pass.tags(unvalidated)[BYPASSED] == [
        "candidate_not_backtest_validated",
        "strategy_lacks_oos_evidence",
    ]
    # Tier 3 with no closed trades yet: too few is bypassed ...
    tier3 = _real(tmp_path / "t3", paper_auto_approval_tier="tier3_strategy_qualified")
    assert tier3.candidate_blockers(_ready()) == ["insufficient_strategy_closed_trade_evidence"]
    assert _blockers(tier3, _ready()) == []
    # ... a losing live record is not.
    losing = _real(
        tmp_path / "l",
        lifecycles=[_lifecycle(-20.0) for _ in range(5)],
        paper_auto_approval_tier="tier3_strategy_qualified",
    )
    lane = _blockers(losing, _ready())
    assert "exploration_lane_auto_strategy_expectancy_not_positive" in lane
    assert "exploration_lane_auto_insufficient_strategy_closed_trade_evidence" not in lane


def test_the_execution_check_bypasses_the_family_only_for_a_tagged_exploration_proposal(
    tmp_path,
) -> None:
    unvalidated = _ready(metadata={"alert_eligible": True, "backtest_validated": False})
    for metadata, executed in (
        ({**EXPLORATION_TAGS, BYPASSED: ["strategy_lacks_oos_evidence"]}, True),
        (dict(TAGS), False),  # OOS-only: the full check, as before
        ({}, False),
    ):
        auto = _real(tmp_path / str(len(metadata)) / str(executed))
        auto.logs, auto.notifier = FakeLogs(), SimpleNamespace(send_text=lambda _t: None)
        order = SimpleNamespace(symbol="NVDA", metadata=metadata)
        auto.proposals = SimpleNamespace(
            approve_proposal=lambda *_a, order=order: SimpleNamespace(id="p1", order=order)
        )
        processed = SimpleNamespace(id="q1", symbol="NVDA", status="executed", validation_reason="")
        auto.execution = SimpleNamespace(
            enqueue_approved_proposal=lambda _id: SimpleNamespace(id="q1"),
            process_queue_item=lambda _id, processed=processed: processed,
        )
        result = auto.approve_enqueue_execute(SimpleNamespace(id="p1", order=order), unvalidated)
        assert (result is processed) is executed


# --- order, the pass and its caps ------------------------------------------------------------


def test_exploration_candidates_come_after_every_other_one(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    candidates = [
        _candidate("A", EXPLORE),
        _candidate("B", OOS),
        _candidate("C", PASSING),
        _candidate("D", EXPLORE),
        _candidate("BTCUSD", EXPLORE),  # crypto: never the lane's, keeps its place
        _candidate("E", EXPLORE, timeframe="1m"),  # not a lane timeframe
    ]
    ordered = PaperOnlyPass(auto).order(candidates)
    assert [c.symbol for c in ordered] == ["C", "BTCUSD", "E", "B", "A", "D"]
    off, _ = _auto(tmp_path / "off")
    assert [c.symbol for c in PaperOnlyPass(off).order(candidates)] == [
        "A",
        "C",
        "D",
        "BTCUSD",
        "E",
        "B",
    ]  # as before the lane


def _lane_workflow(tmp_path, auto, **settings):
    proposals = FakeProposalService()
    auto.approve_enqueue_execute = lambda *_a, **_k: EXECUTED
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, auto_propose_enabled=True, **settings),
        market_screener=None,
        market_data_engine=FakeMarketDataEngine(None),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        proposal_service=proposals,
        auto_trading_service=auto,
    )
    return workflow, proposals


def _tags(request):
    return {k: v for k, v in request.metadata.items() if k.startswith("paper_")}


def test_one_pass_proposes_etoro_eligible_then_oos_only_then_one_exploration_entry(
    tmp_path,
) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    response = _response(
        _scan_candidate("UBER", EXPLORE, 99.0),  # UBER is taken by the eToro-eligible entry
        _scan_candidate("UBER", PASSING, 80.0),
        _scan_candidate("V", EXPLORE, 97.0),
        _scan_candidate("ADP", EXPLORE, 96.0),  # the pass's one exploration entry is V's
        _scan_candidate("MA", OOS, 90.0),
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=True) == 3
    assert [(r.symbol, r.strategy_name) for r in proposals.created] == [
        ("UBER", PASSING),
        ("MA", OOS),
        ("V", EXPLORE),
    ]
    assert [_tags(r) for r in proposals.created] == [
        {},
        TAGS,
        {**EXPLORATION_TAGS, BYPASSED: ["strategy_lacks_oos_evidence"]},
    ]
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert blocked == [
        {"origin": "swing_scan", "symbol": "ADP", "blockers": ["exploration_lane_pass_limit"]}
    ]
    notices = [m for m in workflow.notifier.messages if m.startswith("Auto proposal created")]
    assert "Paper-only exploration (not copied to eToro)" in notices[-1]
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][0]
    assert funnel["proposals_created"] == 3 and funnel["dropped:duplicate_or_no_symbol"] == 1


def test_the_per_pass_limit_is_a_setting(tmp_path) -> None:
    auto, _ = _lane(tmp_path, paper_exploration_lane_max_per_pass=2)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    response = _response(*(_scan_candidate(s, EXPLORE, 90.0) for s in ("V", "ADP", "UBER")))
    assert auto_propose_candidates(workflow, response, origin="intraday", notify=False) == 2
    zero, _ = _lane(tmp_path / "0", paper_exploration_lane_max_per_pass=0)
    assert _blockers(zero, _candidate("V", EXPLORE)) == ["exploration_lane_pass_limit"]


def test_at_most_eight_exploration_positions_open_and_their_own_cap_only(tmp_path) -> None:
    old = NOW - timedelta(days=3)
    names = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "CVX", "KO", "PEP"]

    def blockers(rows, held, strategy=EXPLORE):
        auto, _ = _lane(
            tmp_path / f"{len(rows)}-{len(held)}-{strategy}",
            broker=_Broker(held),
            executions=_Executions(rows=rows),
            **BOOK,
        )
        return _blockers(auto, _candidate("UBER", strategy))

    seven = [_xrow(s, created_at=old) for s in names[:7]]
    assert blockers(seven, names[:7]) == []
    eight = [_xrow(s, created_at=old) for s in names]
    assert blockers(eight, names) == ["exploration_lane_positions_cap"]
    unfilled = [_xrow("WMT", status="submitted"), *seven]  # placed today, not filled yet
    assert blockers(unfilled, names[:7]) == ["exploration_lane_positions_cap"]
    # An OOS-only position counts for its own cap, not the lane's, and the other way round.
    oos = [_row("WMT", tagged=True, created_at=old), *seven]
    assert blockers(oos, [*names[:7], "WMT"]) == []
    five = [_xrow(s, created_at=old) for s in names[:5]]
    assert blockers(five, names[:5], strategy=OOS) == []
    # A held position whose entry is not among the rows read counts for both.
    assert blockers(seven, [*names[:7], "HD"]) == ["exploration_lane_positions_cap"]
    assert blockers([], [], strategy=EXPLORE) == []
    capped, _ = _lane(tmp_path / "cap", paper_exploration_lane_max_open=0)
    assert _blockers(capped, _candidate("UBER", EXPLORE)) == ["exploration_lane_positions_cap"]


def test_at_most_twelve_new_exploration_entries_a_new_york_day(tmp_path) -> None:
    def blockers(rows, **settings):
        auto, _ = _lane(
            tmp_path / f"{len(rows)}-{rows[0].created_at if rows else ''}",
            executions=_Executions(rows=rows),
            max_trades_per_day=100,
            **settings,
        )
        return _blockers(auto, _candidate("UBER", EXPLORE))

    today = NOW - timedelta(hours=1)
    closed = [_xrow(f"S{i}", created_at=today) for i in range(12)]  # opened and closed today
    assert blockers(closed) == ["exploration_lane_daily_cap"]
    assert blockers(closed[:11]) == []
    failed = [_xrow(f"S{i}", status="failed", created_at=today) for i in range(12)]
    assert blockers(failed) == []  # never placed: not an entry
    normal = [_row(f"S{i}", created_at=today) for i in range(12)]
    assert blockers(normal) == []  # other entries are the trades-a-day check's
    # 03:30 UTC on 21 Oct is 23:30 on 20 Oct in New York: yesterday's entries.
    late = [_xrow(f"S{i}", created_at=NOW.replace(hour=3, minute=30)) for i in range(12)]
    assert blockers(late) == []
    assert blockers(closed[:3], paper_exploration_lane_max_new_per_day=3) == [
        "exploration_lane_daily_cap"
    ]


def test_one_position_a_symbol_whoever_holds_it(tmp_path) -> None:
    for case, (broker, rows) in enumerate(
        (
            (_Broker(["UBER"]), []),  # paper holds it (a normal entry)
            (_Broker(), [_row("UBER", status="accepted")]),  # an entry placed today, not filled
            (_Broker(["UBER"]), [_xrow("UBER", created_at=NOW - timedelta(days=2))]),  # exploration
        )
    ):
        auto, _ = _lane(
            tmp_path / str(case), broker=broker, executions=_Executions(rows=rows), **BOOK
        )
        assert _blockers(auto, _candidate("UBER", EXPLORE)) == ["exploration_lane_paper_symbol_held"]
    # A normal entry into a symbol an exploration position holds: paper's own per-symbol limit.
    from app.models.trade import TradeOrder
    from app.risk.context import build_risk_context
    from app.risk.guardrails import RiskManager

    settings = make_settings(tmp_path, **BOOK)
    assert settings.per_symbol_position_limit == 1
    order = TradeOrder(
        symbol="NVDA",
        amount_usd=1_000,
        proposed_price=100.0,
        stop_loss=99.5,
        take_profit=110.0,
        strategy_name=PASSING,
    )
    context = build_risk_context(settings, _Broker(["NVDA"]), _Executions(rows=[_xrow("NVDA")]))
    assert "Per-symbol position limit reached" in RiskManager(settings).validate_order(order, context).reasons


def test_never_where_the_etoro_account_holds_or_has_in_flight(tmp_path) -> None:
    auto, state = _lane(tmp_path, eto=_etoro())
    paper_pass = PaperOnlyPass(auto)
    held = ["exploration_lane_etoro_live_symbol_held"]
    assert _blockers(auto, _candidate("UBER", EXPLORE), paper_pass) == held  # open
    assert _blockers(auto, _candidate("ADP", EXPLORE), paper_pass) == held  # in flight
    assert _blockers(auto, _candidate("MA", EXPLORE), paper_pass) == held  # by its instrument id
    assert _blockers(auto, _candidate("V", EXPLORE), paper_pass) == []  # closed there
    assert len(state.many) == 1  # one round trip a pass, shared with the OOS-only entries
    broken, _ = _lane(tmp_path / "b", eto={**_etoro(), "etoro_live:state": "[]"})
    assert _blockers(broken, _candidate("V", EXPLORE)) == ["exploration_lane_etoro_live_state_unreadable"]


def test_the_lane_leaves_paper_s_day_and_book_headroom(tmp_path) -> None:
    candidate = _candidate("UBER", EXPLORE)
    thirteen, _ = _lane(tmp_path / "13", executions=_Executions(["X"] * 13))
    assert _blockers(thirteen, candidate) == ["exploration_lane_headroom_trades_per_day"]
    twelve, _ = _lane(tmp_path / "12", executions=_Executions(["X"] * 12))
    assert _blockers(twelve, candidate) == []
    group, _ = _lane(tmp_path / "g", executions=_Executions(["MSFT"]))
    assert _blockers(group, _candidate("AAPL", EXPLORE)) == [
        "exploration_lane_headroom_correlation_bucket"
    ]
    ten, _ = _lane(tmp_path / "10", **_held(10), **BOOK)
    assert _blockers(ten, candidate) == ["exploration_lane_headroom_open_positions"]
    nine, _ = _lane(tmp_path / "9", **_held(9), **BOOK)
    assert _blockers(nine, candidate) == []
    heat, _ = _lane(tmp_path / "h", **_held(10), **{**BOOK, "max_open_positions": 20})
    assert _blockers(heat, candidate) == ["exploration_lane_headroom_portfolio_heat"]
    unreadable, _ = _lane(tmp_path / "u", broker=_Broker(fail=True), **BOOK)
    assert _blockers(unreadable, candidate) == ["exploration_lane_paper_book_unreadable"]


def test_no_exploration_proposal_once_the_cadence_tick_nears_its_scan_stop(
    tmp_path, monkeypatch
) -> None:
    from app.workflow.cadence_budget import POST_SCAN_RESERVE_SECONDS, CadenceBudget, tick_budget

    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(paper_only_module.time, "monotonic", lambda: clock.now)
    auto, _ = _lane(tmp_path)
    stop = 1_000.0 + 240.0 - POST_SCAN_RESERVE_SECONDS
    with tick_budget(CadenceBudget(started_at=1_000.0, job_timeout=240.0)):
        clock.now = stop - paper_only_module.PROPOSAL_SECONDS - 0.1
        assert _blockers(auto, _candidate("UBER", EXPLORE)) == []
        clock.now = stop - paper_only_module.PROPOSAL_SECONDS
        assert _blockers(auto, _candidate("UBER", EXPLORE)) == [
            "exploration_lane_cadence_budget_spent"
        ]


def test_the_lane_spends_at_most_two_unattended_checks_a_pass(tmp_path) -> None:
    assert CHECKS_PER_PASS == 2
    calls = []
    auto, _ = _lane(tmp_path, auto_blockers=["outside_regular_market_hours"])
    stub = auto.candidate_blockers
    auto.candidate_blockers = lambda c: calls.append(c.symbol) or stub(c)
    paper_pass = PaperOnlyPass(auto)
    refused = ["exploration_lane_not_automatic", "exploration_lane_auto_outside_regular_market_hours"]
    assert _blockers(auto, _candidate("UBER", EXPLORE), paper_pass) == refused
    assert _blockers(auto, _candidate("V", EXPLORE), paper_pass) == refused
    assert _blockers(auto, _candidate("ADP", EXPLORE), paper_pass) == ["exploration_lane_check_limit"]
    assert calls == ["UBER", "V"]


def test_no_intraday_exploration_entry_in_the_last_half_hour(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)
    late = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)  # 15:30 New York (summer time)
    monkeypatch.setattr(exploration_module, "utc_now", lambda: late)
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="5m")) == [
        "exploration_lane_intraday_entry_too_late"
    ]
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="1h")) == [
        "exploration_lane_intraday_entry_too_late"
    ]
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="1d")) == []  # held overnight
    monkeypatch.setattr(exploration_module, "utc_now", lambda: late - timedelta(minutes=1))
    assert _blockers(auto, _candidate("UBER", EXPLORE, timeframe="5m")) == []


def test_an_intraday_exploration_position_is_flattened_before_the_bell_like_any_other(
    tmp_path,
) -> None:
    from app.automation.intraday_exit import _open_intraday_executions

    auto, _ = _lane(tmp_path)
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", EXPLORE, timeframe="15m")
    assert _blockers(auto, candidate, paper_pass) == []
    metadata = {**candidate.metadata, **paper_pass.tags(candidate)}
    assert metadata["timeframe"] == "15m"  # what the flatten reads from the order
    execution = SimpleNamespace(
        request_payload={"symbol": "UBER", "metadata": metadata},
        response_payload={"broker_execution": {"filled_qty": 10.0, "legs": []}},
        realized_pnl_usd=0.0,
        created_at=NOW.isoformat(),
    )
    found = _open_intraday_executions(SimpleNamespace(list=lambda limit=200: [execution]))
    assert found == {"UBER": execution}


def test_a_lane_refusal_is_logged_once_a_day(tmp_path) -> None:
    auto, _ = _lane(tmp_path, executions=_Executions(["X"] * 13))
    workflow, _ = _lane_workflow(tmp_path, auto)
    response = _response(_scan_candidate("UBER", EXPLORE, 95.0))
    for _ in range(3):
        auto_propose_candidates(workflow, response, origin="intraday", notify=False)
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert blocked == [
        {
            "origin": "intraday",
            "symbol": "UBER",
            "blockers": ["exploration_lane_headroom_trades_per_day"],
        }
    ]


# --- sizing ----------------------------------------------------------------------------------


def test_an_exploration_entry_risks_a_quarter_percent_of_equity_at_its_stop(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(
        tmp_path, auto, default_trade_amount_usd=12_500.0, max_trade_amount_usd=12_500.0
    )
    response = _response(  # entry 100, stop 95: a 5% stop
        _scan_candidate("UBER", PASSING, 80.0), _scan_candidate("V", EXPLORE, 90.0)
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 2
    normal, explored = proposals.created
    assert normal.amount_usd == 12_500.0  # flat, as before (risk-based sizing is off here)
    assert explored.amount_usd == 5_000.0  # $100,000 x 0.25% = $250 at a 5% stop
    assert explored.metadata["sizing"]["sizing_mode"] == "risk_based"
    assert explored.metadata["sizing"]["risk_pct"] == 0.25
    assert explored.metadata["sizing"]["effective_risk_usd"] == 250.0


def test_the_lane_s_risk_never_exceeds_the_gate_s_cap(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        auto_propose_risk_based_sizing=True,
        default_trade_amount_usd=100_000.0,
        max_trade_amount_usd=100_000.0,
        max_risk_per_trade_pct=0.2,
    )
    amount, details = risk_based_proposal_notional(
        settings, entry_price=100.0, stop_price=95.0, equity_usd=100_000.0, risk_pct=0.25
    )
    assert details["risk_pct"] == pytest.approx(0.2 * 0.98)  # the gate's cap, less its headroom
    assert amount == pytest.approx(100_000 * 0.00196 / 0.05)
    flat_off = make_settings(tmp_path, default_trade_amount_usd=50_000.0, max_trade_amount_usd=50_000.0)
    amount, details = risk_based_proposal_notional(
        flat_off, entry_price=100.0, stop_price=98.0, equity_usd=100_000.0, risk_pct=0.25
    )
    assert (amount, details["sizing_mode"]) == (12_500.0, "risk_based")  # sized though the flag is off
    for entry, stop, equity in ((100.0, None, 1.0), (100.0, 100.0, 1.0), (100.0, 95.0, 0.0)):
        with pytest.raises(ValueError):  # never sized flat: no proposal
            risk_based_proposal_notional(
                flat_off, entry_price=entry, stop_price=stop, equity_usd=equity, risk_pct=0.25
            )
    assert risk_based_proposal_notional(
        flat_off, entry_price=100.0, stop_price=None, equity_usd=1.0
    )[1]["sizing_mode"] == "flat_default"  # without risk_pct: unchanged


def test_an_unsized_exploration_candidate_makes_no_proposal(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    candidate = _scan_candidate("V", EXPLORE, 90.0).model_copy(update={"stop_loss": 100.0})
    assert auto_propose_candidates(workflow, _response(candidate), origin="t", notify=False) == 0
    assert proposals.created == []
    assert [e for e, _ in workflow.run_logs.items if e == "auto_proposal_failed"] == [
        "auto_proposal_failed"
    ]


# --- automatic only ---------------------------------------------------------------------------


def test_an_exploration_proposal_not_placed_in_its_pass_is_rejected_at_once(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _stateful(tmp_path, auto)
    auto.approve_enqueue_execute = lambda *_a, **_k: SimpleNamespace(
        status="blocked", validation_reason="risk_failed:x"
    )
    response = _response(_scan_candidate("V", EXPLORE, 95.0))
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 1
    (proposal,) = proposals.items.values()
    assert proposal.status == ApprovalStatus.REJECTED and proposal.decision.reviewer == "paper_auto"
    assert is_exploration(proposal.order.metadata)


# --- real money never sees an exploration entry -----------------------------------------------


def _call(test, tmp_path, monkeypatch):
    names = inspect.signature(test).parameters
    kwargs = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    return test(**{name: kwargs[name] for name in names})


# Every option-B reader test but the mirror's log line: an exploration refusal has its own event
# (test_the_etoro_live_mirror_never_submits_an_exploration_entry checks the rest of that test).
MIRROR_LOG_TEST = (
    "test_a_tagged_order_is_refused_before_any_other_mirror_check_and_untagged_ones_unchanged"
)
READER_TESTS = [
    getattr(reader_tests, name)
    for name in dir(reader_tests)
    if name.startswith("test_") and name != MIRROR_LOG_TEST
]
PASS_TESTS = [
    pass_tests.test_the_execution_coordinator_checks_the_etoro_symbols_again_for_a_tagged_order,
    pass_tests.test_the_queue_refuses_a_tagged_order_once_the_etoro_account_holds_its_symbol,
    pass_tests.test_paper_s_per_symbol_limit_keeps_untagged_entries_out_of_a_held_paper_only_symbol,
    pass_tests.test_a_person_cannot_approve_a_paper_only_proposal,
    pass_tests.test_a_tagged_order_is_never_mirrored_to_the_etoro_demo_comparison,
]


@pytest.mark.parametrize("test", READER_TESTS + PASS_TESTS, ids=lambda t: t.__name__)
def test_every_reader_of_the_paper_only_tag_treats_exploration_alike(
    test, tmp_path, monkeypatch
) -> None:
    """Option B's reader tests (the eToro mirror and its room, the coordinator's re-check, the
    queue, the demo comparison, the demotion and its digest rows, strategy health, tier 3, the
    auto-approval bootstrap, Stage 3, the go-live bar and its drawdown, the weekly readiness, both
    ledgers, the live scorecard and a person's approval), with the exploration tag."""

    assert len(READER_TESTS) >= 15
    monkeypatch.setattr(reader_tests, "TAGS", dict(EXPLORATION_TAGS))
    monkeypatch.setattr(pass_tests, "TAGS", dict(EXPLORATION_TAGS))
    _call(test, tmp_path, monkeypatch)


def test_the_etoro_live_mirror_never_submits_an_exploration_entry(tmp_path) -> None:
    from app.broker.etoro_live_mirror import EXPLORATION_NOT_COPIED_EVENT, PAPER_ONLY_REASON
    from app.broker.etoro_live_room import room_verdict
    from tests.test_etoro_live_mirror import _proposal, _run
    from tests.test_etoro_live_room import _live, _order

    mirror, logs = _live(tmp_path)
    reads: list[str] = []
    get = mirror.state.get
    mirror.state.get = lambda key, *a: reads.append(key) or get(key, *a)
    order = _order("JPM", side="sell", stop=None)  # would fail several other checks too
    order.metadata.update(EXPLORATION_TAGS)
    gate = SimpleNamespace(id=None, order=order, signal=None)
    assert mirror.blockers(gate, "etoro") == [PAPER_ONLY_REASON] and reads == []  # read nothing
    assert room_verdict(mirror, order) is None  # paper's own room rules decide it
    tagged = _proposal("JPM")
    tagged.order.metadata.update({**EXPLORATION_TAGS, BYPASSED: ["strategy_lacks_oos_evidence"]})
    assert _run(mirror, tagged) is None and mirror.client.orders == []
    assert logs.events == [
        (
            EXPLORATION_NOT_COPIED_EVENT,
            {"symbol": "JPM", "proposal_id": tagged.id, "reasons": [PAPER_ONLY_REASON]},
        )
    ]  # its own name: the scorecard's skipped-by-eToro list and event log never list it
    oos = _proposal("JPM")
    oos.order.metadata.update(TAGS)  # an OOS-only entry is logged as before
    assert _run(mirror, oos) is None and logs.events[-1][0] == "etoro_live_mirror_blocked"
    assert _run(mirror, _proposal("JPM")) is not None  # the control: an untagged one is copied
    assert [e for e, _ in logs.events].count("etoro_live_mirror_submitted") == 1
    assert len(mirror.client.orders) == 1


def test_exploration_trades_never_count_in_the_etoro_paper_record() -> None:
    from app.broker.etoro_live_paper_record import TOO_FEW, paper_record_blocker, paper_records

    def trade(pnl, tags):
        return SimpleNamespace(strategy_name="x", timeframe="1d", realized_pnl_usd=pnl, payload=dict(tags))

    explored = [trade(500.0, EXPLORATION_TAGS) for _ in range(10)]
    records = paper_records(explored + [trade(-1.0, {})])
    assert records == {"x:1d": (1, -1.0)}
    assert paper_record_blocker(paper_records(explored), "x", "1d") == TOO_FEW


def test_exploration_trades_never_feed_the_strategy_evidence(tmp_path) -> None:
    """The verdicts are pooled from walk-forward backtest rows alone: a book full of winning
    exploration trades leaves them exactly as they were."""

    from app.performance.strategy_evidence import compute_verdicts, refresh_verdicts
    from app.storage.db import Database
    from app.storage.repositories import RuntimeStateRepository

    db = Database(make_settings(tmp_path))
    db.initialize()
    metrics = {"number_of_trades": 50, "expectancy_usd": -2.0, "holdout_trades": 12, "holdout_expectancy_usd": -1.0}
    with db.connect() as c:
        c.execute(
            "INSERT INTO backtests (id, symbol, strategy_name, file_path, started_at, completed_at, "
            "metrics_json, trades_json) VALUES ('b1', 'UBER', 'x', 'UBER:1d:walk_forward_oos', ?, ?, ?, '[]')",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), json.dumps(metrics)),
        )
    settings = make_settings(tmp_path)
    before = compute_verdicts(db, settings)
    for i in range(40):
        with db.connect() as c:
            c.execute(
                "INSERT INTO paper_trades (id, position_id, symbol, strategy_name, timeframe, side, outcome, "
                "entry_price, exit_price, quantity, realized_pnl_usd, realized_pnl_pct, opened_at, closed_at, payload_json) "
                "VALUES (?, 'p', 'UBER', 'x', '1d', 'buy', 'target_1', 100, 120, 10, 200, 20, ?, ?, ?)",
                (f"t{i}", NOW.isoformat(), NOW.isoformat(), json.dumps(EXPLORATION_TAGS)),
            )
    assert compute_verdicts(db, settings) == before
    assert before["x:1d"]["passed"] is False
    state = RuntimeStateRepository(db)
    refresh_verdicts(db, settings, state)
    assert json.loads(state.get(VERDICTS_KEY))["verdicts"] == before


def test_exploration_trades_never_demote_or_keep_a_strategy() -> None:
    from app.performance.strategy_performance import demoted_strategy_names

    def trade(pnl, tags, day):
        return SimpleNamespace(
            strategy_name="x",
            realized_pnl_usd=pnl,
            closed_at=f"2026-10-{day:02d}T15:00:00+00:00",
            payload=dict(tags),
        )

    losing = [trade(-10.0, EXPLORATION_TAGS, 1 + i % 20) for i in range(25)]
    assert demoted_strategy_names(losing, min_trades=20) == set()
    assert demoted_strategy_names([trade(-10.0, {}, 1 + i % 20) for i in range(25)], min_trades=20) == {"x"}


def test_the_scorecard_paper_record_query_leaves_exploration_out() -> None:
    from app.dashboard.scorecard import scorecard_sql

    sql = scorecard_sql()
    record = sql[sql.index("'paper_record'") : sql.index("'paper_equity'")]
    assert "not like '%\"paper_only\": true%'" in record  # the tag an exploration trade carries
    assert json.dumps(EXPLORATION_TAGS).startswith('{"paper_only": true')


def test_a_failing_lane_fails_closed_to_the_gate_s_own_refusal(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(exploration_module.ExplorationLane, "_room", boom)
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", EXPLORE)
    assert _blockers(auto, candidate, paper_pass) == ["strategy_lacks_oos_evidence"]
    assert paper_pass.tags(candidate) == {}
    assert paper_pass.exploration.skip == "error_RuntimeError"  # counted in the funnel


def test_the_lane_is_never_on_outside_paper_and_reads_nothing_when_off(tmp_path) -> None:
    auto, state = _auto(tmp_path, eto=_etoro())  # lane off (the tests' default)
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", EXPLORE)
    assert not paper_pass.exploration.enabled() and not paper_pass.exploration.likely(candidate)
    assert paper_pass.exploration_blockers(candidate, ["strategy_lacks_oos_evidence"]) is None
    assert state.many == [] and auto.executions.counts == []
    assert not PaperOnlyPass(None).exploration.enabled()


def test_a_refusal_the_lane_stood_aside_from_says_why(tmp_path) -> None:
    """2026-11-13: 86 evidence-only refusals in two sessions, 2 exploration entries. The safety
    check runs before the readiness drop, so the funnel and log now say whether a refused
    candidate was tradeable at all and why the lane stood aside (observability only)."""

    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    watch = _scan_candidate("UBER", EXPLORE, 95.0).model_copy(
        update={"metadata": {"alert_eligible": False, "timeframe": "1d"}}
    )
    off_lane = _scan_candidate("ADP", EXPLORE, 90.0).model_copy(
        update={"timeframe": "30m", "metadata": {"alert_eligible": True, "timeframe": "30m"}}
    )
    response = _response(watch, off_lane)
    assert auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False) == 0
    assert proposals.created == []
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    refused = ["strategy_lacks_oos_evidence"]
    assert blocked == [
        {
            "origin": "intraday_scan",
            "symbol": "UBER",
            "blockers": refused,
            "not_ready": "not_alert_eligible",
            "exploration_skipped": "not_ready_not_alert_eligible",
        },
        {
            "origin": "intraday_scan",
            "symbol": "ADP",
            "blockers": refused,
            "exploration_skipped": "timeframe_30m",
        },
    ]
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][0]
    assert funnel["safety_blocked"] == 2
    assert funnel["safety_blocked:strategy_lacks_oos_evidence"] == 2
    assert funnel["safety_blocked_not_ready:not_alert_eligible"] == 1
    assert funnel["exploration_lane_skipped:not_ready_not_alert_eligible"] == 1
    assert funnel["exploration_lane_skipped:timeframe_30m"] == 1


def test_the_lane_names_each_reason_it_stands_aside(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    lane = PaperOnlyPass(auto).exploration
    candidate = _candidate("UBER", EXPLORE)
    assert lane.blockers(candidate, ["strategy_lacks_oos_evidence", "x"]) is None
    assert lane.skip == "other_blockers"
    assert lane.blockers(candidate, []) is None and lane.skip == "other_blockers"
    assert lane.ineligible(candidate) is None and lane.eligible(candidate)
    assert lane.blockers(candidate, ["strategy_lacks_oos_evidence"]) is not None
    assert lane.skip is None  # the lane applied: its own verdict, no skip
    off, _ = _auto(tmp_path)  # the tests' default: lane off
    assert PaperOnlyPass(off).exploration.ineligible(candidate) == "lane_off"
