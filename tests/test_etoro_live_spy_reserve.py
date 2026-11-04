"""AlgoBot's idle cash in SPY (operator 2026-11-04, "SPY A"): REAL money, so each rule is tested."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.broker.etoro_live_backup_stop as backup_stop
import app.broker.etoro_live_spy_reserve as reserve
from app.broker.etoro_live_mirror import HALTED_KEY, STATE_KEY, EtoroLiveMirrorService
from app.broker.etoro_rate_limit import EToroRateLimitError
from tests.test_etoro_live_mirror import _Logs, _PaperLedger, _proposal, _settings, _state

SPY_ID = 3000
THURSDAY_10AM_NY = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
RATE = 500.0


class _Etoro:
    """A fake live account: raw positions, cash, orders and (partial) closes."""

    def __init__(self, *, credit=6911.87, positions=None, fail_open=None, fail_close=None, settle=True):
        self.credit = credit
        self.positions = [dict(p) for p in positions or []]
        self.fail_open, self.fail_close, self.settle = fail_open, fail_close, settle
        self.orders, self.closes = [], []
        self.settings = SimpleNamespace(broker_simulation_enabled=False)

    def fetch_raw_portfolio(self):
        return {"clientPortfolio": {"credit": self.credit, "positions": [dict(p) for p in self.positions]}}

    def get_portfolio(self):
        return SimpleNamespace(account=SimpleNamespace(equity=0.0), positions=[])

    def _search_instrument(self, symbol):
        return {"instrument_id": SPY_ID if symbol == "SPY" else 1001}

    def open_market_order_by_amount(self, order, *, client_order_id=None):
        if self.fail_open:
            raise self.fail_open
        self.orders.append(order)
        if order.symbol == "SPY":
            self.positions.append(
                {"positionID": 90 + len(self.orders), "instrumentID": SPY_ID, "amount": order.amount_usd,
                 "units": order.amount_usd / RATE, "isBuy": True, "isNoStopLoss": True}
            )
        self.credit -= order.amount_usd
        return SimpleNamespace(order_id=f"o{len(self.orders)}", status="submitted")

    def close_position_by_id(self, position_id, instrument_id, *, units_to_deduct=None):
        if self.fail_close:
            raise self.fail_close
        self.closes.append((position_id, instrument_id, units_to_deduct))
        position = next(p for p in self.positions if p["positionID"] == position_id)
        units = position["units"] if units_to_deduct is None else units_to_deduct
        if self.settle:
            self.credit += units * RATE
        position["units"] -= units


def _spy(amount=4000.0, pid=7):
    return {"positionID": pid, "instrumentID": SPY_ID, "amount": amount, "units": amount / RATE,
            "isBuy": True, "isNoStopLoss": True}


def _trade(amount=2985.07, pid=1):
    return {"positionID": pid, "instrumentID": 1001, "amount": amount, "units": 10.0, "isBuy": True,
            "stopLossRate": 90.0}


def _mirror(tmp_path, client, *, state=None, open_=True, **overrides):
    overrides.setdefault("allowed_instruments", ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"])
    service = EtoroLiveMirrorService(
        settings=_settings(tmp_path, **overrides), client=client,
        runtime_state=state if state is not None else _state(), run_logs=_Logs(), paper_trades=_PaperLedger(),
    )
    service.market_open = lambda: open_
    return service


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(reserve, "utc_now", lambda: THURSDAY_10AM_NY)
    monkeypatch.setattr(backup_stop, "fresh_rate", lambda client, symbol: RATE)
    monkeypatch.setattr(reserve, "RELEASE_POLL_SECONDS", 0.0)
    monkeypatch.setattr(reserve.time, "sleep", lambda s: None)


def _events(service):
    return [e for e, _ in service.logs.events]


def test_the_idle_cash_above_the_buffer_buys_spy_once_a_day(tmp_path) -> None:
    client = _Etoro(positions=[_trade()])
    service = _mirror(tmp_path, client)
    service.state.set(STATE_KEY, json.dumps({"last_equity": 9896.94, "last_cash": 6911.87, "open_symbols": ["MSFT"]}))
    result = reserve.top_up(service)
    assert result["bought"] == round(6911.87 - reserve.CASH_BUFFER_USD - 100.0, 2) == 4311.87
    order = client.orders[0]
    assert (order.symbol, order.leverage, order.stop_loss, order.take_profit) == ("SPY", 1, None, None)
    state = json.loads(service.state.get(STATE_KEY))
    assert state["last_cash"] == 2600.0 and "SPY" in state["open_symbols"]
    assert state["open_notional"]["SPY"]["cost_usd"] == 4311.87
    assert reserve.load(service.state)["last_topup_day"] == "2026-11-05"
    assert reserve.top_up(service) is None and len(client.orders) == 1  # once a New York day
    assert "etoro_live_spy_reserve_bought" in _events(service)


@pytest.mark.parametrize(
    ("when", "open_", "settings"),
    [
        (datetime(2026, 11, 5, 14, 40, tzinfo=timezone.utc), True, {}),  # 09:40 New York: too early
        (datetime(2026, 11, 5, 20, 50, tzinfo=timezone.utc), True, {}),  # 15:50 New York: too late
        (datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc), True, {}),  # Saturday
        (THURSDAY_10AM_NY, False, {}),  # a holiday: Alpaca's clock says closed
        (THURSDAY_10AM_NY, True, {"etoro_live_spy_reserve_enabled": False}),
        (THURSDAY_10AM_NY, True, {"etoro_live_mirror_enabled": False}),
    ],
)
def test_no_buy_outside_the_window_or_when_off(tmp_path, monkeypatch, when, open_, settings) -> None:
    monkeypatch.setattr(reserve, "utc_now", lambda: when)
    client = _Etoro()
    service = _mirror(tmp_path, client, open_=open_, **settings)
    assert reserve.top_up(service) is None and client.orders == []


def test_no_buy_while_the_mirror_is_halted(tmp_path) -> None:
    client = _Etoro()
    service = _mirror(tmp_path, client)
    service.state.set(HALTED_KEY, json.dumps({"reason": "x"}))
    assert reserve.top_up(service) is None and client.orders == []


def test_the_holding_never_exceeds_60_pct_of_equity(tmp_path) -> None:
    client = _Etoro(credit=9000.0, positions=[_spy(5000.0)])  # equity 14,000
    service = _mirror(tmp_path, client)
    assert reserve.top_up(service)["bought"] == 14_000 * 0.6 - 5000.0  # not the 6,400 of cash room


def test_a_small_top_up_is_skipped_but_the_day_is_checked(tmp_path) -> None:
    client = _Etoro(credit=2620.0)
    service = _mirror(tmp_path, client)
    assert reserve.top_up(service)["bought"] == 0.0 and client.orders == []
    assert reserve.load(service.state)["last_topup_day"] == "2026-11-05"


def test_a_rate_limit_retries_later_and_a_refusal_waits_for_tomorrow(tmp_path) -> None:
    service = _mirror(tmp_path, _Etoro(fail_open=EToroRateLimitError("eToro API rate-limited")))
    service.state.set(STATE_KEY, json.dumps({"last_equity": 6911.87, "last_cash": 6911.87}))
    assert reserve.top_up(service) is None
    assert "last_topup_day" not in reserve.load(service.state)  # the next tick tries again
    assert json.loads(service.state.get(STATE_KEY))["last_cash"] == 6911.87  # nothing spent
    refused = _mirror(tmp_path, _Etoro(fail_open=RuntimeError("Broker request failed with status 400")))
    assert reserve.top_up(refused) is None and reserve.load(refused.state)["last_topup_day"] == "2026-11-05"
    unknown = _mirror(tmp_path, _Etoro(fail_open=RuntimeError("read timed out")))
    unknown.state.set(STATE_KEY, json.dumps({"last_equity": 6911.87, "last_cash": 6911.87}))
    assert reserve.top_up(unknown) is None and reserve.load(unknown.state)["last_topup_day"] == "2026-11-05"
    assert json.loads(unknown.state.get(STATE_KEY))["last_cash"] < 6911.87  # write-ahead kept
    assert not unknown.state.get(HALTED_KEY)  # a SPY buy never halts the copies


def test_reconcile_keeps_the_stopless_spy_but_closes_other_unprotected_positions(tmp_path) -> None:
    from tests.test_etoro_live_mirror import _Client

    positions = [SimpleNamespace(symbol="SPY", position_id=7), SimpleNamespace(symbol="MSFT", position_id=2)]
    raw = [{"positionID": 7, "isNoStopLoss": True, "amount": 4000.0}, {"positionID": 2, "isNoStopLoss": True, "amount": 900.0}]
    client = _Client(positions=positions, raw_positions=raw)
    _mirror(tmp_path, client).reconcile()
    assert client.closed == ["MSFT"]
    off = _Client(positions=positions, raw_positions=raw)
    _mirror(tmp_path, off, etoro_live_spy_reserve_enabled=False).reconcile()
    assert off.closed == ["SPY", "MSFT"]  # off means out of SPY


def test_reconcile_knows_spy_by_its_id_when_the_name_lookup_failed(tmp_path) -> None:
    from tests.test_etoro_live_mirror import _Client

    positions = [SimpleNamespace(symbol=str(SPY_ID), position_id=7, instrument_id=SPY_ID)]
    client = _Client(positions=positions, raw_positions=[{"positionID": 7, "isNoStopLoss": True, "amount": 4000.0}])
    client.close_position_by_id = lambda *a, **k: pytest.fail("the SPY reserve was closed")
    service = _mirror(tmp_path, client)
    service.state.set(reserve.RESERVE_KEY, json.dumps({"instrument_id": SPY_ID}))
    assert service.reconcile()["open_symbols"] == ["SPY"]
    assert json.loads(service.state.get(STATE_KEY))["open_notional"]["SPY"]["cost_usd"] == 4000.0


def _copy_state(service, *, cash, spy_cost=6000.0, symbols=()):
    state = json.loads(service.state.get(STATE_KEY))
    state.update(last_cash=cash, day_start_equity=10_000.0, open_symbols=sorted({"SPY", *symbols}),
                 open_notional={"SPY": {"cost_usd": spy_cost}, **{s: {"cost_usd": 100.0} for s in symbols}})
    service.state.set(STATE_KEY, json.dumps(state))


def _copy(service):
    return service.mirror(proposal=_proposal(), primary_execution=SimpleNamespace(broker_order_id="a"), primary_broker="alpaca")


def test_spy_counts_toward_neither_the_position_nor_the_group_cap(tmp_path) -> None:
    service = _mirror(tmp_path, _Etoro(credit=5000.0, positions=[_spy(6000.0)]))
    _copy_state(service, cash=5000.0, symbols=[f"S{i}" for i in range(8)])
    assert "etoro_live_open_position_cap" not in service.blockers(_proposal(), "alpaca")
    _copy_state(service, cash=5000.0, symbols=[f"S{i}" for i in range(9)])
    assert "etoro_live_open_position_cap" in service.blockers(_proposal(), "alpaca")
    spy_order = _proposal(symbol="SPY")
    assert "etoro_live_symbol_already_open" in service.blockers(spy_order, "alpaca")


def test_a_copy_that_needs_the_cash_sells_just_enough_spy_first(tmp_path) -> None:
    client = _Etoro(credit=1000.0, positions=[_spy(6000.0)])
    service = _mirror(tmp_path, client)
    _copy_state(service, cash=1000.0)
    assert service.blockers(_proposal(), "alpaca") == []  # the SPY counts as room
    record = _copy(service)
    assert record["symbol"] == "AAPL" and record["amount_usd"] == 1000.0  # 0.5% risk at a 5% stop
    (position_id, instrument_id, units), = client.closes
    assert (position_id, instrument_id) == (7, SPY_ID)
    assert units == pytest.approx(100.0 * 1.01 / RATE)  # the $100 shortfall plus 1%
    assert [o.symbol for o in client.orders] == ["AAPL"]
    released = dict(service.logs.events)["etoro_live_spy_reserve_released"]
    assert released["cash_confirmed"] is True and released["short_usd"] == 100.0


def test_no_sale_when_the_free_cash_covers_the_copy(tmp_path) -> None:
    client = _Etoro(credit=5000.0, positions=[_spy(6000.0)])
    service = _mirror(tmp_path, client)
    _copy_state(service, cash=5000.0)
    assert _copy(service)["symbol"] == "AAPL" and client.closes == []


def test_a_failed_sale_refuses_that_copy_only(tmp_path) -> None:
    client = _Etoro(credit=1000.0, positions=[_spy(6000.0)], fail_close=RuntimeError("eToro 500"))
    service = _mirror(tmp_path, client)
    _copy_state(service, cash=1000.0)
    assert _copy(service) is None and client.orders == []
    blocked = [p for e, p in service.logs.events if e == "etoro_live_mirror_blocked"]
    assert blocked[-1]["reasons"][0].startswith("etoro_live_spy_reserve_release_failed")
    assert not service.state.get(HALTED_KEY)


def test_the_whole_lot_is_sold_rather_than_leave_crumbs(tmp_path) -> None:
    client = _Etoro(credit=100.0, positions=[_spy(1010.0)])  # 2.02 units of SPY
    service = _mirror(tmp_path, client)
    state = {"last_cash": 100.0, "open_notional": {"SPY": {"cost_usd": 1010.0}}}
    reserve.release_for_copy(service, state, 1000.0)  # needs 2.02 units: nothing worth keeping is left
    assert client.closes == [(7, SPY_ID, None)]


def test_the_live_client_sends_the_units_to_sell(tmp_path) -> None:
    from app.broker.etoro_client import EToroClient

    sent = []
    client = SimpleNamespace(
        settings=SimpleNamespace(broker_simulation_enabled=False, etoro_account_mode="real"),
        _ensure_order_mode_allowed=lambda: None,
        _trading_execution_path=lambda suffix: suffix,
        _normalize_order_status=EToroClient._normalize_order_status,
        _request=lambda method, path, json_body=None, **kw: sent.append(json_body) or {"orderForClose": {}},
    )
    EToroClient.close_position_by_id(client, 7, SPY_ID, units_to_deduct=0.25)
    EToroClient.close_position_by_id(client, 8, SPY_ID)
    assert sent == [{"InstrumentID": SPY_ID, "UnitsToDeduct": 0.25}, {"InstrumentID": SPY_ID, "UnitsToDeduct": None}]


def test_the_guard_runs_the_top_up(monkeypatch) -> None:
    import app.broker.etoro_live_guard as guard

    for name in ("check_backup_stops", "copy_paper_exits", "update_scorecard", "maybe_send_daily_report"):
        monkeypatch.setattr(guard, name, lambda *a, **k: 0)
    monkeypatch.setattr(guard, "top_up", lambda mirror: {"bought": 4311.87})
    mirror = SimpleNamespace(logs=_Logs(), state={})
    assert "etoro_live_spy_reserve" in guard.run_live_checks(mirror, None)


def test_the_guard_prices_the_spy_and_records_the_account_value(tmp_path, monkeypatch) -> None:
    client = _Etoro(credit=2600.0, positions=[_spy(4311.87, pid=7)])
    service = _mirror(tmp_path, client)
    service.state.set(reserve.RESERVE_KEY, json.dumps({"instrument_id": SPY_ID}))
    monkeypatch.setattr(backup_stop, "utc_now", lambda: THURSDAY_10AM_NY)
    monkeypatch.setattr(backup_stop, "fresh_rate", lambda client, symbol: 510.0)  # bought at 500
    backup_stop.check_backup_stops(service)  # no strategy stops at all: SPY alone is priced
    assert json.loads(service.state.get(backup_stop.MARKS_KEY))["SPY"]["rate"] == 510.0
    rows = json.loads(service.state.get(backup_stop.POSITIONS_KEY))["positions"]
    assert [r["symbol"] for r in rows] == ["SPY"]
    history = json.loads(service.state.get(backup_stop.VALUE_KEY))
    units = 4311.87 / RATE
    assert history["day"] == "2026-11-05" and history["points"][-1][1] == round(2600.0 + units * 510.0, 2)


def test_the_value_history_starts_each_new_york_day_from_the_last_close(tmp_path, monkeypatch) -> None:
    client = _Etoro(credit=2600.0, positions=[_spy(4311.87, pid=7)])
    service = _mirror(tmp_path, client)
    service.state.set(reserve.RESERVE_KEY, json.dumps({"instrument_id": SPY_ID}))
    service.state.set(backup_stop.VALUE_KEY, json.dumps({"day": "2026-11-04", "prev_close": 9800.0, "points": [["t", 9900.0]]}))
    monkeypatch.setattr(backup_stop, "utc_now", lambda: THURSDAY_10AM_NY)
    backup_stop.check_backup_stops(service)
    history = json.loads(service.state.get(backup_stop.VALUE_KEY))
    assert history["prev_close"] == 9900.0 and len(history["points"]) == 1


def test_nothing_is_read_without_stops_or_a_spy_reserve(tmp_path) -> None:
    client = _Etoro()
    client.fetch_raw_portfolio = lambda: pytest.fail("no eToro read needed")
    assert backup_stop.check_backup_stops(_mirror(tmp_path, client)) is None
