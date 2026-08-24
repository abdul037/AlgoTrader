from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.live_signal_schema import MarketQuote, SignalState
from app.models.signal import Signal, SignalAction
from app.signals.evaluation import evaluate_equity_catalog, score_with_screener_ranker
from tests.conftest import make_settings


class _Resolver:
    def resolve(self, symbol: str) -> int:
        return 1


def _service(*, ranker_on: bool, strategy_names, settings_obj=None):
    settings = settings_obj or SimpleNamespace(
        live_signal_interval="OneDay",
        live_signal_strategy_names=strategy_names,
        screener_active_strategy_names=["rsi_vwap_ema_confluence"],
        live_signal_use_strategy_catalog=True,
        live_signal_use_screener_ranker=ranker_on,
    )
    svc = SimpleNamespace(settings=settings, resolver=_Resolver(), backtests=None)
    svc._attach_backtest_context = lambda snapshot: snapshot
    return svc


def _candles(rows: int = 2) -> pd.DataFrame:
    base = [100.0 + i for i in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=rows, freq="D", tz="UTC"),
            "open": base,
            "high": [b + 1 for b in base],
            "low": [b - 1 for b in base],
            "close": [b + 0.5 for b in base],
            "volume": [1000 + i * 10 for i in range(rows)],
        }
    )


def _quote() -> MarketQuote:
    return MarketQuote(symbol="NVDA", bid=101.5, ask=102.0, last_execution=101.8, timestamp="2026-01-02T00:00:00Z")


def _buy(name: str, *, confidence: float, rr: float = 2.0) -> Signal:
    return Signal(
        symbol="NVDA",
        strategy_name=name,
        action=SignalAction.BUY,
        rationale=f"{name} long",
        confidence=confidence,
        price=102.0,
        stop_loss=99.0,
        take_profit=108.0,
        metadata={"risk_reward_ratio": rr},
    )


def _factory(mapping):
    return lambda name: SimpleNamespace(generate_signal=lambda data, symbol: mapping[name])


def test_ranker_overrides_confidence_ordering() -> None:
    # Strategy A has higher confidence, B has a higher ranker score. With the
    # ranker on, B must win even though its self-reported confidence is lower.
    service = _service(ranker_on=True, strategy_names=["ma_crossover", "momentum_breakout"])
    factory = _factory(
        {
            "ma_crossover": _buy("ma_crossover", confidence=0.9),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.3),
        }
    )
    scores = {"ma_crossover": 40.0, "momentum_breakout": 75.0}

    def fake_ranker(svc, sym, candles, quote, signal):
        return scores[signal.strategy_name]

    snap = evaluate_equity_catalog(
        service, "NVDA", _candles(), _quote(), strategy_factory=factory, ranker=fake_ranker
    )
    assert snap.state == SignalState.BUY
    assert snap.strategy_name == "momentum_breakout"
    assert snap.score == 75.0
    assert snap.metadata["selection_ranker"] == "screener_final_score"
    assert snap.metadata["screener_final_score"] == 75.0


def test_ranker_error_is_isolated_and_scores_zero() -> None:
    service = _service(ranker_on=True, strategy_names=["ma_crossover", "momentum_breakout"])
    factory = _factory(
        {
            "ma_crossover": _buy("ma_crossover", confidence=0.5),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.5),
        }
    )

    def flaky_ranker(svc, sym, candles, quote, signal):
        if signal.strategy_name == "ma_crossover":
            raise RuntimeError("boom")
        return 60.0

    snap = evaluate_equity_catalog(
        service, "NVDA", _candles(), _quote(), strategy_factory=factory, ranker=flaky_ranker
    )
    assert snap.strategy_name == "momentum_breakout"  # the one that scored
    statuses = {e["strategy"]: e for e in snap.metadata["evaluated_strategies"]}
    assert "ranker_error" in statuses["ma_crossover"]
    assert statuses["ma_crossover"]["screener_final_score"] == 0.0


def test_confidence_ordering_when_ranker_off() -> None:
    service = _service(ranker_on=False, strategy_names=["ma_crossover", "momentum_breakout"])
    factory = _factory(
        {
            "ma_crossover": _buy("ma_crossover", confidence=0.9),
            "momentum_breakout": _buy("momentum_breakout", confidence=0.3),
        }
    )
    snap = evaluate_equity_catalog(service, "NVDA", _candles(), _quote(), strategy_factory=factory)
    assert snap.strategy_name == "ma_crossover"  # highest confidence wins
    assert snap.metadata["selection_ranker"] == "confidence_rr"


def test_score_with_screener_ranker_real_wiring(tmp_path: Path) -> None:
    # Exercise the real build_market_context + rank_live_signal path (no network)
    # on a realistic candle history, and assert a bounded 0-100 score.
    service = SimpleNamespace(settings=make_settings(tmp_path), backtests=None)
    candles = _candles(rows=80)
    signal = _buy("momentum_breakout", confidence=0.6)

    score = score_with_screener_ranker(service, "NVDA", candles, _quote(), signal)

    assert isinstance(score, float)
    assert 0.0 <= score <= 100.0
