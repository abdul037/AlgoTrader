"""Four more eToro LIVE copy rules (operator 2026-10-18, live copies only).

Post-mortem of the 4-16 Oct losers (AMD, META, MSFT, SPY): all four were weak, failed their
own per-signal backtest check, the planner said wait/watchlist/no_trade, the strategy's
pooled evidence flickered, and AMD was +23% in 20 days the day after a record. The mirror now
refuses such a copy; paper keeps trading as before, so evidence keeps coming in.

Real money: each rule, each threshold edge and each missing input gets its own test, and no
new refusal may hand paper's room decision to eToro.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.broker.etoro_live_mirror import (
    COPY_RULE_REASONS,
    HARD_MAX_RETURN_20D_PCT,
    HARD_MAX_RSI_14,
    MIRROR_BUY_NOW_TIMING_LABELS,
    MIRROR_MIN_EVIDENCE_STREAK_HOURS,
    extension_blockers,
)
from app.broker.etoro_live_room import ROOM_REASONS, room_verdict
from app.performance.strategy_evidence import EVIDENCE_STREAK_MAX_GAP_HOURS, VERDICTS_KEY
from tests.test_etoro_live_mirror import (
    COPYABLE_METADATA,
    _Client,
    _mirror,
    _proposal,
    _run,
    verdicts_cache,
)
from tests.test_etoro_live_room import _live, _order

PASSING = {"momentum_breakout:1d": {"passed": True, "oos_expectancy_r": 0.05}}
NEW_RULES = set(COPY_RULE_REASONS)


def _meta(**changes):
    """COPYABLE_METADATA with top-level keys replaced (None: removed)."""

    metadata = copy.deepcopy(COPYABLE_METADATA)
    for key, value in changes.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    return metadata


def _ext(**changes):
    return _meta(entry_extension={**COPYABLE_METADATA["entry_extension"], **changes})


def _reasons(service, proposal):
    return [r for r in service.blockers(proposal, "alpaca") if r in NEW_RULES]


def test_constants_are_the_approved_thresholds() -> None:
    assert frozenset({"immediate"}) == MIRROR_BUY_NOW_TIMING_LABELS
    assert MIRROR_MIN_EVIDENCE_STREAK_HOURS == 24.0
    assert (HARD_MAX_RETURN_20D_PCT, HARD_MAX_RSI_14) == (15.0, 68.0)
    assert EVIDENCE_STREAK_MAX_GAP_HOURS == 2.0


def test_a_copy_meeting_every_rule_is_copied(tmp_path) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client)
    assert service.blockers(_proposal(), "alpaca") == []
    assert _run(service) is not None and len(client.orders) == 1


# -- rule 1: respect the planner ---------------------------------------------------------


def test_the_planners_label_set_has_one_buy_now_label() -> None:
    """Every label build_trade_plan can give: only "immediate" means enter now."""

    from types import SimpleNamespace

    from app.intelligence.trade_plan import build_trade_plan

    seen: dict[str, set[str]] = {}
    for score in (40.0, 56.0, 72.0, 86.0, 99.0):
        for actionability in ("alert", "watchlist", "none"):
            for momentum in ("exhausted", "building"):
                for regime in (0.3, 0.8):
                    plan = build_trade_plan(
                        signal=SimpleNamespace(metadata={}, strategy_name="trend", price=100.0),
                        timeframe="1d",
                        current_price=100.0,
                        entry_price=100.0,
                        stop_loss=95.0,
                        take_profit=110.0,
                        risk_reward_ratio=2.0,
                        final_score=score,
                        context=SimpleNamespace(relative_volume=1.0),
                        intelligence=SimpleNamespace(
                            momentum_state=momentum,
                            market_regime_score=regime,
                            timeframe_alignment_score=0.7,
                        ),
                        actionability=actionability,
                    )
                    seen.setdefault(plan["timing_label"], set()).add(plan["verdict"])
    assert set(seen) == {"immediate", "conditional", "watchlist", "no_trade"}
    assert seen["immediate"] == {"actionable"}  # score >= 85, not exhausted, regime >= 0.45
    assert set(seen) > MIRROR_BUY_NOW_TIMING_LABELS


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        ({"timing_label": "conditional"}, "etoro_live_planner_not_buy_now"),  # AMD: wait
        ({"timing_label": "watchlist"}, "etoro_live_planner_not_buy_now"),  # META, MSFT
        ({"timing_label": "no_trade"}, "etoro_live_planner_not_buy_now"),  # SPY
        ({"timing_label": "data_quality_insufficient"}, "etoro_live_planner_not_buy_now"),
        ({"timing_label": "IMMEDIATE "}, None),
        ({"timing_label": ""}, "etoro_live_planner_unknown"),
        ({"timing_label": None}, "etoro_live_planner_unknown"),
        ({"verdict": "actionable"}, "etoro_live_planner_unknown"),
        ("immediate", "etoro_live_planner_unknown"),  # not a plan
        (None, "etoro_live_planner_unknown"),  # no plan at all
    ],
)
def test_copy_only_what_the_planner_says_to_buy_now(tmp_path, plan, reason) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    proposal = _proposal(metadata=_meta(trade_plan=plan))  # None: no trade_plan at all
    assert _reasons(service, proposal) == ([reason] if reason else [])
    copied = _run(service, proposal)
    assert (copied is not None) is (reason is None)
    if reason:
        assert client.orders == [] and logs.events[-1][1]["reasons"] == [reason]


# -- rule 2: the per-signal backtest check -----------------------------------------------


@pytest.mark.parametrize(
    ("validated", "reason"),
    [
        (True, None),
        (False, "etoro_live_backtest_check_failed"),  # all ten stock copies to 10-16
        ("true", "etoro_live_backtest_check_unknown"),
        (1, "etoro_live_backtest_check_unknown"),
        ("missing", "etoro_live_backtest_check_unknown"),
    ],
)
def test_copy_needs_the_per_signal_backtest_check(tmp_path, validated, reason) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client)
    proposal = _proposal(
        metadata=_meta(backtest_validated=None if validated == "missing" else validated)
    )
    assert _reasons(service, proposal) == ([reason] if reason else [])
    assert (_run(service, proposal) is not None) is (reason is None)


# -- rule 3: no evidence flicker -----------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "age_minutes", "reason"),
    [
        (24.2, 10.0, None),
        (23.8, 10.0, "etoro_live_evidence_streak_too_short"),
        (2.0, 10.0, "etoro_live_evidence_streak_too_short"),  # MSFT/SPY: ~2 h into a window
        (48.0, EVIDENCE_STREAK_MAX_GAP_HOURS * 60 + 5, "etoro_live_evidence_streak_unknown"),
    ],
)
def test_copy_needs_an_unbroken_24h_pass(tmp_path, hours, age_minutes, reason) -> None:
    service, _ = _mirror(tmp_path)
    service.state.set(
        VERDICTS_KEY, verdicts_cache(PASSING, passing_hours=hours, age_minutes=age_minutes)
    )
    assert _reasons(service, _proposal()) == ([reason] if reason else [])


def test_a_missing_or_unreadable_history_blocks(tmp_path) -> None:
    service, _ = _mirror(tmp_path)
    for raw in (
        json.dumps({"verdicts": PASSING}),  # written before streaks existed: no history
        json.dumps({"verdicts": PASSING, "passing_since": {}}),  # no computed_at
        "not json",
    ):
        service.state.set(VERDICTS_KEY, raw)
        assert "etoro_live_evidence_streak_unknown" in service.blockers(_proposal(), "alpaca")
    payload = json.loads(verdicts_cache(PASSING))
    payload["passing_since"]["momentum_breakout:1d"] = "garbage"
    service.state.set(VERDICTS_KEY, json.dumps(payload))
    assert _reasons(service, _proposal()) == ["etoro_live_evidence_streak_unknown"]


def test_a_failing_strategy_has_no_streak(tmp_path) -> None:
    service, _ = _mirror(tmp_path)
    failing = {"momentum_breakout:1d": {"passed": False, "oos_expectancy_r": 0.05}}
    service.state.set(VERDICTS_KEY, verdicts_cache(failing))
    reasons = service.blockers(_proposal(), "alpaca")
    assert "strategy_lacks_oos_evidence" in reasons
    assert "etoro_live_evidence_streak_too_short" in reasons


# -- rule 4: don't chase extended moves ---------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({"return_20d_pct": 15.0}, []),  # "above 15%" blocks; 15.0 does not
        ({"return_20d_pct": 15.01}, ["etoro_live_extended_20d_rise"]),
        ({"return_20d_pct": 23.4}, ["etoro_live_extended_20d_rise"]),  # AMD 10-07
        ({"return_20d_pct": -30.0}, []),
        ({"rsi_14": 67.99}, []),
        ({"rsi_14": 68.0}, ["etoro_live_extended_rsi"]),  # "68 or more"
        ({"rsi_14": 69.7}, ["etoro_live_extended_rsi"]),  # AMD 10-07
        ({"new_high_252_recent": True}, ["etoro_live_extended_new_high"]),
        ({"return_20d_pct": None}, ["etoro_live_extension_unknown"]),
        ({"rsi_14": float("nan")}, ["etoro_live_extension_unknown"]),
        ({"rsi_14": "55"}, ["etoro_live_extension_unknown"]),
        ({"return_20d_pct": True}, ["etoro_live_extension_unknown"]),
        ({"new_high_252_recent": None}, ["etoro_live_extension_unknown"]),
        ({"new_high_252_recent": 0}, ["etoro_live_extension_unknown"]),
        ({"timeframe": "15m"}, ["etoro_live_extension_unknown"]),
        (
            {"rsi_14": None, "return_20d_pct": 20.0},
            ["etoro_live_extension_unknown", "etoro_live_extended_20d_rise"],
        ),
    ],
)
def test_copy_refuses_a_chased_move(tmp_path, changes, reasons) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client)
    proposal = _proposal(metadata=_ext(**changes))
    assert _reasons(service, proposal) == reasons
    assert (_run(service, proposal) is not None) is (not reasons)


def test_missing_extension_inputs_block(tmp_path) -> None:
    service, _ = _mirror(tmp_path)
    for metadata in (_meta(entry_extension=None), _meta(entry_extension="calm")):
        assert _reasons(service, _proposal(metadata=metadata)) == ["etoro_live_extension_unknown"]
    # An intraday signal has no session inputs: never copied on rule 4.
    assert extension_blockers(COPYABLE_METADATA["entry_extension"], "15m") == [
        "etoro_live_extension_unknown"
    ]
    assert extension_blockers(COPYABLE_METADATA["entry_extension"], None) == [
        "etoro_live_extension_unknown"
    ]


# -- inputs: the order's metadata (scan proposals carry no signal) ------------------------


def test_a_proposal_without_any_scan_metadata_is_refused_on_every_rule(tmp_path) -> None:
    service, _ = _mirror(tmp_path)
    proposal = _proposal(metadata={})
    assert set(_reasons(service, proposal)) == {
        "etoro_live_planner_unknown",
        "etoro_live_backtest_check_unknown",
        "etoro_live_extension_unknown",
    }
    assert _run(service, proposal) is None and service.client.orders == []


def test_inputs_come_from_the_order_and_then_the_signal(tmp_path) -> None:
    service, _ = _mirror(tmp_path)
    proposal = _proposal(metadata={})
    proposal.signal.metadata.update(copy.deepcopy(COPYABLE_METADATA))
    assert service.blockers(proposal, "alpaca") == []  # a signal-only proposal still works
    proposal.order.metadata["trade_plan"] = {"timing_label": "watchlist"}  # the order wins
    assert _reasons(service, proposal) == ["etoro_live_planner_not_buy_now"]


def test_unreadable_metadata_or_a_rule_error_refuses_the_copy(tmp_path, monkeypatch) -> None:
    import app.broker.etoro_live_mirror as live

    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    proposal = _proposal()
    proposal.order.metadata = None  # unreadable metadata must not slip through
    assert _run(service, proposal) is None and client.orders == []
    assert "etoro_live_planner_unknown" in logs.events[-1][1]["reasons"]

    def boom(metadata):
        raise RuntimeError("bad plan")

    monkeypatch.setattr(live, "planner_blocker", boom)
    assert _run(service, _proposal()) is None and client.orders == []
    assert logs.events[-1][1]["reasons"] == ["etoro_live_gate_error:bad plan"]
    mirror, _ = _live(tmp_path / "room")
    assert room_verdict(mirror, _order("JPM")) is None  # doubt keeps paper's room checks


# -- room: a copy refusal must leave paper's own room rules in charge ---------------------


def test_no_new_reason_is_a_room_reason() -> None:
    assert not COPY_RULE_REASONS & ROOM_REASONS
    assert all(reason.startswith("etoro_live_") for reason in COPY_RULE_REASONS)


@pytest.mark.parametrize(
    "metadata",
    [
        _meta(trade_plan={"timing_label": "watchlist"}),
        _meta(trade_plan=None),
        {**_meta(), "backtest_validated": False},
        _meta(backtest_validated=None),
        _ext(return_20d_pct=23.4),
        _ext(rsi_14=69.7),
        _ext(new_high_252_recent=True),
        _ext(rsi_14=None),
        _meta(entry_extension=None),
    ],
)
def test_each_copy_rule_refusal_keeps_paper_room_rules(tmp_path, metadata) -> None:
    mirror, _ = _live(tmp_path)
    assert room_verdict(mirror, _order("JPM")) == []  # copyable: eToro's room decides
    order = _order("JPM")
    order.metadata = {**copy.deepcopy(metadata), "timeframe": "1d"}
    assert room_verdict(mirror, order) is None  # not copied: paper's room decides


@pytest.mark.parametrize("hours", [3.0, None])
def test_a_short_or_unknown_streak_keeps_paper_room_rules(tmp_path, hours) -> None:
    mirror, _ = _live(tmp_path)
    assert room_verdict(mirror, _order("JPM")) == []
    raw = (
        verdicts_cache(PASSING, passing_hours=hours)
        if hours is not None
        else json.dumps({"verdicts": PASSING})
    )
    mirror.state.set(VERDICTS_KEY, raw)
    assert room_verdict(mirror, _order("JPM")) is None


def test_a_room_refusal_on_a_copyable_order_is_still_etoros(tmp_path) -> None:
    full = ("100001", "META", "MSFT", "TSLA", "JPM", "XOM", "SPY", "COST", "AMZN")
    mirror, _ = _live(tmp_path, open_symbols=full)
    assert room_verdict(mirror, _order("AAPL")) == ["etoro_live_open_position_cap"]


# -- the post-mortem copies, replayed ------------------------------------------------------


def test_the_four_losing_stock_copies_would_not_be_copied(tmp_path) -> None:
    amd = _meta(
        trade_plan={"verdict": "actionable", "timing_label": "conditional"},
        backtest_validated=False,
        entry_extension={
            "timeframe": "1d",
            "return_20d_pct": 23.4,
            "rsi_14": 69.7,
            "new_high_252_signal_bar": False,
            "new_high_252_prior_bar": True,  # the 10-06 record close
            "new_high_252_recent": True,
        },
    )
    service, _ = _mirror(tmp_path)
    assert _reasons(
        service, _proposal("AMD", stop=600.0, target=700.0, price=643.18, metadata=amd)
    ) == [
        "etoro_live_planner_not_buy_now",
        "etoro_live_backtest_check_failed",
        "etoro_live_extended_20d_rise",
        "etoro_live_extended_rsi",
        "etoro_live_extended_new_high",
    ]
    meta = {**_ext(return_20d_pct=20.4), "trade_plan": {"timing_label": "watchlist"}}
    meta["backtest_validated"] = False
    assert set(_reasons(service, _proposal("META", metadata=meta))) == {  # META 10-05
        "etoro_live_planner_not_buy_now",
        "etoro_live_backtest_check_failed",
        "etoro_live_extended_20d_rise",
    }
    # MSFT and SPY: bought ~2 h into a 28 h evidence window.
    service.state.set(VERDICTS_KEY, verdicts_cache(PASSING, passing_hours=2.1))
    for symbol, label in (("MSFT", "watchlist"), ("SPY", "no_trade")):
        metadata = {**_ext(return_20d_pct=9.3), "trade_plan": {"timing_label": label}}
        metadata["backtest_validated"] = False
        assert set(_reasons(service, _proposal(symbol, metadata=metadata))) == {
            "etoro_live_planner_not_buy_now",
            "etoro_live_backtest_check_failed",
            "etoro_live_evidence_streak_too_short",
        }, symbol
