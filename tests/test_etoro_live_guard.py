"""eToro LIVE guard thread: runs the test-order watch and the backup stops every minute,
independent of the scheduler; maintenance only falls back when its heartbeat is stale."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace

from app.broker.etoro_live_backup_stop import STOPS_KEY, check_backup_stops, remember_stop
from app.broker.etoro_live_guard import HEARTBEAT_KEY, EtoroLiveGuard, attach_live_guard
from app.broker.etoro_live_mirror import HALTED_KEY
from app.broker.etoro_live_test_order import run_from_maintenance
from app.utils.time import utc_now
from tests.test_etoro_live_backup_stop import _Client, _mirror


class _Tester:
    def __init__(self, result=None, fail=False):
        self.calls = 0
        self.result = result
        self.fail = fail

    def run(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.result


def test_tick_runs_test_watch_then_backup_stops_and_beats(tmp_path) -> None:
    client = _Client(rates={"AAPL": 90.0}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, _ = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    tester = _Tester(result={"status": "open"})
    guard = EtoroLiveGuard(mirror=mirror, tester=tester)
    assert guard.tick() == ["etoro_live_test_order", "etoro_live_backup_stop"]
    assert tester.calls == 1 and client.closed == [(7, 1001)]
    assert guard.recently_alive()


def test_tick_never_raises_and_still_beats(tmp_path) -> None:
    mirror, logs = _mirror(tmp_path, _Client())
    guard = EtoroLiveGuard(mirror=mirror, tester=_Tester(fail=True))
    assert guard.tick() == []
    errors = [e for e in logs.events if e[0] == "etoro_live_guard_error"]
    assert len(errors) == 1 and guard.recently_alive()


def test_idle_guard_makes_no_eToro_call(tmp_path) -> None:
    client = _Client()
    client.fetch_raw_portfolio = lambda: (_ for _ in ()).throw(AssertionError("no call expected"))
    mirror, _ = _mirror(tmp_path, client)
    assert EtoroLiveGuard(mirror=mirror, tester=_Tester()).tick() == []


def test_stale_heartbeat_hands_over_to_maintenance(tmp_path) -> None:
    mirror, _ = _mirror(tmp_path, _Client())
    tester = _Tester(result={"status": "open"})
    guard = EtoroLiveGuard(mirror=mirror, tester=tester)
    execution = SimpleNamespace(
        etoro_live_guard=guard, etoro_live_test_order=tester, etoro_live_mirror=mirror
    )
    service = SimpleNamespace(
        auto_trading=SimpleNamespace(execution=execution), run_logs=mirror.logs
    )
    guard.tick()  # fresh heartbeat -> maintenance stays idle
    completed: list[str] = []
    run_from_maintenance(service, completed)
    assert completed == [] and tester.calls == 1
    mirror.state.set(HEARTBEAT_KEY, (utc_now() - timedelta(minutes=10)).isoformat())
    run_from_maintenance(service, completed)
    assert completed == ["etoro_live_test_order"] and tester.calls == 2


def test_thread_ticks_on_its_own_and_stops(tmp_path) -> None:
    mirror, _ = _mirror(tmp_path, _Client())
    tester = _Tester()
    guard = EtoroLiveGuard(
        mirror=mirror, tester=tester, interval_seconds=10, initial_delay_seconds=0
    )
    guard.start()
    deadline = time.time() + 5
    while tester.calls == 0 and time.time() < deadline:
        time.sleep(0.05)
    guard.stop()
    assert tester.calls >= 1  # first tick runs at start, no scheduler involved


def test_position_closed_elsewhere_is_not_an_error(tmp_path) -> None:
    # During a deploy the old and new containers overlap ~20 s; if the other one closed
    # the position first, our close fails but the position is gone: no halt.
    client = _Client(rates={"AAPL": 90.0}, positions=[{"positionID": 7, "instrumentID": 1001}])

    def close_already_gone(position_id, instrument_id):
        client.positions = []
        raise RuntimeError("Broker request failed with status 404: position not found")

    client.close_position_by_id = close_already_gone
    mirror, logs = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    closed = check_backup_stops(mirror)
    assert closed[0]["closed_elsewhere"] is True and not mirror.state.get(HALTED_KEY)
    assert json.loads(mirror.state.get(STOPS_KEY)) == {}


def test_attach_wires_tester_and_guard(tmp_path) -> None:
    mirror, _ = _mirror(tmp_path, _Client())
    coordinator = SimpleNamespace(etoro_live_mirror=mirror)
    attach_live_guard(
        coordinator, settings=SimpleNamespace(etoro_live_guard_interval_seconds=45), bars=None
    )
    assert coordinator.etoro_live_guard.tester is coordinator.etoro_live_test_order
    assert coordinator.etoro_live_guard.interval_seconds == 45.0


def test_test_order_error_does_not_skip_backup_stops(tmp_path) -> None:
    client = _Client(rates={"AAPL": 90.0}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, logs = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    guard = EtoroLiveGuard(mirror=mirror, tester=_Tester(fail=True))
    assert guard.tick() == ["etoro_live_backup_stop"] and client.closed == [(7, 1001)]
    assert any(
        p.get("step") == "etoro_live_test_order"
        for e, p in logs.events
        if e == "etoro_live_guard_error"
    )


def test_unclear_portfolio_after_failed_close_keeps_the_stop(tmp_path, monkeypatch) -> None:
    import app.broker.etoro_live_backup_stop as backup

    monkeypatch.setattr(backup, "CLOSE_RECHECK_SECONDS", 0)
    client = _Client(
        rates={"AAPL": 90.0}, positions=[{"positionID": 7, "instrumentID": 1001}], fail_close=True
    )
    reads = iter([{"clientPortfolio": {"credit": 1.0, "positions": client.positions}}, {}, {}])
    client.fetch_raw_portfolio = lambda: next(reads)  # 1st: the check; then empty/malformed reads
    mirror, _ = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    check_backup_stops(mirror)
    assert mirror.state.get(HALTED_KEY) and "AAPL" in json.loads(mirror.state.get(STOPS_KEY))


def test_stop_recorded_meanwhile_survives_the_save(tmp_path) -> None:
    client = _Client(rates={"AAPL": 99.0}, positions=[{"positionID": 7, "instrumentID": 1001}])
    mirror, _ = _mirror(tmp_path, client)
    remember_stop(mirror, "AAPL", 95.0, 1001)
    original = client.fetch_raw_portfolio

    def portfolio_while_other_container_records_msft():
        stops = json.loads(mirror.state.get(STOPS_KEY))
        stops["MSFT"] = {"stop": 300.0, "instrument_id": 1002, "recorded_at": utc_now().isoformat()}
        mirror.state.set(STOPS_KEY, json.dumps(stops))
        return original()

    client.fetch_raw_portfolio = portfolio_while_other_container_records_msft
    check_backup_stops(mirror)
    assert set(json.loads(mirror.state.get(STOPS_KEY))) == {"AAPL", "MSFT"}
