"""Real Alpaca-paper fills must land in the internal P&L ledger.

Before this, a broker-backed paper trade existed only as an execution + broker
order snapshots; paper_positions / paper_trades (and everything that reads
them: equity curve, EOD digest, strategy scorecard) never saw it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.main import create_app
from app.models.execution import ExecutionRecord
from tests.conftest import MockBroker, make_settings


def _googl_execution(*, exit_leg_status: str = "new", exit_fill: float | None = None) -> ExecutionRecord:
    stop_leg = {
        "broker_order_id": "leg-stop",
        "side": "sell",
        "type": "stop",
        "status": exit_leg_status,
        "qty": 1.0,
        "filled_qty": 1.0 if exit_fill is not None else 0.0,
        "filled_avg_price": exit_fill,
        "stop_price": 333.24,
        "created_at": "2026-09-08T17:00:50+00:00",
        "filled_at": "2026-09-08T19:12:03+00:00" if exit_fill is not None else None,
    }
    target_leg = {
        "broker_order_id": "leg-target",
        "side": "sell",
        "type": "limit",
        "status": "new",
        "qty": 1.0,
        "filled_qty": 0.0,
        "limit_price": 349.39,
        "created_at": "2026-09-08T17:00:50+00:00",
    }
    return ExecutionRecord(
        id="exec_googl",
        proposal_id="prop_googl",
        status="filled",
        mode="alpaca_paper",
        broker_order_id="parent-googl",
        request_payload={
            "symbol": "GOOGL",
            "side": "buy",
            "amount_usd": 500.0,
            "proposed_price": 338.62,
            "stop_loss": 333.235,
            "take_profit": 349.39,
            "strategy_name": "opening_range_breakout_retest",
            "client_order_id": "client-googl",
            "metadata": {"timeframe": "5m", "risk_reward_ratio": 2.0},
        },
        response_payload={
            "broker": "alpaca",
            "fill_price": 338.75,
            "broker_execution": {
                "broker_order_id": "parent-googl",
                "client_order_id": "client-googl",
                "symbol": "GOOGL",
                "side": "buy",
                "qty": 1.0,
                "filled_qty": 1.0,
                "filled_avg_price": 338.75,
                "order_class": "bracket",
                "status": "filled",
                "submitted_at": "2026-09-08T17:00:50+00:00",
                "filled_at": "2026-09-08T17:00:50+00:00",
                "legs": [stop_leg, target_leg],
            },
        },
        created_at="2026-09-08T17:00:50+00:00",
        updated_at="2026-09-08T17:00:57+00:00",
    )


class _Quote:
    def __init__(self, price: float) -> None:
        self.last_execution = price
        self.ask = price
        self.bid = price
        self.timestamp = "2026-09-08T18:00:00+00:00"


def _market(price: float):
    return SimpleNamespace(get_quote=lambda symbol, timeframe=None, force_refresh=False: _Quote(price))


def test_filled_bracket_entry_opens_a_ledger_position(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(_googl_execution())

    result = service.sync_broker_backed_positions()

    assert result == {"scanned": 1, "opened": 1, "closed": 0}
    (position,) = service.positions.list(status="open")
    assert position.symbol == "GOOGL"
    assert position.strategy_name == "opening_range_breakout_retest"
    assert position.timeframe == "5m"
    assert position.quantity == 1.0
    assert position.entry_price == 338.75  # the broker's fill, not the proposed price
    assert position.stop_loss == 333.24  # from the bracket legs
    assert position.target_1 == 349.39
    assert position.opened_at == "2026-09-08T17:00:50+00:00"  # broker fill time
    assert position.payload["ledger_source"] == "alpaca_paper"
    assert position.payload["execution_id"] == "exec_googl"


def test_sync_is_idempotent_across_runs(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(_googl_execution())

    service.sync_broker_backed_positions()
    second = service.sync_broker_backed_positions()

    assert second == {"scanned": 1, "opened": 0, "closed": 0}
    assert len(service.positions.list(limit=50)) == 1


def test_simulator_marks_to_market_but_never_closes_a_broker_backed_position(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(_googl_execution())

    # Price trades through the stop, but the bracket at the broker owns the exit.
    result = service.refresh_open_positions(market_data_engine=_market(330.0), force_refresh=False)

    assert result["closed"] == 0
    assert result["broker_opened"] == 1
    (position,) = service.positions.list(status="open")
    assert position.current_price == 330.0
    assert position.unrealized_pnl_usd == -8.75
    assert service.trades.list() == []


def test_filled_stop_leg_closes_into_a_paper_trade_with_realized_pnl(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(_googl_execution())
    service.sync_broker_backed_positions()

    # Reconciliation later observes the stop leg filled at the broker.
    service.executions.update(_googl_execution(exit_leg_status="filled", exit_fill=333.2))
    result = service.sync_broker_backed_positions()

    assert result == {"scanned": 1, "opened": 0, "closed": 1}
    assert service.positions.list(status="open") == []
    (trade,) = service.trades.list()
    assert trade.symbol == "GOOGL"
    assert trade.outcome == "stop_loss"
    assert trade.entry_price == 338.75
    assert trade.exit_price == 333.2
    assert trade.realized_pnl_usd == -5.55
    assert trade.closed_at == "2026-09-08T19:12:03+00:00"  # broker's fill time
    assert trade.payload["ledger_source"] == "alpaca_paper"
    # A second pass must not double-count the closed trade.
    assert service.sync_broker_backed_positions() == {"scanned": 1, "opened": 0, "closed": 0}
    assert len(service.trades.list()) == 1


def test_unfilled_or_canceled_executions_never_open_positions(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    service = app.state.paper_trading_service
    service.executions.create(
        ExecutionRecord(
            id="exec_canceled",
            proposal_id="prop_canceled",
            status="canceled",
            mode="alpaca_paper",
            broker_order_id="old-canceled-order",
            request_payload={"symbol": "NVDA", "side": "buy", "strategy_name": "x"},
            response_payload={
                "broker": "alpaca",
                "broker_execution": {
                    "broker_order_id": "old-canceled-order",
                    "symbol": "NVDA",
                    "side": "buy",
                    "qty": 1.0,
                    "filled_qty": 0.0,
                    "status": "canceled",
                },
            },
        )
    )

    assert service.sync_broker_backed_positions() == {"scanned": 1, "opened": 0, "closed": 0}
    assert service.positions.list(limit=10) == []
