"""Paper shorts in the screener (production finding 2026-11-16: zero shorts traded).

With ``paper_short_entries_enabled`` on, a bracketed short must meet every screener quality gate
as the mirror image of a long, at the same strictness (app/screener/short_mirror.py,
docs/notes/paper_shorts_screener.md). Three things are proved here:

* longs are byte-identical: every function this work edits runs the same seeded long inputs
  (and, with the switch off, short inputs too) through today's code and through the 91d0a6d
  sources kept verbatim in ``tests/screener_long_parity_reference/``, and the results must
  match exactly (``json.dumps`` of the whole result, key order included);
* the mirror is exact: under a price reflection ``p -> 2K - p`` (EMAs, VWAP and MACD reflect,
  RSI goes to ``100 - RSI``, ATR, ADX and volume are unchanged, swing highs and lows swap) a
  short on the reflected bars scores what the long scores on the original ones;
* end to end through ``scan_universe``: a weak stock in a falling market and sector gives an
  alert-eligible, execution-ready short; the same stock as a long is rejected; a strong stock's
  short is rejected (its rejections are exactly the weak stock long's).

Re-baseline the reference only after a deliberate long change, from a known-good commit: copy
its file over ``tests/screener_long_parity_reference/<name>.py.txt``, update
``REFERENCE_COMMIT`` and ``REFERENCE_SHA256``, and say so in the commit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app import indicators as indicators_module
from app.intelligence.market_regime import MarketIntelligenceService
from app.live_signal_schema import MarketQuote
from app.models.signal import Signal, SignalAction
from app.runtime_settings import DEFAULT_PAPER_NEAR_MISS_ALLOWED_REASONS
from app.screener import accuracy as accuracy_module
from app.screener import filters as filters_module
from app.screener import scan_promotion as promotion_module
from app.screener.profiles import effective_screener_settings
from app.screener.service import MarketScreenerService
from app.screener.service_backtests import compute_risk_reward
from app.screener.short_mirror import (
    LONG_CONSTRUCTIVE_RSI,
    LONG_EXHAUSTED_RSI,
    SHORT_BACKTEST_NAME_SUFFIX,
    SHORT_CONSTRUCTIVE_RSI,
    SHORT_EXHAUSTED_RSI,
    backtest_strategy_name,
    mirrors_short,
)
from tests.conftest import make_settings
from tests.test_screener_service import (
    FakeMarketDataEngine,
    FakeRunLogRepository,
    FakeScanDecisionRepository,
    FakeSignalStateRepository,
    FakeTelegramNotifier,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = Path(__file__).parent / "screener_long_parity_reference"
REFERENCE_COMMIT = "91d0a6d"
REFERENCE_PATHS = {
    "indicators": "app/indicators.py",
    "filters": "app/screener/filters.py",
    "accuracy": "app/screener/accuracy.py",
    "scan_promotion": "app/screener/scan_promotion.py",
}
REFERENCE_SHA256 = {
    "indicators": "6ecc886588ee6314a0926b5b491da6117a03ace05cbeafd1b773c60a598cd825",
    "filters": "e4c61e42bee332b2e98ea6ee51692c472e7bfb6f9e98a2ef4f99a3d8fdb34936",
    "accuracy": "1fb5915afbb63e07e9a8528455cf01859a7b22e3976ee09fece2d8df805e202d",
    "scan_promotion": "96aa3ee0e52ea01ba437d5cf92695295e58528580e8843f5bc2484c30ae33818",
}
SWITCH = dict(
    execution_mode="paper",
    enable_real_trading=False,
    paper_short_entries_enabled=True,
    paper_broker="alpaca",
    broker_for_equities="alpaca",
)
OFF = {**SWITCH, "paper_short_entries_enabled": False}


# --------------------------------------------------------------------------------------------
# The 91d0a6d reference, loaded beside today's code
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ref() -> SimpleNamespace:
    """The 91d0a6d modules, each executed under its own name. Every name they import from a
    module this work edits is rebound to the reference's own (the confluence score lives in
    app/indicators.py), so nothing of today's code takes part in the reference's results."""

    loaded: dict[str, types.ModuleType] = {}
    names = []
    try:
        for stem in REFERENCE_SHA256:
            path = REFERENCE_DIR / f"{stem}.py.txt"
            module = types.ModuleType(f"screener_long_parity_reference.{stem}")
            module.__file__ = str(path)
            sys.modules[module.__name__] = module  # dataclasses resolve through it
            names.append(module.__name__)
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
            loaded[stem] = module
        for stem in ("filters", "accuracy"):
            loaded[stem].compute_confluence_score = loaded["indicators"].compute_confluence_score
            loaded[stem].enrich_technical_indicators = loaded["indicators"].enrich_technical_indicators
        yield SimpleNamespace(**loaded)
    finally:
        for name in names:
            sys.modules.pop(name, None)


def test_the_reference_is_pinned_and_is_the_reference_commit_verbatim() -> None:
    for stem, digest in REFERENCE_SHA256.items():
        text = (REFERENCE_DIR / f"{stem}.py.txt").read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(text).hexdigest() == digest, stem
    for stem, source in REFERENCE_PATHS.items():
        try:
            shown = subprocess.run(
                ["git", "show", f"{REFERENCE_COMMIT}:{source}"],
                cwd=REPO_ROOT, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pytest.skip("git is not available")
        if shown.returncode != 0:
            pytest.skip(f"{REFERENCE_COMMIT} is not in this checkout (shallow clone)")
        assert shown.stdout.replace(b"\r\n", b"\n") == (REFERENCE_DIR / f"{stem}.py.txt").read_bytes().replace(
            b"\r\n", b"\n"
        ), stem


def test_the_reference_runs_its_own_code(ref) -> None:
    assert ref.filters.compute_confluence_score is ref.indicators.compute_confluence_score
    assert ref.accuracy.compute_confluence_score is ref.indicators.compute_confluence_score
    assert ref.filters.compute_confluence_score is not indicators_module.compute_confluence_score
    assert "mirror_short" not in ref.indicators.compute_confluence_score.__code__.co_varnames
    assert ref.scan_promotion._maybe_promote_paper_near_miss.__globals__["_paper_near_miss_blockers"] is (
        ref.scan_promotion._paper_near_miss_blockers
    )


# --------------------------------------------------------------------------------------------
# Seeded inputs
# --------------------------------------------------------------------------------------------
def _bars(seed: int, *, bars: int = 160, step: str = "1D", drift: float | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = float(rng.normal(0.0, 0.004)) if drift is None else drift
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, rng.uniform(0.006, 0.025), bars)))
    high = close * (1.0 + rng.uniform(0.001, 0.02, bars))
    low = close * (1.0 - rng.uniform(0.001, 0.02, bars))
    open_ = low + (high - low) * rng.uniform(0.0, 1.0, bars)
    volume = rng.integers(200_000, 3_000_000, bars).astype("float64")
    start = "2026-01-02T00:00:00Z" if step == "1D" else "2026-03-02T14:30:00Z"
    timestamps = pd.date_range(start=start, periods=bars, freq=step, tz="UTC")
    return pd.DataFrame({"timestamp": timestamps, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def _reflect(frame: pd.DataFrame, k: float | None = None) -> pd.DataFrame:
    """The mirror world: ``p -> 2K - p`` (highs and lows swap); volume and time unchanged."""

    k = float(frame["high"].max()) * 2.0 if k is None else k
    out = frame.copy()
    out["open"] = 2.0 * k - frame["open"]
    out["close"] = 2.0 * k - frame["close"]
    out["high"] = 2.0 * k - frame["low"]
    out["low"] = 2.0 * k - frame["high"]
    return out


def _quote(rng: np.random.Generator, price: float) -> SimpleNamespace:
    last = price * (1.0 + float(rng.normal(0.0, 0.002)))
    spread = float(rng.choice([0.0, 0.0002, 0.001, 0.008]))
    if rng.uniform() < 0.15:
        return SimpleNamespace(bid=None, ask=None, last_execution=last)
    return SimpleNamespace(bid=last * (1 - spread), ask=last * (1 + spread), last_execution=last)


def _signal(rng: np.random.Generator, *, role: str, timeframe: str, price: float) -> SimpleNamespace:
    style = str(rng.choice(["trend", "momentum", "mean_reversion", "breakout"]))
    metadata = {"timeframe": timeframe, "signal_role": role, "style": style}
    if rng.uniform() < 0.5:
        metadata["indicator_confluence_score"] = round(float(rng.uniform(0.0, 1.0)), 4)
    action = SignalAction.SELL if role.startswith("entry_short") or role == "exit" else SignalAction.BUY
    return SimpleNamespace(
        symbol="SYNA", strategy_name="trend_following", action=action, price=price, metadata=metadata,
        confidence=0.7, stop_loss=None, take_profit=None,
    )


def _dump(value) -> str:
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    return json.dumps(value, default=str)


@pytest.fixture(scope="module")
def settings(tmp_path_factory) -> SimpleNamespace:
    root = tmp_path_factory.mktemp("paper_shorts_screener")
    on = make_settings(root / "on", **SWITCH)
    off = make_settings(root / "off", **OFF)
    assert mirrors_short(on, SimpleNamespace(metadata={"signal_role": "entry_short"}))
    assert not mirrors_short(off, SimpleNamespace(metadata={"signal_role": "entry_short"}))
    return SimpleNamespace(on=on, off=off)


def _parity_cases(settings):
    """(history, quote, signal, settings) for every case that must not move: every long with
    the switch on and off, and every short, exit and short watch with the switch off."""

    cases = []
    for seed in range(36):
        rng = np.random.default_rng(1000 + seed)
        timeframe = ["1d", "1d", "15m", "1h"][seed % 4]
        step = {"1d": "1D", "15m": "15min", "1h": "1h"}[timeframe]
        history = _bars(seed, bars=int(rng.integers(90, 220)), step=step)
        price = float(history["close"].iloc[-1])
        for role, cfg in (
            ("entry_long", settings.on),
            ("entry_long", settings.off),
            ("entry_short", settings.off),
            ("entry_short_watch", settings.on),
            ("exit", settings.on),
        ):
            cases.append((history, _quote(rng, price), _signal(rng, role=role, timeframe=timeframe, price=price), cfg))
    return cases


# --------------------------------------------------------------------------------------------
# Longs are byte-identical to 91d0a6d
# --------------------------------------------------------------------------------------------
def test_confluence_score_default_is_byte_identical(ref) -> None:
    rows = 0
    for seed in range(30):
        frame = indicators_module.enrich_technical_indicators(_bars(seed), timeframe="1d")
        for _, row in frame.tail(60).iterrows():
            for is_short in (False, True):
                assert indicators_module.compute_confluence_score(row, is_short=is_short) == (
                    ref.indicators.compute_confluence_score(row, is_short=is_short)
                )
            # the mirror flag never touches a long
            assert indicators_module.compute_confluence_score(row, is_short=False, mirror_short=True) == (
                ref.indicators.compute_confluence_score(row, is_short=False)
            )
            rows += 1
    assert rows == 30 * 60


def test_market_context_accuracy_and_filters_are_byte_identical_for_longs(ref, settings) -> None:
    cases = _parity_cases(settings)
    shorts_off = 0
    for history, quote, signal, cfg in cases:
        new_context = filters_module.build_market_context(history, quote=quote, signal=signal, settings=cfg)
        old_context = ref.filters.build_market_context(history, quote=quote, signal=signal)
        assert _dump(new_context) == _dump(old_context)

        effective = effective_screener_settings(cfg)
        new_profile = accuracy_module.build_accuracy_profile(history, signal=signal, context=new_context, settings=effective)
        old_profile = ref.accuracy.build_accuracy_profile(history, signal=signal, context=old_context, settings=effective)
        assert _dump(new_profile) == _dump(old_profile)

        signal.metadata.update({"accuracy_score": new_profile.overall_score,
                                "confirmation_score": new_profile.confirmation_score,
                                "false_positive_risk_score": new_profile.false_positive_risk_score})
        intelligence = SimpleNamespace(
            market_regime_score=0.6, timeframe_alignment_score=0.4, relative_strength_vs_market=0.3,
            relative_strength_vs_sector=-0.5, sector_strength_score=0.68, benchmark_strength_score=0.2,
            extension_atr_multiple=1.1, momentum_state="constructive", measurements={"x": 1.0},
        )
        backtest = {"validated": bool(len(history) % 2), "credibility_score": 0.4, "recent_consistency_score": 0.3}
        new_outcome = filters_module.ScreenerFilterPipeline(effective).evaluate(
            signal=signal, context=new_context, backtest_snapshot=backtest, intelligence=intelligence
        )
        old_outcome = ref.filters.ScreenerFilterPipeline(effective).evaluate(
            signal=signal, context=old_context, backtest_snapshot=backtest, intelligence=intelligence
        )
        assert _dump(new_outcome) == _dump(old_outcome)
        shorts_off += signal.metadata["signal_role"] == "entry_short"
    assert len(cases) == 36 * 5 and shorts_off == 36


class _Logs:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def log(self, event_type: str, payload: dict) -> None:
        self.items.append((event_type, json.loads(json.dumps(payload, default=str))))


class _Snapshot:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata

    def model_copy(self, *, update: dict) -> dict:
        return {"metadata": self.metadata, **update}


class _PromotionService:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.effective_settings = effective_screener_settings(settings)
        self.logs = _Logs()

    @staticmethod
    def _compute_risk_reward(signal):
        return compute_risk_reward(signal)

    @staticmethod
    def _snapshot_from_signal(signal, **_kwargs):
        return _Snapshot({"symbol": signal.symbol, "execution_blockers": ["final_score_below_auto_threshold"]})


_SOFT = list(DEFAULT_PAPER_NEAR_MISS_ALLOWED_REASONS)


def _near_miss_case(rng: np.random.Generator, *, role: str | None, action: SignalAction):
    entry = float(rng.uniform(20.0, 400.0))
    atr = entry * float(rng.uniform(0.005, 0.03))
    shape = str(rng.choice(["long", "short", "missing", "flat"]))
    stop, target = {
        "long": (entry - 2 * atr, entry + float(rng.uniform(1.0, 5.0)) * atr),
        "short": (entry + 2 * atr, entry - float(rng.uniform(1.0, 5.0)) * atr),
        "missing": (None, entry + atr),
        "flat": (entry, entry),
    }[shape]
    metadata = {} if role is None else {"signal_role": role}
    if rng.uniform() < 0.5:
        metadata["risk_reward_ratio"] = round(float(rng.uniform(0.5, 3.0)), 2)
    signal = SimpleNamespace(symbol="SYNA", strategy_name="trend_following", action=action, price=entry,
                             stop_loss=stop, take_profit=target, metadata=metadata)
    context = SimpleNamespace(
        current_price=entry,
        spread_bps=None if rng.uniform() < 0.1 else float(rng.uniform(0.0, 80.0)),
        relative_volume=float(rng.uniform(0.3, 2.0)),
    )
    reasons = list(rng.choice(_SOFT + ["spread_too_wide", "price_out_of_range"], size=int(rng.integers(0, 4)), replace=False))
    kwargs = dict(
        signal=signal,
        context=context,
        market_data_status={"verified": bool(rng.uniform() < 0.9), "verification_reason": "stale_quote"},
        filter_outcome=filters_module.FilterOutcome(passed=False, pass_reasons=[], rejection_reasons=reasons,
                                                    reason_codes=reasons, watchlist_only=bool(rng.uniform() < 0.2)),
        ranking={"final_score": float(rng.uniform(40.0, 75.0)), "actionability": "watch"},
        reasons=reasons,
    )
    return kwargs


def _promote(module, service, kwargs):
    return module._maybe_promote_paper_near_miss(
        service, quote=None, timeframe="1d", intelligence=None, backtest_snapshot={}, freshness="fresh", **kwargs
    )


def test_near_miss_promotion_is_byte_identical_for_longs_and_for_shorts_switched_off(ref, settings, tmp_path) -> None:
    disabled = make_settings(tmp_path / "disabled", **{**SWITCH, "paper_near_miss_promotion_enabled": False})
    promoted = blocked = 0
    for seed in range(400):
        rng = np.random.default_rng(seed)
        role = [None, "entry_long", "entry_short", "entry_short_watch", "exit"][seed % 5]
        action = SignalAction.SELL if (role or "").startswith("entry_short") or role == "exit" else SignalAction.BUY
        if seed % 7 == 0:
            action = SignalAction.SELL if action is SignalAction.BUY else SignalAction.BUY  # odd pairs too
        cfg = [settings.on, settings.off, disabled][seed % 3]
        kwargs = _near_miss_case(rng, role=role, action=action)
        if mirrors_short(cfg, kwargs["signal"]) and action is SignalAction.SELL:
            continue  # the one case that changes: a short with the switch on (mirrored below)
        new_service, old_service = _PromotionService(cfg), _PromotionService(cfg)
        new_blockers = promotion_module._paper_near_miss_blockers(new_service, **kwargs)
        assert new_blockers == ref.scan_promotion._paper_near_miss_blockers(old_service, **kwargs)
        new = _promote(promotion_module, new_service, kwargs)
        old = _promote(ref.scan_promotion, old_service, kwargs)
        assert _dump(new) == _dump(old)
        assert new_service.logs.items == old_service.logs.items
        promoted += new is not None
        blocked += new is None
    assert promoted >= 5 and blocked >= 100


def test_backtest_name_passes_a_long_through(settings) -> None:
    long = SimpleNamespace(metadata={"signal_role": "entry_long"}, strategy_name="trend_following")
    short = SimpleNamespace(metadata={"signal_role": "entry_short"}, strategy_name="trend_following")
    watch = SimpleNamespace(metadata={"signal_role": "entry_short_watch"}, strategy_name="trend_following")
    for cfg in (settings.on, settings.off, None):
        assert backtest_strategy_name(cfg, long, "trend_following") == "trend_following"
        assert backtest_strategy_name(cfg, watch, "trend_following") == "trend_following"
    assert backtest_strategy_name(settings.off, short, "trend_following") == "trend_following"
    assert backtest_strategy_name(settings.on, short, "trend_following") == "trend_following:short"
    service = SimpleNamespace(settings=settings.on)
    assert MarketScreenerService._backtest_name(service, long) == "trend_following"
    assert MarketScreenerService._backtest_name(service, short) == "trend_following:short"


def test_the_short_rows_are_the_short_research_rows() -> None:
    from app.backtesting.short_research import SHORT_NAME_SUFFIX

    assert SHORT_BACKTEST_NAME_SUFFIX == SHORT_NAME_SUFFIX


# --------------------------------------------------------------------------------------------
# The mirror is exact (switch on)
# --------------------------------------------------------------------------------------------
def test_the_rsi_bands_are_the_long_bands_mirrored() -> None:
    assert (100.0 - LONG_CONSTRUCTIVE_RSI[1], 100.0 - LONG_CONSTRUCTIVE_RSI[0]) == SHORT_CONSTRUCTIVE_RSI
    assert SHORT_EXHAUSTED_RSI == 100.0 - LONG_EXHAUSTED_RSI


def _mirror_pairs(seeds: range):
    for seed in seeds:
        rng = np.random.default_rng(5000 + seed)
        original = _bars(seed, bars=int(rng.integers(100, 220)), drift=float(rng.normal(0.0, 0.006)))
        yield seed, original, _reflect(original)


def test_confluence_score_mirrors_exactly(settings) -> None:
    differs_without = 0
    for _, original, mirrored in _mirror_pairs(range(30)):
        long_frame = indicators_module.enrich_technical_indicators(original, timeframe="1d")
        short_frame = indicators_module.enrich_technical_indicators(mirrored, timeframe="1d")
        for index in range(len(long_frame) - 60, len(long_frame)):
            long_row, short_row = long_frame.iloc[index], short_frame.iloc[index]
            assert abs((100.0 - short_row["rsi_14"]) - long_row["rsi_14"]) < 1e-6
            long_score = indicators_module.compute_confluence_score(long_row)
            if 68.0 - 1e-6 < float(long_row["rsi_14"]) < 68.0 + 1e-6:
                continue  # float noise at the band edge
            assert indicators_module.compute_confluence_score(short_row, is_short=True, mirror_short=True) == long_score
            differs_without += indicators_module.compute_confluence_score(short_row, is_short=True) != long_score
    assert differs_without > 0  # the old 35-48 band was stricter than the mirror


def _accuracy(history, *, role: str, cfg) -> object:
    price = float(history["close"].iloc[-1])
    signal = SimpleNamespace(price=price, strategy_name=STRATEGY, metadata={"timeframe": "1d", "signal_role": role, "style": "trend"})
    quote = SimpleNamespace(bid=price, ask=price, last_execution=price)
    context = filters_module.build_market_context(history, quote=quote, signal=signal, settings=cfg)
    profile = accuracy_module.build_accuracy_profile(
        history, signal=signal, context=context, settings=effective_screener_settings(cfg)
    )
    return context, profile


def test_accuracy_profile_and_market_context_mirror_exactly(settings) -> None:
    compared = differs_without = 0
    for seed, original, mirrored in _mirror_pairs(range(60)):
        long_context, long_profile = _accuracy(original, role="entry_long", cfg=settings.on)
        short_context, short_profile = _accuracy(mirrored, role="entry_short", cfg=settings.on)
        assert short_profile.pass_reasons == long_profile.pass_reasons, seed
        assert short_profile.rejection_reasons == long_profile.rejection_reasons, seed
        for field in ("overall_score", "entry_location_score", "support_resistance_score",
                      "confirmation_score", "false_positive_risk_score"):
            assert getattr(short_profile, field) == pytest.approx(getattr(long_profile, field), abs=2e-4), (seed, field)
        for key, value in long_profile.measurements.items():
            assert short_profile.measurements[key] == (value if isinstance(value, str) else pytest.approx(value, abs=2e-4))
        # the context's direction-bearing scores mirror exactly; efficiency and volume are
        # direction-free (percent measures such as momentum keep only their sign: they divide by
        # a different price)
        for key in ("indicator_confluence_score", "regime_alignment_score", "efficiency_ratio", "relative_volume"):
            assert short_context.measurements[key] == pytest.approx(long_context.measurements[key], abs=2e-4), (seed, key)
        assert (short_context.momentum_pct > 0) == (long_context.momentum_pct > 0)
        compared += 1
        _, old_short = _accuracy(mirrored, role="entry_short", cfg=settings.off)
        differs_without += _dump(old_short) != _dump(short_profile)
    assert compared == 60
    assert differs_without > 0  # without the mirror a short was judged on stricter RSI bands


def test_intelligence_scores_mirror_exactly(tmp_path) -> None:
    service = MarketIntelligenceService(make_settings(tmp_path), market_data_engine=None)
    for seed, original, mirrored in _mirror_pairs(range(30)):
        assert service._trend_score(mirrored, is_short=True) == service._trend_score(original, is_short=False), seed
        _, _, long_extension = service._momentum_state(original, is_short=False)
        _, _, short_extension = service._momentum_state(mirrored, is_short=True)
        assert short_extension == pytest.approx(long_extension, abs=1e-6), seed
        other = _bars(seed + 99)
        # relative strength is a signed pct: a short reads the long's value negated, on the same bars
        assert service._relative_strength(original, other, is_short=True) == -service._relative_strength(
            original, other, is_short=False
        )
        # breadth: a short's breadth is the long's breadth with the two legs' roles swapped
        assert service._breadth_score(benchmark_daily=original, breadth_daily=other, is_short=True) == (
            service._breadth_score(benchmark_daily=other, breadth_daily=original, is_short=False)
        )
        long_close, short_close = float(original["close"].iloc[-1]), float(mirrored["close"].iloc[-1])
        for style in ("trend", "mean_reversion"):
            ema_fast, ema_slow = float(original["close"].ewm(span=20).mean().iloc[-1]), float(original["close"].ewm(span=50).mean().iloc[-1])
            k2 = short_close + long_close
            assert filters_module._regime_alignment_score(
                style=style, is_short=True, close=short_close, ema_fast=k2 - ema_fast, ema_slow=k2 - ema_slow, momentum_pct=-1.0
            ) == filters_module._regime_alignment_score(
                style=style, is_short=False, close=long_close, ema_fast=ema_fast, ema_slow=ema_slow, momentum_pct=1.0
            )


def test_near_miss_judges_a_short_as_its_mirrored_long(settings) -> None:
    compared = promoted = 0
    for seed in range(300):
        rng = np.random.default_rng(20_000 + seed)
        kwargs = _near_miss_case(rng, role="entry_short", action=SignalAction.SELL)
        signal = kwargs["signal"]
        entry = float(signal.price)
        mirror = SimpleNamespace(
            **{**vars(signal), "action": SignalAction.BUY, "metadata": {**signal.metadata, "signal_role": "entry_long"},
               "stop_loss": None if signal.stop_loss is None else 2 * entry - signal.stop_loss,
               "take_profit": None if signal.take_profit is None else 2 * entry - signal.take_profit}
        )
        service = _PromotionService(settings.on)
        short_blockers = promotion_module._paper_near_miss_blockers(service, **kwargs)
        long_blockers = promotion_module._paper_near_miss_blockers(service, **{**kwargs, "signal": mirror})
        assert short_blockers == long_blockers, seed
        compared += 1
        promoted += not short_blockers
        off_blockers = promotion_module._paper_near_miss_blockers(_PromotionService(settings.off), **kwargs)
        assert "paper_near_miss_short_blocked" in off_blockers and "paper_near_miss_long_only" in off_blockers
    assert compared == 300 and promoted >= 3


# --------------------------------------------------------------------------------------------
# End to end through scan_universe
# --------------------------------------------------------------------------------------------
SYMBOL = "JPM"  # sector ETF XLF, benchmark SPY
STRATEGY = "trend_following"
BACKTEST = {
    "completed_at": "2026-04-10T12:00:00Z",
    "out_of_sample": True,
    "metrics": {"number_of_trades": 40, "profit_factor": 1.8, "win_rate": 58.0, "max_drawdown_pct": 10.0,
                "annualized_return_pct": 12.0, "out_of_sample": True},
    "trades": [{"pnl_pct": 1.0}, {"pnl_pct": -0.4}, {"pnl_pct": 0.8}, {"pnl_pct": 0.6}],
}
# Runtime defaults for the near-miss path (.env.example widens the score gap to 10: see
# test_near_miss_is_equally_loose_for_both_sides).
SCAN = dict(market_universe_symbols=[SYMBOL], screener_default_timeframes=["1d"], paper_near_miss_max_score_gap=5.0)


def _wave(base: float, drift: float, amplitude: float, bars: int, period: float = 3.0) -> list[float]:
    return [base + drift * i + amplitude * math.sin(i / period) for i in range(bars)]


def _scenario_frame(closes: list[float], *, step: str = "1D", last_volume: float = 1.4) -> pd.DataFrame:
    timestamps = pd.date_range(start="2026-01-01T00:00:00Z", periods=len(closes), freq=step, tz="UTC")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [c - 0.3 for c in closes],
        "high": [c + 0.8 for c in closes],
        "low": [c - 0.8 for c in closes],
        "close": closes,
        "volume": [2_000_000.0] * len(closes),
    })
    frame.loc[frame.index[-1], "volume"] = 2_000_000.0 * last_volume
    return frame


def _strong_world(*, last_volume: float = 1.4, bars: int = 180) -> dict:
    """A strong stock leading a rising sector in a rising market (small caps leading)."""

    return {
        (SYMBOL, "1d"): _scenario_frame(_wave(100, 0.35, 1.6, bars), last_volume=last_volume),
        (SYMBOL, "1w"): _scenario_frame(_wave(60, 0.9, 1.0, bars), step="7D"),
        (SYMBOL, "1h"): _scenario_frame(_wave(150, 0.05, 0.4, bars), step="1h"),
        ("SPY", "1d"): _scenario_frame(_wave(400, 0.5, 2.0, bars, 4.0)),
        ("QQQ", "1d"): _scenario_frame(_wave(300, 0.45, 2.0, bars, 4.0)),
        ("IWM", "1d"): _scenario_frame(_wave(150, 0.6, 0.8, bars, 4.0)),
        ("XLF", "1d"): _scenario_frame(_wave(40, 0.04, 0.2, bars, 4.0)),
    }


def _weak_world(**kwargs) -> dict:
    """The strong world reflected: a weak stock leading a falling sector in a falling market."""

    return {key: _reflect(frame, 2.0 * float(frame["close"].max())) for key, frame in _strong_world(**kwargs).items()}


class _Backtests:
    """Per-symbol backtest rows by strategy name (the short research writes ``<name>:short``)."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.names: list[str] = []

    def get_latest_summary(self, symbol, strategy_name=None, timeframe=None):
        self.names.append(strategy_name)
        row = self.rows.get(strategy_name)
        return None if row is None else {**row, "strategy_name": strategy_name}


