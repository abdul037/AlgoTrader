"""Shorts step 3: admission of paper shorts in the auto-propose pass (docs/notes/paper_shorts.md).

* switch off (or any paper condition missing): a short candidate is judged, dropped, ordered,
  tagged and logged exactly as before, and nothing new is read (no short verdict, no Alpaca);
* a short's evidence is its own short verdict (``<strategy>:short:<tf>``), never the long one;
* (a) a short its verdict admits (``passed``, or the paper OOS bar) is a paper-only short:
  tagged ``paper_only_reason: "short"`` with ``opens_short`` / ``position_side`` and Alpaca's
  facts, sized at the gate's risk budget, after eToro-eligible entries, one a pass, under every
  paper-only rule; a hard-to-borrow, unshortable or unreadable name is refused;
* (b) a short refused only for its evidence is an exploration short under the lane's rules and
  shared caps (0.25 %, open and daily caps, 1 a pass, 15:30 cut-off, its timeframes, equities);
* the long-evidence execution blockers are bypassed for a short (recorded), every other one
  refuses; the unattended checks no longer refuse a short for being one only with the switch on;
* end to end: a scanned short becomes a SELL proposal sized from (stop - entry), tagged so the
  eToro live mirror refuses it.

No wall clock: the pass's clocks are patched.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.workflow.exploration as exploration_module
import app.workflow.paper_only as paper_only_module
from app.automation.reliability import (
    PAPER_NEAR_MISS,
    STRICT_VALID,
    auto_approval_tier_blockers,
    candidate_propose_drop_reason,
    proposal_quality_label,
)
from app.automation.unattended import PaperAutoTradingService
from app.broker.etoro_live_mirror import SHORT_NOT_ENABLED_REASON
from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.models.trade import OrderSide, TradeOrder
from app.performance.strategy_evidence import SHORT_VERDICTS_KEY
from app.risk.rules import sizing_risk_pct
from app.utils.time import utc_now
from app.workflow.operations import auto_propose_candidates
from app.workflow.paper_only import PaperOnlyPass
from app.workflow.paper_shorts import SHORT_BYPASSED_FIELD, short_evidence_blocker
from tests.test_paper_only_pass import (
    NOW,
    TAGS,
    _auto,
    _Broker,
    _candidate,
    _etoro,
    _Executions,
    _response,
    _row,
    _scan_candidate,
    _workflow,
)
from tests.test_paper_shorts_execution import ASSET, FakeShortAlpaca

SWITCH = dict(
    paper_short_entries_enabled=True,
    paper_broker="alpaca",
    broker_for_equities="alpaca",
    paper_oos_bar_enabled=True,
)
LANE = {"paper_exploration_lane_enabled": True}
SHORT_OOS = {"passed": False, "paper_oos_passed": True, "oos_expectancy_r": 0.065, "oos_trades": 138}
SHORT_FULL = {"passed": True, "paper_oos_passed": True, "oos_expectancy_r": 0.05}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    import app.risk.context as risk_context

    monkeypatch.setattr(paper_only_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(risk_context, "utc_now", lambda: NOW)
    monkeypatch.setattr(exploration_module, "utc_now", lambda: NOW)  # 11:00 New York
    monkeypatch.setattr(paper_only_module, "_LOGGED", {})


def _short_verdicts(verdicts=None, *, age_hours=1.0):
    computed = (utc_now() - timedelta(hours=age_hours)).isoformat()  # the gate reads the real clock
    payload = {"computed_at": computed, "research_only": True, "verdicts": verdicts or {}}
    return {SHORT_VERDICTS_KEY: json.dumps(payload)}


def _shorts(tmp_path, *, short_verdicts=None, alpaca=None, eto=None, **settings):
    values = {**SWITCH, **settings}
    state = {**_short_verdicts(short_verdicts if short_verdicts is not None else {"trend_following:short:1d": SHORT_OOS}), **(eto or {})}
    auto, store = _auto(tmp_path, eto=state, **values)
    auto.alpaca = alpaca if alpaca is not None else FakeShortAlpaca()
    return auto, store


def _short(symbol="TSLA", strategy="trend_following", *, timeframe="1d", ready=True):
    candidate = _candidate(symbol, strategy, timeframe=timeframe, ready=ready)
    candidate.signal_role = "entry_short"
    candidate.stop_loss = 105.0
    candidate.take_profit = 90.0
    return candidate


def _blockers(auto, candidate, paper_pass=None):
    return auto.candidate_proposal_blockers(candidate, paper_only=paper_pass)


# --- switch off: identical ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "off",
    [
        {"paper_short_entries_enabled": False},
        {"execution_mode": "live"},
        {"enable_real_trading": True},
        {"paper_broker": "self_simulated"},
        {"broker_for_equities": "etoro"},
    ],
)
def test_with_the_switch_off_a_short_is_handled_exactly_as_before(tmp_path, off) -> None:
    alpaca = FakeShortAlpaca()
    auto, state = _shorts(tmp_path, alpaca=alpaca, **LANE, **off)
    paper_pass = PaperOnlyPass(auto)
    short = _short()  # its LONG verdict (trend_following:1d) fails both bars
    assert _blockers(auto, short, paper_pass) == ["strategy_lacks_oos_evidence"]
    # The lane never takes it: dropped as a short, or the lane is off with the paper conditions.
    assert paper_pass.exploration.skip in {"not_ready_entry_short", "lane_off"}
    assert paper_pass.tags(short) == {} and paper_pass.risk_pct(short) is None
    assert paper_pass.drop_reason(short) == "entry_short" == candidate_propose_drop_reason(short)
    passing = _short("ADP", "inside_bar_narrow_range_breakout")  # its LONG verdict passes
    assert _blockers(auto, passing, paper_pass) == []  # then the loop drops it: entry_short
    assert paper_pass.order([short, passing, _candidate("UBER", "momentum_breakout")])[-1].symbol == "UBER"
    assert SHORT_VERDICTS_KEY not in state.reads and alpaca.fact_reads == 0  # nothing new read
    assert not paper_pass.shorts.enabled() and not paper_pass.shorts.applies(short)


def test_with_the_switch_off_the_loop_drops_every_short_as_before(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, paper_short_entries_enabled=False)
    workflow, proposals = _workflow(tmp_path, auto)
    short = _scan_short("ADP", "inside_bar_narrow_range_breakout")  # long verdict passes
    assert auto_propose_candidates(workflow, _response(short), origin="swing_scan", notify=False) == 0
    assert proposals.created == []
    (funnel,) = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"]
    assert funnel["dropped:entry_short"] == 1


def test_the_unattended_checks_refuse_a_short_only_while_the_switch_is_off(tmp_path) -> None:
    def service(**settings):
        from tests.conftest import make_settings

        return PaperAutoTradingService(
            settings=make_settings(tmp_path, **settings),
            proposal_service=None,
            execution_coordinator=None,
            automation=SimpleNamespace(execution_blockers=lambda: []),
            reconciliation=SimpleNamespace(account_verified=lambda: True),
            safety_state=SimpleNamespace(is_blacklisted=lambda _s: False, strategy_active=lambda _s: True),
            executions=None,
            run_logs=None,
            notifier=None,
            alpaca_client=None,
        )

    short = _short()
    short.metadata["backtest_validated"] = True
    off = service().candidate_blockers(short)
    assert "short_entries_disabled" in off and "proposal_quality_not_tradeable" in off
    on = service(**SWITCH).candidate_blockers(short)
    assert "short_entries_disabled" not in on and "proposal_quality_not_tradeable" not in on
    crypto = _short("BTCUSD")
    assert "short_equities_only" in service(**SWITCH).candidate_blockers(crypto)


def test_the_quality_label_and_the_drop_reason_take_a_short_only_when_allowed() -> None:
    short = _short()
    assert proposal_quality_label(short) == "not_tradeable"
    assert proposal_quality_label(short, allow_short=True) == STRICT_VALID
    assert candidate_propose_drop_reason(short) == "entry_short"
    assert candidate_propose_drop_reason(short, allow_short=True) is None
    near = _short()
    near.metadata.update(signal_classification=PAPER_NEAR_MISS)
    assert proposal_quality_label(near, allow_short=True) == PAPER_NEAR_MISS
    settings = SimpleNamespace(paper_auto_approval_tier="tier2_strict_valid", paper_auto_min_clean_supervised_lifecycles=0, **SWITCH, execution_mode="paper", enable_real_trading=False)
    assert "paper_auto_requires_strict_valid_quality" not in auto_approval_tier_blockers(settings=settings, candidate=short, lifecycles=[])
    settings.paper_short_entries_enabled = False
    assert "paper_auto_requires_strict_valid_quality" in auto_approval_tier_blockers(settings=settings, candidate=short, lifecycles=[])


# --- the short's own evidence ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "oos_bar", "blocker"),
    [
        (SHORT_FULL, False, None),
        (SHORT_OOS, True, None),  # today's trend_following:short:1d
        (SHORT_OOS, False, "strategy_lacks_oos_evidence"),
        ({"passed": False, "paper_oos_passed": False}, True, "strategy_lacks_oos_evidence"),
        (None, True, "strategy_lacks_oos_evidence"),
    ],
)
def test_a_short_s_evidence_is_its_own_short_verdict(verdict, oos_bar, blocker) -> None:
    settings = SimpleNamespace(paper_oos_bar_enabled=oos_bar)
    verdicts = {"trend_following:short:1d": verdict} if verdict else {}
    raw = _short_verdicts(verdicts)[SHORT_VERDICTS_KEY]
    assert short_evidence_blocker(settings, raw, strategy="trend_following", timeframe="1d") == blocker
    assert short_evidence_blocker(settings, raw, strategy="trend_following", timeframe="5m") == (
        "strategy_lacks_oos_evidence"
    )


def test_stale_unreadable_and_retired_short_evidence() -> None:
    settings = SimpleNamespace(paper_oos_bar_enabled=True)
    stale = _short_verdicts({"t:short:1d": SHORT_FULL}, age_hours=30)[SHORT_VERDICTS_KEY]
    assert short_evidence_blocker(settings, stale, strategy="t", timeframe="1d") == "strategy_evidence_stale"
    undated = json.dumps({"verdicts": {"t:short:1d": SHORT_FULL}})
    assert short_evidence_blocker(settings, undated, strategy="t", timeframe="1d") == "strategy_evidence_stale"
    assert short_evidence_blocker(settings, "{broken", strategy="t", timeframe="1d") == "strategy_lacks_oos_evidence"
    retired = "etf_mega_cap_relative_strength_rotation"
    assert short_evidence_blocker(settings, stale, strategy=retired, timeframe="1d") == "strategy_retired"


def test_a_long_verdict_never_admits_a_short(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, short_verdicts={})  # no short verdict at all
    paper_pass = PaperOnlyPass(auto)
    short = _short("ADP", "inside_bar_narrow_range_breakout")  # its LONG verdict passes
    assert _blockers(auto, short, paper_pass) == ["strategy_lacks_oos_evidence"]
    assert paper_pass.tags(short) == {}


# --- (a) a short its verdict admits ----------------------------------------------------------


def test_a_short_its_verdict_admits_is_a_tagged_paper_only_short(tmp_path) -> None:
    auto, _ = _shorts(tmp_path)
    paper_pass = PaperOnlyPass(auto)
    short = _short()
    assert _blockers(auto, short, paper_pass) == []
    tags = paper_pass.tags(short)
    assert {k: tags[k] for k in ("paper_only", "paper_only_reason", "opens_short", "position_side")} == {
        "paper_only": True,
        "paper_only_reason": "short",
        "opens_short": True,
        "position_side": "short",
    }
    assert tags["easy_to_borrow"] is True and tags["margin_requirement_pct"] == 30.0
    assert tags["borrow_cost_annual_pct"] == pytest.approx(0.30) and tags["shortable"] is True
    assert tags["proposal_quality"] == STRICT_VALID and tags["timeframe"] == "1d"
    assert tags[SHORT_BYPASSED_FIELD] == []
    assert paper_pass.risk_pct(short) == sizing_risk_pct(auto.settings, live=False)  # risk-sized
    assert paper_pass.notice(short)[0] == "Paper-only SHORT (sell to open; not copied to eToro)"
    assert paper_pass.is_paper_only(short)


def test_the_tagged_short_is_refused_by_the_etoro_mirror(tmp_path) -> None:
    from tests.test_etoro_live_room import _live

    auto, _ = _shorts(tmp_path)
    paper_pass = PaperOnlyPass(auto)
    short = _short("JPM")
    assert _blockers(auto, short, paper_pass) == []
    order = TradeOrder(
        symbol="JPM", side=OrderSide.SELL, amount_usd=5_000.0, proposed_price=100.0, stop_loss=105.0,
        take_profit=90.0, strategy_name="trend_following", metadata=paper_pass.tags(short),
    )
    mirror, _ = _live(tmp_path / "mirror")
    assert mirror.blockers(SimpleNamespace(id=None, order=order, signal=None), "alpaca")[0] == SHORT_NOT_ENABLED_REASON


@pytest.mark.parametrize(
    ("asset", "shorting", "blocker"),
    [
        ({"easy_to_borrow": False}, True, "paper_only_short_hard_to_borrow"),
        ({"shortable": False}, True, "paper_only_short_not_shortable"),
        ({"marginable": False}, True, "paper_only_short_not_marginable"),
        ({"maintenance_margin_requirement": None}, True, "paper_only_short_margin_requirement_missing"),
        ({}, False, "paper_only_short_account_shorting_disabled"),
    ],
)
def test_a_name_alpaca_will_not_lend_is_refused_before_any_proposal(tmp_path, asset, shorting, blocker) -> None:
    alpaca = FakeShortAlpaca(asset={**ASSET, **asset}, shorting=shorting)
    auto, _ = _shorts(tmp_path, alpaca=alpaca)
    assert _blockers(auto, _short()) == [blocker]


def test_unreadable_alpaca_facts_refuse_and_are_read_once_a_pass(tmp_path) -> None:
    failing = FakeShortAlpaca(fail_facts=True)
    auto, _ = _shorts(tmp_path, alpaca=failing)
    assert _blockers(auto, _short()) == ["paper_only_short_asset_unreadable"]
    alpaca = FakeShortAlpaca()
    auto, _ = _shorts(tmp_path / "ok", alpaca=alpaca)
    paper_pass = PaperOnlyPass(auto)
    _blockers(auto, _short(), paper_pass)
    paper_pass.tags(_short())
    assert alpaca.fact_reads == 1


def test_the_long_evidence_execution_blockers_are_bypassed_for_a_short_and_no_others(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, auto_blockers=["candidate_not_backtest_validated", "insufficient_strategy_closed_trade_evidence"])
    paper_pass = PaperOnlyPass(auto)
    short = _short()
    assert _blockers(auto, short, paper_pass) == []
    assert paper_pass.tags(short)[SHORT_BYPASSED_FIELD] == [
        "candidate_not_backtest_validated",
        "insufficient_strategy_closed_trade_evidence",
    ]
    hours, _ = _shorts(tmp_path / "hours", auto_blockers=["outside_regular_market_hours", "candidate_not_backtest_validated"])
    assert _blockers(hours, _short()) == [
        "paper_only_not_automatic",
        "paper_only_auto_outside_regular_market_hours",
    ]


def test_every_paper_only_rule_applies_to_a_short(tmp_path) -> None:
    held, _ = _shorts(tmp_path / "held", eto=_etoro(open_symbols=("TSLA",)))
    assert _blockers(held, _short()) == ["paper_only_etoro_live_symbol_held"]
    paper, _ = _shorts(tmp_path / "paper", executions=_Executions(rows=[_row("UBER", status="submitted")]))
    assert _blockers(paper, _short("UBER")) == ["paper_only_paper_symbol_held"]  # UBER: no group
    day, _ = _shorts(tmp_path / "day", executions=_Executions(["X"] * 13))
    assert _blockers(day, _short()) == ["paper_only_headroom_trades_per_day"]
    five = ("A", "B", "C", "D", "E")  # five OOS-only paper-only positions held
    full, _ = _shorts(
        tmp_path / "cap", broker=_Broker(five), executions=_Executions(rows=[_row(s, tagged=True) for s in five]),
    )
    assert _blockers(full, _short()) == ["paper_only_positions_cap"]  # shared with OOS-only longs


def test_one_paper_only_entry_a_pass_ordered_after_etoro_eligible_ones(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, **LANE)
    paper_pass = PaperOnlyPass(auto)
    explore = _short("V", "momentum_breakout")  # no short verdict: an exploration short
    short, oos, eligible = _short(), _candidate("UBER", "momentum_breakout"), _candidate("ADP", "inside_bar_narrow_range_breakout")
    ordered = paper_pass.order([explore, short, oos, eligible])
    assert [c.symbol for c in ordered] == ["ADP", "TSLA", "UBER", "V"]
    assert _blockers(auto, short, paper_pass) == []
    paper_pass.created(short)
    assert _blockers(auto, oos, paper_pass) == ["paper_only_pass_limit"]  # one paper-only a pass


# --- (b) an exploration short ------------------------------------------------------------------


def test_a_short_refused_only_for_its_evidence_is_an_exploration_short(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, short_verdicts={}, **LANE)
    paper_pass = PaperOnlyPass(auto)
    short = _short("TSLA", "momentum_breakout")
    assert _blockers(auto, short, paper_pass) == []
    tags = paper_pass.tags(short)
    assert tags["paper_only"] is True and tags["paper_only_reason"] == "exploration"
    assert tags["opens_short"] is True and tags["position_side"] == "short"
    assert tags["paper_exploration_bypassed"] == ["strategy_lacks_oos_evidence"]
    assert tags["easy_to_borrow"] is True
    assert paper_pass.risk_pct(short) == 0.25
    assert paper_pass.notice(short)[0] == "Paper-only exploration SHORT (sell to open) (not copied to eToro)"
    off, _ = _shorts(tmp_path / "off", short_verdicts={})  # the lane off: the refusal stands
    assert _blockers(off, short) == ["strategy_lacks_oos_evidence"]


def test_an_exploration_short_takes_the_lane_s_rules_and_shared_caps(tmp_path) -> None:
    from tests.test_paper_exploration_lane import _xrow

    short = _short("TSLA", "momentum_breakout")
    daily, _ = _shorts(tmp_path / "d", short_verdicts={}, **LANE, executions=_Executions(rows=[_xrow(f"S{i}") for i in range(12)]), max_trades_per_day=40, max_open_positions=40, paper_exploration_lane_max_open=40)
    assert _blockers(daily, short) == ["exploration_lane_daily_cap"]  # 12 a NY day, longs and shorts
    eight = [f"S{i}" for i in range(8)]  # exploration positions held since two days ago
    opened, _ = _shorts(
        tmp_path / "o", short_verdicts={}, **LANE, broker=_Broker(eight), max_open_positions=40,
        executions=_Executions(rows=[_xrow(s, created_at=NOW - timedelta(days=2)) for s in eight]),
    )
    assert _blockers(opened, short) == ["exploration_lane_positions_cap"]  # 8 open
    htb, _ = _shorts(tmp_path / "h", short_verdicts={}, **LANE, alpaca=FakeShortAlpaca(asset={**ASSET, "easy_to_borrow": False}))
    assert _blockers(htb, short) == ["exploration_lane_short_hard_to_borrow"]
    late, _ = _shorts(tmp_path / "l", short_verdicts={}, **LANE)
    exploration_module.utc_now = lambda: NOW.replace(hour=19, minute=31)  # 15:31 New York
    try:
        assert _blockers(late, _short("TSLA", "momentum_breakout", timeframe="5m")) == [
            "exploration_lane_intraday_entry_too_late"
        ]
    finally:
        exploration_module.utc_now = lambda: NOW
    minute, _ = _shorts(tmp_path / "1m", short_verdicts={}, **LANE)
    assert _blockers(minute, _short("TSLA", "momentum_breakout", timeframe="1m")) == [
        "strategy_lacks_oos_evidence"
    ]


def test_a_crypto_short_is_dropped_as_a_short_whatever_its_long_verdict(tmp_path) -> None:
    """Review fix 4: with the switch on, a crypto short whose LONG verdict passes used to pass the
    drop check and fail at the risk gate (auto_proposal_failed) every scan."""

    auto, _ = _shorts(tmp_path, **LANE)
    paper_pass = PaperOnlyPass(auto)
    crypto = _short("BTCUSD", "inside_bar_narrow_range_breakout")  # its LONG verdict passes
    assert not paper_pass.shorts.applies(crypto)
    assert _blockers(auto, crypto, paper_pass) == []  # judged as a long would be ...
    assert paper_pass.drop_reason(crypto) == "entry_short"  # ... and dropped as a short
    assert paper_pass.drop_reason(_short()) is None  # an equity short is not
    workflow, proposals = _workflow(tmp_path, auto)
    scanned = _scan_short("BTCUSD", "inside_bar_narrow_range_breakout")
    assert auto_propose_candidates(workflow, _response(scanned), origin="swing_scan", notify=False) == 0
    assert proposals.created == []
    (funnel,) = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"]
    assert funnel["dropped:entry_short"] == 1 and "proposal_failed" not in funnel
    assert not [e for e, _ in workflow.run_logs.items if e == "auto_proposal_failed"]


def test_a_stale_short_verdict_goes_to_the_lane_too(tmp_path) -> None:
    from app.workflow.exploration import PROPOSAL_BYPASS

    auto, store = _auto(tmp_path, eto=_short_verdicts({"t:short:1d": SHORT_FULL}, age_hours=30), **SWITCH, **LANE)
    auto.alpaca = FakeShortAlpaca()
    paper_pass = PaperOnlyPass(auto)
    short = _short("TSLA", "t")
    assert paper_pass.exploration.likely(short)
    assert _blockers(auto, short, paper_pass) == []
    assert paper_pass.tags(short)["paper_exploration_bypassed"] == ["strategy_evidence_stale"]
    assert "strategy_evidence_stale" in PROPOSAL_BYPASS


# --- end to end: the auto-propose loop ----------------------------------------------------------


def _scan_short(symbol="TSLA", strategy="trend_following", *, entry=100.0, stop=104.0, target=92.0):
    candidate = _scan_candidate(symbol, strategy, 90.0)
    return candidate.model_copy(
        update={
            "state": SignalState.SELL,
            "signal_role": "entry_short",
            "entry_price": entry,
            "current_price": entry,
            "stop_loss": stop,
            "take_profit": target,
            "targets": [target],
        }
    )


def test_a_scanned_short_becomes_a_tagged_sell_proposal_sized_from_its_stop(tmp_path) -> None:
    auto, _ = _shorts(tmp_path)
    workflow, proposals = _workflow(tmp_path, auto)
    workflow.settings.auto_propose_risk_based_sizing = False  # a short is risk-sized regardless
    assert isinstance(_scan_short(), LiveSignalSnapshot)
    created = auto_propose_candidates(workflow, _response(_scan_short()), origin="swing_scan", notify=False)
    assert created == 1
    (request,) = proposals.created
    assert request.side == OrderSide.SELL and (request.stop_loss, request.take_profit) == (104.0, 92.0)
    assert request.metadata["paper_only"] is True and request.metadata["paper_only_reason"] == "short"
    assert request.metadata["opens_short"] is True and request.metadata["proposal_quality"] == STRICT_VALID
    sizing = request.metadata["sizing"]
    assert sizing["sizing_mode"] == "risk_based"
    shares = sizing["uncapped_amount_usd"] / 100.0
    assert shares == pytest.approx(sizing["risk_budget_usd"] / (104.0 - 100.0))  # risk $ / (stop - entry)
    (funnel,) = [p for e, p in workflow.run_logs.items if e == "auto_propose_funnel"]
    assert funnel["proposals_created"] == 1 and "dropped:entry_short" not in funnel
    (logged,) = [p for e, p in workflow.run_logs.items if e == "auto_proposal_created"]
    assert logged["proposal_quality"] == STRICT_VALID  # review nit: not "not_tradeable"


def test_the_real_unattended_checks_run_on_a_short_end_to_end(tmp_path) -> None:
    """Review nit: the unattended path's own ``candidate_blockers`` (not stubbed) on a short."""

    from app.workflow.exploration import EXECUTION_BYPASS

    def unattended(path, **settings):
        values = dict(
            paper_auto_operation_mode="unattended", paper_auto_approve_proposals=True,
            auto_execution_worker_enabled=True, alpaca_expected_account_number="PA1",
            paper_auto_approval_tier="tier2_strict_valid", paper_auto_min_clean_supervised_lifecycles=0,
            market_universe_symbols=["TSLA"], auto_execution_min_score=60.0, **settings,
        )
        auto, _ = _shorts(path, **values)
        del auto.candidate_blockers  # the real PaperAutoTradingService.candidate_blockers
        auto.automation = SimpleNamespace(execution_blockers=lambda: [])
        auto.reconciliation = SimpleNamespace(account_verified=lambda: True)
        auto.paper_trading = SimpleNamespace(lifecycles=lambda limit=1000: [])
        auto.alpaca.is_regular_market_open = lambda: True
        auto.alpaca.is_supported_equity = lambda symbol: True
        return auto

    short = _short()
    short.score = 90.0
    auto = unattended(tmp_path)
    reasons = auto.candidate_blockers(short)
    assert set(reasons) <= EXECUTION_BYPASS and "candidate_not_backtest_validated" in reasons
    paper_pass = PaperOnlyPass(auto)
    assert _blockers(auto, short, paper_pass) == []  # admitted: only the long-evidence family set aside
    assert paper_pass.tags(short)[SHORT_BYPASSED_FIELD] == ["candidate_not_backtest_validated"]
    closed = unattended(tmp_path / "closed")
    closed.alpaca.is_regular_market_open = lambda: False
    assert _blockers(closed, short) == [
        "paper_only_not_automatic",
        "paper_only_auto_outside_regular_market_hours",
    ]
    off = unattended(tmp_path / "off", paper_short_entries_enabled=False)
    assert {"short_entries_disabled", "proposal_quality_not_tradeable"} <= set(off.candidate_blockers(short))


