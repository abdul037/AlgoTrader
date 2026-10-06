"""Option 3 (operator 2026-10-06): eToro's own limits decide portfolio room.

On 10-06 MSFT/NVDA/AAPL were refused by the PAPER account's gross/correlated limits while
the real eToro account had 3 of 6 positions and $6,990 free. While the mirror would copy
an order, eToro's room (6 positions, 3 per group, free cash, one per symbol) now replaces
paper's room checks; every per-trade check stays.
"""

from __future__ import annotations

import json

from app.broker.etoro_live_mirror import STATE_KEY
from app.broker.etoro_live_room import (
    HARD_MAX_OPEN_PER_BUCKET,
    effective_open_symbols,
    room_verdict,
)
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from app.risk.guardrails import RiskContext, RiskManager
from tests.test_etoro_live_mirror import _mirror, _proposal, _run, _state

TECH = ["AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMD"]
ALLOWED = [*TECH, "TSLA", "JPM", "XOM", "SPY"]


def _live(tmp_path, open_symbols=("100001", "META", "MSFT"), cash=6_990.1, **overrides):
    state = _state(equity=9_988.0)
    data = {"open_symbols": list(open_symbols), "last_equity": 9_988.0, "last_cash": cash}
    state.set(STATE_KEY, json.dumps(data))
    overrides.setdefault("allowed_instruments", ALLOWED)
    overrides.setdefault("paper_broker", "alpaca")  # production; test settings default to sim
    overrides.setdefault("broker_for_equities", "alpaca")
    return _mirror(tmp_path, state=state, **overrides)


def _order(symbol, **kw):
    order = _proposal(symbol, **kw).order
    order.metadata["timeframe"] = "1d"  # production proposals carry it here, without a signal
    order.amount_usd = 10_000.0  # paper's per-trade cap since 10-06 (10% of ~$100k)
    return order


def _paper_full_context(**extra) -> RiskContext:
    # Paper on 10-06: $28.8k of ~$100k held, tech_complex ~21.9%.
    values = dict(
        account_balance=100_000.0,
        open_positions=4,
        positions_by_symbol={"MSFT": 1, "TSLA": 1, "NVDA": 1, "META": 1},
        exposure_by_symbol_pct={"MSFT": 12.2, "TSLA": 6.8, "NVDA": 5.3, "META": 4.4},
        exposure_by_sector_pct={"XLK": 12.2, "XLY": 6.8, "SMH": 5.3, "XLC": 4.4},
        exposure_by_correlation_bucket_pct={"tech_complex": 21.9, "consumer_cyclical": 6.8},
        gross_exposure_pct=28.7,
    )
    values.update(extra)
    return RiskContext(**values)


def _risk(tmp_path, **settings):
    from tests.conftest import make_settings

    values = dict(
        institutional_portfolio_controls_enabled=True,
        portfolio_max_gross_exposure_pct=30.0,
        portfolio_max_correlated_exposure_pct=30.0,
        allowed_instruments=ALLOWED,
        max_risk_per_trade_pct=5.0,
    )
    values.update(settings)
    return RiskManager(make_settings(tmp_path, **values))


def test_paper_full_but_etoro_has_room_lets_aapl_through(tmp_path) -> None:
    mirror, _ = _live(tmp_path)
    order = _order("AAPL")
    room = room_verdict(mirror, order)
    assert room == []
    risk = _risk(tmp_path).validate_order(order, _paper_full_context(), etoro_room=room)
    assert risk.passed, risk.reasons
    assert risk.room_authority == "etoro_live"
    # The same order under paper's room rules (today's behaviour) is refused.
    paper = _risk(tmp_path).validate_order(order, _paper_full_context())
    assert not paper.passed and paper.room_authority == "paper"
    assert "Projected gross exposure exceeds the portfolio limit" in paper.reasons


def test_etoro_full_blocks_paper_too(tmp_path) -> None:
    mirror, _ = _live(tmp_path, open_symbols=("100001", "META", "MSFT", "TSLA", "JPM", "XOM"))
    order = _order("AAPL")
    room = room_verdict(mirror, order)
    assert room == ["etoro_live_open_position_cap"]
    risk = _risk(tmp_path).validate_order(order, _paper_full_context(), etoro_room=room)
    assert not risk.passed
    assert "eToro live room: etoro_live_open_position_cap" in risk.reasons


