"""A candidate the exploration lane refused only for the tick's spent budget is offered again at
the start of the next tick, through every gate (2026-11-13: 4 of 5 refusals that day)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.performance import strategy_evidence
from app.workflow import exploration_deferred as deferred
from app.workflow import paper_only as paper_only_module
from app.workflow.exploration import ExplorationLane
from app.workflow.operations import auto_propose_candidates
from tests.test_paper_exploration_lane import (
    BYPASSED,
    EXPLORATION_TAGS,
    EXPLORE,
    _lane,
    _lane_workflow,
    _tags,
)
from tests.test_paper_only_pass import _response, _scan_candidate


@pytest.fixture(autouse=True)
def _empty_store():
    deferred._KEPT.clear()
    yield
    deferred._KEPT.clear()


def _spent(monkeypatch, spent: bool) -> None:
    monkeypatch.setattr(paper_only_module, "_budget_spent", lambda: spent)


def test_a_late_refusal_is_proposed_at_the_next_tick_start(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    _spent(monkeypatch, True)  # the scan used its whole budget
    response = _response(_scan_candidate("UBER", EXPLORE, 95.0))
    assert auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False) == 0
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][0]
    assert funnel["safety_blocked:exploration_lane_cadence_budget_spent"] == 1
    assert funnel["exploration_deferred_kept"] == 1 and deferred.kept_count() == 1

    _spent(monkeypatch, False)  # the next tick, before maintenance and the scans
    assert deferred.run_deferred_exploration(workflow) == 1
    (request,) = proposals.created
    assert request.symbol == "UBER"
    assert _tags(request) == {**EXPLORATION_TAGS, BYPASSED: ["strategy_lacks_oos_evidence"]}
    assert request.metadata["proposal_origin"] == deferred.ORIGIN
    assert deferred.kept_count() == 0 and deferred.run_deferred_exploration(workflow) == 0


def test_the_next_tick_s_pass_keeps_every_gate(tmp_path, monkeypatch) -> None:
    """The kept candidate meets the lane's rules again: here its daily cap is full by then."""

    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    _spent(monkeypatch, True)
    response = _response(_scan_candidate("UBER", EXPLORE, 95.0))
    auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False)
    _spent(monkeypatch, False)
    workflow.settings.paper_exploration_lane_max_new_per_day = 0
    auto.settings.paper_exploration_lane_max_new_per_day = 0
    assert deferred.run_deferred_exploration(workflow) == 0
    assert proposals.created == []
    blocked = [p for e, p in workflow.run_logs.items if e == "auto_proposal_safety_blocked"]
    assert blocked[-1]["blockers"] == ["exploration_lane_daily_cap"]
    assert deferred.kept_count() == 0  # its own refusals are never kept again


