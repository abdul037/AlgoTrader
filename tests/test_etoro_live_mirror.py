"""Capped eToro LIVE test mirror: inert by default, every lock and cap enforced.

Real money, so each gate gets its own test. No real client is ever constructed
here: a fake client records what would have been sent.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
from app.broker.etoro_live_mirror import (
    HALTED_KEY,
    HARD_MAX_TRADE_USD,
    HARD_MAX_TRADES_PER_DAY,
    MIN_TRADE_USD,
    STATE_KEY,
    EtoroLiveMirrorService,
    build_live_client,
)
from app.models.signal import Signal, SignalAction
from app.models.trade import TradeOrder
from app.performance.strategy_evidence import VERDICTS_KEY
from app.utils.time import utc_now
from tests.conftest import make_settings


class _State(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, payload))


class _Client:
    def __init__(self, *, fail=False, positions=None, raw_positions=None, equity=500.0):
        self.fail = fail
        self.orders = []
        self.closed = []
        self.positions = positions or []
        self.raw_positions = raw_positions or []
        self.equity = equity

    def open_market_order_by_amount(self, order, *, client_order_id=None):
        if self.fail:
            raise RuntimeError("eToro 500")
        self.orders.append(order)
        return SimpleNamespace(order_id=f"et{len(self.orders)}", status="submitted")

    def fetch_raw_portfolio(self):
        return {"clientPortfolio": {"positions": self.raw_positions}}

    def get_portfolio(self):
        return SimpleNamespace(
            account=SimpleNamespace(equity=self.equity), positions=self.positions
        )

    def close_position_by_id(self, position_id, instrument_id):
        symbol = {p.position_id: p.symbol for p in self.positions}[position_id]
        self.closed.append(symbol)


def _settings(tmp_path, **overrides):
    values = dict(
        etoro_live_mirror_enabled=True,
        etoro_live_acknowledgement=LIVE_OPERATOR_ACKNOWLEDGEMENT,
        allowed_instruments=["AAPL", "MSFT", "NVDA", "TSLA"],
    )
    values.update(overrides)
    return make_settings(tmp_path, **values)


def verdicts_cache(verdicts, *, passing_hours=48.0, age_minutes=10.0):
    """A fresh verdicts cache whose passing keys have passed ``passing_hours`` unbroken
    (the live copy's 24 h streak rule, operator 2026-10-18)."""

    now = utc_now()
    since = (now - timedelta(hours=passing_hours)).isoformat()
    return json.dumps(
        {
            "computed_at": (now - timedelta(minutes=age_minutes)).isoformat(),
            "verdicts": verdicts,
            "passing_since": {key: since for key, v in verdicts.items() if v.get("passed")},
        }
    )


# What a scan candidate's order metadata carries when every 2026-10-18 copy rule holds: the
# planner says buy now, the per-signal backtest passed, and the move was not chased.
COPYABLE_METADATA = {
    "trade_plan": {"verdict": "actionable", "timing_label": "immediate"},
    "backtest_validated": True,
    "entry_extension": {
        "timeframe": "1d",
        "return_20d_pct": 6.0,
        "rsi_14": 58.0,
        "new_high_252_signal_bar": False,
        "new_high_252_prior_bar": False,
        "new_high_252_recent": False,
    },
}


def _state(passing=("momentum_breakout:1d",), equity=10_000.0):
    state = _State()
    # +0.05 R: above the live copy's +0.03 R bar (operator 2026-10-16).
    verdict = {"passed": True, "oos_expectancy_r": 0.05}
    state.set(VERDICTS_KEY, verdicts_cache({key: verdict for key in passing}))
    if equity is not None:
        state.set(STATE_KEY, json.dumps({"last_equity": equity}))
    return state


def _proposal(
    symbol="AAPL",
    strategy="momentum_breakout",
    timeframe="1d",
    side="buy",
    stop=95.0,
    target=110.0,
    price=100.0,
    metadata=None,
):
    order = TradeOrder(
        symbol=symbol,
        amount_usd=12_500,
        leverage=1,
        proposed_price=price,
        stop_loss=stop,
        take_profit=target,
        strategy_name=strategy,
        side=side,
        metadata=copy.deepcopy(COPYABLE_METADATA if metadata is None else metadata),
    )
    signal = Signal(
        symbol=symbol,
        strategy_name=strategy,
        action=SignalAction.BUY,
        rationale="t",
        metadata={"timeframe": timeframe},
    )
    return SimpleNamespace(id=f"prop_{symbol}", order=order, signal=signal)


def _mirror(tmp_path, client=None, state=None, **overrides):
    logs = _Logs()
    service = EtoroLiveMirrorService(
        settings=_settings(tmp_path, **overrides),
        client=client if client is not None else _Client(),
        runtime_state=state if state is not None else _state(),
        run_logs=logs,
    )
    return service, logs


def _run(service, proposal=None, broker="alpaca"):
    return service.mirror(
        proposal=proposal or _proposal(),
        primary_execution=SimpleNamespace(broker_order_id="alp1"),
        primary_broker=broker,
    )


def test_inert_by_default(tmp_path) -> None:
    client = _Client()
    service = EtoroLiveMirrorService(
        settings=make_settings(tmp_path), client=client, runtime_state=_state(), run_logs=_Logs()
    )
    assert _run(service) is None and client.orders == []


def test_no_live_client_without_both_keys(tmp_path) -> None:
    assert build_live_client(make_settings(tmp_path)) is None
    assert build_live_client(make_settings(tmp_path, etoro_live_api_key="k")) is None


def test_live_client_scopes_real_mode_without_touching_global_flag(tmp_path) -> None:
    settings = make_settings(tmp_path, etoro_live_api_key="k", etoro_live_user_key="u")
    client = build_live_client(settings)
    assert (
        client.settings.etoro_account_mode == "real" and client.settings.enable_real_trading is True
    )
    assert settings.enable_real_trading is False and settings.etoro_account_mode == "demo"


def test_mirrors_a_qualifying_entry_at_10_pct_of_equity_1x(tmp_path) -> None:
    # AlgoBot reports $10,000; the operator copies it with $500, so a 10% ($1,000)
    # AlgoBot position puts about $50 of the operator's money to work.
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    record = _run(service)
    assert record["amount_usd"] == 1_000.0 and record["leverage"] == 1
    assert record["equity_basis_usd"] == 10_000.0
    sent = client.orders[0]
    assert (sent.amount_usd, sent.leverage, sent.stop_loss, sent.take_profit) == (
        1_000.0,
        1,
        95.0,
        110.0,
    )
    assert logs.events[-1][0] == "etoro_live_mirror_submitted"


def test_directly_funded_500_account_trades_50(tmp_path) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=_state(equity=500.0))
    assert _run(service)["amount_usd"] == 50.0