def test_an_exploration_short_through_the_loop(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, short_verdicts={}, **LANE)
    workflow, proposals = _workflow(tmp_path, auto)
    assert auto_propose_candidates(workflow, _response(_scan_short()), origin="swing_scan", notify=False) == 1
    (request,) = proposals.created
    assert request.side == OrderSide.SELL and request.metadata["paper_only_reason"] == "exploration"
    assert request.metadata["position_side"] == "short" and request.metadata["sizing"]["risk_pct"] == 0.25


def test_approve_enqueue_execute_sets_aside_only_the_long_evidence_family_for_a_short(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, auto_blockers=["candidate_not_backtest_validated"])
    calls = []
    auto.logs = SimpleNamespace(log=lambda *a, **k: None)
    auto.proposals = SimpleNamespace(approve_proposal=lambda pid, decision: calls.append(pid) or SimpleNamespace(id=pid, order=SimpleNamespace(metadata={})))
    auto.execution = SimpleNamespace(
        enqueue_approved_proposal=lambda pid: SimpleNamespace(id="q1"),
        process_queue_item=lambda qid: SimpleNamespace(id=qid, symbol="TSLA", status="executed", validation_reason="ready"),
    )
    auto.notifier = SimpleNamespace(send_text=lambda *_a: None)
    short_meta = {**TAGS, "paper_only_reason": "short", "opens_short": True, "position_side": "short"}
    proposal = SimpleNamespace(id="p1", order=SimpleNamespace(symbol="TSLA", metadata=short_meta))
    funnel: Counter = Counter()
    assert auto.approve_enqueue_execute(proposal, _short(), funnel=funnel) is not None and calls == ["p1"]
    plain = SimpleNamespace(id="p2", order=SimpleNamespace(symbol="TSLA", metadata={}))
    assert auto.approve_enqueue_execute(plain, _short(), funnel=funnel) is None  # not a tagged short
    assert funnel["exec_blocked:candidate_not_backtest_validated"] == 1


def test_a_short_without_a_strategy_name_is_refused_never_proposed_untagged(tmp_path) -> None:
    auto, _ = _shorts(tmp_path, **LANE)
    paper_pass = PaperOnlyPass(auto)
    nameless = _short("TSLA", "")
    assert _blockers(auto, nameless, paper_pass) == ["strategy_lacks_oos_evidence"]
    assert paper_pass.tags(nameless) == {}


def test_the_short_lane_reads_the_short_verdicts_and_never_the_shadow_data() -> None:
    """The fences of steps 1-2 (tests/test_short_shadow.py, tests/test_backtest_short_research.py)
    admit app/workflow/paper_shorts.py as a reader of the short VERDICTS only."""

    from pathlib import Path

    import app.workflow.paper_shorts as lane

    text = Path(lane.__file__).read_text()
    assert "SHORT_VERDICTS_KEY" in text
    for token in ("short_signals", "shadow_ledger:short", "SHORT_LEDGER_KEY", "walk_forward_oos_short"):
        assert token not in text, token
