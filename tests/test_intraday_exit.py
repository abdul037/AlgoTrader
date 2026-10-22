"""Intraday-strategy positions are closed before the bell; swing positions are not.

Review Team 2026-10-02: INTC (5m) was held two days and COST (15m) overnight
with intraday-sized stops.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.automation.intraday_exit import flatten_intraday_before_close
from app.utils.time import utc_now


def _execution(execution_id: str, symbol: str, timeframe: str, *, exited: bool = False):
    legs = [
        {"broker_order_id": f"{execution_id}-tp", "status": "filled" if exited else "new"},
        {"broker_order_id": f"{execution_id}-stop", "status": "canceled" if exited else "held"},
    ]
    return SimpleNamespace(
        id=execution_id,
        created_at=utc_now().isoformat(),
        realized_pnl_usd=12.0 if exited else 0.0,
        request_payload={"symbol": symbol, "metadata": {"timeframe": timeframe}},
        response_payload={"broker_execution": {"filled_qty": 10.0, "legs": legs}},
    )


class _Alpaca:
    def __init__(self, minutes_to_close: float, held: list[str], close_error: Exception | None = None):
        self.next_close = utc_now() + timedelta(minutes=minutes_to_close)
        self.held = held
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.close_error = close_error

    def market_clock(self):
        return True, self.next_close

    def get_portfolio(self):
        return SimpleNamespace(positions=[SimpleNamespace(symbol=s) for s in self.held])

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def close_position(self, symbol: str):
        if self.close_error is not None:
            raise self.close_error
        self.closed.append(symbol)
        return SimpleNamespace(order_id=f"close-{symbol}")


class _Logs:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event, payload):
        self.events.append((event, payload))


def _host(alpaca, executions, **settings):
    base = {"intraday_flatten_minutes_before_close": 10.0, "execution_mode": "paper", "enable_real_trading": False}
    base.update(settings)
    reconciliation = SimpleNamespace(alpaca=alpaca, executions=SimpleNamespace(list=lambda limit=200: executions))
    return SimpleNamespace(settings=SimpleNamespace(**base), reconciliation=reconciliation, run_logs=_Logs())


def test_closes_intraday_positions_in_the_last_minutes() -> None:
    alpaca = _Alpaca(minutes_to_close=8, held=["CSCO", "IWM", "NVDA"])
    executions = [
        _execution("e1", "CSCO", "5m"),
        _execution("e2", "IWM", "15m"),
        _execution("e3", "NVDA", "1d"),  # swing: keeps its GTC bracket
        _execution("e4", "INTC", "5m", exited=True),
    ]
    host = _host(alpaca, executions)

    closed = flatten_intraday_before_close(host)

    assert sorted(closed) == ["CSCO", "IWM"]
    assert sorted(alpaca.closed) == ["CSCO", "IWM"]
    assert set(alpaca.cancelled) == {"e1-tp", "e1-stop", "e2-tp", "e2-stop"}
    assert [e for e, _ in host.run_logs.events].count("intraday_position_flattened_before_close") == 2


def test_does_nothing_earlier_in_the_session() -> None:
    alpaca = _Alpaca(minutes_to_close=45, held=["CSCO"])
    host = _host(alpaca, [_execution("e1", "CSCO", "5m")])

    assert flatten_intraday_before_close(host) == []
    assert alpaca.closed == [] and alpaca.cancelled == []


def test_never_runs_outside_paper() -> None:
    alpaca = _Alpaca(minutes_to_close=5, held=["CSCO"])
    host = _host(alpaca, [_execution("e1", "CSCO", "5m")], execution_mode="live")

    assert flatten_intraday_before_close(host) == []
    assert alpaca.closed == []


def test_qty_still_held_by_cancelling_legs_is_deferred_to_next_tick() -> None:
    alpaca = _Alpaca(minutes_to_close=5, held=["CSCO"], close_error=RuntimeError("insufficient qty available for order"))
    host = _host(alpaca, [_execution("e1", "CSCO", "5m")])

    assert flatten_intraday_before_close(host) == ["CSCO"]
    assert host.run_logs.events[0][0] == "intraday_flatten_deferred"
