"""A 1d paper position closes once its signal's max_hold_bars sessions have started (2026-11-07).

Operator: "build the overnight_drift one-day hold for paper". The backtest closes a trade at the
open of the bar ``max_hold_bars`` after its entry bar; paper now does the same at the first
scheduler tick of that session, so overnight_drift's evidence describes the trade paper makes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.automation import max_hold_exit
from app.automation.max_hold_exit import close_held_past_max_hold, max_hold_bars, sessions_since
from app.strategies import get_strategy

# Fri 6 Nov 2026 15:00 New York (EST, UTC-5) and the following sessions' opens.
FRI_AFTERNOON = "2026-11-06T20:00:00+00:00"
MON_OPEN = datetime(2026, 11, 9, 14, 31, tzinfo=UTC)  # 09:31 New York
MON_LATE = datetime(2026, 11, 9, 20, 30, tzinfo=UTC)
TUE_OPEN = datetime(2026, 11, 10, 14, 31, tzinfo=UTC)


def _execution(execution_id, symbol, *, timeframe="1d", hold=1, filled_at=FRI_AFTERNOON, created_at=None, exited=False):
    metadata = {"timeframe": timeframe}
    if hold is not None:
        metadata["max_hold_bars"] = hold
    legs = [
        {"broker_order_id": f"{execution_id}-tp", "status": "filled" if exited else "new"},
        {"broker_order_id": f"{execution_id}-stop", "status": "canceled" if exited else "held"},
    ]
    return SimpleNamespace(
        id=execution_id,
        strategy_name="overnight_drift",
        created_at=created_at or filled_at,
        realized_pnl_usd=5.0 if exited else 0.0,
        request_payload={"symbol": symbol, "metadata": metadata},
        response_payload={"broker_execution": {"filled_qty": 4.0, "filled_at": filled_at, "legs": legs}},
    )


class _Alpaca:
    def __init__(self, held, *, is_open=True, close_error=None):
        self.held = held
        self.is_open = is_open
        self.close_error = close_error
        self.cancelled: list[str] = []
        self.closed: list[str] = []

    def market_clock(self):
        return self.is_open, None

    def get_portfolio(self):
        return SimpleNamespace(positions=[SimpleNamespace(symbol=s) for s in self.held])

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def close_position(self, symbol):
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
    base = {"execution_mode": "paper", "enable_real_trading": False, "paper_max_hold_exit_enabled": True}
    base.update(settings)
    store = SimpleNamespace(list=lambda limit=200: executions)
    return SimpleNamespace(
        settings=SimpleNamespace(**base),
        reconciliation=SimpleNamespace(alpaca=alpaca, executions=store),
        run_logs=_Logs(),
    )


def test_a_friday_overnight_entry_closes_at_mondays_open() -> None:
    alpaca = _Alpaca(["AAPL", "MSFT"])
    host = _host(alpaca, [_execution("e1", "AAPL"), _execution("e2", "MSFT", hold=None)])  # MSFT: no time stop

    assert close_held_past_max_hold(host, now=MON_OPEN) == ["AAPL"]
    assert alpaca.closed == ["AAPL"]
    assert set(alpaca.cancelled) == {"e1-tp", "e1-stop"}  # the bracket legs first
    ((event, payload),) = host.run_logs.events
    assert event == "paper_position_closed_max_hold"
    assert payload["max_hold_bars"] == 1 and payload["sessions_held"] == 1


def test_nothing_closes_in_the_entry_session_or_while_the_market_is_shut() -> None:
    same_day = _execution("e1", "AAPL", filled_at="2026-11-09T15:00:00+00:00")  # Mon 10:00 New York
    assert close_held_past_max_hold(_host(_Alpaca(["AAPL"]), [same_day]), now=MON_LATE) == []

    shut = _Alpaca(["AAPL"], is_open=False)  # weekend, holiday, before the open or after the close
    assert close_held_past_max_hold(_host(shut, [_execution("e1", "AAPL")]), now=MON_OPEN) == []
    assert shut.closed == [] and shut.cancelled == []


def test_the_hold_counts_from_the_fill_not_the_submission() -> None:
    """An order sent after Monday's close fills at Tuesday's open: Tuesday is its entry session."""

    queued = _execution(
        "e1", "AAPL", created_at="2026-11-09T21:10:00+00:00", filled_at="2026-11-10T14:30:05+00:00"
    )
    alpaca = _Alpaca(["AAPL"])
    assert close_held_past_max_hold(_host(alpaca, [queued]), now=TUE_OPEN.replace(hour=15)) == []
    assert close_held_past_max_hold(_host(alpaca, [queued]), now=datetime(2026, 11, 11, 14, 31, tzinfo=UTC)) == ["AAPL"]


def test_a_longer_hold_waits_for_its_sessions() -> None:
    two = _execution("e1", "AAPL", hold=2)
    assert close_held_past_max_hold(_host(_Alpaca(["AAPL"]), [two]), now=MON_OPEN) == []
    assert close_held_past_max_hold(_host(_Alpaca(["AAPL"]), [two]), now=TUE_OPEN) == ["AAPL"]


@pytest.mark.parametrize(
    "execution, held",
    [
        (_execution("e1", "AAPL", timeframe="5m"), ["AAPL"]),  # intraday: the before-the-bell close owns it
        (_execution("e1", "AAPL", exited=True), ["AAPL"]),  # already out at its target
        (_execution("e1", "AAPL"), []),  # no longer held at Alpaca
    ],
)
def test_leaves_other_positions_alone(execution, held) -> None:
    alpaca = _Alpaca(held)
    assert close_held_past_max_hold(_host(alpaca, [execution]), now=MON_OPEN) == []
    assert alpaca.closed == [] and alpaca.cancelled == []


@pytest.mark.parametrize(
    "settings",
    [{"execution_mode": "live"}, {"enable_real_trading": True}, {"paper_max_hold_exit_enabled": False}],
)
def test_paper_only_and_switchable(settings) -> None:
    alpaca = _Alpaca(["AAPL"])
    assert close_held_past_max_hold(_host(alpaca, [_execution("e1", "AAPL")], **settings), now=MON_OPEN) == []
    assert alpaca.closed == []


def test_qty_still_held_by_cancelling_legs_is_retried_next_tick() -> None:
    alpaca = _Alpaca(["AAPL"], close_error=RuntimeError("insufficient qty available for order"))
    host = _host(alpaca, [_execution("e1", "AAPL")])
    assert close_held_past_max_hold(host, now=MON_OPEN) == ["AAPL"]
    assert host.run_logs.events[0][0] == "max_hold_close_deferred"


def test_a_failure_never_escapes_the_scheduler_tick() -> None:
    def broken(limit=200):
        raise RuntimeError("database unavailable")

    host = _host(_Alpaca(["AAPL"]), [])
    host.reconciliation.executions = SimpleNamespace(list=broken)
    assert close_held_past_max_hold(host, now=MON_OPEN) == []
    assert host.run_logs.events[0][0] == "max_hold_close_error"


def test_sessions_and_hold_parsing() -> None:
    fri = datetime(2026, 11, 6).date()
    assert sessions_since(fri, fri) == 0
    assert sessions_since(fri, datetime(2026, 11, 8).date()) == 0  # the weekend
    assert sessions_since(fri, datetime(2026, 11, 9).date()) == 1
    assert sessions_since(fri, datetime(2026, 11, 13).date()) == 5
    for raw, expected in ((1, 1), ("2", 2), (0, None), (-1, None), (True, None), ("x", None), (None, None)):
        assert max_hold_bars({"metadata": {"max_hold_bars": raw}}) == expected, raw
    assert max_hold_bars({}) is None


def test_overnight_drift_signals_carry_their_one_bar_hold() -> None:
    """What the rule reads: the signal's metadata reaches the execution's request metadata whole
    (``**signal.metadata``), so an overnight_drift entry carries max_hold_bars=1."""

    rng = np.random.RandomState(3)
    n = 400
    close = 100 * np.exp(np.cumsum(rng.normal(0.0009, 0.016, n)))
    openp = close * (1 + rng.normal(0, 0.004, n))
    frame = pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2025-01-02", periods=n, tz="UTC"),
            "open": openp,
            "high": np.maximum(openp, close) * 1.004,
            "low": np.minimum(openp, close) * 0.996,
            "close": close,
            "volume": np.full(n, 5_000_000.0),
        }
    )
    strategy = get_strategy("overnight_drift")
    signals = [strategy.generate_signal(frame.iloc[: end + 1], "SYN") for end in range(260, n)]
    signal = next(s for s in signals if s is not None)
    assert max_hold_bars({"metadata": {**signal.metadata, "timeframe": "1d"}}) == 1


def test_the_scheduler_runs_it_every_tick_even_while_paused(tmp_path, monkeypatch) -> None:
    from app.workflow import service as workflow_module
    from tests.test_automation_auto_recover import DIRTY, _build

    calls: list[object] = []
    monkeypatch.setattr(workflow_module, "close_held_past_max_hold", lambda host: calls.append(host) or [])
    workflow, automation, *_ = _build(tmp_path, recon_results=[DIRTY, DIRTY, DIRTY])
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)

    workflow.run_scheduled_tasks()

    assert calls == [workflow]
    assert sorted(max_hold_exit.MAX_HOLD_TIMEFRAMES) == ["1d"]