def test_trade_size_is_hard_capped_by_pct_and_dollars(tmp_path) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, etoro_live_trade_pct_of_equity=50.0)
    _run(service)
    assert client.orders[0].amount_usd == 1_000.0  # pct capped at 10%
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, state=_state(equity=1_000_000.0))
    _run(service)
    assert client.orders[0].amount_usd == HARD_MAX_TRADE_USD


def test_no_trade_until_equity_is_known_or_below_minimum(tmp_path) -> None:
    for equity, reason in (
        (None, "etoro_live_equity_unknown"),
        (MIN_TRADE_USD * 5, "etoro_live_trade_below_minimum"),
    ):
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, state=_state(equity=equity))
        assert _run(service) is None and client.orders == []
        assert reason in logs.events[-1][1]["reasons"]


def test_each_lock_blocks_on_its_own(tmp_path) -> None:
    cases = {
        "etoro_live_acknowledgement_missing": dict(overrides={"etoro_live_acknowledgement": "yes"}),
        "strategy_lacks_oos_evidence": dict(
            proposal=_proposal(strategy="vwap_reclaim", timeframe="5m")
        ),
        "mirror_long_only": dict(proposal=_proposal(side="sell")),
        "mirror_requires_alpaca_paper_primary": dict(broker="etoro"),
    }
    for reason, case in cases.items():
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, **case.get("overrides", {}))
        assert _run(service, case.get("proposal"), case.get("broker", "alpaca")) is None
        assert client.orders == [], reason
        assert reason in logs.events[-1][1]["reasons"], (reason, logs.events[-1])