def test_three_per_group_cap_and_ids_not_counted(tmp_path) -> None:
    assert HARD_MAX_OPEN_PER_BUCKET == 3
    mirror, _ = _live(tmp_path, open_symbols=("100001", "META", "MSFT", "NVDA"))
    assert room_verdict(mirror, _order("AAPL")) == ["etoro_live_bucket_cap"]
    assert room_verdict(mirror, _order("JPM")) == []  # financials: own group
    mirror, _ = _live(tmp_path / "b", open_symbols=("100001", "META", "MSFT"))
    assert room_verdict(mirror, _order("AAPL")) == []  # ETH id is unclassified


def test_the_mirror_itself_enforces_the_group_cap(tmp_path) -> None:
    mirror, logs = _live(tmp_path, open_symbols=("META", "MSFT", "NVDA"))
    assert _run(mirror, _proposal("AAPL")) is None
    assert mirror.client.orders == []
    mirror, _ = _live(tmp_path / "b")
    assert _run(mirror, _proposal("AAPL")) is not None
    # The write-ahead adds AAPL, so a 4th tech name is refused on the next check.
    assert room_verdict(mirror, _order("GOOGL")) == ["etoro_live_bucket_cap"]


def test_non_room_refusals_keep_paper_room_rules(tmp_path) -> None:
    mirror, _ = _live(tmp_path)
    assert room_verdict(mirror, _order("AAPL", side="sell")) is None
    assert room_verdict(mirror, _order("AAPL", strategy="no_evidence")) is None
    assert room_verdict(mirror, _order("AAPL"), primary_broker="etoro") is None
    assert room_verdict(None, _order("AAPL")) is None
    off, _ = _live(tmp_path / "off", etoro_live_mirror_enabled=False)
    assert room_verdict(off, _order("AAPL")) is None
    flag, _ = _live(tmp_path / "flag", etoro_live_room_authority_enabled=False)
    assert room_verdict(flag, _order("AAPL")) is None
    unknown, _ = _live(tmp_path / "eq")
    unknown.state.set(STATE_KEY, json.dumps({"open_symbols": []}))
    assert room_verdict(unknown, _order("AAPL")) is None  # equity unknown


def test_per_trade_checks_still_apply_with_etoro_room(tmp_path) -> None:
    order = _order("AAPL")
    risk = _risk(tmp_path, kill_switch_enabled=True).validate_order(
        order, _paper_full_context(), etoro_room=[]
    )
    assert "Kill switch is enabled" in risk.reasons
    ctx = _paper_full_context(
        daily_realized_pnl_usd=-10_000.0,
        entries_today_by_correlation_bucket={"tech_complex": 2},
        portfolio_drawdown_pct=50.0,
    )
    reasons = _risk(tmp_path).validate_order(order, ctx, etoro_room=[]).reasons
    assert "Daily loss limit has already been reached" in reasons
    assert "Daily entry cap of 2 reached for the tech_complex group" in reasons
    assert "Portfolio hard drawdown limit reached" in reasons
    # One paper position per symbol stays: exit copy follows the paper position.
    held = _risk(tmp_path).validate_order(_order("NVDA"), _paper_full_context(), etoro_room=[])
    assert "Per-symbol position limit reached" in held.reasons


def test_effective_open_set_maps_ids_and_keeps_unfilled_orders(tmp_path) -> None:
    mirror, _ = _live(tmp_path, open_symbols=("100001", "1004"))
    cards = {
        "e1": {"symbol": "MSFT", "instrument_id": 1004, "status": "open"},
        "e2": {"symbol": "AAPL", "status": "pending_fill"},
        "e3": {"symbol": "TSLA", "status": "closed"},
    }
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    mirror.state.set(
        "etoro_live:test_order", json.dumps({"symbol": "ETH", "instrument_id": 100001})
    )
    held = effective_open_symbols(mirror.state, mirror._state())
    assert held == ["AAPL", "ETH", "MSFT"]
    assert room_verdict(mirror, _order("AAPL")) == ["etoro_live_symbol_already_open"]


