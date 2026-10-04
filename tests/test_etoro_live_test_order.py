"""One-off eToro LIVE crypto test order: inert without a request, every lock enforced,
bracket from daily ATR, watched to the close. A fake client records what would be sent."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
from app.broker.etoro_live_mirror import HALTED_KEY, EtoroLiveMirrorService
from app.broker.etoro_live_test_order import (
    HARD_MAX_TEST_ORDER_USD,
    REQUEST_KEY,
    TEST_STATE_KEY,
    EtoroLiveTestOrder,
    bracket_from_atr,
    run_from_maintenance,
)
from app.utils.time import utc_now
from tests.conftest import make_settings


class _State(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, json.loads(json.dumps(payload))))


class _Client:
    def __init__(self, *, fail_open=False, tradable=True):
        self.fail_open = fail_open
        self.tradable = tradable
        self.posted = []
        self.closed = []
        self.positions = []
        self.equity = 10_000.0
        self.settings = SimpleNamespace(broker_simulation_enabled=False)

    def _search_instrument(self, symbol):
        return {
            "symbol": symbol,
            "instrument_id": 100001,
            "current_rate": 2_000.0,
            "is_tradable": self.tradable,
            "is_buy_enabled": True,
        }

    def _ensure_order_mode_allowed(self):
        return None

    def _trading_execution_path(self, suffix):
        return f"/trading/execution/{suffix}"

    def _request(self, method, path, *, json_body=None, params=None):
        if self.fail_open:
            raise RuntimeError("Broker request failed with status 403: InsufficientPermissions")
        self.posted.append((method, path, json_body))
        return {"orderForOpen": {"orderID": 555, "statusID": 1}}

    def fetch_raw_portfolio(self):
        return {"clientPortfolio": {"positions": self.positions}}

    def get_portfolio(self):
        return SimpleNamespace(account=SimpleNamespace(equity=self.equity), positions=[])

    def close_position_by_id(self, position_id, instrument_id):
        self.closed.append((position_id, instrument_id))
        self.positions = [p for p in self.positions if p["positionID"] != position_id]


def _bars(symbol, *, timeframe, start, end):
    # 30 daily bars with a constant 60-point range -> ATR(14) = 60.
    rows = [
        {
            "timestamp": start + timedelta(days=i),
            "open": 2_000,
            "high": 2_030,
            "low": 1_970,
            "close": 2_000,
        }
        for i in range(30)
    ]
    return pd.DataFrame(rows)


def _tester(tmp_path, client=None, state=None, bars=_bars, **overrides):
    values = dict(
        etoro_live_mirror_enabled=True, etoro_live_acknowledgement=LIVE_OPERATOR_ACKNOWLEDGEMENT
    )
    values.update(overrides)
    logs = _Logs()
    mirror = EtoroLiveMirrorService(
        settings=make_settings(tmp_path, **values),
        client=client if client is not None else _Client(),
        runtime_state=state if state is not None else _State(),
        run_logs=logs,
    )
    return EtoroLiveTestOrder(mirror=mirror, bars=bars), logs


def _request(state, symbol="ETH", amount=200):
    state.set(REQUEST_KEY, json.dumps({"symbol": symbol, "amount_usd": amount}))


def test_does_nothing_without_a_request(tmp_path) -> None:
    client = _Client()
    tester, logs = _tester(tmp_path, client=client)
    assert tester.run() is None and client.posted == [] and logs.events == []


def test_bracket_is_1_5_atr_stop_and_2r_target_with_floors() -> None:
    assert bracket_from_atr(2_000.0, 60.0) == (1_910.0, 2_180.0, 90.0)
    assert bracket_from_atr(2_000.0, 1.0)[0] == 1_980.0  # 1% minimum stop distance
    assert bracket_from_atr(2_000.0, 500.0)[0] == 1_800.0  # 10% maximum stop distance


def test_opens_200_usd_eth_at_1x_with_stop_and_target(tmp_path) -> None:
    client, state = _Client(), _State()
    _request(state)
    tester, logs = _tester(tmp_path, client=client, state=state)
    test = tester.run()
    method, path, body = client.posted[0]
    assert path.endswith("market-open-orders/by-amount")
    assert body["InstrumentID"] == 100001 and body["Amount"] == 200.0 and body["Leverage"] == 1
    assert body["IsBuy"] is True and body["IsNoStopLoss"] is False
    assert (body["StopLossRate"], body["TakeProfitRate"]) == (1_910.0, 2_180.0)
    assert test["status"] == "open" and state.get(REQUEST_KEY) == ""  # one-shot
    assert logs.events[-1][0] == "etoro_live_test_order_submitted"


def test_amount_is_hard_capped(tmp_path) -> None:
    client, state = _Client(), _State()
    _request(state, amount=5_000)
    tester, _ = _tester(tmp_path, client=client, state=state)
    tester.run()
    assert client.posted[0][2]["Amount"] == HARD_MAX_TEST_ORDER_USD == 1_000.0


def test_amount_never_exceeds_10_pct_of_the_algobot_balance(tmp_path) -> None:
    client, state = _Client(), _State()
    state.set("etoro_live:state", json.dumps({"last_equity": 5_000.0}))
    _request(state, amount=1_000)
    tester, _ = _tester(tmp_path, client=client, state=state)
    tester.run()
    assert client.posted[0][2]["Amount"] == 500.0


def test_each_lock_blocks_and_nothing_is_sent(tmp_path) -> None:
    cases = [
        ({"etoro_live_mirror_enabled": False}, "ETH", "etoro_live_mirror_disabled"),
        ({"etoro_live_acknowledgement": "yes"}, "ETH", "etoro_live_acknowledgement_missing"),
        ({}, "DOGE", "test_symbol_not_allowed:DOGE"),
    ]
    for overrides, symbol, reason in cases:
        client, state = _Client(), _State()
        _request(state, symbol=symbol)
        tester, logs = _tester(tmp_path, client=client, state=state, **overrides)
        test = tester.run()
        assert client.posted == [] and test["status"] == "failed" and reason in test["reason"], (
            reason
        )
    client, state = _Client(), _State()
    state.set(HALTED_KEY, json.dumps({"reason": "x"}))
    _request(state)
    tester, _ = _tester(tmp_path, client=client, state=state)
    assert "etoro_live_mirror_halted" in tester.run()["reason"] and client.posted == []


def test_broker_error_is_logged_and_never_halts_the_mirror(tmp_path) -> None:
    state = _State()
    _request(state)
    tester, logs = _tester(tmp_path, client=_Client(fail_open=True), state=state)
    test = tester.run()
    assert test["status"] == "failed" and "403" in test["reason"]
    assert not state.get(HALTED_KEY) and logs.events[-1][0] == "etoro_live_test_order_failed"


def test_not_tradable_is_refused(tmp_path) -> None:
    client, state = _Client(tradable=False), _State()
    _request(state)
    tester, _ = _tester(tmp_path, client=client, state=state)
    assert tester.run()["reason"] == "ETH_not_tradable_on_etoro_now" and client.posted == []


def test_watch_fill_then_close_at_broker(tmp_path) -> None:
    client, state = _Client(), _State()
    _request(state)
    tester, logs = _tester(tmp_path, client=client, state=state)
    tester.run()
    client.positions = [
        {
            "positionID": 9,
            "instrumentID": 100001,
            "openRate": 2_001.5,
            "units": 0.0999,
            "stopLossRate": 1_910.0,
            "takeProfitRate": 2_180.0,
        }
    ]
    assert tester.run()["position_id"] == 9
    assert logs.events[-1][0] == "etoro_live_test_order_filled"
    client.positions, client.equity = [], 10_017.0  # target hit at eToro
    closed = tester.run()
    assert (
        closed["status"] == "closed"
        and closed["close_reason"] == "closed_at_broker_by_stop_or_target"
    )
    assert closed["approx_pnl_usd"] == 17.0
    assert tester.run() is None  # finished; no new order without a new request


def test_unprotected_or_expired_position_is_closed_by_the_bot(tmp_path) -> None:
    client, state = _Client(), _State()
    _request(state)
    tester, _ = _tester(tmp_path, client=client, state=state)
    tester.run()
    client.positions = [
        {"positionID": 9, "instrumentID": 100001, "stopLossRate": 0, "isNoStopLoss": True}
    ]
    assert tester.run()["close_reason"] == "no_stop_at_broker" and client.closed == [(9, 100001)]

    client, state = _Client(), _State()
    _request(state)
    tester, _ = _tester(tmp_path, client=client, state=state)
    tester.run()
    test = json.loads(state.get(TEST_STATE_KEY))
    test["max_hold_until"] = (utc_now() - timedelta(minutes=1)).isoformat()
    state.set(TEST_STATE_KEY, json.dumps(test))
    client.positions = [{"positionID": 9, "instrumentID": 100001, "stopLossRate": 1_910.0}]
    assert tester.run()["close_reason"] == "time_stop" and client.closed == [(9, 100001)]


def test_no_position_after_grace_is_reported(tmp_path) -> None:
    client, state = _Client(), _State()
    _request(state)
    tester, _ = _tester(tmp_path, client=client, state=state)
    tester.run()
    assert tester.run()["status"] == "open"  # inside the grace window
    test = json.loads(state.get(TEST_STATE_KEY))
    test["opened_at"] = (utc_now() - timedelta(hours=1)).isoformat()
    state.set(TEST_STATE_KEY, json.dumps(test))
    assert tester.run()["close_reason"] == "no_position_seen_order_likely_rejected"


def test_maintenance_hook_survives_errors(tmp_path) -> None:
    logs = _Logs()
    boom = SimpleNamespace(run=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    service = SimpleNamespace(
        auto_trading=SimpleNamespace(execution=SimpleNamespace(etoro_live_test_order=boom)),
        run_logs=logs,
    )
    completed: list[str] = []
    run_from_maintenance(service, completed)
    assert completed == [] and logs.events[-1][0] == "etoro_live_test_order_error"