def test_missing_keys_block(tmp_path) -> None:
    logs = _Logs()
    service = EtoroLiveMirrorService(
        settings=_settings(tmp_path), client=None, runtime_state=_state(), run_logs=logs
    )
    assert _run(service) is None
    assert "etoro_live_keys_missing" in logs.events[-1][1]["reasons"]


def test_daily_trade_cap_and_symbol_dedupe(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, state=_state())
    assert _run(service, _proposal("AAPL"))
    assert _run(service, _proposal("AAPL")) is None  # already open
    assert "etoro_live_symbol_already_open" in logs.events[-1][1]["reasons"]
    assert _run(service, _proposal("MSFT"))
    assert len(client.orders) == HARD_MAX_TRADES_PER_DAY
    assert _run(service, _proposal("NVDA")) is None
    assert "etoro_live_daily_trade_cap" in logs.events[-1][1]["reasons"]


def test_one_2x_test_after_two_1x_trades_at_the_same_exposure(tmp_path, monkeypatch) -> None:
    import app.broker.etoro_live_mirror as live

    monkeypatch.setattr(live, "LEVERAGE_TEST_ENABLED", True)  # how the 10-07 AMD test ran
    client = _Client()
    state = _state()
    state.set(
        STATE_KEY,
        json.dumps({"successful_1x": 2, "leverage_2x_done": False, "last_equity": 10_000.0}),
    )
    service, _ = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal("AAPL"))["leverage"] == 2
    # eToro's Amount is margin: $500 at 2x = the same $1,000 exposure as a 1x trade.
    assert client.orders[0].leverage == 2 and client.orders[0].amount_usd == 500.0
    assert _run(service, _proposal("MSFT"))["leverage"] == 1  # only one 2x test, ever
    assert client.orders[1].amount_usd == 1_000.0


def test_the_one_2x_test_cannot_be_re_armed(tmp_path) -> None:
    # The operator's one 2x test ran on 2026-10-07 (AMD). A lost or reset state must not
    # arm a second one.
    client = _Client()
    state = _state()
    state.set(
        STATE_KEY,
        json.dumps({"successful_1x": 5, "leverage_2x_done": False, "last_equity": 10_000.0}),
    )
    service, _ = _mirror(tmp_path, client=client, state=state)
    assert _run(service, _proposal("AAPL"))["leverage"] == 1
    assert (client.orders[0].leverage, client.orders[0].amount_usd) == (1, 1_000.0)


def test_each_copy_is_sized_from_its_stop(tmp_path) -> None:
    # Operator 2026-10-16: a stop-out costs at most 0.5% of equity ($50 of $10,000). Wide
    # stops get smaller positions; tight stops keep the 10% / $1,000 cap.
    for stop, amount in ((98.6, 1_000.0), (95.0, 1_000.0), (93.0, 714.29), (92.5, 666.67)):
        client = _Client()
        service, _ = _mirror(tmp_path, client=client)
        record = _run(service, _proposal("AAPL", stop=stop))
        assert record["amount_usd"] == amount == client.orders[0].amount_usd, stop
        assert amount * (100.0 - stop) / 100.0 <= 50.01, stop  # dollars lost at the stop


