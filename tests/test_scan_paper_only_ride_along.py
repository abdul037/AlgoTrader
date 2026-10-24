"""Paper-only specs riding along in the universe scan (option B, app/screener/scan_ranking.py).

* on each symbol they are evaluated after the scan's other specs, on the same bars and quote;
* their candidates never take a place in the scan's top-k, however they score: the other specs'
  candidates are cut exactly as without them, and the paper-only ones keep their own top 3;
* without them (or without a requested spec list) the scan is as it was.

Deterministic: in-memory bars, stub strategies, controlled scores; no clock, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.screener.service_scan as service_scan
from app.live_signal_schema import MarketQuote
from app.models.signal import Signal, SignalAction
from app.screener.filters import FilterOutcome
from app.screener.scan_ranking import PAPER_ONLY_TOP_K, scan_spec_keys
from app.screener.service import MarketScreenerService
from tests.conftest import make_settings
from tests.test_screener_service import (
    FakeBacktestRepository,
    FakeMarketDataEngine,
    FakeRunLogRepository,
    FakeScanDecisionRepository,
    FakeSignalStateRepository,
    FakeTelegramNotifier,
    _frame,
)

SYMBOLS = ["AAA", "BBB", "CCC"]
ETORO = ["ma_crossover:1d", "trend_following:1d", "ema_trend_stack:1d"]  # passing the full check
# In the registry, momentum_breakout:1d comes before ema_trend_stack:1d.
PAPER_ONLY = ["momentum_breakout:1d", "etf_mega_cap_relative_strength_rotation:1d"]


class _CountingEngine(FakeMarketDataEngine):
    def __init__(self):
        frame = _frame([100 + index * 0.2 for index in range(130)])
        quotes = {
            symbol: MarketQuote(
                symbol=symbol,
                bid=125.9,
                ask=126.0,
                last_execution=125.95,
                timestamp="2026-04-11T10:00:00Z",
            )
            for symbol in SYMBOLS
        }
        super().__init__({(symbol, "1d"): frame for symbol in SYMBOLS}, quotes)
        self.reads: list[tuple[str, str]] = []

    def get_history(self, symbol, **kwargs):
        self.reads.append(("bars", symbol.upper()))
        return super().get_history(symbol, **kwargs)

    def get_quote(self, symbol, **kwargs):
        self.reads.append(("quote", symbol.upper()))
        return super().get_quote(symbol, **kwargs)


def _service(tmp_path, **settings):
    engine = _CountingEngine()
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=SYMBOLS,
            screener_default_timeframes=["1d"],
            screener_top_k=2,
            **settings,
        ),
        market_data_engine=engine,
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=None),
        scan_decision_repository=FakeScanDecisionRepository(),
        telegram_notifier=FakeTelegramNotifier(),
    )
    return service, engine


def _scan(service, paper_only=None, keys=ETORO):
    return service.scan_universe(
        symbols=SYMBOLS,
        timeframes=["1d"],
        scan_task="swing_scan",
        strategy_spec_keys=keys,
        paper_only_spec_keys=paper_only,
    )


def test_paper_only_specs_run_after_the_others_on_each_symbol_on_the_same_bars(
    tmp_path, monkeypatch
) -> None:
    service, engine = _service(tmp_path)
    evaluated: list[tuple[str, str]] = []

    class _Recorder:
        last_diagnostics = None

        def __init__(self, name):
            self.name = name

        def generate_signal(self, _history, symbol):
            evaluated.append((symbol, self.name))
            return None

    monkeypatch.setattr(service, "_build_strategy", lambda spec: _Recorder(spec.name))
    alone = _scan(service)
    reads_alone, engine.reads = list(engine.reads), []
    evaluated.clear()
    riding = _scan(service, PAPER_ONLY)
    own = ["ma_crossover", "trend_following", "ema_trend_stack"]  # the registry's own order
    later = ["momentum_breakout", "etf_mega_cap_relative_strength_rotation"]
    assert evaluated == [(symbol, name) for symbol in SYMBOLS for name in own + later]
    assert engine.reads == reads_alone  # the same bars and quote a symbol: nothing fetched more
    assert reads_alone == [(kind, s) for s in SYMBOLS for kind in ("bars", "quote")]
    assert "paper_only" not in alone.coverage
    assert riding.coverage["paper_only"] == {
        "specs": 2,
        "specs_evaluated": 2,
        "candidates_found": 0,
        "candidates": 0,
    }  # counts only, no key lists
    assert riding.coverage["specs_evaluated"] == 5  # the scan's own counts: every spec it ran


def test_a_deadline_inside_a_symbol_stops_before_its_paper_only_specs(
    tmp_path, monkeypatch
) -> None:
    service, _engine = _service(tmp_path, screener_batch_deadline_seconds=30.0)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(service_scan.time, "monotonic", lambda: clock.now)
    evaluated: list[tuple[str, str]] = []

    class _Slow:
        last_diagnostics = None

        def __init__(self, name):
            self.name = name

        def generate_signal(self, _history, symbol):
            evaluated.append((symbol, self.name))
            clock.now += 10.0  # the third of the symbol's own specs reaches the deadline
            return None

    monkeypatch.setattr(service, "_build_strategy", lambda spec: _Slow(spec.name))
    response = _scan(service, PAPER_ONLY)
    own = ("ma_crossover", "trend_following", "ema_trend_stack")
    assert evaluated == [("AAA", name) for name in own]  # all of the symbol's own specs
    assert "scan_deadline_exceeded" in response.errors
    assert response.coverage["paper_only"]["specs_evaluated"] == 0


def test_a_key_is_paper_only_only_beside_a_requested_spec_list() -> None:
    assert scan_spec_keys(ETORO, PAPER_ONLY) == ({*ETORO, *PAPER_ONLY}, set(PAPER_ONLY))
    # A key the run asked for is its own; without a spec list the scan runs every spec.
    assert scan_spec_keys(ETORO, [ETORO[0]]) == (set(ETORO), set())
    assert scan_spec_keys(None, PAPER_ONLY) == (set(), set())
    assert scan_spec_keys([" MA_Crossover:1D "], None) == ({"ma_crossover:1d"}, set())


SCORES = {  # every paper-only candidate outscores every eToro-passing one
    "ma_crossover": 60.0,
    "trend_following": 55.0,
    "ema_trend_stack": 50.0,
    "momentum_breakout": 99.0,
    "etf_mega_cap_relative_strength_rotation": 98.0,
}


@pytest.mark.parametrize("momentum_overlay", [False, True])
def test_paper_only_candidates_never_take_a_place_in_the_scans_top_k(
    tmp_path, monkeypatch, momentum_overlay
) -> None:
    class _Stub:
        def __init__(self, name):
            self.name = name

        def generate_signal(self, _history, symbol):
            return Signal(
                symbol=symbol,
                strategy_name=self.name,
                action=SignalAction.BUY,
                confidence=0.9,
                price=125.95,
                stop_loss=120.0,
                take_profit=140.0,
                rationale="ride-along fixture",
                metadata={"signal_role": "entry_long", "risk_reward_ratio": 2.5},
            )

    def rank(**kwargs):
        return {
            "final_score": SCORES[kwargs["signal"].strategy_name],
            "score_breakdown": {},
            "confidence_label": "high",
            "direction_label": "buy",
            "actionability": "alert",
        }

    monkeypatch.setattr(service_scan, "rank_live_signal", rank)

    def scan(paper_only):  # a fresh screener each time: no earlier candidate to repeat
        service, _engine = _service(
            tmp_path,
            cross_sectional_momentum_enabled=momentum_overlay,
            cross_sectional_momentum_top_pct=50.0,
        )
        monkeypatch.setattr(service, "_build_strategy", lambda spec: _Stub(spec.name))
        passed = FilterOutcome(
            passed=True, pass_reasons=["ok"], rejection_reasons=[], reason_codes=[]
        )
        monkeypatch.setattr(service.filters, "evaluate", lambda **_k: passed)
        return _scan(service, paper_only)

    def kept(response):
        return [(c.rank, c.symbol, c.strategy_name, c.score) for c in response.candidates]

    alone = kept(scan(None))  # as before paper-only specs rode along: 9 found, top 2 kept
    riding = scan(PAPER_ONLY)
    assert len(alone) == 2 and {name for _r, _s, name, _x in alone} == {"ma_crossover"}
    assert kept(riding)[:2] == alone  # the same eToro-passing candidates, the same ranks
    extra = kept(riding)[2:]
    assert len(extra) == PAPER_ONLY_TOP_K == 3  # their own cut, after the top-k
    assert [rank for rank, *_ in extra] == [3, 4, 5]
    assert {name for _r, _s, name, _x in extra} == {"momentum_breakout"}  # 99 beats 98
    coverage = riding.coverage["paper_only"]
    assert (coverage["candidates_found"], coverage["candidates"]) == (6, 3)
    assert riding.coverage["candidates_found"] == 15  # 3 symbols x 5 specs


def _cut_before_option_b(settings, candidates, limit):
    """The scan's single cut as it was at d2c7faf (service_scan, before scan_ranking)."""

    from app.screener.cross_sectional_momentum import filter_top_momentum
    from app.screener.service_diagnostics import ranking_key

    ranked = sorted(candidates, key=ranking_key, reverse=True)
    if bool(getattr(settings, "cross_sectional_momentum_enabled", False)) and ranked:
        top_pct = float(getattr(settings, "cross_sectional_momentum_top_pct", 30.0) or 30.0)
        ranked = filter_top_momentum(ranked, top_pct=top_pct)
    top_k = min(limit or settings.screener_top_k, len(ranked)) if ranked else 0
    return [item.model_copy(update={"rank": i + 1}) for i, item in enumerate(ranked[:top_k])]


