"""Shorts step 3: the paper lifecycle of a short (docs/notes/paper_shorts.md, section 5).

Every place that assumed a long, with a short:

* the ledger: a filled sell-to-open opens a SELL position (stop above, target below, from its BUY
  legs); a filled buy-stop or buy-limit leg, or a standalone BUY (a flatten), closes it with the
  short's P/L sign (entry - exit) and R; the bracket counts as verified on its BUY legs; a
  standalone SELL never closes a short and a standalone BUY never closes a long;
* reconciliation: a short's live buy-stop protects it; dead legs flatten it (buy to cover); a
  standalone BUY after the entry books its realized P/L with the short's sign, a SELL does not;
  a filled BUY leg is a closure; slippage is signed by the side;
* the intraday flatten and the max-hold exit close a short like a long (``close_position``);
* auto-recover resumes after a clean reconciliation of a protected short;
* the leverage simulation: a short pays eToro's overnight at 1x and is force-closed at a 100 %
  adverse move; the leaderboard and the paper-only report key shorts on their own, side shown;
* every long case keeps its old result (the existing suites run unchanged).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.automation.intraday_exit import flatten_intraday_before_close
from app.automation.max_hold_exit import close_held_past_max_hold
from app.automation.reconciliation import AlpacaReconciliationService
from app.backtesting.cost_model import CostModel
from app.main import create_app
from app.models.execution import ExecutionRecord, PortfolioPosition
from app.performance.leverage_sim import compute_leaderboard, lane_of, simulate
from app.utils.time import utc_now
from tests.conftest import MockBroker, make_settings
from tests.test_intraday_exit import _Alpaca as _IntradayAlpaca
from tests.test_intraday_exit import _execution as _intraday_execution
from tests.test_intraday_exit import _host as _intraday_host
from tests.test_max_hold_exit import MON_OPEN
from tests.test_max_hold_exit import _Alpaca as _HoldAlpaca
from tests.test_max_hold_exit import _execution as _hold_execution
from tests.test_max_hold_exit import _host as _hold_host
from tests.test_reconciliation_unprotected_positions import BrokerWithDeadBracket, _events, _service

SHORT_TAGS = {"paper_only": True, "paper_only_reason": "short", "opens_short": True, "position_side": "short"}


# --- the ledger ------------------------------------------------------------------------------


def _short_execution(*, stop_status="new", stop_fill=None, target_status="new", target_fill=None):
    """A filled sell-to-open of 10 TSLA at 200.00: stop leg a BUY stop at 206, target a BUY limit
    at 188."""

    def leg(ident, kind, status, fill, **price):
        return {
            "broker_order_id": ident,
            "side": "buy",
            "type": kind,
            "status": status,
            "qty": 10.0,
            "filled_qty": 10.0 if fill is not None else 0.0,
            "filled_avg_price": fill,
            "created_at": "2026-11-17T15:00:00+00:00",
            "filled_at": "2026-11-18T15:30:00+00:00" if fill is not None else None,
            **price,
        }

    return ExecutionRecord(
        id="exec_tsla",
        proposal_id="prop_tsla",
        status="filled",
        mode="alpaca_paper",
        broker_order_id="parent-tsla",
        request_payload={
            "symbol": "TSLA",
            "side": "sell",
            "amount_usd": 2_000.0,
            "proposed_price": 200.0,
            "stop_loss": 206.0,
            "take_profit": 188.0,
            "strategy_name": "trend_following",
            "client_order_id": "client-tsla",
            "metadata": {"timeframe": "1d", **SHORT_TAGS},
        },
        response_payload={
            "broker": "alpaca",
            "broker_execution": {
                "broker_order_id": "parent-tsla",
                "client_order_id": "client-tsla",
                "symbol": "TSLA",
                "side": "sell",
                "qty": 10.0,
                "filled_qty": 10.0,
                "filled_avg_price": 200.0,
                "order_class": "bracket",
                "status": "filled",
                "submitted_at": "2026-11-17T15:00:00+00:00",
                "filled_at": "2026-11-17T15:00:00+00:00",
                "legs": [
                    leg("leg-tp", "limit", target_status, target_fill, limit_price=188.0),
                    leg("leg-stop", "stop", stop_status, stop_fill, stop_price=206.0),
                ],
            },
        },
        created_at="2026-11-17T15:00:00+00:00",
        updated_at="2026-11-17T15:00:05+00:00",
    )


def _ledger(tmp_path):
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    return app.state.paper_trading_service


def test_a_filled_sell_to_open_opens_a_short_ledger_position(tmp_path) -> None:
    service = _ledger(tmp_path)
    service.executions.create(_short_execution())
    assert service.sync_broker_backed_positions() == {"scanned": 1, "opened": 1, "closed": 0}
    (position,) = service.positions.list(status="open")
    assert (position.side, position.quantity, position.entry_price) == ("sell", 10.0, 200.0)
    assert (position.stop_loss, position.target_1) == (206.0, 188.0)  # stop above, target below
    assert position.payload["paper_only"] is True and position.payload["paper_only_reason"] == "short"
    (record,) = service.broker_executions(limit=10)
    assert service._bracket_legs_verified(record) is True  # its protective legs are BUYs
    lifecycle = service.lifecycles(limit=10)[0]
    assert lifecycle.flags.bracket_legs_verified is True


def test_mark_to_market_of_a_short_gains_on_a_fall(tmp_path) -> None:
    from tests.test_paper_broker_ledger import _market

    service = _ledger(tmp_path)
    service.executions.create(_short_execution())
    service.refresh_open_positions(market_data_engine=_market(196.0))
    (position,) = service.positions.list(status="open")
    assert position.unrealized_pnl_usd == 40.0  # never closed by the simulator: the broker owns it


@pytest.mark.parametrize(
    ("legs", "outcome", "pnl", "r"),
    [
        (dict(stop_status="filled", stop_fill=206.5, target_status="canceled"), "stop_loss", -65.0, -1.083),
        (dict(target_status="filled", target_fill=188.0, stop_status="canceled"), "target_1", 120.0, 2.0),
    ],
)
def test_a_filled_buy_leg_closes_the_short_with_its_sign(tmp_path, legs, outcome, pnl, r) -> None:
    service = _ledger(tmp_path)
    service.executions.create(_short_execution())
    service.sync_broker_backed_positions()
    execution = service.executions.get("exec_tsla")
    execution.response_payload = _short_execution(**legs).response_payload
    service.executions.update(execution)
    assert service.sync_broker_backed_positions()["closed"] == 1
    (trade,) = service.trades.list()
    assert (trade.side, trade.outcome, trade.realized_pnl_usd) == ("sell", outcome, pnl)
    assert trade.payload["realized_r_multiple"] == r
    assert trade.closed_at.startswith("2026-11-18T15:30:00")
    assert trade.payload["leverage_sim"]["by_leverage"]["1"]["financing_usd"] > 0  # a CFD on eToro
    (record,) = service.broker_executions(limit=10)
    assert record.realized_pnl_usd == pnl  # the lifecycle view signs it too


def _standalone(order_id, side, *, price, filled_at, symbol="TSLA"):
    return dict(
        broker_order_id=order_id, execution_id=None, client_order_id=None, symbol=symbol, side=side,
        order_class="simple", status="filled", filled_qty=10.0, filled_avg_price=price, parent_order_id=None,
        payload={"filled_qty": 10.0, "filled_avg_price": price, "filled_at": filled_at, "created_at": filled_at},
    )


def test_a_standalone_buy_covers_the_short_and_a_sell_never_does(tmp_path) -> None:
    sold = _ledger(tmp_path / "sold")
    sold.executions.create(_short_execution(stop_status="canceled", target_status="canceled"))
    sold.sync_broker_backed_positions()
    sold.broker_orders.upsert(**_standalone("sell-x", "sell", price=150.0, filled_at="2026-11-18T14:00:00+00:00"))
    assert sold.sync_broker_backed_positions()["closed"] == 0  # a SELL is not a short's close
    service = _ledger(tmp_path)
    service.executions.create(_short_execution(stop_status="canceled", target_status="canceled"))
    service.sync_broker_backed_positions()
    service.broker_orders.upsert(**_standalone("cover", "buy", price=203.0, filled_at="2026-11-18T20:50:00+00:00"))
    assert service.sync_broker_backed_positions()["closed"] == 1
    (trade,) = service.trades.list()
    assert (trade.exit_price, trade.outcome, trade.realized_pnl_usd) == (203.0, "broker_close", -30.0)


def test_a_standalone_buy_never_closes_a_long(tmp_path) -> None:
    from tests.test_paper_broker_ledger import _googl_execution

    service = _ledger(tmp_path)
    service.executions.create(_googl_execution(exit_leg_status="canceled"))
    service.sync_broker_backed_positions()
    service.broker_orders.upsert(
        **_standalone("buy-x", "buy", price=351.10, filled_at="2026-09-29T13:30:05+00:00", symbol="GOOGL")
    )
    assert service.sync_broker_backed_positions()["closed"] == 0


def test_the_next_short_entry_bounds_a_short_s_standalone_close(tmp_path) -> None:
    service = _ledger(tmp_path)
    service.executions.create(_short_execution(stop_status="canceled", target_status="canceled"))
    service.sync_broker_backed_positions()
    service.broker_orders.upsert(  # a later short entry in the same symbol...
        broker_order_id="parent-tsla-2", execution_id="exec_tsla_2", client_order_id=None, symbol="TSLA",
        side="sell", order_class="bracket", status="filled", filled_qty=10.0, filled_avg_price=210.0,
        parent_order_id=None,
        payload={"filled_qty": 10.0, "filled_avg_price": 210.0, "filled_at": "2026-11-20T14:00:00+00:00"},
    )
    service.broker_orders.upsert(**_standalone("cover", "buy", price=203.0, filled_at="2026-11-21T20:50:00+00:00"))
    assert service.sync_broker_backed_positions()["closed"] == 0  # it belongs to the later entry


# --- reconciliation --------------------------------------------------------------------------


class ShortBroker(BrokerWithDeadBracket):
    """Alpaca paper holding a TSLA short: a filled sell-to-open with BUY legs."""

    def get_all_orders(self):
        tp_status, stop_status = self.leg_statuses
        parent = {
            "broker_order_id": "parent-googl",  # the execution _service creates points here
            "symbol": "TSLA",
            "side": "sell",
            "qty": 10.0,
            "filled_qty": 10.0,
            "filled_avg_price": 200.0,
            "filled_at": self.entry_filled_at,
            "order_class": "bracket",
            "status": "filled",
            "legs": [
                {"broker_order_id": "leg-tp", "side": "buy", "type": "limit", "status": tp_status, "limit_price": 188.0},
                {"broker_order_id": "leg-stop", "side": "buy", "type": "stop", "status": stop_status, "stop_price": 206.0},
            ],
        }
        return [
            SimpleNamespace(broker_order_id="parent-googl", response_payload=parent),
            *(SimpleNamespace(broker_order_id=p["broker_order_id"], response_payload=p) for p in self.extra_orders),
        ]

    def get_portfolio(self):
        return SimpleNamespace(
            positions=[PortfolioPosition(symbol="TSLA", quantity=-10.0, is_buy=False, entry_price=200.0, current_price=201.0)]
        )


def test_a_short_s_live_buy_stop_protects_it(tmp_path) -> None:
    broker = ShortBroker(leg_statuses=("new", "held"))
    service, automation, _ = _service(tmp_path, broker)
    result = service.reconcile()
    assert result["status"] == "ok" and result["issues"] == [] and broker.closed == []
    assert automation.status().kill_switch_enabled is False


def test_a_short_with_dead_legs_is_bought_back(tmp_path) -> None:
    broker = ShortBroker(leg_statuses=("expired", "canceled"))
    service, _automation, logs = _service(tmp_path, broker)
    result = service.reconcile()
    assert broker.closed == ["TSLA"] and result["issues"] == []
    assert "unprotected_position_flattened" in _events(logs)


def test_a_live_buy_stop_order_or_cover_in_flight_counts_for_a_short(tmp_path) -> None:
    stop = {"broker_order_id": "s1", "symbol": "TSLA", "side": "buy", "type": "stop", "status": "new", "legs": []}
    broker = ShortBroker(leg_statuses=("expired", "canceled"), extra_orders=[stop])
    service, _, _ = _service(tmp_path, broker)
    assert service.reconcile()["issues"] == [] and broker.closed == []
    wrong_side = {**stop, "side": "sell"}  # a sell-stop would not cap a short's loss
    broker2 = ShortBroker(leg_statuses=("expired", "canceled"), extra_orders=[wrong_side])
    service2, _, _ = _service(tmp_path / "2", broker2)
    service2.reconcile()
    assert broker2.closed == ["TSLA"]


def test_a_standalone_cover_books_the_short_s_realized_pnl(tmp_path) -> None:
    cover = {
        "broker_order_id": "close-1", "symbol": "TSLA", "side": "buy", "qty": 10.0, "filled_qty": 10.0,
        "filled_avg_price": 203.0, "status": "filled", "legs": [],
        "filled_at": (utc_now() - timedelta(hours=1)).isoformat(),
    }
    broker = ShortBroker(leg_statuses=("expired", "canceled"), extra_orders=[cover])
    service, _, _ = _service(tmp_path, broker)
    service.reconcile()
    assert service.executions.get("exec_googl").realized_pnl_usd == -30.0  # bought back 3 higher
    daily_pnl, streak = service.executions.daily_loss_stats()
    assert (daily_pnl, streak) == (-30.0, 1)
    sale = {**cover, "broker_order_id": "close-2", "side": "sell", "filled_avg_price": 150.0}
    broker2 = ShortBroker(leg_statuses=("expired", "canceled"), extra_orders=[sale])
    service2, _, _ = _service(tmp_path / "2", broker2)
    service2.reconcile()
    assert service2.executions.get("exec_googl").realized_pnl_usd == 0  # a SELL is not its exit


def test_a_filled_buy_leg_books_the_short_s_pnl_and_is_a_closure(tmp_path) -> None:
    payload = ShortBroker(leg_statuses=("canceled", "filled")).get_all_orders()[0].response_payload
    payload = copy.deepcopy(payload)
    payload["legs"][1].update(filled_avg_price=206.5, filled_qty=10.0)
    assert AlpacaReconciliationService._realized_pnl(payload) == -65.0
    assert AlpacaReconciliationService._learning_event_type(payload) == "closure"
    long_payload = copy.deepcopy(payload)
    long_payload["side"] = "buy"
    for leg in long_payload["legs"]:
        leg["side"] = "sell"
    assert AlpacaReconciliationService._realized_pnl(long_payload) == 65.0  # unchanged for a long


def test_slippage_is_signed_by_the_side(tmp_path) -> None:
    broker = ShortBroker(leg_statuses=("new", "held"))
    service, _, _ = _service(tmp_path, broker)
    execution = service.executions.get("exec_googl")
    execution.request_payload = {"proposed_price": 201.0}
    payload = broker.get_all_orders()[0].response_payload  # sold at 200.00, decided at 201.00
    service._update_execution(execution, payload)
    assert service.executions.get("exec_googl").response_payload["slippage_bps"] == pytest.approx(49.751, abs=1e-3)


def test_auto_recover_resumes_after_a_clean_reconciliation_of_a_protected_short(tmp_path) -> None:
    from app.automation.auto_recover import try_auto_recover

    broker = ShortBroker(leg_statuses=("new", "held"))
    service, automation, logs = _service(tmp_path, broker, paper_auto_recover_circuit_breaker=True)
    automation.trip_circuit_breaker(reason="missing_bracket_protection:TSLA", emergency_stop=False)
    host = SimpleNamespace(
        settings=service.settings, automation=automation, reconciliation=service,
        runtime_state=service.state, run_logs=logs, notifier=None,
    )
    assert try_auto_recover(host, list(automation.scan_blockers())) is True
    assert automation.status().kill_switch_enabled is False


# --- the intraday flatten and the max-hold exit ----------------------------------------------


def _as_short(execution):
    execution.request_payload["side"] = "sell"
    execution.request_payload["metadata"].update(SHORT_TAGS)
    execution.response_payload["broker_execution"]["side"] = "sell"
    return execution


def test_the_intraday_flatten_buys_back_an_intraday_short() -> None:
    alpaca = _IntradayAlpaca(minutes_to_close=8, held=["TSLA", "NVDA"])
    host = _intraday_host(alpaca, [_as_short(_intraday_execution("s1", "TSLA", "5m")), _as_short(_intraday_execution("s2", "NVDA", "1d"))])
    assert flatten_intraday_before_close(host) == ["TSLA"]  # the 1d short keeps its GTC bracket
    assert alpaca.closed == ["TSLA"] and set(alpaca.cancelled) == {"s1-tp", "s1-stop"}


def test_the_max_hold_exit_closes_a_short() -> None:
    alpaca = _HoldAlpaca(["TSLA"])
    host = _hold_host(alpaca, [_as_short(_hold_execution("s1", "TSLA"))])
    assert close_held_past_max_hold(host, now=MON_OPEN) == ["TSLA"]
    assert alpaca.closed == ["TSLA"]


# --- the leverage simulation and the reports ------------------------------------------------


MON = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
RATE = CostModel().overnight_fee_daily_pct


def _sim(side, *, worst=None, days=3, exit_=98.0):
    return simulate(
        side=side, entry_price=100.0, exit_price=exit_, quantity=50.0, opened_at=MON.isoformat(),
        closed_at=(MON + timedelta(days=days)).isoformat(), stop_loss=103.0 if side == "sell" else 97.0,
        worst_price=worst, marks_since=MON.isoformat() if worst else None,
    )


def test_a_short_pays_overnight_financing_at_1x_and_a_long_does_not() -> None:
    short, long = _sim("sell"), _sim("buy", exit_=102.0)
    assert short["by_leverage"]["1"]["financing_usd"] == round(5_000.0 * RATE * 3, 2)
    assert short["by_leverage"]["1"]["pnl_usd"] == round(100.0 - 5_000.0 * RATE * 3, 2)
    assert long["by_leverage"]["1"]["financing_usd"] == 0.0 and long["by_leverage"]["1"]["pnl_usd"] == 100.0
    assert short["by_leverage"]["2"]["financing_usd"] == long["by_leverage"]["2"]["financing_usd"]


def test_a_short_whose_price_doubled_loses_its_margin_even_at_1x() -> None:
    doubled = _sim("sell", worst=200.0, exit_=99.0)
    assert [doubled["by_leverage"][k]["forced_close"] for k in ("1", "2", "5", "10")] == [True] * 4
    assert doubled["by_leverage"]["1"]["gross_usd"] == -5_000.0
    long = _sim("buy", worst=1.0, exit_=101.0)  # a long at 1x can never lose more than its margin
    assert long["by_leverage"]["1"]["forced_close"] is False


def _trade(side, tags, pnl=10.0):
    return SimpleNamespace(
        strategy_name="trend_following", timeframe="1d", side=side, entry_price=100.0,
        exit_price=100.0 + (-pnl if side == "sell" else pnl) / 10.0, quantity=10.0,
        opened_at=MON.isoformat(), closed_at=(MON + timedelta(hours=2)).isoformat(),
        realized_pnl_usd=pnl, payload={**tags, "realized_r_multiple": pnl / 50.0},
    )


def test_the_leaderboard_keys_shorts_on_their_own_with_the_side() -> None:
    exploration = {"paper_only": True, "paper_only_reason": "exploration", "position_side": "short"}
    trades = [
        _trade("buy", {"paper_only": True, "paper_only_reason": "exploration"}),
        _trade("sell", exploration, pnl=-20.0),
        _trade("sell", SHORT_TAGS, pnl=30.0),
    ]
    board = compute_leaderboard(trades, now=MON)
    rows = {(row["lane"], row["key"]): row for lane in board["lanes"].values() for row in lane}
    assert set(rows) == {
        ("exploration", "trend_following:1d"),
        ("exploration", "trend_following:short:1d"),
        ("short", "trend_following:short:1d"),
    }
    short = rows[("short", "trend_following:short:1d")]
    assert (short["strategy"], short["timeframe"], short["side"]) == ("trend_following", "1d", "short")
    assert rows[("exploration", "trend_following:1d")]["side"] == "long"
    assert rows[("exploration", "trend_following:short:1d")]["by_leverage"]["1"]["total_usd"] == -20.0
    assert lane_of(SHORT_TAGS) == "short" and lane_of({}) == "normal"


def test_the_paper_only_report_shows_shorts_on_their_own(tmp_path) -> None:
    import json

    from app.performance.paper_only import paper_only_report

    app = create_app(make_settings(tmp_path), enable_background_jobs=False)
    now = utc_now()
    with app.state.db.connect() as connection:
        for ident, side, stop, current in (("l", "buy", 95.0, 110.0), ("s", "sell", 105.0, 90.0)):
            connection.execute(
                "INSERT INTO paper_positions (id, symbol, strategy_name, timeframe, side, status, quantity, "
                "entry_price, current_price, stop_loss, opened_at, updated_at, realized_pnl_usd, "
                "unrealized_pnl_usd, payload_json) VALUES (?, 'UBER', 'trend_following', '1d', ?, 'closed', "
                "10, 100, ?, ?, ?, ?, 0, 0, ?)",
                (ident, side, current, stop, now.isoformat(), now.isoformat(), json.dumps(SHORT_TAGS if side == "sell" else {"paper_only": True})),
            )
    report = paper_only_report(app.state.db, now=now)
    assert report["by_strategy"] == {
        "trend_following:1d": {"side": "long", "entries": 1, "open": 0, "closed": 1, "mean_r": 2.0},
        "trend_following:short:1d": {"side": "short", "entries": 1, "open": 0, "closed": 1, "mean_r": 2.0},
    }


def test_the_lifecycle_r_of_a_short_uses_its_buy_stop_above() -> None:
    from app.performance.routes import _r_multiple

    def item(side, stop_side, stop, pnl):
        legs = [SimpleNamespace(stop_price=stop, side=stop_side), SimpleNamespace(stop_price=None, side=stop_side)]
        execution = SimpleNamespace(quantity=10.0, filled_qty=10.0, side=side, legs=legs)
        return SimpleNamespace(entry_fill_price=200.0, realized_pnl_usd=pnl, execution=execution)

    assert _r_multiple(item("sell", "buy", 206.0, -60.0)) == -1.0
    assert _r_multiple(item("sell", "sell", 206.0, -60.0)) is None  # a sell-stop is not a short's stop
    assert _r_multiple(item("buy", "sell", 194.0, 60.0)) == 1.0  # a long, unchanged
