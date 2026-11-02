"""Copy paper exits to eToro LIVE: when the paper bot exits a mirrored trade, the eToro
position follows -- but only on clean, repeated evidence that paper is really flat."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.broker.etoro_live_backup_stop import STOPS_KEY, remember_stop
from app.broker.etoro_live_exit_copy import (
    KILL_SWITCH_KEY,
    SAFETY_KEY,
    copy_paper_exits,
    mark_paper_safety_flatten,
    paper_reader,
)
from app.broker.etoro_live_guard import run_live_checks
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from tests.test_etoro_live_backup_stop import _Client, _mirror


def _open_trade(mirror, symbol="AAPL"):
    card = {
        "symbol": symbol,
        "strategy_name": "momentum_breakout",
        "timeframe": "1d",
        "status": "open",
        "open_rate": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "notional_usd": 1_000.0,
        "position_ids": [7],
        "instrument_id": 1001,
    }
    mirror.state.set(SCORECARD_KEY, json.dumps({"e1": card}))
    remember_stop(mirror, symbol, 95.0, 1001)


def _setup(tmp_path, held, market_open=True):
    client = _Client(rates={"AAPL": 103.0}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, logs = _mirror(tmp_path, client)
    paper = {"held": set(held), "open": market_open, "fail": False}

    def read():
        if paper["fail"]:
            raise RuntimeError("alpaca timeout")
        return paper["open"], set(paper["held"]) if paper["open"] else set()

    mirror.paper_reader = read
    _open_trade(mirror)
    return mirror, client, paper, logs


def _card(mirror):
    return json.loads(mirror.state.get(SCORECARD_KEY))["e1"]


def test_paper_exit_closes_after_two_flat_reads(tmp_path) -> None:
    mirror, client, paper, logs = _setup(tmp_path, {"AAPL"})
    assert copy_paper_exits(mirror) == [] and _card(mirror)["paper_seen"] is True
    paper["held"] = set()
    assert copy_paper_exits(mirror) == [] and client.closed == []  # one flat read: wait
    closes = copy_paper_exits(mirror)
    assert client.closed == [(7, 1001)] and closes[0]["symbol"] == "AAPL"
    card = _card(mirror)
    assert card["status"] == "closed" and card["exit_reason"] == "paper_exit"
    assert card["realized_r"] == 0.6  # (103 - 100) / (100 - 95)
    assert "AAPL" not in json.loads(mirror.state.get(STOPS_KEY))
    assert logs.events[-1][0] == "etoro_live_exit_copied"


def test_a_paper_reappearance_resets_the_count(tmp_path) -> None:
    mirror, client, paper, _ = _setup(tmp_path, {"AAPL"})
    copy_paper_exits(mirror)
    paper["held"] = set()
    copy_paper_exits(mirror)
    paper["held"] = {"AAPL"}
    copy_paper_exits(mirror)
    paper["held"] = set()
    copy_paper_exits(mirror)
    assert client.closed == [] and _card(mirror)["paper_flat_reads"] == 1


def test_never_acts_when_closed_failed_or_never_seen(tmp_path) -> None:
    mirror, client, paper, logs = _setup(tmp_path, set())  # never seen on paper
    for _ in range(3):
        copy_paper_exits(mirror)
    assert client.closed == []
    paper["held"] = {"AAPL"}
    copy_paper_exits(mirror)
    paper["held"], paper["open"] = set(), False  # market closed: no counting
    for _ in range(3):
        copy_paper_exits(mirror)
    assert client.closed == [] and _card(mirror)["paper_flat_reads"] == 0
    paper["open"], paper["fail"] = True, True  # failed paper read: nothing changes
    for _ in range(3):
        copy_paper_exits(mirror)
    assert client.closed == [] and logs.events[-1][0] == "etoro_live_exit_copy_read_failed"


def test_failed_close_keeps_the_trade_open_and_retries(tmp_path) -> None:
    mirror, client, paper, logs = _setup(tmp_path, {"AAPL"})
    copy_paper_exits(mirror)
    paper["held"] = set()
    client.fail_close = True
    copy_paper_exits(mirror)
    assert copy_paper_exits(mirror) == [] and _card(mirror)["status"] == "open"
    assert logs.events[-1][0] == "etoro_live_exit_copy_failed"
    client.fail_close = False
    assert copy_paper_exits(mirror) and client.closed == [(7, 1001)]


def test_guard_runs_the_exit_copy(tmp_path) -> None:
    mirror, client, paper, _ = _setup(tmp_path, {"AAPL"})
    run_live_checks(mirror, None)
    paper["held"] = set()
    run_live_checks(mirror, None)
    assert "etoro_live_exit_copy" in run_live_checks(mirror, None)
    assert client.closed == [(7, 1001)] and _card(mirror)["exit_reason"] == "paper_exit"


def test_paper_reader_uses_the_clock_and_positions() -> None:
    position = SimpleNamespace(symbol="msft", quantity=23.0)
    flat = SimpleNamespace(symbol="AAPL", quantity=0.0)
    alpaca = SimpleNamespace(
        is_regular_market_open=lambda: True,
        get_portfolio=lambda: SimpleNamespace(positions=[position, flat]),
    )
    assert paper_reader(alpaca)() == (True, {"MSFT"})
    closed = SimpleNamespace(is_regular_market_open=lambda: False, get_portfolio=None)
    assert paper_reader(closed)() == (False, set())


def _flatten_then_two_reads(mirror, paper):
    copy_paper_exits(mirror)  # seen on paper
    paper["held"] = set()
    copy_paper_exits(mirror)
    return copy_paper_exits(mirror)


def test_emergency_flatten_on_paper_detaches_instead_of_closing(tmp_path) -> None:
    mirror, client, paper, logs = _setup(tmp_path, {"AAPL"})
    copy_paper_exits(mirror)
    mark_paper_safety_flatten(mirror.state, symbol=None, reason="emergency_stop:breaker")
    paper["held"] = set()
    for _ in range(4):
        assert copy_paper_exits(mirror) == []
    card = _card(mirror)
    assert client.closed == [] and card["status"] == "open"
    assert card["exit_copy"] == "detached" and card["detached_reason"] == "paper_emergency_flatten"
    assert sum(1 for e, _ in logs.events if e == "etoro_live_exit_copy_detached") == 1
    assert "AAPL" in json.loads(mirror.state.get(STOPS_KEY))  # backup stop still guards it


def test_kill_switch_on_detaches(tmp_path) -> None:
    mirror, client, paper, _ = _setup(tmp_path, {"AAPL"})
    mirror.state.set(KILL_SWITCH_KEY, "true")
    assert _flatten_then_two_reads(mirror, paper) == [] and client.closed == []
    assert _card(mirror)["detached_reason"] == "paper_kill_switch_on"


def test_unprotected_flatten_only_detaches_that_symbol(tmp_path) -> None:
    mirror, client, paper, _ = _setup(tmp_path, {"AAPL"})
    mark_paper_safety_flatten(mirror.state, symbol="MSFT", reason="unprotected_position")
    assert _flatten_then_two_reads(mirror, paper) and client.closed == [(7, 1001)]
    mirror2, client2, paper2, _ = _setup(tmp_path / "b", {"AAPL"})
    mark_paper_safety_flatten(mirror2.state, symbol="aapl", reason="unprotected_position")
    assert _flatten_then_two_reads(mirror2, paper2) == [] and client2.closed == []
    assert _card(mirror2)["detached_reason"] == "paper_unprotected_flatten"


def test_flatten_before_the_trade_opened_does_not_detach(tmp_path) -> None:
    mirror, client, paper, _ = _setup(tmp_path, {"AAPL"})
    mark_paper_safety_flatten(mirror.state, symbol=None, reason="emergency_stop:old")
    cards = json.loads(mirror.state.get(SCORECARD_KEY))
    cards["e1"]["opened_at"] = "2999-01-01T00:00:00+00:00"  # opened after the flatten
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert _flatten_then_two_reads(mirror, paper) and client.closed == [(7, 1001)]


def test_paper_emergency_stop_writes_the_marker_first() -> None:
    from app.automation.service import AutomationService

    seen = []
    state = {}

    class _State(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    state = _State()
    client = SimpleNamespace(
        cancel_all_orders=lambda: 0,
        close_all_positions=lambda: seen.append(state.get(SAFETY_KEY)) or 2,
    )
    service = AutomationService(
        settings=SimpleNamespace(execution_mode="paper"),
        runtime_state=state,
        run_logs=SimpleNamespace(log=lambda *a: None),
        broker_router=SimpleNamespace(all_clients=lambda: [client]),
    )
    service._emergency_stop(reason="breaker")
    assert seen and json.loads(seen[0])["all_at"]  # marked before anything closed


def test_unprotected_flatten_writes_the_symbol_marker() -> None:
    from app.automation.reconciliation import AlpacaReconciliationService

    class _State(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    recon = AlpacaReconciliationService.__new__(AlpacaReconciliationService)
    recon.settings = SimpleNamespace(execution_mode="paper", enable_real_trading=False)
    recon.state = _State()
    recon.logs = SimpleNamespace(log=lambda *a: None)
    marks = []
    recon.alpaca = SimpleNamespace(
        close_position=lambda s: marks.append(recon.state.get(SAFETY_KEY)) or SimpleNamespace()
    )
    recon._flatten_unprotected("NVDA", SimpleNamespace())
    assert marks and "NVDA" in json.loads(marks[0])["symbols"]
