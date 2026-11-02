"""Live-vs-backtest scorecard: each eToro LIVE trade is followed from entry to close and
measured in R against what its strategy's backtest expected."""

from __future__ import annotations

import json
from datetime import timedelta

from app.broker.etoro_live_backup_stop import remember_stop
from app.broker.etoro_live_guard import run_live_checks
from app.broker.etoro_live_scorecard import (
    SCORECARD_KEY,
    record_entry,
    scorecard_report,
    update_scorecard,
)
from app.performance.strategy_evidence import VERDICTS_KEY
from app.utils.time import utc_now
from tests.test_etoro_live_backup_stop import _Client, _mirror


def _record(**overrides):
    record = {
        "symbol": "AAPL",
        "proposal_id": "p1",
        "primary_order_id": "a1",
        "etoro_order_id": "e1",
        "amount_usd": 1_000.0,
        "leverage": 1,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "strategy_name": "momentum_breakout",
    }
    record.update(overrides)
    return record


def _setup(tmp_path, client):
    mirror, logs = _mirror(tmp_path, client)
    verdicts = {
        "momentum_breakout:1d": {"passed": True, "oos_expectancy_usd": 7.0, "oos_trades": 235}
    }
    mirror.state.set(VERDICTS_KEY, json.dumps({"verdicts": verdicts}))
    record_entry(mirror, _record(), timeframe="1d", entry_price=100.0)
    return mirror, logs


def _cards(mirror):
    return json.loads(mirror.state.get(SCORECARD_KEY))


def _position(rate=100.5):
    return {"positionID": 7, "instrumentID": 1001, "openRate": rate, "units": 9.95}


def test_entry_records_the_backtest_expectation(tmp_path) -> None:
    mirror, _ = _setup(tmp_path, _Client())
    trade = _cards(mirror)["e1"]
    assert trade["expected_r"] == 0.07 and trade["planned_reward_r"] == 2.0
    assert trade["status"] == "pending_fill" and trade["timeframe"] == "1d"


def test_fill_then_target_close_measured_in_r(tmp_path) -> None:
    client = _Client(positions=[_position()])
    mirror, logs = _setup(tmp_path, client)
    assert update_scorecard(mirror) == 1
    trade = _cards(mirror)["e1"]
    assert trade["status"] == "open" and trade["open_rate"] == 100.5
    assert trade["entry_slippage_bps"] == 50.0  # filled 0.5% above the signal price
    client.positions, client.rates = [], {"AAPL": 110.2}
    assert update_scorecard(mirror) == 1
    trade = _cards(mirror)["e1"]
    assert (
        trade["exit_reason"] == "target" and trade["close_rate_source"] == "rate_when_seen_closed"
    )
    assert trade["realized_r"] == round((110.2 - 100.5) / (100.5 - 95.0), 3)
    assert trade["pnl_usd"] == round(1_000 * (110.2 / 100.5 - 1), 2)
    assert logs.events[-1][0] == "etoro_live_scorecard_closed"


def test_bot_backup_stop_close_uses_the_bot_price(tmp_path) -> None:
    client = _Client(positions=[_position(100.0)], rates={"AAPL": 94.0})
    mirror, _ = _setup(tmp_path, client)
    update_scorecard(mirror)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    run_live_checks(mirror, None)  # backup stop closes at 94.0, the scorecard books it
    trade = _cards(mirror)["e1"]
    assert trade["exit_reason"] == "bot_backup_stop" and trade["close_rate"] == 94.0
    assert trade["realized_r"] == -1.2