def test_app_wiring_uses_etoro_room_at_proposal_time(tmp_path, monkeypatch) -> None:
    from app.main import create_app
    from app.models.approval import TradeProposalCreate
    from tests.conftest import MockBroker, make_settings

    app = create_app(
        make_settings(tmp_path, allowed_instruments=ALLOWED),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    proposals = app.state.proposal_service
    coordinator = app.state.execution_coordinator
    assert proposals is coordinator.proposals
    assert proposals.etoro_live_mirror is coordinator.etoro_live_mirror  # set by the guard
    live, _ = _live(tmp_path / "live")
    proposals.etoro_live_mirror = live
    proposals.risk_manager = _risk(tmp_path)
    monkeypatch.setattr(proposals, "_risk_context", _paper_full_context)
    request = TradeProposalCreate(
        symbol="AAPL",
        amount_usd=10_000,
        proposed_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        strategy_name="momentum_breakout",
        metadata={"timeframe": "1d"},
    )
    assert proposals.create_proposal(request).order.symbol == "AAPL"
    created = proposals.logs.list_by_event("proposal_created", limit=1)
    assert created[0]["payload"]["room_authority"] == "etoro_live"
    proposals.etoro_live_mirror = None  # no mirror: paper's room rules, today's behaviour
    try:
        proposals.create_proposal(request)
        raise AssertionError("paper room should refuse")
    except ValueError as exc:
        assert "Projected gross exposure" in str(exc)


# -- review 2026-10-06: grant eToro room only when the copy can actually happen ----------


def test_rate_limit_cooldown_keeps_paper_room_rules(tmp_path) -> None:
    from types import SimpleNamespace

    from app.broker import etoro_rate_limit

    mirror, _ = _live(tmp_path)
    mirror.client.settings = SimpleNamespace(etoro_api_key="live-test-key-cooldown")
    assert room_verdict(mirror, _order("AAPL")) == []
    etoro_rate_limit.mark_etoro_rate_limited(mirror.client.settings, status_code=429, body="")
    try:
        assert room_verdict(mirror, _order("AAPL")) is None
    finally:
        etoro_rate_limit._state.clear()


def test_a_rejected_copy_keeps_paper_room_rules_for_30_minutes(tmp_path) -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    from app.broker.etoro_live_room import COPY_FAILED_KEY
    from app.utils.time import utc_now

    mirror, _ = _live(tmp_path)
    mirror.client.open_market_order_by_amount = lambda order, client_order_id=None: SimpleNamespace(
        order_id="x", status="rejected"
    )
    assert _run(mirror, _proposal("AAPL")) is None
    assert json.loads(mirror.state.get(COPY_FAILED_KEY))["reason"] == "rejected"
    assert room_verdict(mirror, _order("JPM")) is None  # paper's own room rules again
    old = (utc_now() - timedelta(minutes=31)).isoformat()
    mirror.state.set(COPY_FAILED_KEY, json.dumps({"at": old}))
    assert room_verdict(mirror, _order("JPM")) == []


def test_self_simulated_paper_broker_keeps_paper_room_rules(tmp_path) -> None:
    mirror, _ = _live(tmp_path, paper_broker="self_simulated")
    assert room_verdict(mirror, _order("AAPL")) is None  # mirror() is never called there
    mirror, _ = _live(tmp_path / "b", broker_for_equities="etoro")
    assert room_verdict(mirror, _order("AAPL")) is None


def test_queue_items_are_processed_one_at_a_time(monkeypatch) -> None:
    import threading
    import time

    from app.execution.coordinator import ExecutionCoordinator

    coordinator = ExecutionCoordinator.__new__(ExecutionCoordinator)
    active, overlap = [0], [False]

    def _slow(queue_id):
        active[0] += 1
        overlap[0] = overlap[0] or active[0] > 1
        time.sleep(0.05)
        active[0] -= 1
        return queue_id

    monkeypatch.setattr(coordinator, "_process_queue_item", _slow)
    threads = [threading.Thread(target=coordinator.process_queue_item, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap[0]
