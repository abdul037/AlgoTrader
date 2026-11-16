"""Shorts step 3 (operator 2026-11-16): the switch, the short-sale controls, and "no short ever
reaches the eToro live copy" (docs/notes/paper_shorts.md).

* ``paper_shorts_enabled``: the paper switch, paper mode, real trading off, Alpaca as the paper and
  equities broker; any one missing turns it off;
* ``ShortTradePolicy``: every SELL entry meets the short controls (with or without ``opens_short``);
  the paper switch counts as "shorting on" only for a tagged paper-only short on the paper account;
  hard-to-borrow, not shortable, not marginable, no margin requirement, too little equity, a
  borrow cost over the cap and any live mode are refused; a BUY is untouched;
* the eToro live mirror refuses ANY order that opens a short (``etoro_live_short_not_enabled``)
  before it reads anything else, tagged paper-only or not, logs it as its own event, never sends
  an order (also when its own gate is bypassed), and leaves the room to paper's rules.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.etoro_live_mirror import (
    PAPER_ONLY_REASON,
    SHORT_NOT_COPIED_EVENT,
    SHORT_NOT_ENABLED_REASON,
)
from app.broker.etoro_live_room import room_verdict
from app.models.trade import OrderSide, TradeOrder
from app.risk.guardrails import RiskContext, RiskManager
from app.risk.short_side import (
    closing_side,
    etb_borrow_cost_annual_pct,
    metadata_is_short,
    order_opens_short,
    paper_short_order,
    paper_shorts_enabled,
    signed_pnl,
)
from app.risk.shorting import ShortTradePolicy
from tests.conftest import make_settings
from tests.test_etoro_live_mirror import _proposal, _run
from tests.test_etoro_live_room import _live

ON = dict(
    paper_short_entries_enabled=True,
    execution_mode="paper",
    enable_real_trading=False,
    paper_broker="alpaca",
    broker_for_equities="alpaca",
)
ETB = {  # what app/workflow/paper_shorts.py writes from Alpaca's asset and account data
    "opens_short": True,
    "position_side": "short",
    "shortable": True,
    "easy_to_borrow": True,
    "marginable": True,
    "margin_requirement_pct": 30.0,
    "borrow_cost_annual_pct": 0.3,
}
PAPER_SHORT = {"paper_only": True, "paper_only_reason": "short", **ETB}
EXPLORATION_SHORT = {"paper_only": True, "paper_only_reason": "exploration", **ETB}


def _short(metadata=None, *, side=OrderSide.SELL, symbol="NVDA"):
    return TradeOrder(
        symbol=symbol,
        side=side,
        amount_usd=5_000,
        proposed_price=100.0,
        stop_loss=104.0,
        take_profit=92.0,
        metadata=dict(PAPER_SHORT if metadata is None else metadata),
    )


# --- the switch ------------------------------------------------------------------------------


def test_the_switch_is_off_by_default_and_needs_every_paper_condition(tmp_path) -> None:
    assert make_settings(tmp_path).paper_short_entries_enabled is False
    assert paper_shorts_enabled(make_settings(tmp_path)) is False
    assert paper_shorts_enabled(make_settings(tmp_path, **ON)) is True
    for name, value in (
        ("paper_short_entries_enabled", False),
        ("execution_mode", "live"),
        ("enable_real_trading", True),
        ("paper_broker", "self_simulated"),
        ("broker_for_equities", "etoro"),
    ):
        assert paper_shorts_enabled(SimpleNamespace(**{**ON, name: value})) is False, name
    assert paper_shorts_enabled(SimpleNamespace()) is False  # absent: off


def test_side_helpers() -> None:
    assert closing_side("sell") == "buy" and closing_side("buy") == "sell"
    assert closing_side(OrderSide.SELL) == "buy" and closing_side(None) == "sell"
    assert signed_pnl("sell", 100.0, 92.0, 10) == 80.0  # a short gains on a fall
    assert signed_pnl("sell", 100.0, 104.0, 10) == -40.0
    assert signed_pnl("buy", 100.0, 104.0, 10) == 40.0
    assert order_opens_short(_short()) and order_opens_short(_short({}, side=OrderSide.SELL))
    assert not order_opens_short(_short({}, side=OrderSide.BUY))
    assert order_opens_short(_short({"position_side": "short"}, side=OrderSide.BUY))
    assert metadata_is_short({"opens_short": True}) and not metadata_is_short(None)
    # Alpaca publishes no borrow rate: an ETB name gets the cost model's own ETB assumption.
    assert etb_borrow_cost_annual_pct() == pytest.approx(0.30)


def test_the_paper_switch_admits_only_a_tagged_paper_short(tmp_path) -> None:
    on = make_settings(tmp_path, **ON)
    assert paper_short_order(on, _short())
    assert paper_short_order(on, _short(EXPLORATION_SHORT))
    assert not paper_short_order(on, _short(ETB))  # not tagged paper-only
    assert not paper_short_order(on, _short({**PAPER_SHORT, "opens_short": False}))
    assert not paper_short_order(on, _short(side=OrderSide.BUY))
    assert not paper_short_order(make_settings(tmp_path / "off"), _short())


# --- ShortTradePolicy ------------------------------------------------------------------------


def _policy(tmp_path, **settings):
    return ShortTradePolicy(make_settings(tmp_path, **settings))


def test_a_tagged_paper_short_on_easy_to_borrow_data_passes_the_short_controls(tmp_path) -> None:
    assert _policy(tmp_path, **ON).blockers(_short(), account_equity_usd=100_000.0) == []
    assert _policy(tmp_path, **ON).blockers(_short(EXPLORATION_SHORT), account_equity_usd=100_000.0) == []


def test_with_the_switch_off_every_short_is_refused(tmp_path) -> None:
    reasons = _policy(tmp_path).blockers(_short(), account_equity_usd=100_000.0)
    assert reasons == ["short_trading_disabled"]
    # An untagged short with the paper switch on is not a paper short either.
    assert _policy(tmp_path, **ON).blockers(_short(ETB), account_equity_usd=100_000.0) == [
        "short_trading_disabled"
    ]


def test_every_sell_entry_meets_the_short_controls_even_without_the_flag(tmp_path) -> None:
    """Tightening: a SELL without ``opens_short`` used to skip every short control."""

    bare = _short({})
    reasons = _policy(tmp_path, short_trading_enabled=True).blockers(bare, account_equity_usd=1e5)
    assert {
        "short_not_shortable",
        "short_not_easy_to_borrow",
        "short_borrow_cost_missing",
        "short_margin_requirement_missing",
    } <= set(reasons)
    assert _policy(tmp_path, **ON).blockers(_short(side=OrderSide.BUY, metadata={}), account_equity_usd=1.0) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"easy_to_borrow": False}, "short_not_easy_to_borrow"),  # hard-to-borrow is refused
        ({"shortable": False}, "short_not_shortable"),
        ({"marginable": False}, "short_not_marginable"),
        ({"margin_requirement_pct": None}, "short_margin_requirement_missing"),
        ({"borrow_cost_annual_pct": None}, "short_borrow_cost_missing"),
        ({"borrow_cost_annual_pct": 7.5}, "short_borrow_cost_above_limit"),
    ],
)
def test_each_short_control_refuses_on_its_own(tmp_path, change, reason) -> None:
    order = _short({**PAPER_SHORT, **change})
    assert _policy(tmp_path, **ON).blockers(order, account_equity_usd=100_000.0) == [reason]


def test_too_little_equity_and_any_live_mode_are_refused(tmp_path) -> None:
    policy = _policy(tmp_path, **ON)
    assert policy.blockers(_short(), account_equity_usd=20_000.0) == ["short_minimum_account_equity_not_met"]
    live = _policy(tmp_path / "live", short_trading_enabled=True, execution_mode="live")
    assert "short_trading_live_not_enabled" in live.blockers(_short(), account_equity_usd=1e5)
    real = _policy(tmp_path / "real", short_trading_enabled=True, enable_real_trading=True)
    assert "short_trading_live_not_enabled" in real.blockers(_short(), account_equity_usd=1e5)


def test_the_risk_gate_runs_the_short_controls_and_every_other_gate(tmp_path) -> None:
    manager = RiskManager(make_settings(tmp_path, **ON, allowed_instruments=["NVDA"]))
    context = RiskContext(account_balance=100_000.0, mode="paper")
    assert manager.validate_order(_short(), context).passed
    refused = manager.validate_order(_short({**PAPER_SHORT, "easy_to_borrow": False}), context)
    assert not refused.passed and refused.reasons == ["short_not_easy_to_borrow"]
    # The usual gates still apply to a short: a stop is required, risk is |entry - stop|.
    no_stop = _short().model_copy(update={"stop_loss": None})
    assert "A stop loss is required before submitting any order" in manager.validate_order(no_stop, context).reasons
    risk = manager.validate_order(_short(), context)
    assert risk.risk_amount_usd == pytest.approx(5_000 * 0.04)


# --- the eToro live mirror -------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [PAPER_SHORT, EXPLORATION_SHORT, ETB, {}, {"paper_only": True, "paper_only_reason": "oos_only"}],
)
def test_the_mirror_refuses_any_short_first_and_reads_nothing_else(tmp_path, metadata) -> None:
    mirror, logs = _live(tmp_path)
    reads: list[str] = []
    get = mirror.state.get
    mirror.state.get = lambda key, *a: reads.append(key) or get(key, *a)
    order = _proposal("JPM", side="sell", stop=104.0, target=92.0, metadata=metadata).order
    gate = SimpleNamespace(id=None, order=order, signal=None)
    assert mirror.blockers(gate, "alpaca") == [SHORT_NOT_ENABLED_REASON, "mirror_long_only"]
    assert reads == []
    assert PAPER_ONLY_REASON not in mirror.blockers(gate, "alpaca")
    assert room_verdict(mirror, order) is None  # not a room reason: paper's own room decides


def test_a_short_never_reaches_the_etoro_client(tmp_path) -> None:
    mirror, logs = _live(tmp_path)
    short = _proposal("JPM", side="sell", stop=104.0, target=92.0, metadata=dict(PAPER_SHORT))
    assert _run(mirror, short) is None
    assert mirror.client.orders == []
    assert logs.events == [
        (
            SHORT_NOT_COPIED_EVENT,
            {
                "symbol": "JPM",
                "proposal_id": short.id,
                "reasons": [SHORT_NOT_ENABLED_REASON, "mirror_long_only"],
            },
        )
    ]  # its own event: paper shorts never crowd the scorecard's skipped-by-eToro list


def test_a_buy_marked_short_is_refused_too(tmp_path) -> None:
    mirror, _ = _live(tmp_path)
    marked = _proposal("JPM", metadata={"position_side": "short"})
    assert mirror.blockers(marked, "alpaca") == [SHORT_NOT_ENABLED_REASON]
    assert _run(mirror, marked) is None and mirror.client.orders == []


def test_the_mirror_refuses_a_short_even_when_its_gate_is_bypassed(tmp_path) -> None:
    """Backstop in mirror(): a replaced or broken blockers() still cannot send a short."""

    mirror, logs = _live(tmp_path)
    mirror.blockers = lambda *_a, **_k: []
    short = _proposal("JPM", side="sell", stop=104.0, target=92.0, metadata=dict(PAPER_SHORT))
    assert _run(mirror, short) is None
    assert mirror.client.orders == []
    assert logs.events[-1][0] == SHORT_NOT_COPIED_EVENT
    assert logs.events[-1][1]["reasons"] == [SHORT_NOT_ENABLED_REASON]


def test_a_long_is_copied_exactly_as_before(tmp_path) -> None:
    mirror, logs = _live(tmp_path)
    assert SHORT_NOT_ENABLED_REASON not in mirror.blockers(_proposal("JPM"), "alpaca")
