"""Copy paper exits to eToro LIVE: when the paper bot exits a mirrored trade, the eToro
position follows -- but only on clean, repeated evidence that paper is really flat."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.broker.etoro_live_backup_stop import STOPS_KEY, remember_stop
from app.broker.etoro_live_exit_copy import copy_paper_exits, paper_reader
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
