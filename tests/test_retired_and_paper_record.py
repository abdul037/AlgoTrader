"""Operator 2026-11-01: retire the rotation strategy, and copy to eToro only what paper has shown
making money (3+ closed paper trades with a positive total).

The rotation strategy lost all four of its paper trades (-$1,557) and made all three of eToro's
losing copies (-$280); it was first copied on 10-05, ten days before its first closed paper trade.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from app.backtesting.strategy_selection import strategy_specs_for
from app.broker.etoro_live_mirror import STATE_KEY
from app.broker.etoro_live_paper_record import (
    MIN_CLOSED_PAPER_TRADES,
    NOT_POSITIVE,
    TOO_FEW,
    UNKNOWN,
    paper_record_blocker,
    paper_records,
    read_paper_records,
)
from app.broker.etoro_live_room import ROOM_REASONS, room_verdict
from app.performance.strategy_evidence import STRATEGY_RETIRED, VERDICTS_KEY, evidence_blocker
from app.runtime_settings import AppSettings
from app.signals.evaluation import resolve_live_strategy_names
from app.strategies import (
    RETIRED_STRATEGY_NAMES,
    STRATEGY_REGISTRY,
    get_strategy,
    get_strategy_specs,
    is_retired,
)
from tests.test_etoro_live_mirror import _Client, _mirror, _PaperLedger, _proposal, _run, _state
from tests.test_etoro_live_sizing import _blocked
from tests.test_workflow_service import FakeState

ROTATION = "etf_mega_cap_relative_strength_rotation"


def _trade(strategy, pnl, timeframe="1d", **payload):
    return SimpleNamespace(
        strategy_name=strategy, timeframe=timeframe, realized_pnl_usd=pnl, payload=payload
    )


# -- 1. the rotation strategy is retired -----------------------------------------------------------
def test_the_rotation_strategy_is_retired() -> None:
    assert sorted(RETIRED_STRATEGY_NAMES) == [ROTATION]
    assert is_retired(ROTATION) and is_retired(f"  {ROTATION.upper()} ")
    assert not is_retired("momentum_breakout") and not is_retired(None)


def test_no_scan_or_backtest_ever_lists_it(tmp_path) -> None:
    assert all(spec.name != ROTATION for spec in get_strategy_specs())
    assert all(spec.name != ROTATION for spec in get_strategy_specs(timeframe="1d"))
    settings = SimpleNamespace(screener_active_strategy_names=[ROTATION, "momentum_breakout"])
    names = {spec.name for spec in strategy_specs_for(settings, timeframe="1d")}
    assert names == {"momentum_breakout"}  # configured by name: still left out
    asked = strategy_specs_for(SimpleNamespace(), timeframe="1d", requested={ROTATION})
    assert asked == []  # asked for by name (the batch backtester): still left out
    live = SimpleNamespace(settings=SimpleNamespace(live_signal_strategy_names=[ROTATION, "ma_crossover"]))
    assert resolve_live_strategy_names(live) == ["ma_crossover"]


def test_its_class_still_resolves_for_stored_trades_and_reports() -> None:
    assert ROTATION in STRATEGY_REGISTRY
    assert get_strategy(ROTATION, timeframe="1d").name == ROTATION


def test_the_weak_signal_default_list_no_longer_names_it() -> None:
    default = AppSettings.model_fields["paper_strategy_weak_signal_allowed_strategies"]
    assert ROTATION not in default.default_factory()


@pytest.mark.parametrize("required", [True, False])
def test_the_evidence_gate_refuses_it_whatever_its_evidence(required) -> None:
    state = FakeState()
    cache = {"computed_at": "2099-01-01T00:00:00+00:00", "verdicts": {}}
    cache["verdicts"][f"{ROTATION}:1d"] = {"passed": True, "oos_expectancy_r": 0.5}
    state.set(VERDICTS_KEY, json.dumps(cache))
    settings = SimpleNamespace(require_strategy_oos_evidence=required, paper_oos_bar_enabled=True)
    for paper in (True, False):
        reason = evidence_blocker(settings, state, strategy=ROTATION, timeframe="1d", paper=paper)
        assert reason == STRATEGY_RETIRED
    assert evidence_blocker(settings, None, strategy=ROTATION, timeframe="1d") == STRATEGY_RETIRED


def test_the_etoro_copy_refuses_it_and_says_why(tmp_path) -> None:
    client = _Client()
    state = _state(passing=(f"{ROTATION}:1d",))
    service, logs = _mirror(tmp_path, client=client, state=state, paper_trades=_PaperLedger((ROTATION,)))
    assert _run(service, _proposal("AAPL", strategy=ROTATION)) is None and client.orders == []
    assert STRATEGY_RETIRED in _blocked(logs)


# -- 2. eToro copies only a strategy paper has shown making money --------------------------------------
def test_the_approved_bar() -> None:
    assert MIN_CLOSED_PAPER_TRADES == 3
    assert not {TOO_FEW, NOT_POSITIVE, UNKNOWN} & ROOM_REASONS  # paper's own entry goes ahead


def test_records_are_kept_per_strategy_and_timeframe_without_paper_only_trades() -> None:
    trades = [
        _trade("Momentum_Breakout", 100.0, timeframe="1D"),
        _trade("momentum_breakout ", -40.0),
        _trade("momentum_breakout", 70.0, timeframe="15m"),
        _trade("momentum_breakout", 10.0, paper_only=True, paper_only_reason="oos_only"),
        _trade("", 999.0),
    ]
    assert paper_records(trades) == {
        "momentum_breakout:1d": (2, 60.0),
        "momentum_breakout:15m": (1, 70.0),
    }


@pytest.mark.parametrize(
    ("pnls", "reason"),
    [
        ([], TOO_FEW),
        ([500.0, 500.0], TOO_FEW),
        ([100.0, -50.0, -50.0], NOT_POSITIVE),  # a total of exactly zero is not a profit
        ([-10.0, -10.0, -10.0, 100.0], None),
        ([0.01, 0.0, 0.0], None),
        ([100.0, 100.0, math.nan], UNKNOWN),
        ([100.0, 100.0, None], UNKNOWN),
        ([100.0, 100.0, "1"], UNKNOWN),
        ([100.0, 100.0, True], UNKNOWN),
        ([100.0, 100.0, math.inf], UNKNOWN),
    ],
)
def test_the_blocker(pnls, reason) -> None:
    records = paper_records([_trade("s", pnl) for pnl in pnls])
    assert paper_record_blocker(records, "s", "1d") == reason
    assert paper_record_blocker(records, "S ", " 1D") == reason
    assert paper_record_blocker(records, "other", "1d") == TOO_FEW  # never another's record
    assert paper_record_blocker(records, "s", "15m") == TOO_FEW  # nor another timeframe's


def test_paper_only_winners_never_earn_a_copy() -> None:
    tagged = {"paper_only": True, "paper_only_reason": "oos_only"}
    records = paper_records([_trade("s", 100.0, **tagged) for _ in range(5)])
    assert paper_record_blocker(records, "s", "1d") == TOO_FEW


def test_the_paper_ledger_of_2026_11_01() -> None:
    inside_bar, anchored = "inside_bar_narrow_range_breakout", "anchored_vwap_pullback_continuation"
    ledger = [
        *(_trade(ROTATION, pnl) for pnl in (-408.63, -496.59, -511.61, -140.07)),
        *(_trade("momentum_breakout", pnl) for pnl in (-1.49, 484.38, 247.30)),
        _trade("ema_trend_stack", 76.80, timeframe="15m"),
        *(_trade("ema_trend_stack", pnl) for pnl in (439.15, -501.02)),
        *(_trade(inside_bar, pnl, timeframe="15m") for pnl in (-71.93, -70.12)),
        _trade(inside_bar, 509.82),
        *(_trade(anchored, pnl, timeframe="15m") for pnl in (-46.51, -53.41)),
        *(_trade(anchored, pnl, timeframe="5m") for pnl in (545.70, 168.72)),
    ]
    records = paper_records(ledger)
    assert paper_record_blocker(records, ROTATION, "1d") == NOT_POSITIVE
    assert paper_record_blocker(records, "momentum_breakout", "1d") is None  # 3 trades, +$730.19
    # Pooled by name these three would pass (+$14.93, +$367.77, +$614.50); per timeframe, no.
    assert paper_record_blocker(records, "ema_trend_stack", "1d") == TOO_FEW  # 2, net -$61.87
    assert paper_record_blocker(records, inside_bar, "1d") == TOO_FEW
    assert paper_record_blocker(records, anchored, "5m") == TOO_FEW
    # On 10-05, before its first close, the rotation strategy had no record: no copy.
    assert paper_record_blocker(paper_records([]), ROTATION, "1d") == TOO_FEW


class _Unreadable:
    def list(self, *, limit=500):
        raise RuntimeError("database is down")


def test_no_ledger_or_an_unreadable_one_reads_as_unknown() -> None:
    assert read_paper_records(None) is None
    assert read_paper_records(_Unreadable()) is None
    assert paper_record_blocker(None, "momentum_breakout", "1d") == UNKNOWN


@pytest.mark.parametrize(
    ("ledger", "reason"),
    [
        (_PaperLedger(names=()), TOO_FEW),
        (_PaperLedger(pnl=-5.0), NOT_POSITIVE),
        (_Unreadable(), UNKNOWN),
    ],
)
def test_the_live_copy_refuses_without_the_record(tmp_path, ledger, reason) -> None:
    client = _Client()
    service, logs = _mirror(tmp_path, client=client, paper_trades=ledger)
    assert _run(service, _proposal("AAPL")) is None and client.orders == []
    assert reason in _blocked(logs)


def test_the_live_copy_refuses_when_no_ledger_is_attached(tmp_path) -> None:
    from app.broker.etoro_live_mirror import EtoroLiveMirrorService

    built, _ = _mirror(tmp_path)
    client = _Client()
    service = EtoroLiveMirrorService(
        settings=built.settings, client=client, runtime_state=_state(), run_logs=built.logs
    )
    assert _run(service, _proposal("AAPL")) is None and client.orders == []
    assert UNKNOWN in _blocked(built.logs)


def test_the_live_copy_goes_ahead_with_the_record(tmp_path) -> None:
    client = _Client()
    service, _ = _mirror(tmp_path, client=client, paper_trades=_PaperLedger(pnl=1.0))
    assert _run(service, _proposal("AAPL")) and len(client.orders) == 1


def test_the_live_copy_reads_the_record_of_its_own_timeframe(tmp_path) -> None:
    client = _Client()
    intraday = _PaperLedger(timeframe="15m")  # winners, but on another timeframe
    service, logs = _mirror(tmp_path, client=client, paper_trades=intraday)
    assert _run(service, _proposal("AAPL")) is None and client.orders == []
    assert TOO_FEW in _blocked(logs)


def test_paper_keeps_its_own_room_rules_while_the_record_is_missing(tmp_path) -> None:
    state = _state(equity=9_988.0)
    data = {"open_symbols": [], "last_equity": 9_988.0, "last_cash": 9_000.0, "open_notional": {}}
    state.set(STATE_KEY, json.dumps(data))
    settings = dict(paper_broker="alpaca", broker_for_equities="alpaca")
    service, _ = _mirror(tmp_path, state=state, paper_trades=_PaperLedger(names=()), **settings)
    order = _proposal("AAPL").order
    order.metadata["timeframe"] = "1d"
    assert room_verdict(service, order) is None  # not a copy, so paper's room decides as before
    service.paper_trades = _PaperLedger()
    assert room_verdict(service, order) == []  # with the record, eToro's room again
