"""The exploration lane's focus and its two entries a pass (operator 2026-11-22: "Focus practice
on intraday"; app/workflow/exploration.py, app/workflow/exploration_deferred.py).

* the production defaults: the focus keys, 2 entries a pass, 5 kept for the next tick;
* a key outside a set focus is refused until 15:30 New York (``focus_only``) and never takes the
  lane's last four open places (``focus_reserve``); a focus key meets the lane's rules unchanged;
  an empty focus is the lane as it was;
* a short is named by its short key, a long never by it;
* a pass considers its focus candidates before its other exploration candidates, makes at most
  two exploration entries and spends at most one unattended check more than that;
* the next-tick pass starts no exploration entry later than its window into the tick, and before
  15:30 New York only focus candidates are kept for it.

Paper only: nothing here reaches the eToro mirror (tests/test_paper_exploration_lane.py re-runs
every reader of the paper-only tag for exploration entries, which these still are).

No wall clock: the pass's clocks are patched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.workflow.exploration as exploration_module
import app.workflow.paper_only as paper_only_module
from app.automation.policy_snapshot import effective_execution_policy
from app.workflow import exploration_deferred as deferred
from app.workflow.exploration import FOCUS_RESERVED_OPEN, SPARE_CHECKS, lane_focus
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import PaperOnlyPass
from tests.conftest import make_settings
from tests.test_paper_exploration_lane import (
    BYPASSED,
    EXPLORATION_TAGS,
    EXPLORE,
    PASSING,
    _blockers,
    _lane_workflow,
    _tags,
    _xrow,
)
from tests.test_paper_only_pass import (
    BOOK,
    EXECUTED,
    NOW,
    _auto,
    _Broker,
    _candidate,
    _Executions,
    _response,
    _scan_candidate,
)
from tests.test_paper_shorts_admission import _short, _shorts

OTHER = "pullback_trend"  # no verdict at all: refused by the evidence gate, outside the focus
FOCUS_1D = [f"{EXPLORE}:1d"]
LATE = datetime(2026, 10, 21, 19, 30, tzinfo=UTC)  # 15:30 New York (summer time)
PRODUCTION_FOCUS = [
    "gap_continuation_fade:15m",
    "vwap_reclaim:short:5m",
    "vwap_reclaim:short:10m",
    "vwap_reclaim:short:15m",
    "intraday_vwap_trend:short:5m",
    "intraday_vwap_trend:short:10m",
    "intraday_vwap_trend:short:15m",
]


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    import app.risk.context as risk_context

    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(risk_context, "utc_now", lambda: NOW)
    monkeypatch.setattr(exploration_module, "utc_now", lambda: NOW)  # 11:00 New York
    monkeypatch.setattr(paper_only_module, "_LOGGED", {})
    deferred._KEPT.clear()
    yield
    deferred._KEPT.clear()


def _at(monkeypatch, moment) -> None:
    monkeypatch.setattr(exploration_module, "utc_now", lambda: moment)


def _focus(tmp_path, focus=FOCUS_1D, **settings):
    """The lane on with ``focus``; everything else at its production default (2 a pass)."""

    return _auto(
        tmp_path, paper_exploration_lane_enabled=True, paper_exploration_lane_focus=focus, **settings
    )


# --- the defaults --------------------------------------------------------------------------------


def test_the_production_defaults(tmp_path) -> None:
    from app.runtime_settings import AppSettings

    fields = AppSettings.model_fields
    assert fields["paper_exploration_lane_max_per_pass"].default == 2
    settings = make_settings(tmp_path)
    assert settings.paper_exploration_lane_focus == PRODUCTION_FOCUS
    assert lane_focus(settings) == frozenset(PRODUCTION_FOCUS)
    assert (FOCUS_RESERVED_OPEN, SPARE_CHECKS) == (4, 1)
    assert (deferred.MAX_KEPT, deferred.ENTRY_WINDOW_SECONDS) == (5, 60.0)
    # The caps on risk and on open and daily entries are the lane's old ones.
    assert settings.paper_exploration_lane_risk_pct == 0.25
    assert settings.paper_exploration_lane_max_open == 8
    assert settings.paper_exploration_lane_max_new_per_day == 12
    # The boot snapshot shows both, so a deploy can be checked.
    policy = effective_execution_policy(settings)
    assert policy["paper_exploration_lane_focus"] == PRODUCTION_FOCUS
    assert policy["paper_exploration_lane_max_per_pass"] == 2


def test_the_focus_is_a_comma_separated_setting_read_in_lower_case(tmp_path) -> None:
    settings = make_settings(tmp_path, paper_exploration_lane_focus="VWAP_Reclaim:Short:5M, x:1d ,")
    assert settings.paper_exploration_lane_focus == ["vwap_reclaim:short:5m", "x:1d"]
    assert make_settings(tmp_path, paper_exploration_lane_focus="").paper_exploration_lane_focus == []
    assert lane_focus(SimpleNamespace()) == frozenset()  # absent: no focus


# --- a key outside the focus ---------------------------------------------------------------------


def test_a_key_outside_the_focus_waits_for_1530_new_york(tmp_path, monkeypatch) -> None:
    auto, _ = _focus(tmp_path, focus=[f"{EXPLORE}:15m"])
    focus_key = _candidate("UBER", EXPLORE, timeframe="15m")
    other_daily = _candidate("V", EXPLORE, timeframe="1d")  # the strategy, another timeframe
    other_intraday = _candidate("ADP", OTHER, timeframe="15m")  # the timeframe, another strategy
    # 11:00 New York: the focus key meets the lane's rules; the others wait.
    assert _blockers(auto, focus_key) == []
    assert _blockers(auto, other_daily) == ["exploration_lane_focus_only"]
    assert _blockers(auto, other_intraday) == ["exploration_lane_focus_only"]
    # 15:29: still waiting. 15:30: a daily key outside the focus is admitted; every intraday key,
    # in the focus or not, is past the lane's last intraday entry.
    _at(monkeypatch, LATE - timedelta(minutes=1))
    assert _blockers(auto, other_daily) == ["exploration_lane_focus_only"]
    _at(monkeypatch, LATE)
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, other_daily, paper_pass) == []
    assert paper_pass.tags(other_daily) == {
        **EXPLORATION_TAGS,
        BYPASSED: ["strategy_lacks_oos_evidence"],
        "timeframe": "1d",
    }
    late = ["exploration_lane_intraday_entry_too_late"]
    assert _blockers(auto, other_intraday) == late and _blockers(auto, focus_key) == late


def test_an_empty_focus_is_the_lane_as_it_was(tmp_path) -> None:
    auto, _ = _focus(tmp_path, focus=[])
    paper_pass = PaperOnlyPass(auto)
    for candidate in (_candidate("UBER", EXPLORE), _candidate("V", OTHER, timeframe="15m")):
        assert paper_pass.exploration.focused(candidate)
        assert _blockers(auto, candidate) == []


def test_the_focus_never_admits_what_the_lane_does_not(tmp_path) -> None:
    """A focus key is still an exploration candidate only when the evidence gate refused it and
    nothing else did: an eToro-eligible key in the focus stays eToro-eligible, untagged."""

    auto, _ = _focus(tmp_path, focus=[f"{PASSING}:1d", f"{EXPLORE}:1w"])
    paper_pass = PaperOnlyPass(auto)
    passing = _candidate("UBER", PASSING)
    assert _blockers(auto, passing, paper_pass) == [] and paper_pass.tags(passing) == {}
    weekly = _candidate("V", EXPLORE, timeframe="1w")  # not a lane timeframe: the gate's refusal
    assert _blockers(auto, weekly, paper_pass) == ["strategy_lacks_oos_evidence"]
    off, _ = _auto(tmp_path / "off", paper_exploration_lane_enabled=False, paper_exploration_lane_focus=FOCUS_1D)
    assert _blockers(off, _candidate("UBER", EXPLORE)) == ["strategy_lacks_oos_evidence"]


def test_a_short_is_named_by_its_short_key(tmp_path) -> None:
    lane = dict(paper_exploration_lane_enabled=True, short_verdicts={})
    short = _short("TSLA", EXPLORE, timeframe="15m")
    long = _candidate("UBER", EXPLORE, timeframe="15m")
    shorts, _ = _shorts(tmp_path / "s", paper_exploration_lane_focus=[f"{EXPLORE}:short:15m"], **lane)
    paper_pass = PaperOnlyPass(shorts)
    assert paper_pass.exploration.key(short) == f"{EXPLORE}:short:15m"
    assert paper_pass.exploration.key(long) == f"{EXPLORE}:15m"
    assert _blockers(shorts, short, paper_pass) == []
    assert paper_pass.tags(short)["paper_only_reason"] == "exploration"
    assert paper_pass.tags(short)["opens_short"] is True
    assert _blockers(shorts, long, paper_pass) == ["exploration_lane_focus_only"]
    longs, _ = _shorts(tmp_path / "l", paper_exploration_lane_focus=[f"{EXPLORE}:15m"], **lane)
    assert _blockers(longs, short) == ["exploration_lane_focus_only"]
    assert _blockers(longs, long) == []


def test_a_key_outside_the_focus_never_takes_the_lane_s_last_four_open_places(
    tmp_path, monkeypatch
) -> None:
    _at(monkeypatch, LATE)  # 15:30 New York: a daily key outside the focus may be admitted
    old = NOW - timedelta(days=3)
    names = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "CVX", "KO", "PEP"]

    def blockers(held, strategy):
        auto, _ = _focus(
            tmp_path / f"{held}-{strategy}",
            broker=_Broker(names[:held]),
            executions=_Executions(rows=[_xrow(s, created_at=old) for s in names[:held]]),
            **BOOK,
        )
        return _blockers(auto, _candidate("UBER", strategy))

    assert blockers(3, OTHER) == []
    assert blockers(4, OTHER) == ["exploration_lane_focus_reserve"]  # 8 open places - 4 kept free
    assert blockers(7, OTHER) == ["exploration_lane_focus_reserve"]
    # A focus key has all eight, and then the lane's own cap.
    assert blockers(4, EXPLORE) == [] and blockers(7, EXPLORE) == []
    assert blockers(8, EXPLORE) == ["exploration_lane_positions_cap"]


# --- the pass ------------------------------------------------------------------------------------


def test_a_pass_makes_two_exploration_entries_its_focus_candidates_first(
    tmp_path, monkeypatch
) -> None:
    _at(monkeypatch, LATE)  # every candidate here is admissible: only the order and the limit decide
    auto, _ = _focus(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    response = _response(
        _scan_candidate("UBER", OTHER, 99.0),  # ranked first, outside the focus
        _scan_candidate("V", EXPLORE, 97.0),
        _scan_candidate("ADP", EXPLORE, 96.0),
        _scan_candidate("MA", EXPLORE, 95.0),
        _scan_candidate("HD", PASSING, 60.0),  # eToro-eligible: before every exploration candidate
    )
    assert auto_propose_candidates(workflow, response, origin="swing_scan", notify=False) == 3
    assert [(r.symbol, r.strategy_name) for r in proposals.created] == [
        ("HD", PASSING),
        ("V", EXPLORE),
        ("ADP", EXPLORE),
    ]
    assert [_tags(r) for r in proposals.created][1:] == [
        {**EXPLORATION_TAGS, BYPASSED: ["strategy_lacks_oos_evidence"]}
    ] * 2
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert blocked == [
        {"origin": "swing_scan", "symbol": symbol, "blockers": ["exploration_lane_pass_limit"]}
        for symbol in ("MA", "UBER")  # the third focus candidate, then the one outside the focus
    ]


def test_the_order_puts_focus_candidates_before_the_lane_s_others(tmp_path) -> None:
    auto, _ = _focus(tmp_path)
    paper_pass = PaperOnlyPass(auto)
    other, focus, passing = _candidate("UBER", OTHER), _candidate("V", EXPLORE), _candidate("HD", PASSING)
    assert paper_pass.order([other, focus, passing]) == [passing, focus, other]
    plain = PaperOnlyPass(_focus(tmp_path / "none", focus=[])[0])
    assert plain.order([other, focus, passing]) == [passing, other, focus]  # no focus: ranks kept


def test_the_lane_spends_one_unattended_check_more_than_its_entries(tmp_path) -> None:
    calls = []
    auto, _ = _focus(tmp_path, auto_blockers=["outside_regular_market_hours"])
    stub = auto.candidate_blockers
    auto.candidate_blockers = lambda c: calls.append(c.symbol) or stub(c)
    paper_pass = PaperOnlyPass(auto)
    refused = ["exploration_lane_not_automatic", "exploration_lane_auto_outside_regular_market_hours"]
    for symbol in ("UBER", "V", "ADP"):  # 2 entries a pass + 1
        assert _blockers(auto, _candidate(symbol, EXPLORE), paper_pass) == refused
    assert _blockers(auto, _candidate("MA", EXPLORE), paper_pass) == ["exploration_lane_check_limit"]
    assert calls == ["UBER", "V", "ADP"]


# --- the next-tick pass ----------------------------------------------------------------------------


def test_before_1530_only_focus_candidates_are_kept_for_the_next_tick(tmp_path, monkeypatch) -> None:
    auto, _ = _focus(tmp_path)
    workflow, _proposals = _lane_workflow(tmp_path, auto)
    monkeypatch.setattr(paper_only_module, "_budget_spent", lambda: True)  # a full scan
    response = _response(_scan_candidate("UBER", OTHER, 99.0), _scan_candidate("V", EXPLORE, 90.0))
    assert auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False) == 0
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][0]
    assert funnel["safety_blocked:exploration_lane_focus_only"] == 1
    assert funnel["safety_blocked:exploration_lane_cadence_budget_spent"] == 1
    assert funnel["exploration_deferred_kept"] == 1
    assert [entry[1].symbol for entry in deferred._KEPT] == ["V"]


def test_the_next_tick_pass_starts_no_entry_after_its_window(tmp_path, monkeypatch) -> None:
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(deferred.time, "monotonic", lambda: clock.now)
    auto, _ = _focus(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)

    def place(*_a, **_k):  # an entry takes about 40 s from its proposal to its fill
        clock.now += 40.0
        return EXECUTED

    auto.approve_enqueue_execute = place
    kept = [_scan_candidate(symbol, EXPLORE, 90.0) for symbol in ("UBER", "V", "ADP")]

    def offer(elapsed):
        for candidate in kept:
            assert deferred.keep(candidate, "intraday_scan")
        workflow._tick_started_at = clock.now - elapsed
        return deferred.run_deferred_exploration(workflow)

    # 5 s into the tick: the first entry starts, the second at 45 s; a third is past the pass limit.
    assert offer(5.0) == 2
    assert [r.symbol for r in proposals.created] == ["UBER", "V"]
    assert {r.metadata["proposal_origin"] for r in proposals.created} == {deferred.ORIGIN}
    # 25 s into the tick: the first entry ends at 65 s, past the window, so no second one starts.
    proposals.created.clear()
    workflow.run_logs.items.clear()
    monkeypatch.setattr(paper_only_module, "_LOGGED", {})
    assert offer(25.0) == 1
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert [(p["symbol"], p["blockers"]) for p in blocked] == [
        ("V", ["exploration_lane_pass_time_spent"]),
        ("ADP", ["exploration_lane_pass_time_spent"]),
    ]
    assert deferred.kept_count() == 0  # its own refusals are never kept again
    # Exactly at the window: nothing starts.
    proposals.created.clear()
    assert offer(deferred.ENTRY_WINDOW_SECONDS) == 0 and proposals.created == []
    # Outside a scheduled tick (a manual run) there is no window.
    proposals.created.clear()
    for candidate in kept:
        deferred.keep(candidate, "intraday_scan")
    del workflow._tick_started_at
    assert deferred.run_deferred_exploration(workflow) == 2


def test_a_scan_s_own_pass_has_no_entry_window(tmp_path) -> None:
    auto, _ = _focus(tmp_path)
    assert PaperOnlyPass(auto).past_entry_deadline() is False
    assert PaperOnlyPass(auto, entry_deadline=0.0).past_entry_deadline() is True
