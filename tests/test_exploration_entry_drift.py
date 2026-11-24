"""The exploration lane fails fast on an entry the market has already left (2026-11-24;
app/workflow/exploration.py rule 7, ``ExecutionCoordinator.planned_entry_drift_bps``).

On 11-24 the next-tick pass proposed an AMD short 6 minutes after its scan read the price; the
execution check refused it 33 s later (``entry_drift_too_large``, 150 bps) and the pass's three
other focus candidates were out of time. The lane now reads one fresh quote before it spends
anything on a candidate and refuses a drifted one itself, so the next candidate gets the pass's
time. The limit is the execution check's own and that check is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.execution.coordinator import quote_entry_drift
from app.live_signal_schema import MarketQuote
from app.workflow import exploration_deferred as deferred
from app.workflow import paper_only as paper_only_module
from app.workflow.exploration import MAX_DRIFT_READS, PREFIX
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import PaperOnlyPass
from tests.test_execution_coordinator_alpaca_routing import FakeAlpacaClient, queued_app
from tests.test_paper_exploration_lane import EXPLORE, _blockers, _lane, _lane_workflow
from tests.test_paper_only_pass import _candidate, _response, _scan_candidate
from tests.test_workflow_service import FakeLogs

DRIFTED = [PREFIX + "entry_drifted"]
LIMIT = 35.0  # execution_max_entry_drift_bps


@pytest.fixture(autouse=True)
def _empty_store():
    deferred._KEPT.clear()
    yield
    deferred._KEPT.clear()


def _reads(auto, drift):
    """Give the pass's coordinator a drift read: ``drift`` a number, None, an exception or a
    ``{symbol: bps}`` map (a symbol it does not name has not moved)."""

    seen: list[str] = []

    def read(candidate):
        seen.append(candidate.symbol)
        value = drift.get(candidate.symbol, 0.0) if isinstance(drift, dict) else drift
        if isinstance(value, Exception):
            raise value
        return value

    auto.execution.planned_entry_drift_bps = read
    auto.logs = FakeLogs()
    return seen


# --- the lane --------------------------------------------------------------------------------


def test_a_drifted_entry_is_refused_before_anything_is_spent_on_it(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    seen = _reads(auto, 150.0)
    checks: list[str] = []
    auto.candidate_blockers = lambda candidate: checks.append(candidate.symbol) or []
    paper_pass = PaperOnlyPass(auto)
    candidate = _candidate("UBER", EXPLORE)
    assert _blockers(auto, candidate, paper_pass) == DRIFTED
    assert seen == ["UBER"] and checks == [] and paper_pass.exploration.checks == 0
    assert paper_pass.tags(candidate) == {} and paper_pass.exploration.admitted(candidate) is None
    assert auto.logs.items == [
        (
            "exploration_entry_drifted",
            {"symbol": "UBER", "key": f"{EXPLORE}:1d", "drift_bps": 150.0, "limit_bps": LIMIT},
        )
    ]


@pytest.mark.parametrize("drift", [0.0, LIMIT, None, RuntimeError("quote read failed"), "n/a"])
def test_an_entry_within_the_limit_or_unreadable_is_admitted_as_before(tmp_path, drift) -> None:
    """At the limit is not over it (the execution check refuses only above it), and what cannot
    be told refuses nothing here: the execution check decides."""

    auto, _ = _lane(tmp_path)
    seen = _reads(auto, drift)
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, _candidate("UBER", EXPLORE), paper_pass) == []
    assert seen == ["UBER"] and paper_pass.exploration.checks == 1
    assert auto.logs.items == []


def test_without_a_coordinator_read_or_a_limit_nothing_is_read(tmp_path) -> None:
    auto, _ = _lane(tmp_path)  # the test coordinator has no drift read
    assert _blockers(auto, _candidate("UBER", EXPLORE), PaperOnlyPass(auto)) == []
    for limit in (0.0, None):
        auto, _ = _lane(tmp_path / f"limit-{limit}")
        seen = _reads(auto, 150.0)
        auto.settings.execution_max_entry_drift_bps = limit
        assert _blockers(auto, _candidate("UBER", EXPLORE), PaperOnlyPass(auto)) == []
        assert seen == []


def test_a_candidate_the_lane_s_room_refuses_costs_no_quote_read(tmp_path, monkeypatch) -> None:
    auto, _ = _lane(tmp_path)
    seen = _reads(auto, 150.0)
    monkeypatch.setattr(paper_only_module, "_budget_spent", lambda: True)
    refused = _blockers(auto, _candidate("UBER", EXPLORE), PaperOnlyPass(auto))
    assert refused == [PREFIX + "cadence_budget_spent"] and seen == []


def test_a_pass_reads_at_most_its_quota_of_fresh_quotes(tmp_path) -> None:
    auto, _ = _lane(tmp_path)
    seen = _reads(auto, 150.0)
    paper_pass = PaperOnlyPass(auto)
    symbols = ["UBER", "V", "ADP", "COST", "PEP", "KO"]
    verdicts = [_blockers(auto, _candidate(symbol, EXPLORE), paper_pass) for symbol in symbols]
    assert verdicts[:MAX_DRIFT_READS] == [DRIFTED] * MAX_DRIFT_READS
    assert seen == symbols[:MAX_DRIFT_READS]
    # Past the quota nothing is read: the candidate goes on to the unattended check as before.
    assert verdicts[MAX_DRIFT_READS] == [] and paper_pass.exploration.checks == 1


# --- the pass --------------------------------------------------------------------------------


def test_the_next_tick_pass_moves_on_from_a_drifted_candidate(tmp_path, monkeypatch) -> None:
    """11-24: the first kept candidate has run; the second is proposed in the same pass."""

    auto, _ = _lane(tmp_path)
    seen = _reads(auto, {"UBER": 150.0})
    workflow, proposals = _lane_workflow(tmp_path, auto)
    monkeypatch.setattr(paper_only_module, "_budget_spent", lambda: True)  # a full scan
    response = _response(
        _scan_candidate("UBER", EXPLORE, 95.0), _scan_candidate("V", EXPLORE, 90.0)
    )
    assert auto_propose_candidates(workflow, response, origin="intraday_scan", notify=False) == 0
    assert deferred.kept_count() == 2 and seen == []

    monkeypatch.setattr(paper_only_module, "_budget_spent", lambda: False)  # the next tick
    assert deferred.run_deferred_exploration(workflow) == 1
    (request,) = proposals.created
    assert request.symbol == "V" and seen == ["UBER", "V"]
    funnel = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"][-1]
    assert funnel["origin"] == deferred.ORIGIN and funnel["proposals_created"] == 1
    assert funnel["safety_blocked:exploration_lane_entry_drifted"] == 1
    assert deferred.kept_count() == 0  # a drifted candidate is not kept for another tick


# --- the coordinator's read -------------------------------------------------------------------


def _scan(symbol="NVDA", *, entry=120.0, current=None, timeframe="15m", asset_class="equity"):
    return SimpleNamespace(
        symbol=symbol,
        entry_price=entry,
        current_price=current,
        timeframe=timeframe,
        asset_class=asset_class,
        metadata={},
    )


def test_the_read_measures_what_the_execution_check_measures(tmp_path) -> None:
    quote = MarketQuote(symbol="NVDA", bid=129.99, ask=130.01, last_execution=130.0)
    assert quote_entry_drift(quote, 120.0) == (130.0, pytest.approx(833.33, abs=0.01))
    assert quote_entry_drift(MarketQuote(symbol="NVDA", ask=120.6), 120.0)[0] == 120.6
    assert quote_entry_drift(MarketQuote(symbol="NVDA"), 120.0) == (120.0, 0.0)  # no price: none

    # The same quote and plan: the read's number is over the limit and the check refuses.
    app, alpaca, _, queued = queued_app(tmp_path, alpaca=FakeAlpacaClient(price=130.0))
    coordinator = app.state.execution_coordinator
    assert coordinator.planned_entry_drift_bps(_scan()) == pytest.approx(833.33, abs=0.01)
    assert coordinator.planned_entry_drift_bps(_scan(entry=None, current=130.0)) == 0.0
    result = coordinator.process_queue_item(queued.id)
    assert "entry_drift_too_large" in result.validation_reason and alpaca.submitted_orders == []


def test_the_read_tells_nothing_it_cannot_judge(tmp_path) -> None:
    app, _, _, _ = queued_app(tmp_path, alpaca=FakeAlpacaClient(price=130.0, age=9999.0))
    stale = app.state.execution_coordinator
    assert stale.planned_entry_drift_bps(_scan()) is None  # quote_too_old: the check's own refusal
    app, alpaca, _, _ = queued_app(tmp_path / "fresh", alpaca=FakeAlpacaClient(price=130.0))
    coordinator = app.state.execution_coordinator
    assert coordinator.planned_entry_drift_bps(_scan(entry=None)) is None  # no planned price
    assert coordinator.planned_entry_drift_bps(_scan(entry=0.0, current=0.0)) is None
    alpaca.get_quote = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))
    assert coordinator.planned_entry_drift_bps(_scan()) is None  # a failed read
    assert alpaca.submitted_orders == []  # a read only