def test_malformed_read_and_unfilled_order(tmp_path) -> None:
    client = _Client()
    mirror, _ = _setup(tmp_path, client)
    client.fetch_raw_portfolio = lambda: {}
    assert update_scorecard(mirror) == 0 and _cards(mirror)["e1"]["status"] == "pending_fill"
    client.fetch_raw_portfolio = lambda: {"clientPortfolio": {"credit": 1.0, "positions": []}}
    cards = _cards(mirror)
    cards["e1"]["opened_at"] = (utc_now() - timedelta(hours=30)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 1 and _cards(mirror)["e1"]["status"] == "not_filled"


def test_report_compares_live_r_with_backtest(tmp_path) -> None:
    mirror, _ = _setup(tmp_path, _Client())
    cards = {}
    for i, r in enumerate([-1.0] * 9 + [0.2] * 3):
        cards[f"e{i}"] = {
            "symbol": "AAPL",
            "strategy_name": "momentum_breakout",
            "timeframe": "1d",
            "proposal_id": f"p{i}",
            "status": "closed",
            "realized_r": r,
            "expected_r": 0.07,
            "open_rate": 100.0,
            "pnl_usd": r * 50,
            "exit_reason": "stop" if r < 0 else "other",
        }
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    report = scorecard_report(mirror.state, {"p0": 99.9})
    summary = report["strategies"]["momentum_breakout:1d"]
    assert summary["closed"] == 12 and summary["verdict"] == "below_backtest"
    assert summary["exits"] == {"stop": 9, "other": 3}
    row = next(t for t in report["trades"] if t["proposal_id"] == "p0")
    assert row["fill_vs_paper_bps"] == 10.0


def test_few_trades_are_still_collecting(tmp_path) -> None:
    mirror, _ = _setup(tmp_path, _Client())
    summary = scorecard_report(mirror.state)["strategies"]["momentum_breakout:1d"]
    assert summary["verdict"] == "collecting (0/10 closed)" and summary["open"] == 1


def test_a_mirrored_entry_creates_its_scorecard_row(tmp_path) -> None:
    from tests.test_etoro_live_mirror import _Client as _MirrorClient
    from tests.test_etoro_live_mirror import _mirror as _live_mirror
    from tests.test_etoro_live_mirror import _run

    service, _ = _live_mirror(tmp_path, client=_MirrorClient())
    assert _run(service) is not None
    trade = json.loads(service.state.get(SCORECARD_KEY))["et1"]
    assert trade["symbol"] == "AAPL" and trade["timeframe"] == "1d"
    assert trade["signal_price"] == 100.0 and trade["status"] == "pending_fill"


def test_live_scorecard_endpoint_joins_the_paper_fill(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.storage.repositories import RuntimeStateRepository
    from tests.conftest import MockBroker, make_settings

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    card = {"symbol": "MSFT", "strategy_name": "momentum_breakout", "timeframe": "1d"}
    card.update(proposal_id="p1", status="open", open_rate=526.0, expected_r=0.07)
    RuntimeStateRepository(app.state.db).set(SCORECARD_KEY, json.dumps({"e1": card}))
    client = TestClient(app)
    assert client.get("/performance/live-scorecard").status_code == 403
    body = client.get("/performance/live-scorecard", headers={"X-Control-Token": "secret"}).json()
    assert body["trades"][0]["symbol"] == "MSFT"
    assert body["strategies"]["momentum_breakout:1d"]["open"] == 1


class _HistoryClient(_Client):
    """Adds eToro's closed-trade history (``GET /trading/info/trade/history``)."""

    def __init__(self, history=None, fail=False, **kwargs):
        super().__init__(**kwargs)
        self.history = history or []
        self.fail = fail
        self.history_calls = []

    def fetch_trade_history(self, min_date, *, page=1, page_size=100):
        self.history_calls.append((min_date, page, page_size))
        if self.fail:
            raise RuntimeError("Broker request failed with status 404")
        return list(self.history) if page == 1 else []


def _closed(rate, units=9.95, position_id=7):
    return {
        "positionId": position_id,
        "closeRate": rate,
        "closeTimestamp": "2026-10-15T13:27:40Z",
        "netProfit": -55.1,
        "fees": 0.0,
        "units": units,
    }


def _filled(tmp_path, client):
    mirror, logs = _setup(tmp_path, client)
    assert update_scorecard(mirror) == 1 and _cards(mirror)["e1"]["status"] == "open"
    client.positions = []
    return mirror, logs


def test_a_close_by_etoro_books_etoros_own_fill(tmp_path) -> None:
    # 2026-10-14 COST: its target filled at 972.08; a minute later the bot saw 966.82 and
    # booked "other" at +0.98 R instead of the target at +1.24 R.
    client = _HistoryClient(positions=[_position()], rates={"AAPL": 109.0})
    mirror, logs = _filled(tmp_path, client)
    client.history = [_closed(110.0)]
    assert update_scorecard(mirror) == 1
    trade = _cards(mirror)["e1"]
    assert (trade["close_rate"], trade["close_rate_source"], trade["exit_reason"]) == (
        110.0,
        "etoro_history",
        "target",
    )
    assert trade["realized_r"] == round((110.0 - 100.5) / (100.5 - 95.0), 3)
    assert trade["etoro_net_profit_usd"] == -55.1
    assert trade["etoro_closed_at"] == "2026-10-15T13:27:40Z"
    assert client.history_calls[0][0] == trade["opened_at"][:10]
    assert logs.events[-1][0] == "etoro_live_scorecard_closed"


def test_an_estimated_close_is_re_read_from_the_history(tmp_path) -> None:
    # 2026-10-15 AMD: eToro's stop filled at 541.53; the bot booked the 539.00 it saw.
    client = _HistoryClient(positions=[_position()], rates={"AAPL": 94.0})
    mirror, logs = _filled(tmp_path, client)
    assert update_scorecard(mirror) == 1  # not in the history yet: the seen price stands
    trade = _cards(mirror)["e1"]
    assert (trade["close_rate"], trade["exit_reason"]) == (94.0, "stop")
    assert trade["close_rate_source"] == "rate_when_seen_closed"
    calls = len(client.history_calls)
    assert update_scorecard(mirror) == 0 and len(client.history_calls) == calls  # just closed
    cards = _cards(mirror)
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(minutes=16)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 0 and len(client.history_calls) == calls + 1  # not yet
    assert update_scorecard(mirror) == 0 and len(client.history_calls) == calls + 1  # stamped
    client.history = [_closed(95.0)]
    assert update_scorecard(mirror) == 0  # checked a moment ago: wait 15 minutes
    cards = _cards(mirror)
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(minutes=16)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 1
    trade = _cards(mirror)["e1"]
    assert (trade["close_rate"], trade["seen_close_rate"], trade["exit_reason"]) == (
        95.0,
        94.0,
        "stop",
    )
    assert trade["close_rate_source"] == "etoro_history" and trade["realized_r"] == -1.0
    assert trade["pnl_usd"] == round(1_000 * (95.0 / 100.5 - 1), 2)
    assert logs.events[-1][0] == "etoro_live_scorecard_refined"
    calls = len(client.history_calls)
    cards = _cards(mirror)
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(hours=1)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 0 and len(client.history_calls) == calls  # done


def test_bot_exits_keep_their_reason_when_re_read(tmp_path) -> None:
    client = _HistoryClient(positions=[_position(100.0)], rates={"AAPL": 94.0})
    mirror, _ = _setup(tmp_path, client)
    update_scorecard(mirror)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    run_live_checks(mirror, None)  # the backup stop closes at the 94.0 it saw
    cards = _cards(mirror)
    assert cards["e1"]["exit_reason"] == "bot_backup_stop"
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(minutes=16)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    client.history = [_closed(93.8, units=10.0)]
    assert update_scorecard(mirror) == 1
    trade = _cards(mirror)["e1"]
    assert (trade["close_rate"], trade["exit_reason"]) == (93.8, "bot_backup_stop")


def test_no_history_keeps_estimates_and_old_closes_are_left_alone(tmp_path) -> None:
    client = _HistoryClient(positions=[_position()], rates={"AAPL": 94.0}, fail=True)
    mirror, _ = _filled(tmp_path, client)
    assert update_scorecard(mirror) == 1  # history down: booked at the seen price
    assert _cards(mirror)["e1"]["close_rate_source"] == "rate_when_seen_closed"
    client.fail, client.history = False, [_closed(95.0)]
    cards = _cards(mirror)
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(minutes=16)).isoformat()
    cards["e1"]["closed_seen_at"] = (utc_now() - timedelta(days=8)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 0  # more than a week old: not re-read
    assert _cards(mirror)["e1"]["close_rate"] == 94.0


def test_a_partly_closed_trade_waits_for_all_its_positions(tmp_path) -> None:
    client = _HistoryClient(positions=[_position()], rates={"AAPL": 94.0})
    mirror, _ = _filled(tmp_path, client)
    cards = _cards(mirror)
    cards["e1"]["position_ids"] = [7, 8]
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    client.history = [_closed(95.0)]  # position 8 is not in the history
    assert update_scorecard(mirror) == 1
    assert _cards(mirror)["e1"]["close_rate_source"] == "rate_when_seen_closed"
    client.history = [_closed(95.0, units=3.0), _closed(96.0, units=1.0, position_id=8)]
    cards = _cards(mirror)
    cards["e1"]["history_checked_at"] = (utc_now() - timedelta(minutes=16)).isoformat()
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    assert update_scorecard(mirror) == 1
    assert _cards(mirror)["e1"]["close_rate"] == 95.25  # units-weighted


def test_the_client_reads_the_documented_history_endpoint(tmp_path) -> None:
    from app.broker.etoro_client import EToroClient
    from tests.conftest import make_settings

    simulated = EToroClient(make_settings(tmp_path))
    assert simulated.fetch_trade_history("2026-10-05") == []  # never calls out in simulation
    live = EToroClient(
        make_settings(
            tmp_path,
            etoro_api_key="k",
            etoro_user_key="u",
            etoro_base_url="https://public-api.etoro.invalid",
        )
    )
    calls = []
    replies = iter([[_closed(95.0), "junk"], {"items": [_closed(96.0)]}, {"error": "x"}])

    def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, path, params))
        return next(replies)

    live._request = fake_request
    assert [r["closeRate"] for r in live.fetch_trade_history("2026-10-05")] == [95.0]
    assert calls[0] == (
        "GET",
        "/trading/info/trade/history",
        {"minDate": "2026-10-05", "page": 1, "pageSize": 100},
    )
    assert [r["closeRate"] for r in live.fetch_trade_history("2026-10-05", page=2)] == [96.0]
    assert live.fetch_trade_history("2026-10-05") == []