def test_the_top_k_keeps_what_it_kept_before_option_b_rode_along() -> None:
    """Seeded parity: the eToro-passing candidates kept, and their ranks, equal the old single
    cut over them alone, whatever the paper-only candidates score; and with no paper-only
    keys the cut is the old one over every candidate."""

    import random

    from app.screener.scan_ranking import top_candidates
    from app.screener.service_diagnostics import ranking_key
    from tests.test_workflow_service import _snapshot

    rng = random.Random(20261023)
    names = ["ma_crossover", "trend_following", "ema_trend_stack", "momentum_breakout", "x"]
    for case in range(400):
        settings = SimpleNamespace(
            screener_top_k=rng.choice([1, 2, 3, 5, 10]),
            cross_sectional_momentum_enabled=rng.random() < 0.5,
            cross_sectional_momentum_top_pct=rng.choice([10.0, 30.0, 50.0, 100.0]),
        )
        service = SimpleNamespace(settings=settings, _ranking_key=ranking_key)
        paper_only = set(rng.sample([f"{n}:1d" for n in names], rng.randint(0, 3)))
        candidates = [
            _snapshot().model_copy(
                update={
                    "symbol": f"S{index}",
                    "strategy_name": rng.choice(names),
                    "score": round(rng.uniform(40, 99), rng.choice([0, 1])),
                    "confidence": rng.choice([0.5, 0.7, 0.9]),
                    "metadata": {
                        "alert_eligible": rng.random() < 0.8,
                        "backtest_validated": rng.random() < 0.5,
                        "momentum_pct": round(rng.uniform(-5, 5), 1),
                    },
                }
            )
            for index in range(rng.randint(0, 25))
        ]
        limit = rng.choice([None, 1, 3, 10])
        kept, coverage = top_candidates(
            service, candidates, limit=limit, paper_only_keys=paper_only, evaluated_spec_keys=set()
        )
        own = [c for c in candidates if f"{c.strategy_name}:1d" not in paper_only]
        before = _cut_before_option_b(settings, own, limit)
        etoro_kept = [c for c in kept if f"{c.strategy_name}:1d" not in paper_only]
        assert [c.model_dump() for c in etoro_kept] == [c.model_dump() for c in before], case
        assert kept[: len(before)] == etoro_kept  # all of them first, ranks 1..k unchanged
        assert len(kept) - len(before) <= PAPER_ONLY_TOP_K
        assert ("paper_only" in coverage) is bool(paper_only)