def _scan(tmp_path, world: dict, *, side: str, backtests: dict | None = None, **overrides):
    frame = world[(SYMBOL, "1d")]
    last = float(frame["close"].iloc[-1])
    quote = MarketQuote(symbol=SYMBOL, bid=last - 0.01, ask=last + 0.01, last_execution=last, timestamp="2026-04-11T10:00:00Z")
    rows = backtests if backtests is not None else {STRATEGY: BACKTEST, f"{STRATEGY}:short": BACKTEST}
    service = MarketScreenerService(
        settings=make_settings(tmp_path, **{**SWITCH, **SCAN, **overrides}),
        market_data_engine=FakeMarketDataEngine(world, {SYMBOL: quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=_Backtests(rows),
        scan_decision_repository=FakeScanDecisionRepository(),
        telegram_notifier=FakeTelegramNotifier(),
    )
    atr = 1.8
    if side == "long":
        action, role, stop, target = SignalAction.BUY, "entry_long", last - 2 * atr, last + 4 * atr
    else:
        action, role, stop, target = SignalAction.SELL, "entry_short", last + 2 * atr, last - 4 * atr

    class Strategy:
        def generate_signal(self, _history, symbol):
            return Signal(
                symbol=symbol, strategy_name=STRATEGY, action=action, confidence=0.5, price=last,
                stop_loss=stop, take_profit=target, rationale="mirror fixture",
                metadata={"style": "trend", "signal_role": role, "risk_reward_ratio": 2.0},
            )

    spec = SimpleNamespace(name=STRATEGY, timeframe="1d", style="trend", default_kwargs={})
    service._strategy_specs_for_timeframe = lambda *_args, **_kwargs: [spec]
    service._build_strategy = lambda _spec: Strategy()
    response = service.scan_universe(limit=3, scan_task="swing_scan", strategy_spec_keys=[f"{STRATEGY}:1d"])
    decisions = service.scan_decisions.items
    assert len(decisions) == 1
    attempts = [payload for event, payload in service.logs.items if event == "paper_near_miss_promotion_attempt"]
    return SimpleNamespace(response=response, decision=decisions[0], service=service, attempts=attempts)


DIRECTION_GATES = {
    "market_regime_fit_ok", "timeframe_alignment_ok", "relative_strength_market_ok", "relative_strength_sector_ok",
    "sector_strength_ok", "benchmark_strength_ok", "regime_alignment_ok", "indicator_confluence_ok",
    "confirmation_ok", "accuracy_ok", "entry_not_extended", "backtest_validated",
}


def test_weak_stock_in_a_falling_market_gives_an_execution_ready_short(tmp_path) -> None:
    run = _scan(tmp_path, _weak_world(), side="short")
    assert run.service.backtests.names == [f"{STRATEGY}:short"]  # the short's own rows
    [candidate] = run.response.candidates
    assert candidate.execution_ready is True and candidate.tradable is True
    assert candidate.metadata["alert_eligible"] is True
    assert candidate.metadata["signal_classification"] == "execution_ready"  # every gate passed, no promotion
    assert candidate.direction_label == "sell"
    assert candidate.metadata["signal_role"] == "entry_short"
    assert run.decision.status == "candidate" and run.decision.alert_eligible is True
    assert run.decision.rejection_reasons == []
    assert set(run.decision.reason_codes) >= DIRECTION_GATES
    assert candidate.metadata["market_regime_label"] == "risk-off trend"


def test_the_same_weak_stock_as_a_long_is_rejected(tmp_path) -> None:
    run = _scan(tmp_path, _weak_world(), side="long")
    assert run.service.backtests.names == [STRATEGY]
    assert run.response.candidates == []
    assert run.decision.status == "rejected" and run.decision.alert_eligible is False
    assert {"market_regime_fit_too_low", "sector_strength_too_low", "benchmark_strength_too_low",
            "relative_strength_market_too_low", "timeframe_alignment_too_low"} <= set(run.decision.rejection_reasons)
    [attempt] = run.attempts
    assert attempt["promotion_blockers"] == ["paper_near_miss_score_gap_too_large"]


def test_a_strong_stock_short_is_rejected_exactly_as_the_weak_stock_long(tmp_path) -> None:
    short = _scan(tmp_path / "short", _strong_world(), side="short")
    long = _scan(tmp_path / "long", _weak_world(), side="long")
    assert short.service.backtests.names == [f"{STRATEGY}:short"]
    assert short.response.candidates == []
    assert short.decision.status == "rejected" and short.decision.alert_eligible is False
    assert short.decision.rejection_reasons == long.decision.rejection_reasons
    assert [a["promotion_blockers"] for a in short.attempts] == [a["promotion_blockers"] for a in long.attempts]
    # ...and the weak stock's short is what the strong stock's long is
    strong_long = _scan(tmp_path / "strong_long", _strong_world(), side="long")
    weak_short = _scan(tmp_path / "weak_short", _weak_world(), side="short")
    assert strong_long.decision.status == weak_short.decision.status == "candidate"
    assert strong_long.decision.reason_codes == weak_short.decision.reason_codes
    assert weak_short.decision.final_score == pytest.approx(strong_long.decision.final_score, abs=5.0)


def test_a_short_reads_only_its_own_backtest_rows(tmp_path) -> None:
    # The long strategy's rows say nothing about its short; with none of its own, a short is
    # treated as a long with no backtest (the runtime default watchlists it: not execution-ready).
    run = _scan(tmp_path, _weak_world(), side="short", backtests={STRATEGY: BACKTEST})
    assert run.service.backtests.names == [f"{STRATEGY}:short"]
    assert "backtest_unvalidated" in run.decision.reason_codes
    assert "backtest_validated" not in run.decision.reason_codes
    mirror = _scan(tmp_path / "long", _strong_world(), side="long", backtests={f"{STRATEGY}:short": BACKTEST})
    assert mirror.service.backtests.names == [STRATEGY]
    assert run.decision.status == mirror.decision.status
    assert [c.execution_ready for c in run.response.candidates] == [c.execution_ready for c in mirror.response.candidates]


def test_a_short_near_miss_is_promoted_on_the_long_rules(tmp_path) -> None:
    # Volume below the 0.80 exploration floor but above the 0.75 near-miss floor: a soft miss.
    loose = dict(paper_exploration_min_relative_volume=0.80, paper_exploration_near_miss_min_relative_volume=0.75)
    short = _scan(tmp_path / "short", _weak_world(last_volume=0.78), side="short", **loose)
    long = _scan(tmp_path / "long", _strong_world(last_volume=0.78), side="long", **loose)
    assert short.decision.rejection_reasons == long.decision.rejection_reasons == ["relative_volume_too_low"]
    [candidate] = short.response.candidates
    assert candidate.execution_ready is True and candidate.metadata["alert_eligible"] is True
    assert candidate.metadata["signal_classification"] == "paper_near_miss"
    assert candidate.direction_label == "sell"
    assert [c.metadata["signal_classification"] for c in long.response.candidates] == ["paper_near_miss"]
    assert short.attempts[-1]["promotion_blockers"] == long.attempts[-1]["promotion_blockers"] == []

    # Switched off, a short is never promoted (and reads the long strategy's rows, as before).
    off = _scan(tmp_path / "off", _weak_world(last_volume=0.78), side="short", **loose, paper_short_entries_enabled=False)
    assert off.service.backtests.names == [STRATEGY]
    assert off.response.candidates == []
    assert {"paper_near_miss_short_blocked", "paper_near_miss_long_only"} <= set(off.attempts[-1]["promotion_blockers"])


def test_near_miss_is_equally_loose_for_both_sides(tmp_path) -> None:
    """With .env.example's score gap of 10, the paper near-miss path treats every market
    direction gate as soft: a long in a falling market scoring 50 or more is promoted today, and
    so is its mirror, a short in a rising market. Same strictness on both sides, as asked; an
    operator who wants counter-trend entries refused narrows paper_near_miss_allowed_reasons or
    the gap for both."""

    weak_long = _scan(tmp_path / "long", _weak_world(), side="long", paper_near_miss_max_score_gap=10.0)
    strong_short = _scan(tmp_path / "short", _strong_world(), side="short", paper_near_miss_max_score_gap=10.0)
    assert [c.metadata["signal_classification"] for c in weak_long.response.candidates] == ["paper_near_miss"]
    assert [c.metadata["signal_classification"] for c in strong_short.response.candidates] == ["paper_near_miss"]
    assert strong_short.decision.rejection_reasons == weak_long.decision.rejection_reasons
