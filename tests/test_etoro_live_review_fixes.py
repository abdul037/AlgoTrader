"""Fixes from the 2026-10-05 independent review of the real-money code paths.

Each test pins one verified finding: write-ahead mirror state, rejected/refused orders
rolled back, rate limits never halting, the order's own timeframe gating evidence,
crypto kept out of the stock mirror, stale gates failing closed, and the per-account
eToro limiter.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from app.broker import etoro_rate_limit
from app.broker.etoro_live_backup_stop import STOPS_KEY
from app.broker.etoro_live_mirror import HALTED_KEY, STATE_KEY
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.execution.coordinator import _proposal_timeframe
from app.models.execution import ExecutionStatus
from app.performance.go_live_readiness import READINESS_KEY, readiness_ready
from app.performance.strategy_evidence import VERDICTS_KEY, evidence_blocker
from app.utils.time import utc_now
from tests.test_etoro_live_mirror import _Client, _mirror, _proposal, _run, _state


class _RaisingClient(_Client):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def open_market_order_by_amount(self, order, *, client_order_id=None):
        raise self.exc


class _StatusClient(_Client):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def open_market_order_by_amount(self, order, *, client_order_id=None):
        self.orders.append(order)
        return SimpleNamespace(order_id="et1", status=self.status)


def _mirror_state(state):
    return json.loads(state.get(STATE_KEY) or "{}")


def test_state_is_written_before_the_order_is_sent(tmp_path) -> None:
    state = _state()
    seen = {}

    class _Peek(_Client):
        def open_market_order_by_amount(self, order, *, client_order_id=None):
            seen.update(_mirror_state(state))
            seen["stops"] = json.loads(state.get(STOPS_KEY) or "{}")
            return super().open_market_order_by_amount(order, client_order_id=client_order_id)

    service, _ = _mirror(tmp_path, client=_Peek(), state=state)
    assert _run(service) is not None
    # A crash or timeout after the POST can no longer leave an untracked real position.
    assert seen["trades_today"] == 1 and "AAPL" in seen["open_symbols"]
    assert seen["stops"]["AAPL"]["stop"] == 95.0


def test_refused_order_is_rolled_back_and_halts(tmp_path) -> None:
    state = _state()
    client = _RaisingClient(RuntimeError("eToro request failed with status 400: bad amount"))
    service, _ = _mirror(tmp_path, client=client, state=state)
    assert _run(service) is None
    current = _mirror_state(state)
    assert current.get("trades_today", 0) == 0 and "AAPL" not in current.get("open_symbols", [])
    assert json.loads(state.get(STOPS_KEY) or "{}") == {}
    assert state.get(HALTED_KEY)  # still unexpected: the operator looks at it


def test_rate_limit_rolls_back_without_halting(tmp_path) -> None:
    state = _state()
    service, logs = _mirror(
        tmp_path, client=_RaisingClient(EToroRateLimitError("cooldown")), state=state
    )
    assert _run(service) is None
    assert not state.get(HALTED_KEY)
    assert _mirror_state(state).get("trades_today", 0) == 0


def test_unknown_error_keeps_the_write_ahead_state_and_halts(tmp_path) -> None:
    # A 5xx/timeout may still have opened the position: keep it counted and tracked.
    state = _state()
    service, _ = _mirror(tmp_path, client=_RaisingClient(RuntimeError("timeout")), state=state)
    assert _run(service) is None
    assert state.get(HALTED_KEY) and "AAPL" in _mirror_state(state)["open_symbols"]


def test_rejected_order_is_not_a_trade_or_a_successful_1x(tmp_path) -> None:
    state = _state()
    service, logs = _mirror(tmp_path, client=_StatusClient("rejected"), state=state)
    _run(service)
    current = _mirror_state(state)
    assert current.get("trades_today", 0) == 0 and current.get("successful_1x", 0) == 0
    assert any(event == "etoro_live_mirror_rejected" for event, _ in logs.events)
    assert json.loads(state.get(STOPS_KEY) or "{}") == {}


def test_order_timeframe_gates_the_evidence_check(tmp_path) -> None:
    # Strategy passes on 1d only; a 5m order of it must not ride the 1d evidence.
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    proposal = _proposal(timeframe="1d")
    proposal.order.metadata["timeframe"] = "5m"
    assert _run(service, proposal) is None and client.orders == []
    assert "strategy_lacks_oos_evidence" in logs.events[-1][1]["reasons"]


def test_missing_timeframe_blocks(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    proposal = _proposal()
    proposal.signal = None  # scanner proposals carry no signal
    assert _run(service, proposal) is None and client.orders == []
    assert "mirror_timeframe_unknown" in logs.events[-1][1]["reasons"]


def test_crypto_is_never_mirrored_by_the_stock_mirror(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, allowed_instruments=["AAPL", "BTC"])
    assert _run(service, _proposal("BTC")) is None and client.orders == []
    assert "mirror_equities_only" in logs.events[-1][1]["reasons"]


def test_reconcile_rate_limit_does_not_halt(tmp_path) -> None:
    class _Limited(_Client):
        def get_portfolio(self):
            raise EToroRateLimitError("cooldown")

    state = _state()
    service, logs = _mirror(tmp_path, client=_Limited(), state=state)
    assert service.reconcile() is None and not state.get(HALTED_KEY)
    assert logs.events[-1][0] == "etoro_live_reconcile_rate_limited"


def test_proposal_timeframe_prefers_order_metadata() -> None:
    proposal = _proposal(timeframe="1d")
    assert _proposal_timeframe(proposal) == "1d"
    proposal.order.metadata["timeframe"] = "15m"
    assert _proposal_timeframe(proposal) == "15m"
    assert _proposal_timeframe(SimpleNamespace(order=SimpleNamespace(metadata={}))) is None


def test_live_mirror_skipped_when_the_paper_order_failed() -> None:
    from app.execution.coordinator import ExecutionCoordinator

    calls = []
    coordinator = ExecutionCoordinator.__new__(ExecutionCoordinator)
    coordinator.parallel_broker = None
    coordinator.etoro_live_mirror = SimpleNamespace(mirror=lambda **kw: calls.append(kw))
    for status in (ExecutionStatus.FAILED, ExecutionStatus.BLOCKED):
        coordinator._mirror_parallel(_proposal(), SimpleNamespace(status=status), "alpaca")
    assert calls == []
    coordinator._mirror_parallel(_proposal(), SimpleNamespace(status="submitted"), "alpaca")
    assert len(calls) == 1


def test_stale_readiness_is_not_ready() -> None:
    state = {}
    fresh = {"ready": True, "computed_at": utc_now().isoformat()}
    state[READINESS_KEY] = json.dumps(fresh)
    assert readiness_ready(state)
    old = {**fresh, "computed_at": (utc_now() - timedelta(hours=3)).isoformat()}
    state[READINESS_KEY] = json.dumps(old)
    assert not readiness_ready(state)
    state[READINESS_KEY] = json.dumps({"ready": True})  # no timestamp: not trusted
    assert not readiness_ready(state)


def test_stale_evidence_fails_closed() -> None:
    settings = SimpleNamespace(require_strategy_oos_evidence=True)
    verdicts = {"momentum_breakout:1d": {"passed": True}}
    now = utc_now().isoformat()
    state = {VERDICTS_KEY: json.dumps({"verdicts": verdicts, "computed_at": now})}
    assert evidence_blocker(settings, state, strategy="momentum_breakout", timeframe="1d") is None
    old = (utc_now() - timedelta(hours=30)).isoformat()
    state[VERDICTS_KEY] = json.dumps({"verdicts": verdicts, "computed_at": old})
    assert (
        evidence_blocker(settings, state, strategy="momentum_breakout", timeframe="1d")
        == "strategy_evidence_stale"
    )


def test_rate_limit_cooldown_is_per_account() -> None:
    live = SimpleNamespace(etoro_api_key="live-key", etoro_request_min_interval_seconds=0)
    demo = SimpleNamespace(etoro_api_key="demo-key", etoro_request_min_interval_seconds=0)
    etoro_rate_limit._state.clear()
    try:
        assert etoro_rate_limit.mark_etoro_rate_limited(demo, status_code=429, body="slow down")
        try:
            etoro_rate_limit.wait_for_etoro_slot(demo)
            raise AssertionError("demo account should be cooling down")
        except EToroRateLimitError:
            pass
        etoro_rate_limit.wait_for_etoro_slot(live)  # live backup stop is not blinded
        assert all("live-key" not in key for key in etoro_rate_limit._state)
    finally:
        etoro_rate_limit._state.clear()


def test_open_position_cap_is_nine(tmp_path) -> None:
    from app.broker.etoro_live_mirror import HARD_MAX_OPEN_POSITIONS

    assert HARD_MAX_OPEN_POSITIONS == 9
    state = _state()
    state.set(STATE_KEY, json.dumps({"last_equity": 10_000.0, "open_symbols": ["A", "B", "C"]}))
    service, _ = _mirror(tmp_path, state=state)
    assert _run(service) is not None  # 3 open no longer blocks
    state.set(
        STATE_KEY,
        json.dumps(
            {"last_equity": 10_000.0, "open_symbols": ["A", "B", "C", "D", "E", "F", "G", "H", "I"]}
        ),
    )
    service, logs = _mirror(tmp_path, state=state)
    assert _run(service, _proposal("MSFT")) is None
    assert "etoro_live_open_position_cap" in logs.events[-1][1]["reasons"]


def test_entry_needs_free_cash_and_spends_it(tmp_path) -> None:
    # Operator 2026-10-06: more open positions "if there is any fund left".
    state = _state()
    state.set(STATE_KEY, json.dumps({"last_equity": 10_000.0, "last_cash": 1_150.0}))
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=state)
    assert _run(service) is not None and client.orders[0].amount_usd == 1_000.0
    assert _mirror_state(state)["last_cash"] == 150.0
    assert _run(service, _proposal("MSFT")) is None and len(client.orders) == 1
    assert "etoro_live_insufficient_cash" in logs.events[-1][1]["reasons"]


def test_reconcile_records_free_cash(tmp_path) -> None:
    class _Cash(_Client):
        def fetch_raw_portfolio(self):
            return {"clientPortfolio": {"credit": 6_990.0, "positions": [{"amount": 3_000.0}]}}

    state = _state()
    service, _ = _mirror(tmp_path, client=_Cash(), state=state)
    service.reconcile()
    current = _mirror_state(state)
    assert current["last_cash"] == 6_990.0 and current["last_equity"] == 9_990.0
