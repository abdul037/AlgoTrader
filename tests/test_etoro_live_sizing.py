"""Equal-risk eToro copies, the 100% exposure cap and three copies a day (operator 2026-10-26).

The five copies since 10-05 had paper's R (MSFT +1.3, COST +1.2, XOM +2.2, AMD -1.0, META -1.0)
but eToro lost -$153 while paper made +$336: a flat ~$1,000 cap kept tight-stop copies at
0.2-0.3% risk. The stake is now min(25% of equity, $2,500, 0.5% of equity / stop distance);
open notional + the copy stays within 100% of equity (a copy that does not fit is refused,
never cut); three new copies a UTC day.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import app.broker.etoro_live_mirror as live
from app.broker.etoro_live_backup_stop import MARKS_KEY
from app.broker.etoro_live_mirror import (
    CASH_RESERVE_USD,
    HALTED_KEY,
    HARD_MAX_EXPOSURE_PCT_OF_EQUITY,
    HARD_MAX_RISK_PCT_OF_EQUITY,
    HARD_MAX_STOP_DISTANCE_PCT,
    HARD_MAX_TRADE_PCT_OF_EQUITY,
    HARD_MAX_TRADE_USD,
    HARD_MAX_TRADES_PER_DAY,
    MIN_TRADE_USD,
    STATE_KEY,
    fit_to_room,
)
from app.broker.etoro_live_room import ROOM_REASONS, open_exposure_usd, room_verdict
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from app.utils.time import utc_now
from tests.test_etoro_live_mirror import _Client, _Logs, _proposal, _run, _state
from tests.test_etoro_live_mirror import _mirror as _base_mirror

EQUITY = 9_770.0  # the real AlgoBot account, 2026-10-26


def _mirror(tmp_path, **kwargs):
    for name in ("paper_broker", "broker_for_equities"):  # production: orders reach Alpaca
        kwargs.setdefault(name, "alpaca")
    return _base_mirror(tmp_path, **kwargs)


def _account(equity=EQUITY, cash=None, open_notional=None, **extra):
    state = _state(equity=None)
    data = {"last_equity": equity, "last_cash": EQUITY if cash is None else cash}
    data["open_notional"] = open_notional or {}
    data["open_symbols"] = sorted(open_notional or {})
    data.update(extra)
    state.set(STATE_KEY, json.dumps(data))
    return state


def _blocked(logs):
    event, payload = logs.events[-1]
    return payload["reasons"] if event == "etoro_live_mirror_blocked" else []


# -- the constants the operator approved ---------------------------------------------------------
def test_the_approved_caps() -> None:
    assert (HARD_MAX_TRADE_PCT_OF_EQUITY, HARD_MAX_TRADE_USD) == (25.0, 2_500.0)
    assert (HARD_MAX_RISK_PCT_OF_EQUITY, HARD_MAX_STOP_DISTANCE_PCT) == (0.5, 8.0)  # unchanged
    assert HARD_MAX_TRADES_PER_DAY == 3 and HARD_MAX_EXPOSURE_PCT_OF_EQUITY == 100.0
    assert live.HARD_MAX_OPEN_POSITIONS == 9 and live.HARD_DAILY_LOSS_STOP_PCT == 5.0
    assert live.LEVERAGE_TEST_ENABLED is False
    assert "etoro_live_exposure_cap" in ROOM_REASONS
    assert "etoro_live_exposure_unknown" not in ROOM_REASONS


# -- sizing table at $9,770 ----------------------------------------------------------------------
# stop % below entry -> stake (min of 25% = $2,442.50, $2,500, $48.85 / stop%) -> $ at the stop.
SIZING_TABLE = [
    (2.0, 2_442.50, 48.85),  # 25% binds: risk exactly 0.5%
    (2.3, 2_123.91, 48.85),
    (2.8, 1_744.64, 48.85),
    (5.0, 977.00, 48.85),
    (7.9, 618.35, 48.85),
]


@pytest.mark.parametrize(("stop_pct", "stake", "risk"), SIZING_TABLE)
def test_sizing_table_at_9770(tmp_path, stop_pct, stake, risk) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=_account())
    record = _run(service, _proposal(stop=round(100.0 - stop_pct, 4)))
    assert record["amount_usd"] == stake and client.orders[0].amount_usd == stake
    assert client.orders[0].leverage == 1
    assert round(stake * stop_pct / 100.0, 2) == pytest.approx(risk, abs=0.01)
    assert stake * stop_pct / 100.0 <= EQUITY * HARD_MAX_RISK_PCT_OF_EQUITY / 100.0 + 1e-6


def test_a_stop_wider_than_8_pct_is_still_not_copied(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_account())
    assert _run(service, _proposal(stop=91.9)) is None and client.orders == []  # 8.1%
    assert "etoro_live_stop_too_wide" in _blocked(logs)


def test_a_bad_equity_reading_is_capped_at_2500(tmp_path) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=_account(equity=1_000_000.0, cash=50_000))
    assert _run(service, _proposal(stop=92.1))["amount_usd"] == HARD_MAX_TRADE_USD  # 7.9% stop
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_account(equity=30.0, cash=30.0))
    assert _run(service, _proposal(stop=98.0)) is None  # 25% of $30 = $7.50 < eToro's $10
    assert "etoro_live_trade_below_minimum" in _blocked(logs)


def test_the_pct_setting_cannot_raise_the_25_pct_cap(tmp_path) -> None:
    client = _Client()
    state = _account(equity=100_000.0, cash=100_000.0)
    service, _ = _mirror(tmp_path, client=client, state=state, etoro_live_trade_pct_of_equity=90)
    assert _run(service, _proposal(stop=99.0))["amount_usd"] == HARD_MAX_TRADE_USD
    client = _Client()
    state = _account(equity=4_000.0, cash=4_000.0)
    service, _ = _mirror(tmp_path, client=client, state=state, etoro_live_trade_pct_of_equity=90)
    assert _run(service, _proposal(stop=99.0))["amount_usd"] == 1_000.0  # 25% of $4,000


# -- the exposure cap ----------------------------------------------------------------------------
def test_fit_to_room_is_binary() -> None:
    # Operator 2026-10-26: a copy that does not fit is refused, never cut to a smaller copy.
    rooms = {"etoro_live_exposure_cap": 3_000.0, "etoro_live_insufficient_cash": 5_000.0}
    assert fit_to_room(2_442.5, rooms) == (2_442.5, [])
    assert fit_to_room(2_442.5, {**rooms, "etoro_live_exposure_cap": 2_442.5}) == (2_442.5, [])
    assert fit_to_room(2_442.5, {**rooms, "etoro_live_exposure_cap": 2_442.49}) == (
        None,
        ["etoro_live_exposure_cap"],
    )
    both = {"etoro_live_exposure_cap": 2_000.0, "etoro_live_insufficient_cash": 2_400.0}
    assert fit_to_room(2_442.5, both) == (None, list(both))  # each room under the stake
    nan, inf = float("nan"), float("inf")
    assert fit_to_room(2_442.5, {**rooms, "etoro_live_insufficient_cash": nan})[0] is None
    for stake in (nan, inf, -1.0, 0.0):
        assert fit_to_room(stake, rooms)[0] is None, stake


def _held(cost, *symbols):
    return {s: {"cost_usd": cost, "units": cost / 100.0} for s in symbols}


def test_the_full_stake_fits(tmp_path) -> None:
    client = _Client()
    state = _account(cash=2_600.0, open_notional=_held(2_390.0, "JPM", "XOM", "COST"))
    service, _ = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal(stop=98.0))["amount_usd"] == 2_442.5  # 7,170 + 2,442.5 <= 9,770


def test_a_stake_over_the_exposure_room_is_refused_as_a_room_reason(tmp_path) -> None:
    client = _Client()
    state = _account(cash=8_000.0, open_notional=_held(2_500.0, "JPM", "XOM", "COST"))
    service, logs = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal(stop=98.0)) is None and client.orders == []  # 2,270 < 2,442.5
    assert _blocked(logs) == ["etoro_live_exposure_cap"]
    order = _proposal(stop=98.0).order
    order.metadata["timeframe"] = "1d"
    assert room_verdict(service, order) == ["etoro_live_exposure_cap"]  # eToro's room says no
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=state)  # a 5% stop's $977 still fits
    assert _run(service, _proposal("MSFT", stop=95.0))["amount_usd"] == 977.0


def test_the_cash_rule_still_refuses_a_stake_it_cannot_pay(tmp_path) -> None:
    client = _Client()
    state = _account(cash=1_600.0, open_notional=_held(1_000.0, "JPM"))
    service, logs = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal(stop=98.0)) is None and client.orders == []  # 1,500 < 2,442.5
    assert _blocked(logs) == ["etoro_live_insufficient_cash"]


def test_non_finite_setting_equity_or_cash_never_sends_an_order(tmp_path) -> None:
    nan, inf = float("nan"), float("inf")
    for pct in (nan, inf, "nan"):
        client = _Client()
        state = _account()
        service, logs = _mirror(tmp_path, client=client, state=state, etoro_live_trade_pct_of_equity=pct)
        assert _run(service, _proposal(stop=95.0)) is None and client.orders == [], pct
        assert "etoro_live_unsized" in _blocked(logs) or pct == inf, (pct, _blocked(logs))
        order = _proposal(stop=95.0).order
        order.metadata["timeframe"] = "1d"
        assert room_verdict(service, order) is None, pct  # no copy: paper's own room decides
    for equity in (nan, inf, -inf):
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, state=_account(equity=equity))
        assert _run(service, _proposal(stop=95.0)) is None and client.orders == [], equity
        assert "etoro_live_equity_unknown" in _blocked(logs)
    for cash in (nan, inf):
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, state=_account(cash=cash))
        assert _run(service, _proposal(stop=95.0)) is None and client.orders == [], cash
        assert _blocked(logs) == ["etoro_live_insufficient_cash"]
    from app.automation.policy_snapshot import effective_execution_policy
    from tests.test_etoro_live_mirror import _settings

    snapshot = effective_execution_policy(_settings(tmp_path, etoro_live_trade_pct_of_equity=nan))
    assert snapshot["etoro_live_trade_pct_of_equity"] == 0.0


def test_in_flight_copies_and_current_value_count(tmp_path) -> None:
    state = _account(cash=8_000.0, open_notional=_held(2_000.0, "JPM", "XOM"))
    cards = {"et9": {"symbol": "COST", "status": "pending_fill", "notional_usd": 2_400.0}}
    state.set(SCORECARD_KEY, json.dumps(cards))
    view = live.EtoroLiveMirrorService._state(SimpleNamespace(state=state))
    assert open_exposure_usd(state, view) == 6_400.0  # the unfilled COST order counts
    fresh = {"rate": 110.0, "at": utc_now().isoformat()}  # JPM 20 units now $2,200
    stale = {"rate": 200.0, "at": (utc_now() - timedelta(minutes=16)).isoformat()}
    state.set(MARKS_KEY, json.dumps({"JPM": fresh, "XOM": stale}))
    assert open_exposure_usd(state, view) == 6_600.0  # a gain counts, a stale mark does not
    state.set(MARKS_KEY, json.dumps({"JPM": {"rate": 50.0, "at": utc_now().isoformat()}}))
    assert open_exposure_usd(state, view) == 6_400.0  # a loss frees no room: cost stays
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal(stop=98.0))["amount_usd"] == 2_442.5
    assert open_exposure_usd(state, service._state()) == 8_842.5  # the write-ahead counts it


def test_the_write_ahead_counts_a_copy_whose_scorecard_entry_failed(tmp_path, monkeypatch) -> None:
    import app.broker.etoro_live_scorecard as scorecard

    def broken(*args, **kwargs):
        raise RuntimeError("scorecard down")

    monkeypatch.setattr(scorecard, "record_entry", broken)  # bookkeeping only: the copy stands
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=_account(cash=9_770.0))
    assert _run(service, _proposal("AAPL", stop=98.0))["amount_usd"] == 2_442.5
    assert open_exposure_usd(service.state, service._state()) == 2_442.5
    assert _run(service, _proposal("MSFT", stop=98.0))["amount_usd"] == 2_442.5


def test_unreadable_positions_fail_closed_with_a_non_room_reason(tmp_path) -> None:
    def check(state, expect_room=True):
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, state=state)
        assert _run(service, _proposal(stop=98.0)) is None and client.orders == []
        assert _blocked(logs) == ["etoro_live_exposure_unknown"]
        order = _proposal(stop=98.0).order
        order.metadata["timeframe"] = "1d"
        assert room_verdict(service, order) is None  # paper's own room rules decide

    held_without_amount = _account()
    data = json.loads(held_without_amount.get(STATE_KEY))
    data["open_symbols"] = ["JPM"]  # reconciled before the exposure record existed
    held_without_amount.set(STATE_KEY, json.dumps(data))
    check(held_without_amount)
    check(_account(open_notional={"JPM": {"unreadable": 1.0, "units": 3.0}}))
    card = _account(open_notional={"JPM": {"unreadable": 1.0}})  # a card knows its notional,
    card.set(SCORECARD_KEY, json.dumps({"e": {"symbol": "JPM", "status": "open", "notional_usd": 9}}))
    check(card)  # but eToro's own row for it is malformed: still unknown
    broken = _account()
    broken.set(SCORECARD_KEY, "{not json")
    check(broken)
    marks = _account()
    marks.set(MARKS_KEY, json.dumps(["not", "a", "dict"]))
    check(marks)
    odd = _account()
    data = json.loads(odd.get(STATE_KEY))
    data["open_notional"] = ["JPM"]
    odd.set(STATE_KEY, json.dumps(data))
    check(odd)


def test_reconcile_records_notional_at_cost_with_leverage(tmp_path) -> None:
    positions = [
        SimpleNamespace(symbol="AAPL", position_id=1),
        SimpleNamespace(symbol="AMD", position_id=2),
        SimpleNamespace(symbol="100001", position_id=3),
    ]
    raw = [
        {"positionID": 1, "stopLossRate": 95.0, "amount": 2_400.0, "leverage": 1, "units": 9.5},
        {"positionID": 2, "stopLossRate": 541.5, "amount": 496.79, "leverage": 2, "units": 1.55},
        {"positionID": 3, "stopLossRate": 2_400.0, "units": 0.08},  # no amount: unknown
    ]
    client = _Client(positions=positions, raw_positions=raw, equity=9_770.0)
    state = _state()
    service, _ = _mirror(tmp_path, client=client, state=state)
    service.reconcile()
    notional = json.loads(state.get(STATE_KEY))["open_notional"]
    assert notional["AAPL"] == {"cost_usd": 2_400.0, "units": 9.5}
    assert notional["AMD"] == {"cost_usd": 993.58, "units": 1.55}  # margin x 2
    assert notional["100001"] == {"unreadable": 1.0, "units": 0.08}


# -- three copies a day, counted per UTC day -----------------------------------------------------
def test_three_copies_a_utc_day_then_the_next_utc_day_starts_fresh(tmp_path, monkeypatch) -> None:
    clock = {"now": datetime(2026, 10, 27, 23, 50, tzinfo=UTC)}  # 19:50 New York, Tue
    monkeypatch.setattr(live, "utc_now", lambda: clock["now"])
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_account(cash=9_770.0))
    for symbol in ("AAPL", "MSFT", "NVDA"):
        assert _run(service, _proposal(symbol, stop=95.0)), symbol
    assert _run(service, _proposal("TSLA", stop=95.0)) is None
    assert _blocked(logs) == ["etoro_live_daily_trade_cap"]
    clock["now"] = datetime(2026, 10, 28, 0, 10, tzinfo=UTC)  # still Tue 20:10 in New York
    assert _run(service, _proposal("TSLA", stop=95.0))  # the mirror counts UTC days
    assert service._state()["trades_today"] == 1


# -- parity with d2c7faf: every other rule decides exactly as before --------------------------------
BASELINE = "d2c7faf"
ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = ["AAPL", "MSFT", "NVDA", "META", "AMD", "GOOGL", "JPM", "XOM", "COST", "SPY", "TSLA"]
STAKE_REASONS = {
    "etoro_live_trade_below_minimum",
    "etoro_live_insufficient_cash",
    "etoro_live_exposure_cap",
    "etoro_live_exposure_unknown",
}


def _baseline(name: str, path: str) -> ModuleType:
    try:
        source = subprocess.run(
            ["git", "show", f"{BASELINE}:{path}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001 - a shallow clone has no baseline
        pytest.skip(f"{BASELINE} is not in this checkout")
    module = ModuleType(name)
    module.__file__ = f"<{BASELINE}:{path}>"
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
    return module


def _model(state: dict, order) -> tuple[float | None, list[str]]:
    """The approved stake and room rule, written out independently of the mirror."""

    equity, stop = state.get("last_equity"), order.stop_loss
    if not equity or equity <= 0 or stop is None or not 0 < stop < order.proposed_price:
        return None, []
    stop_pct = (order.proposed_price - stop) / order.proposed_price * 100.0
    stake = round(min(equity * 0.25, 2_500.0, equity * 0.5 / stop_pct), 2)
    if stake < 10.0:
        return None, ["etoro_live_trade_below_minimum"]
    held = sum(r["cost_usd"] for r in state["open_notional"].values())
    rooms = {"etoro_live_exposure_cap": equity - held}
    if state.get("last_cash") is not None:
        rooms["etoro_live_insufficient_cash"] = state["last_cash"] - CASH_RESERVE_USD
    short = [k for k, v in rooms.items() if v < stake]  # binary: refused, never cut
    return (None, short) if short else (stake, [])


def _case(rng: random.Random):
    held = rng.sample(UNIVERSE, rng.choice([0, 0, 1, 2, 3, 4, 5, 9]))
    equity = rng.choice([None, 0.0, 50.0, 500.0, EQUITY, EQUITY, 10_000.0, 1_000_000.0])
    cost = rng.choice([300.0, 990.0, 1_500.0, 2_400.0])
    data = {
        "day": utc_now().date().isoformat(),
        "trades_today": rng.choice([0, 1, 2, 2, 3]),
        "last_equity": equity,
        "day_start_equity": None if equity is None else equity * rng.choice([1.0, 1.0, 1.06]),
        "last_cash": rng.choice([None, 50.0, 700.0, 1_300.0, 2_000.0, 9_000.0]),
        "open_symbols": held,
        "open_notional": {s: {"cost_usd": cost} for s in held},
    }
    metadata = {}
    if rng.random() < 0.15:
        metadata = {"trade_plan": {"timing_label": "watchlist"}}
    proposal = _proposal(
        rng.choice(UNIVERSE),
        strategy=rng.choice(["momentum_breakout", "momentum_breakout", "no_evidence"]),
        side=rng.choice(["buy", "buy", "buy", "sell"]),
        stop=rng.choice([98.0, 97.7, 97.2, 95.0, 92.1, 91.9, 99.5, None]),
        target=rng.choice([110.0, 110.0, None]),
        metadata=metadata or None,
    )
    if metadata:
        proposal.order.metadata.update(
            {k: v for k, v in _proposal().order.metadata.items() if k != "trade_plan"}
        )
    proposal.order.metadata["timeframe"] = "1d"
    if rng.random() < 0.1:
        proposal.order.metadata.update({"paper_only": True, "paper_only_reason": "oos_only"})
    return data, proposal, rng.random() < 0.05


def test_parity_with_d2c7faf_on_seeded_orders(tmp_path) -> None:
    old_mirror = _baseline("_baseline_mirror", "app/broker/etoro_live_mirror.py")
    old_room = _baseline("_baseline_room", "app/broker/etoro_live_room.py")
    assert (old_mirror.HARD_MAX_TRADES_PER_DAY, old_mirror.HARD_MAX_TRADE_USD) == (2, 1_000.0)
    settings = _mirror(tmp_path)[0].settings
    rng = random.Random(20261026)
    seen = {"copied": 0, "exposure": 0, "third": 0, "same": 0, "old_refused_new_copies": 0}
    for index in range(600):
        data, proposal, halted = _case(rng)
        state = _state()
        state.set(STATE_KEY, json.dumps(data))
        if halted:
            state.set(HALTED_KEY, json.dumps({"reason": "test"}))
        new = live.EtoroLiveMirrorService(
            settings=settings, client=_Client(), runtime_state=state, run_logs=_Logs()
        )
        old = old_mirror.EtoroLiveMirrorService(  # its own default share: 10% of equity
            settings=settings.model_copy(update={"etoro_live_trade_pct_of_equity": 10.0}),
            client=new.client,
            runtime_state=state,
            run_logs=new.logs,
        )
        before, after = old.blockers(proposal, "alpaca"), new.blockers(proposal, "alpaca")
        # 1. Every rule but the stake's and the daily cap's decides exactly as at d2c7faf.
        other = STAKE_REASONS | {"etoro_live_daily_trade_cap"}
        assert [r for r in after if r not in other] == [r for r in before if r not in other], index
        # 2. Three copies a day: refused at 3 or more, no longer at 2.
        early = after in (["etoro_live_paper_only"], ["etoro_live_mirror_disabled"])
        daily = "etoro_live_daily_trade_cap" in after
        assert early or daily == (data["trades_today"] >= 3), index
        seen["third"] += data["trades_today"] == 2 and "etoro_live_daily_trade_cap" in before
        # 3. The stake's own reasons are the approved rule's, computed independently.
        if not early:
            amount, expected = _model(data, proposal.order)
            assert [r for r in after if r in STAKE_REASONS] == expected, (index, after)
        # No order old refused is copied now, except by the third daily slot -- or, on an
        # account under $100 (never the real one), a 25% stake that now reaches eToro's $10.
        if before and not after:
            assert set(before) <= {"etoro_live_daily_trade_cap", "etoro_live_trade_below_minimum"}
            assert "etoro_live_trade_below_minimum" not in before or data["last_equity"] < 100
            seen["old_refused_new_copies"] += 1
        # 4. The room verdict follows: None for any non-room reason, else eToro's room reasons.
        order = proposal.order
        verdict = room_verdict(new, order)
        if any(r not in ROOM_REASONS for r in after):
            assert verdict is None, index
        else:
            assert verdict == after, index
        if [r for r in before if r not in other] == [r for r in after if r not in other]:
            old_verdict = old_room.room_verdict(old, order)
            if not (set(before) | set(after)) & other:
                assert old_verdict == verdict, index  # untouched by either change
                seen["same"] += 1
        seen["exposure"] += "etoro_live_exposure_cap" in after
        # 5. A copy sends the approved stake at 1x with the same stop and target.
        if not after:
            client = new.client
            record = _run(new, proposal)
            amount, _ = _model(data, proposal.order)
            assert record is not None and client.orders[-1].amount_usd == amount, index
            assert client.orders[-1].leverage == 1
            assert (client.orders[-1].stop_loss, client.orders[-1].take_profit) == (
                proposal.order.stop_loss,
                proposal.order.take_profit,
            )
            seen["copied"] += 1
    assert min(seen.values()) >= 3, seen  # every path was exercised
