from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.models.signal import Signal, SignalAction
from app.signals import evaluation
from app.signals.evaluation import (
    evaluate_equity_catalog,
    evaluate_symbol,
    resolve_live_strategy_names,
)


class _Resolver:
    def resolve(self, symbol: str) -> int:
        return 1


def _service(**settings):
    base = dict(
        live_signal_interval="OneDay",
        live_signal_strategy_names=[],
        screener_active_strategy_names=["rsi_vwap_ema_confluence"],
        live_signal_use_strategy_catalog=True,
        live_signal_candles_count=250,
    )
    base.update(settings)
    svc = SimpleNamespace(settings=SimpleNamespace(**base), resolver=_Resolver())
    svc._attach_backtest_context = lambda snapshot: snapshot
    return svc


def _candles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 1100],
        }
    )


def _quote() -> MarketQuote:
    return MarketQuote(symbol="NVDA", bid=101.5, ask=102.0, last_execution=101.8, timestamp="2026-01-02T00:00:00Z")


def _buy(name: str, *, confidence: float, rr: float, price=102.0, stop=99.0, target=108.0) -> Signal:
    return Signal(
        symbol="NVDA",
        strategy_name=name,
        action=SignalAction.BUY,
        rationale=f"{name} long",
        confidence=confidence,
        price=price,
        stop_loss=stop,
        take_profit=target,
        metadata={"risk_reward_ratio": rr},
    )


class _FakeStrategy:
    def __init__(self, signal) -> None:
        self._signal = signal

    def generate_signal(self, data, symbol):
        if isinstance(self._signal, Exception):
            raise self._signal
        return self._signal


def _factory(mapping):
    return lambda name: _FakeStrategy(mapping[name])


# -- resolve_live_strategy_names ---------------------------------------------


def test_resolve_expands_all_to_core_pack_without_gold() -> None:
    names = resolve_live_strategy_names(_service(live_signal_strategy_names=["all"]))
    assert "gold_momentum" not in names
    assert "pullback_trend" in names
    assert "rsi_vwap_ema_confluence" in names
    assert len(names) == 11  # 12 core strategies minus gold_momentum


def test_resolve_falls_back_to_screener_active_when_unset() -> None:
    names = resolve_live_strategy_names(
        _service(live_signal_strategy_names=[], screener_active_strategy_names=["momentum_breakout"])
    )
    assert names == ["momentum_breakout"]


def test_resolve_filters_unknown_names_and_dedupes() -> None:
    names = resolve_live_strategy_names(
        _service(live_signal_strategy_names=["ma_crossover", "not_a_strategy", "ma_crossover"])
    )
    assert names == ["ma_crossover"]


# -- selection ---------------------------------------------------------------


def test_selects_highest_confidence_buy() -> None:
    service = _service(live_signal_strategy_names=["ma_crossover", "momentum_breakout", "mean_reversion"])
    factory = _factory(
        {
            "ma_crossover": _buy("ma_crossover", confidence=0.6, rr=2.0),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.8, rr=1.5),
            "mean_reversion": None,
        }
    )
    snap = evaluate_equity_catalog(service, "NVDA", _candles(), _quote(), strategy_factory=factory)

    assert isinstance(snap, LiveSignalSnapshot)
    assert snap.state == SignalState.BUY
    assert snap.strategy_name == "momentum_breakout"
    assert snap.confidence == 0.8
    assert snap.risk_reward_ratio == 1.5
    assert snap.stop_loss == 99.0 and snap.take_profit == 108.0
    assert snap.metadata["selected_strategy"] == "momentum_breakout"
    # all three strategies recorded in the audit trail
    assert {e["strategy"] for e in snap.metadata["evaluated_strategies"]} == {
        "ma_crossover",
        "momentum_breakout",
        "mean_reversion",
    }


def test_ties_break_on_reward_to_risk() -> None:
    service = _service(live_signal_strategy_names=["ma_crossover", "momentum_breakout"])
    factory = _factory(
        {
            "ma_crossover": _buy("ma_crossover", confidence=0.7, rr=1.5),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.7, rr=2.5),
        }
    )
    snap = evaluate_equity_catalog(service, "NVDA", _candles(), _quote(), strategy_factory=factory)
    assert snap.strategy_name == "momentum_breakout"
    assert snap.risk_reward_ratio == 2.5


def test_strategy_error_is_isolated() -> None:
    service = _service(live_signal_strategy_names=["ma_crossover", "momentum_breakout"])
    factory = _factory(
        {
            "ma_crossover": RuntimeError("bad data"),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.5, rr=2.0),
        }
    )
    snap = evaluate_equity_catalog(service, "NVDA", _candles(), _quote(), strategy_factory=factory)
    assert snap.strategy_name == "momentum_breakout"
    statuses = {e["strategy"]: e["status"] for e in snap.metadata["evaluated_strategies"]}
    assert statuses["ma_crossover"].startswith("error:")


def test_no_buy_falls_back_to_legacy(monkeypatch) -> None:
    service = _service(live_signal_strategy_names=["ma_crossover", "mean_reversion"])
    factory = _factory({"ma_crossover": None, "mean_reversion": None})

    legacy = LiveSignalSnapshot(symbol="NVDA", strategy_name="pullback_trend", state=SignalState.NONE)
    monkeypatch.setattr(evaluation, "evaluate_equity", lambda *a, **k: legacy)

    snap = evaluate_equity_catalog(service, "NVDA", _candles(), _quote(), strategy_factory=factory)
    assert snap.state == SignalState.NONE
    assert snap.metadata["strategy_selection"] == "catalog_no_buy_fallback"
    assert len(snap.metadata["evaluated_strategies"]) == 2


# -- dispatch ----------------------------------------------------------------


def test_evaluate_symbol_dispatches_to_catalog_when_enabled(monkeypatch) -> None:
    service = _service(live_signal_use_strategy_catalog=True)
    service.market_data = SimpleNamespace(
        get_daily_candles=lambda *a, **k: _candles(),
        get_rates=lambda symbols: {"NVDA": _quote()},
    )
    called = {}
    monkeypatch.setattr(
        evaluation,
        "evaluate_equity_catalog",
        lambda *a, **k: called.setdefault("catalog", True)
        or LiveSignalSnapshot(symbol="NVDA", strategy_name="x", state=SignalState.NONE),
    )
    monkeypatch.setattr(
        evaluation, "evaluate_equity", lambda *a, **k: called.setdefault("legacy", True) or None
    )
    evaluate_symbol(service, "NVDA")
    assert called == {"catalog": True}
