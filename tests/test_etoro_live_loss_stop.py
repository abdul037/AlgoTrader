"""The eToro daily loss stop and the daily copy count never switch off on a bad reading.

Found in the 2026-10-26 review: a NaN or missing day-start equity skipped the 5% loss stop for
the whole day (the day roll copies ``last_equity`` into it and reconcile only refilled a None),
and a NaN copy count passed the three-a-day cap (``NaN >= 3`` is False).
"""

from __future__ import annotations

import json

import pytest

from app.broker.etoro_live_mirror import HARD_MAX_TRADES_PER_DAY, STATE_KEY
from app.utils.time import utc_now
from tests.test_etoro_live_mirror import _Client, _proposal, _run, _state
from tests.test_etoro_live_sizing import EQUITY, _account, _blocked, _mirror


def _today(**fields):
    state = _account(cash=EQUITY)
    data = json.loads(state.get(STATE_KEY))
    data.update(day=utc_now().date().isoformat(), **fields)
    state.set(STATE_KEY, json.dumps(data))
    return state


@pytest.mark.parametrize("start", [None, float("nan"), float("inf"), 0.0, -1.0, "x"])
def test_an_unknown_day_start_refuses_the_copy(tmp_path, start) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_today(day_start_equity=start))
    assert _run(service, _proposal("AAPL", stop=95.0)) is None and client.orders == []
    assert "etoro_live_day_start_unknown" in _blocked(logs)


def test_a_known_day_start_still_applies_the_5_pct_stop(tmp_path) -> None:
    client = _Client()
    down = _today(day_start_equity=10_000.0, last_equity=9_500.0)
    service, logs = _mirror(tmp_path, client=client, state=down)
    assert _run(service, _proposal("AAPL", stop=95.0)) is None and client.orders == []
    assert "etoro_live_daily_loss_stop" in _blocked(logs)
    assert "etoro_live_day_start_unknown" not in _blocked(logs)
    fine = _today(day_start_equity=EQUITY)
    service, _ = _mirror(tmp_path / "ok", client=client, state=fine)
    assert _run(service, _proposal("AAPL", stop=95.0))


@pytest.mark.parametrize("bad", [float("nan"), None, "x"])
def test_reconcile_refills_a_bad_day_start(tmp_path, bad) -> None:
    client = _Client(equity=EQUITY)
    state = _today(day_start_equity=bad)
    service, _ = _mirror(tmp_path, client=client, state=state)
    service.reconcile()
    start = json.loads(state.get(STATE_KEY))["day_start_equity"]
    assert isinstance(start, float) and start > 0


def test_reconcile_keeps_a_good_day_start(tmp_path) -> None:
    client = _Client(equity=EQUITY)
    state = _today(day_start_equity=10_000.0)
    service, _ = _mirror(tmp_path, client=client, state=state)
    service.reconcile()
    assert json.loads(state.get(STATE_KEY))["day_start_equity"] == 10_000.0


@pytest.mark.parametrize("count", [float("nan"), "x", "2", True, float(HARD_MAX_TRADES_PER_DAY)])
def test_an_unreadable_or_full_copy_count_refuses(tmp_path, count) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_today(trades_today=count))
    assert _run(service, _proposal("AAPL", stop=95.0)) is None and client.orders == []
    assert "etoro_live_daily_trade_cap" in _blocked(logs)