def test_stops_more_than_8_pct_below_the_entry_are_not_copied(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    assert _run(service, _proposal("AAPL", stop=91.9)) is None and client.orders == []  # 8.1%
    assert logs.events[-1][1]["reasons"] == ["etoro_live_stop_too_wide"]
    record = _run(service, _proposal("MSFT", stop=92.1))  # 7.9%: copied, sized to its stop
    assert record["amount_usd"] == round(10_000 * 0.5 / 7.9, 2) == 632.91


def test_the_amd_trade_of_10_07_would_not_be_copied(tmp_path) -> None:
    # Fill 642.00, stop 541.53 (15.65% below): it lost $155 on a flat $994 copy.
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    proposal = _proposal("AMD", stop=541.53, target=876.98, price=643.18)
    assert _run(service, proposal) is None and client.orders == []
    assert logs.events[-1][1]["reasons"] == ["etoro_live_stop_too_wide"]


def test_a_stop_at_or_above_the_entry_is_not_copied(tmp_path) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client)
    assert _run(service, _proposal("AAPL", stop=100.0)) is None and client.orders == []
    assert logs.events[-1][1]["reasons"] == ["etoro_live_stop_distance_unknown"]


def test_copy_needs_a_backtest_edge_of_at_least_0_03_r(tmp_path) -> None:
    # Operator 2026-10-16: AMD was copied at +0.024 R and META at 0.00 R; both were "> 0".
    cases = (
        ({"oos_expectancy_r": 0.0237}, False),
        ({"oos_expectancy_r": 0.0}, False),
        ({}, False),  # no figure: refused
        ({"oos_expectancy_r": float("nan")}, False),
        ({"oos_expectancy_r": 0.03}, True),
        ({"oos_expectancy_usd": 7.16}, True),  # $ per $100 risked when R is missing
    )
    for edge, copied in cases:
        state = _state()
        verdicts = {"momentum_breakout:1d": {"passed": True, **edge}}
        state.set(VERDICTS_KEY, verdicts_cache(verdicts))
        client = _Client()
        service, logs = _mirror(tmp_path, client=client, state=state)
        assert bool(_run(service)) is copied, edge
        if not copied:
            assert logs.events[-1][1]["reasons"] == ["strategy_edge_below_bar"], edge


def test_paper_keeps_its_own_evidence_bar(tmp_path) -> None:
    from app.performance.strategy_evidence import evidence_blocker

    state = _state()
    thin = {"momentum_breakout:1d": {"passed": True, "oos_expectancy_r": 0.0237}}
    state.set(VERDICTS_KEY, json.dumps({"verdicts": thin}))
    paper = SimpleNamespace(require_strategy_oos_evidence=True)
    kwargs = dict(strategy="momentum_breakout", timeframe="1d")
    assert evidence_blocker(paper, state, **kwargs) is None
    assert evidence_blocker(paper, state, **kwargs, min_expectancy_r=0.03) == (
        "strategy_edge_below_bar"
    )


def test_daily_loss_stop(tmp_path) -> None:
    client = _Client()
    state = _state()
    from app.utils.time import utc_now

    state.set(
        STATE_KEY,
        json.dumps(
            {
                "day": utc_now().date().isoformat(),
                "day_start_equity": 10_000.0,
                "last_equity": 9_500.0,  # -5%: about -$25 on the operator's $500 copy
            }
        ),
    )
    service, logs = _mirror(tmp_path, client=client, state=state)
    assert _run(service) is None and client.orders == []
    assert "etoro_live_daily_loss_stop" in logs.events[-1][1]["reasons"]
    state.set(
        STATE_KEY,
        json.dumps(
            {
                "day": utc_now().date().isoformat(),
                "day_start_equity": 10_000.0,
                "last_equity": 9_600.0,
            }
        ),
    )
    assert _run(service, _proposal("MSFT")) is not None


def test_broker_error_halts_mirror_without_raising(tmp_path) -> None:
    state = _state()
    service, logs = _mirror(tmp_path, client=_Client(fail=True), state=state)
    assert _run(service) is None  # never raises into the paper execution path
    assert state.get(HALTED_KEY) and logs.events[-1][0] == "etoro_live_mirror_halted"
    healthy = _Client()
    service2, logs2 = _mirror(tmp_path, client=healthy, state=state)
    assert _run(service2) is None and healthy.orders == []
    assert "etoro_live_mirror_halted" in logs2.events[-1][1]["reasons"]


