"""Backup stop for eToro LIVE positions: eToro may set its own (wider) stop, so the bot
closes a live position itself once a fresh price reaches the strategy's stop."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
from app.broker.etoro_live_backup_stop import (
    STOPS_KEY,
    check_backup_stops,
    fresh_rate,
    remember_stop,
)
from app.broker.etoro_live_mirror import HALTED_KEY, EtoroLiveMirrorService
from app.broker.etoro_live_test_order import REQUEST_KEY, EtoroLiveTestOrder
from app.utils.time import utc_now
from tests.conftest import make_settings
from tests.test_etoro_live_test_order import _bars


class _State(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, payload))


class _Client:
    """Fake eToro client: search returns ``rates``; positions are raw portfolio rows."""

    def __init__(self, rates=None, positions=None, fail_close=False):
        self.rates = rates or {}
        self.positions = positions or []
        self.fail_close = fail_close
        self.closed = []
        self.settings = SimpleNamespace(broker_simulation_enabled=False)
        self._instrument_cache_by_symbol = {}

    def _request(self, method, path, *, params=None, json_body=None):
        if method == "GET":
            symbol = params["internalSymbolFull"]
            rate = self.rates.get(symbol)
            items = [{"internalSymbolFull": symbol, "currentRate": rate}] if rate else []
            return {"items": items + [{"internalSymbolFull": symbol + "X", "currentRate": 1.0}]}
        return {"orderForOpen": {"orderID": 1, "statusID": 1}}

    def _search_instrument(self, symbol):
        return {
            "symbol": symbol,
            "instrument_id": {"AAPL": 1001, "ETH": 100001}[symbol],
            "current_rate": 2_000.0,
            "is_tradable": True,
            "is_buy_enabled": True,
        }

    def _ensure_order_mode_allowed(self):
        return None

    def _trading_execution_path(self, suffix):
        return f"/trading/execution/{suffix}"

    def fetch_raw_portfolio(self):
        return {"clientPortfolio": {"credit": 9_000.0, "positions": self.positions}}

    def close_position_by_id(self, position_id, instrument_id):
        if self.fail_close:
            raise RuntimeError("eToro 500")
        self.closed.append((position_id, instrument_id))
        self.positions = [p for p in self.positions if p["positionID"] != position_id]


def _mirror(tmp_path, client, state=None):
    settings = make_settings(
        tmp_path,
        etoro_live_mirror_enabled=True,
        etoro_live_acknowledgement=LIVE_OPERATOR_ACKNOWLEDGEMENT,
    )
    logs = _Logs()
    return EtoroLiveMirrorService(
        settings=settings,
        client=client,
        runtime_state=state if state is not None else _State(),
        run_logs=logs,
    ), logs


def test_fresh_rate_uses_the_exact_symbol_match() -> None:
    client = _Client(rates={"ETH": 2_650.5})
    assert fresh_rate(client, "eth") == 2_650.5
    assert fresh_rate(_Client(), "ETH") is None


def test_mirrored_position_closed_when_price_reaches_strategy_stop(tmp_path) -> None:
    client = _Client(rates={"AAPL": 94.9}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, logs = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    closed = check_backup_stops(mirror)
    assert client.closed == [(7, 1001)] and closed[0]["symbol"] == "AAPL"
    assert logs.events[-1][0] == "etoro_live_backup_stop_closed"
    assert json.loads(mirror.state.get(STOPS_KEY)) == {}


def test_position_above_its_stop_is_kept(tmp_path) -> None:
    client = _Client(rates={"AAPL": 99.0}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, _ = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, None)  # instrument id resolved at check time
    assert check_backup_stops(mirror) == [] and client.closed == []
    assert json.loads(mirror.state.get(STOPS_KEY))["AAPL"]["instrument_id"] == 1001


def test_unseen_entry_ages_out_and_no_price_never_closes(tmp_path) -> None:
    client = _Client(rates={}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, _ = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    assert check_backup_stops(mirror) == [] and client.closed == []  # no price: eToro's stop holds
    stops = json.loads(mirror.state.get(STOPS_KEY))
    stops["AAPL"]["recorded_at"] = (utc_now() - timedelta(hours=2)).isoformat()
    mirror.state.set(STOPS_KEY, json.dumps(stops))
    client.positions = []  # closed at eToro by its own stop or target
    check_backup_stops(mirror)
    assert json.loads(mirror.state.get(STOPS_KEY)) == {}


def test_failed_close_halts_new_entries_and_keeps_the_stop(tmp_path) -> None:
    client = _Client(
        rates={"AAPL": 90.0}, positions=[{"positionID": 7, "instrumentID": 1001}], fail_close=True
    )
    mirror, logs = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    check_backup_stops(mirror)
    assert mirror.state.get(HALTED_KEY) and "AAPL" in json.loads(mirror.state.get(STOPS_KEY))
    assert any(event == "etoro_live_backup_stop_close_failed" for event, _ in logs.events)


def test_mirror_entry_records_its_strategy_stop(tmp_path) -> None:
    from app.models.signal import Signal, SignalAction
    from app.models.trade import TradeOrder
    from app.performance.strategy_evidence import VERDICTS_KEY

    client = _Client()
    client._instrument_cache_by_symbol = {"AAPL": {"instrument_id": 1001}}
    client.open_market_order_by_amount = lambda order, client_order_id=None: SimpleNamespace(
        order_id="e1", status="submitted"
    )
    state = _State()
    state.set(VERDICTS_KEY, json.dumps({"verdicts": {"momentum_breakout:1d": {"passed": True}}}))
    state.set("etoro_live:state", json.dumps({"last_equity": 10_000.0}))
    mirror, _ = _mirror(tmp_path, client, state=state)
    mirror.settings = mirror.settings.model_copy(update={"allowed_instruments": ["AAPL"]})
    order = TradeOrder(
        symbol="AAPL",
        amount_usd=5_000,
        leverage=1,
        proposed_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        strategy_name="momentum_breakout",
        side="buy",
    )
    signal = Signal(
        symbol="AAPL",
        strategy_name="momentum_breakout",
        action=SignalAction.BUY,
        rationale="t",
        metadata={"timeframe": "1d"},
    )
    proposal = SimpleNamespace(id="p1", order=order, signal=signal)
    assert mirror.mirror(
        proposal=proposal,
        primary_execution=SimpleNamespace(broker_order_id="a1"),
        primary_broker="alpaca",
    )
    assert json.loads(state.get(STOPS_KEY))["AAPL"]["stop"] == 95.0


def test_eth_test_closed_by_the_bot_at_the_strategy_stop(tmp_path) -> None:
    client = _Client(rates={})
    state = _State()
    state.set(REQUEST_KEY, json.dumps({"symbol": "ETH", "amount_usd": 1_000}))
    mirror, logs = _mirror(tmp_path, client, state=state)
    tester = EtoroLiveTestOrder(mirror=mirror, bars=_bars)
    opened = tester.run()  # rate 2,000, ATR 60 -> stop 1,910
    assert opened["stop_loss"] == 1_910.0
    client.positions = [
        {"positionID": 9, "instrumentID": 100001, "stopLossRate": 1_800.0, "openRate": 2_000.0}
    ]
    client.rates = {"ETH": 1_950.0}
    assert tester.run()["status"] == "open" and client.closed == []  # above the stop
    client.rates = {"ETH": 1_905.0}
    closed = tester.run()
    assert closed["close_reason"] == "bot_backup_stop" and client.closed == [(9, 100001)]