def test_a_paused_bot_proposes_nothing_and_drops_what_was_kept(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    deferred.keep(_scan_candidate("UBER", EXPLORE, 95.0), "intraday_scan")
    workflow.automation = SimpleNamespace(scan_blockers=lambda: ["automation_paused"])
    assert deferred.run_deferred_exploration(workflow) == 0
    assert proposals.created == [] and deferred.kept_count() == 0


def test_the_store_keeps_a_pass_s_best_fresh_candidates_once(monkeypatch) -> None:
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(deferred.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(ExplorationLane, "likely", lambda self, c: True)
    seen = []
    monkeypatch.setattr(
        "app.workflow.operations.auto_propose_candidates",
        lambda service, response, **kw: seen.append(([c.symbol for c in response.candidates], kw))
        or 0,
    )

    def c(symbol, **extra):
        return SimpleNamespace(symbol=symbol, strategy_name="s", timeframe="5m", **extra)

    assert deferred.keep(c("AAA"), "intraday_scan")
    clock.now += deferred.MAX_AGE_SECONDS + 1  # AAA is stale by the next keep
    assert [deferred.keep(c(s), "swing") for s in ("BBB", "CCC", "DDD", "EEE")] == [
        True, True, True, False  # a pass runs best-first: EEE ranked below the three kept
    ]
    newer = c("CCC", price=2)
    assert deferred.keep(newer, "swing")  # the newer copy, in its place
    assert not deferred.keep(c("FFF"), deferred.ORIGIN)  # the offering pass keeps nothing
    assert deferred.kept_count() == deferred.MAX_KEPT == 3
    service = SimpleNamespace(run_logs=None, auto_trading=None)
    deferred.run_deferred_exploration(service)
    ((symbols, kw),) = seen
    assert symbols == ["BBB", "CCC", "DDD"] and kw["origin"] == deferred.ORIGIN
    assert kw["notify"] is True and kw["paper_only"] is not None

    deferred.keep(c("AAA"), "intraday_scan")
    clock.now += deferred.MAX_AGE_SECONDS + 1  # too old by the next tick: dropped
    assert deferred.run_deferred_exploration(service) == 0
    assert len(seen) == 1 and deferred.kept_count() == 0


def test_one_pass_keeps_its_three_best_ranked_late_candidates(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)
    workflow, _proposals = _lane_workflow(tmp_path, auto)
    _spent(monkeypatch, True)
    ranked = [_scan_candidate(s, EXPLORE, score) for s, score in zip("ABCDE", (99, 95, 90, 85, 80), strict=True)]
    auto_propose_candidates(workflow, _response(*ranked), origin="swing_scan", notify=False)
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][0]
    assert funnel["exploration_deferred_kept"] == 3
    assert [entry[1].symbol for entry in deferred._KEPT] == ["A", "B", "C"]


def test_a_kept_candidate_never_becomes_an_etoro_copyable_entry(tmp_path, monkeypatch) -> None:
    """Review 2026-11-13: if its verdict passes by the next tick, it is no longer an
    exploration candidate, and the offering pass takes only those: nothing is proposed."""

    auto, _ = _lane(tmp_path)
    workflow, proposals = _lane_workflow(tmp_path, auto)
    _spent(monkeypatch, True)
    response = _response(_scan_candidate("UBER", EXPLORE, 95.0))
    auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False)
    assert deferred.kept_count() == 1
    _spent(monkeypatch, False)
    monkeypatch.setattr(strategy_evidence, "evidence_blocker", lambda *a, **k: None)  # passes now
    assert deferred.run_deferred_exploration(workflow) == 0
    assert proposals.created == [] and deferred.kept_count() == 0


def test_a_tick_with_the_ledger_cycle_due_leaves_them_for_the_next(monkeypatch) -> None:
    offered = []
    monkeypatch.setattr(
        "app.workflow.operations.auto_propose_candidates",
        lambda *a, **k: offered.append(a) or 0,
    )
    deferred.keep(SimpleNamespace(symbol="AAA", strategy_name="s", timeframe="5m"), "swing")
    busy = SimpleNamespace(run_logs=None, auto_trading=None, _ledger_cycle_due=lambda: True)
    assert deferred.run_deferred_exploration(busy) == 0
    assert offered == [] and deferred.kept_count() == 1  # still kept for the next tick


def test_a_failure_never_escapes_the_tick(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.workflow.operations.auto_propose_candidates", boom)
    monkeypatch.setattr(ExplorationLane, "likely", lambda self, c: True)
    logs = []
    deferred.keep(SimpleNamespace(symbol="AAA", strategy_name="s", timeframe="5m"), "swing")
    service = SimpleNamespace(run_logs=SimpleNamespace(log=lambda e, p: logs.append((e, p))))
    assert deferred.run_deferred_exploration(service) == 0
    assert logs == [("exploration_deferred_failed", {"error": "database unavailable"})]


def test_the_scheduler_offers_them_after_the_pause_check(tmp_path, monkeypatch) -> None:
    from app.workflow import service as workflow_module
    from tests.test_automation_auto_recover import CLEAN, DIRTY, _build

    calls: list[object] = []
    monkeypatch.setattr(workflow_module, "run_deferred_exploration", calls.append)
    paused, automation, *_ = _build(tmp_path / "p", recon_results=[DIRTY, DIRTY, DIRTY])
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)
    paused.run_scheduled_tasks()
    assert calls == []  # paused, and not auto-recovered: nothing offered
    running, *_ = _build(tmp_path / "r", recon_results=[CLEAN, CLEAN, CLEAN])
    running.run_scheduled_tasks()
    assert calls == [running]