def test_reconcile_closes_positions_without_a_stop(tmp_path) -> None:
    positions = [
        SimpleNamespace(symbol="AAPL", position_id=1),
        SimpleNamespace(symbol="MSFT", position_id=2),
    ]
    raw = [
        {"positionID": 1, "stopLossRate": 95.0},
        {"positionID": 2, "stopLossRate": 0, "isNoStopLoss": True},
    ]
    client = _Client(positions=positions, raw_positions=raw, equity=498.0)
    state = _state()
    service, _ = _mirror(tmp_path, client=client, state=state)
    summary = service.reconcile()
    assert client.closed == ["MSFT"]
    assert summary["equity"] == 498.0 and summary["open_symbols"] == ["AAPL", "MSFT"]


def test_coordinator_mirror_hook_never_breaks_paper_execution(tmp_path) -> None:
    from app.execution.coordinator import ExecutionCoordinator

    calls = []
    coordinator = ExecutionCoordinator.__new__(ExecutionCoordinator)
    coordinator.parallel_broker = None
    coordinator.etoro_live_mirror = SimpleNamespace(mirror=lambda **kw: calls.append(kw))
    coordinator._mirror_parallel(_proposal(), SimpleNamespace(broker_order_id="alp1"), "alpaca")
    assert calls and calls[0]["primary_broker"] == "alpaca"


def test_simulated_client_is_never_used_for_live_trading(tmp_path) -> None:
    # 2026-10-04: the first live check reported a fake $10,000 account -- the client
    # had fallen back to simulation. The mirror must refuse it, not "trade" on it.
    simulated = build_live_client(
        make_settings(tmp_path, etoro_live_api_key="k", etoro_live_user_key="u")
    )
    assert simulated.settings.broker_simulation_enabled  # tests use a .example base URL
    logs = _Logs()
    service = EtoroLiveMirrorService(
        settings=_settings(tmp_path), client=simulated, runtime_state=_state(), run_logs=logs
    )
    assert _run(service) is None
    assert any(
        "etoro_live_client_in_simulation_mode:base_url_host=api.etoro.example" in r
        for r in logs.events[-1][1]["reasons"]
    )
    assert service.reconcile() == {
        "unusable": "etoro_live_client_in_simulation_mode:base_url_host=api.etoro.example"
    }
    assert logs.events[-1][0] == "etoro_live_client_unusable"


def test_balance_counts_money_in_open_positions(tmp_path) -> None:
    # 2026-10-04: after a $1,000 ETH buy eToro's cash credit read $8,990. Treating cash as the
    # balance would shrink later trades and trip the 5% daily loss stop with no real loss.
    from app.broker.etoro_live_mirror import account_value_at_cost

    raw = {
        "clientPortfolio": {"credit": 8_990.0, "positions": [{"positionID": 9, "amount": 1_000.0}]}
    }
    assert account_value_at_cost(raw) == 9_990.0
    assert account_value_at_cost({"clientPortfolio": {}}, fallback=123.0) == 123.0

    class _CashClient(_Client):
        def fetch_raw_portfolio(self):
            return raw

    state = _state()
    state.set(
        STATE_KEY,
        json.dumps(
            {
                "day": __import__("app.utils.time", fromlist=["utc_now"])
                .utc_now()
                .date()
                .isoformat(),
                "day_start_equity": 10_000.0,
                "last_equity": 10_000.0,
            }
        ),
    )
    service, logs = _mirror(tmp_path, client=_CashClient(equity=8_990.0), state=state)
    assert service.reconcile()["equity"] == 9_990.0
    assert "etoro_live_daily_loss_stop" not in service.blockers(_proposal("MSFT"), "alpaca")
    assert _run(service, _proposal("MSFT"))["amount_usd"] == 999.0  # 10% of 9,990
