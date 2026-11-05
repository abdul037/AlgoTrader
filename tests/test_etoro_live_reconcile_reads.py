"""A failed reconcile READ halts the eToro mirror only after 3 in a row (2026-11-04)."""

from __future__ import annotations

from types import SimpleNamespace

from app.broker.etoro_live_mirror import HALTED_KEY, RECONCILE_FAILURES_KEY
from tests.test_etoro_live_mirror import _Client, _mirror, _state


class _Flaky(_Client):
    def __init__(self, failures):
        super().__init__(positions=[SimpleNamespace(symbol="AAPL", position_id=1)],
                         raw_positions=[{"positionID": 1, "stopLossRate": 95.0}])
        self.failures = failures

    def fetch_raw_portfolio(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Broker request failed: The read operation timed out")
        return {"clientPortfolio": {"credit": 400.0, "positions": self.raw_positions}}


def test_two_timeouts_do_not_halt_and_a_good_read_resets_the_count(tmp_path) -> None:
    state = _state()
    service, logs = _mirror(tmp_path, client=_Flaky(failures=2), state=state)
    assert service.reconcile() is None and service.reconcile() is None
    assert not state.get(HALTED_KEY) and state.get(RECONCILE_FAILURES_KEY) == "2"
    assert [e for e, _ in logs.events] == ["etoro_live_reconcile_read_failed"] * 2
    assert service.reconcile()["open_symbols"] == ["AAPL"]
    assert state.get(RECONCILE_FAILURES_KEY) == "0" and not state.get(HALTED_KEY)


def test_three_in_a_row_halt(tmp_path) -> None:
    state = _state()
    service, logs = _mirror(tmp_path, client=_Flaky(failures=3), state=state)
    for _ in range(3):
        assert service.reconcile() is None
    assert "reconcile_failed:3_in_a_row" in state.get(HALTED_KEY)
    assert logs.events[-1][0] == "etoro_live_mirror_halted"
