"""Long backtests stay byte-identical to the pre-short code, checked against it in the same run.

Short research (``EngineConfig.short_entries``, the short cost legs, the
``:walk_forward_oos_short`` rows, the wiped-out-account guards) must not move one long
number: the live evidence gate, the screener's backtest validation and the eToro copy bar
all read the long rows. So the same seeded frames go through ``BatchBacktestService.run``
twice in this process: once with today's code (short flag off) and once with the bc82efd
sources of every module the short work edits on that path, kept verbatim in
``tests/long_parity_reference/`` (engine, batch, cost_model, walk_forward) and importable
under their own ``app.backtesting`` names for that run only. What
``BacktestRepository.create`` would write (``json.dumps`` without ``sort_keys``, so key
order counts), minus the row id and the wall-clock stamps, every run log and the summary
``run`` returns must match exactly.

Today's batch adds eToro's own trade fees (``with_etoro_trade_fees``: $1 a leg on a US stock),
which bc82efd never charged, so the parity run sets them to 0. A third run with the real fees
must differ from it by the fee alone: $1 off each leg's P&L, and the slightly smaller sizes
that the cash those fees took buys for a fold's later trades (sizing is 1% of current cash).

Both runs share this machine's numpy build, its CPU dispatch and this process's hash seed,
so an exact comparison holds on any CPU. (A golden digest of float reprs did not: numpy's
AVX-512 and AVX2 ``exp`` differ in the last bit, which moves the seeded frames.)

Cases: every daily strategy walk-forward and in-sample on seed 7 (in-sample also covers the
engine's own persistence), the SELL-emitting daily strategies walk-forward on seed 11 with
every cost leg non-zero (the Alpaca preset, which also sets the short-only borrow fee, plus
commission, FX and weekend financing), and two 15m strategies with the live session flatten
and the hold_overnight / stop_floor variants.

The 15m case runs with the 2026-10-20 options off (``walk_forward_intraday_entry_delay_bars``
0), and its rows' additive ``cost_profiles`` (the Alpaca / stress re-runs, which bc82efd never
wrote) are left out of the comparison: everything else, the replayed variants included, must
still match bc82efd exactly.

Re-baseline only after a deliberate long change, from a known-good commit: copy its
``app/backtesting/<name>.py`` over ``tests/long_parity_reference/<name>.py.txt``, update
``REFERENCE_COMMIT`` and ``REFERENCE_SHA256``, and say so in the commit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import json
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.backtesting import batch as batch_module
from app.backtesting import cost_model as cost_module
from app.backtesting import engine as engine_module
from app.backtesting import walk_forward as walk_forward_module
from app.backtesting.venue_costs import COST_PROFILES_FIELD
from tests.conftest import make_settings

SYMBOL = "SYNA"
DAILY_BARS = 400
# Every daily strategy that emits a SELL (entry_short, entry_short_watch or an exit): in long
# mode each of those SELLs must still close a long and never open anything.
SELL_EMITTERS = (
    "trend_following", "momentum_breakout", "mean_reversion", "ema_trend_stack",
    "rsi_trend_continuation", "rsi_vwap_ema_confluence", "ma_crossover", "pullback_trend",
)
INTRADAY_SPECS = ("vwap_reclaim", "ema_trend_stack")
COST_LEGS = ("spread_usd", "financing_usd", "fx_usd", "commission_usd", "slippage_usd", "regulatory_usd")

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = Path(__file__).with_name("long_parity_reference")
REFERENCE_COMMIT = "bc82efd"
# sha256 of each reference file (LF line endings), in load order: a module's own
# app.backtesting imports must already resolve to the reference when it is executed.
REFERENCE_SHA256 = {
    "cost_model": "c015ddbf0b2a1b20506d995ac9f2d5ed5c44d0d4fffa36cf7868fbe29818b274",
    "walk_forward": "89029f05744e939254f1dd9ac36e1fa4c7879566d31e1c2c72b8bf370a0b15e0",
    "engine": "1494b8ce7b92f162e530dbdb966d37c5e18667431ddd66523b1eb179f3875ee5",
    "batch": "c2f1c5a1d881eb1b4e0dc84d28b62b16ffe7a91763e200175850b184290a458a",
}
CURRENT = {"cost_model": cost_module, "walk_forward": walk_forward_module, "engine": engine_module, "batch": batch_module}


def _daily_frame(seed: int, n: int = DAILY_BARS) -> pd.DataFrame:
    """Down / up / down drift regimes, so long setups, stops and targets all occur."""

    rng = np.random.RandomState(seed)
    rets = rng.normal(-0.0008, 0.018, n)
    rets[n // 3 : 2 * n // 3] += 0.0024
    close = 100 * np.exp(np.cumsum(rets))
    openp = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    stamps = pd.bdate_range("2024-01-02", periods=n, tz="UTC") + pd.Timedelta(hours=4)
    volume = rng.randint(2_000_000, 9_000_000, n).astype(float)
    return pd.DataFrame(
        {"timestamp": stamps, "open": openp, "high": high, "low": low, "close": close, "volume": volume}
    )


def _intraday_frame(seed: int, sessions: int = 60) -> pd.DataFrame:
    """Regular-hours 15m bars (26 per New York session) with overnight gaps."""

    rng = np.random.RandomState(seed)
    days = [
        pd.date_range(f"{day.date()} 09:30", periods=26, freq="15min", tz="America/New_York").tz_convert("UTC")
        for day in pd.bdate_range("2025-03-03", periods=sessions)
    ]
    stamps = days[0].append(days[1:])
    n = len(stamps)
    rets = rng.normal(0.0, 0.0035, n)
    rets[::26] += rng.normal(0.0, 0.012, sessions)  # the overnight gap lands on the first bar
    rets[n // 3 : 2 * n // 3] += 0.0004
    close = 50 * np.exp(np.cumsum(rets))
    openp = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.0008, n))
    high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.0015, n)))
    low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.0015, n)))
    volume = rng.randint(20_000, 400_000, n).astype(float)
    return pd.DataFrame(
        {"timestamp": stamps, "open": openp, "high": high, "low": low, "close": close, "volume": volume}
    )


class _Bars:
    """Market data double: the seeded frame for SYNA, a correlated hedge leg for anything else."""

    def __init__(self, frame: pd.DataFrame, seed: int):
        self.frame = frame
        rng = np.random.RandomState(seed + 1000)
        hedge = frame.copy()
        noise = 1 + rng.normal(0, 0.004, len(frame))
        for column in ("open", "high", "low", "close"):
            hedge[column] = frame[column] * noise
        self.hedge = hedge

    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False, backtest=False):
        return (self.frame if symbol.upper() == SYMBOL else self.hedge).copy()


class _Rows:
    def __init__(self):
        self.rows: list[dict] = []

    def create(self, **kwargs):
        self.rows.append(kwargs)


class _Logs:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event, payload):
        self.events.append((event, payload))


def _every_cost_leg(cost_model_module):
    return dataclasses.replace(
        cost_model_module.CostModel.alpaca_equities(),
        overnight_fee_daily_pct=0.0002, weekend_multiplier=3.0, include_weekend_financing=True,
        fx_spread_bps=5.0, commission_per_share=0.005,
    )


def _cases(tmp_dir):
    daily = make_settings(tmp_dir, walk_forward_intraday_variants=[])
    intraday = make_settings(
        tmp_dir,
        walk_forward_intraday_variants=["hold_overnight", "stop_floor_1.0"],
        walk_forward_intraday_entry_delay_bars=0,
    )
    # (case, settings, frame, seed, timeframe, walk_forward, strategies, every cost leg)
    return [
        ("daily7_wf", daily, _daily_frame(7), 7, "1d", True, None, False),
        ("daily11_wf_costs", daily, _daily_frame(11), 11, "1d", True, list(SELL_EMITTERS), True),
        ("daily7_insample", daily, _daily_frame(7), 7, "1d", False, None, False),
        ("intraday15m_wf", intraday, _intraday_frame(5), 5, "15m", True, list(INTRADAY_SPECS), False),
    ]


@contextlib.contextmanager
def _patched(module, name, value):
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def _record(case, summary, rows, events) -> list[tuple[str, str]]:
    """What a run leaves behind, as (label, exact text) pairs."""

    def etoro_priced(metrics: dict) -> dict:  # key order kept
        return {key: value for key, value in metrics.items() if key != COST_PROFILES_FIELD}

    out = []
    for row in rows:  # exactly what BacktestRepository.create persists, minus id and stamps
        label = f"{case} row {row['strategy_name']} {row['file_path']}"
        out.append((f"{label} keys", json.dumps(sorted(row))))
        out.append((label, json.dumps([row["symbol"], row["strategy_name"], row["file_path"]])))
        out.append((f"{label} metrics", json.dumps(etoro_priced(row["metrics"]))))
        out.append((f"{label} trades", json.dumps(row["trades"])))
    out += [(f"{case} log {event}", json.dumps(payload)) for event, payload in events]
    dumped = summary.model_dump(exclude={"generated_at"})
    dumped["results"] = [etoro_priced(result) for result in dumped["results"]]
    out.append((f"{case} summary", json.dumps(dumped)))
    return out


def _sweep(modules, tmp_dir, persisted: list | None = None) -> tuple[list[tuple[str, str]], Counter]:
    """Every case through ``modules['batch'].BatchBacktestService.run`` (the long path).

    ``persisted`` collects each case's raw rows as ``(case, row)``."""

    batch, engine = modules["batch"], modules["engine"]
    record: list[tuple[str, str]] = []
    coverage: Counter = Counter()
    traded: set[str] = set()
    original_floor = engine.floored_stop_and_target

    def counting_floor(**kwargs):
        stop, target = original_floor(**kwargs)
        coverage["floor_calls"] += 1
        coverage["floor_widened"] += int(stop != kwargs["stop"])
        return stop, target

    with _patched(engine, "floored_stop_and_target", counting_floor):
        for case, settings, frame, seed, timeframe, walk_forward, requested, all_costs in _cases(tmp_dir):
            config = engine.EngineConfig
            if all_costs:  # the batch builds its EngineConfig itself; give it every cost leg
                config = functools.partial(engine.EngineConfig, cost_model=_every_cost_leg(modules["cost_model"]))
            rows, logs = _Rows(), _Logs()
            service = batch.BatchBacktestService(
                settings=settings, market_data_engine=_Bars(frame, seed), backtest_repository=rows, run_log_repository=logs
            )
            with _patched(batch, "EngineConfig", config):
                summary = service.run(
                    symbols=[SYMBOL], timeframes=[timeframe], strategy_names=requested, walk_forward=walk_forward
                )
            record += _record(case, summary, rows.rows, logs.events)
            if persisted is not None:
                persisted += [(case, row) for row in rows.rows]
            for row in rows.rows:
                for trade in row["trades"]:
                    coverage[f"reason:{trade['reason']}"] += 1
                    coverage.update(f"{case}:{leg}" for leg in COST_LEGS if trade[leg] > 0)
                if row["trades"] and timeframe == "1d":
                    traded.add(row["strategy_name"])
    coverage["daily_traded"] = len(traded)
    return record, coverage


@contextlib.contextmanager
def bc82efd_backtesting(report: dict):
    """The bc82efd engine, batch, cost_model and walk_forward, importable as ``app.backtesting.*``
    inside the block (so their imports, and the engine's function-local one, resolve to each
    other). Every ``sys.modules`` entry is restored on the way out. An app module imported for
    the first time inside the block would have bound to the reference: it is dropped too, and
    named in ``report['late_imports']``."""

    before = dict(sys.modules)
    modules: dict[str, types.ModuleType] = {}
    try:
        for stem in REFERENCE_SHA256:
            path = REFERENCE_DIR / f"{stem}.py.txt"
            module = types.ModuleType(f"long_parity_reference.{stem}")
            module.__file__ = str(path)
            sys.modules[module.__name__] = module  # dataclasses and pydantic resolve through it
            sys.modules[f"app.backtesting.{stem}"] = module
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
            modules[stem] = module
        yield modules
    finally:
        added = set(sys.modules) - set(before)
        for name in added:
            del sys.modules[name]
        sys.modules.update(before)
        report["late_imports"] = sorted(name for name in added if name.startswith("app."))


@pytest.fixture(scope="module")
def computed(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("parity")
    # First (every lazy import binds to today's code), with eToro's fees at 0 like bc82efd.
    fee_free_rows: list = []
    with _patched(cost_module, "ETORO_STOCK_FEE_USD_PER_LEG", 0.0), _patched(
        cost_module, "ETORO_CRYPTO_FEE_PER_LEG", 0.0
    ):
        current, coverage = _sweep(CURRENT, tmp_dir, fee_free_rows)
    fee_rows: list = []
    with_fees, _ = _sweep(CURRENT, tmp_dir, fee_rows)  # today's real default
    report: dict = {}
    with bc82efd_backtesting(report) as reference:
        legacy, legacy_coverage = _sweep(reference, tmp_dir)
        wiring = {
            "batch_engine": reference["batch"].BacktestEngine is reference["engine"].BacktestEngine,
            "engine_costs": reference["engine"].CostModel is reference["cost_model"].CostModel,
            "batch_aggregate": reference["batch"].aggregate_out_of_sample
            is reference["walk_forward"].aggregate_out_of_sample,
            "pre_short_engine": "short_entries" not in {f.name for f in dataclasses.fields(reference["engine"].EngineConfig)},
            "pre_short_batch": not hasattr(reference["batch"].BatchBacktestService, "run_short_research"),
            "pre_short_costs": not hasattr(reference["cost_model"].CostModel, "short_holding_cost_usd"),
        }
    restored = all(sys.modules[f"app.backtesting.{stem}"] is module for stem, module in CURRENT.items())
    return {
        "current": current, "legacy": legacy, "coverage": coverage, "legacy_coverage": legacy_coverage,
        "wiring": wiring, "restored": restored, "with_fees": with_fees, "fee_free_rows": fee_free_rows,
        "fee_rows": fee_rows, **report,
    }


def test_long_backtests_match_the_pre_short_code_exactly(computed) -> None:
    current, legacy = computed["current"], computed["legacy"]
    labels, legacy_labels = [label for label, _ in current], [label for label, _ in legacy]
    assert labels == legacy_labels, sorted(set(labels) ^ set(legacy_labels))[:8] or "same records, other order"
    moved = [
        f"{label}\n  now: {now[:400]}\n  bc82efd: {then[:400]}"
        for (label, now), (_, then) in zip(current, legacy, strict=True)
        if now != then
    ]
    assert not moved, f"{len(moved)} of {len(current)} records moved:\n" + "\n".join(moved[:8])
    # The stop floor ran the same way too (the floor is long-only and its count is not persisted).
    floors = ("floor_calls", "floor_widened")
    assert [computed["coverage"][k] for k in floors] == [computed["legacy_coverage"][k] for k in floors]


def test_the_reference_is_the_pre_short_code_and_leaves_no_trace(computed) -> None:
    assert computed["wiring"] == dict.fromkeys(computed["wiring"], True)
    assert computed["restored"] and computed["late_imports"] == []
    assert not [name for name in sys.modules if name.startswith("long_parity_reference")]
    for stem, digest in REFERENCE_SHA256.items():
        text = (REFERENCE_DIR / f"{stem}.py.txt").read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(text).hexdigest() == digest, stem


def test_the_reference_files_are_the_reference_commit_verbatim() -> None:
    for stem in REFERENCE_SHA256:
        try:
            shown = subprocess.run(
                ["git", "show", f"{REFERENCE_COMMIT}:app/backtesting/{stem}.py"],
                cwd=REPO_ROOT, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pytest.skip("git is not available")
        if shown.returncode != 0:
            pytest.skip(f"{REFERENCE_COMMIT} is not in this checkout (shallow clone)")
        assert shown.stdout.replace(b"\r\n", b"\n") == (REFERENCE_DIR / f"{stem}.py.txt").read_bytes().replace(
            b"\r\n", b"\n"
        ), stem


def test_parity_cases_are_not_vacuous(computed) -> None:
    """The comparison must exercise every long exit path and cost leg the short edits touch."""

    coverage = computed["coverage"]
    assert coverage["daily_traded"] >= 12
    for reason in ("stop_loss", "take_profit", "strategy_exit", "time_stop", "end_of_data", "session_close"):
        assert coverage[f"reason:{reason}"] > 0, reason
    for leg in COST_LEGS:
        assert coverage[f"daily11_wf_costs:{leg}"] >= 10, leg  # every leg, on real trades
    assert coverage["floor_widened"] > 0  # the stop_floor variant really widened long stops


# Trade fields that scale with the position size; every other field must match exactly.
SIZED = ("quantity", "notional_usd", "spread_usd", "financing_usd", "fx_usd", "slippage_usd", "regulatory_usd")


def test_etoro_fees_are_the_only_difference_from_the_fee_free_run(computed) -> None:
    """$1 comes off each leg's P&L and nothing else moves but the size later trades get: risk
    is 1% of current cash, so a fold's first trade is sized exactly as without the fee and a
    later one by the cash the earlier fees took (the ratio of the two quantities)."""

    fee = cost_module.ETORO_STOCK_FEE_USD_PER_LEG
    assert fee == 1.0  # the default the evidence runs use
    current, with_fees = computed["current"], computed["with_fees"]
    assert [label for label, _ in with_fees] == [label for label, _ in current]  # same rows, logs, summary
    run_logs = [(label, text) for label, text in current if label.endswith("log batch_backtest_run")]
    assert run_logs and run_logs == [(label, text) for label, text in with_fees if label.endswith("log batch_backtest_run")]

    fee_free, fee_rows = computed["fee_free_rows"], computed["fee_rows"]
    same_size = smaller = 0
    for (case, row), (_, base) in zip(fee_rows, fee_free, strict=True):
        assert (row["strategy_name"], row["file_path"]) == (base["strategy_name"], base["file_path"])
        assert row["metrics"]["number_of_trades"] == base["metrics"]["number_of_trades"] == len(row["trades"])
        assert row["metrics"].get("holdout_trades") == base["metrics"].get("holdout_trades")
        for trade, ref in zip(row["trades"], base["trades"], strict=True):
            assert list(trade) == list(ref)  # no new key: the fee is part of commission_usd
            scale = trade["quantity"] / ref["quantity"]
            assert 0.99 < scale <= 1.0, (case, scale)
            for key, value in trade.items():
                if key in SIZED:
                    assert value == pytest.approx(scale * ref[key], rel=1e-9, abs=2e-6), (case, key)
                elif key == "commission_usd":
                    assert value == pytest.approx(scale * ref[key] + 2 * fee, rel=1e-9, abs=2e-6), case
                elif key == "pnl_usd":
                    assert value == pytest.approx(scale * ref[key] - 2 * fee, rel=1e-9, abs=1e-8), case
                elif key == "pnl_pct":
                    assert value == pytest.approx(100 * trade["pnl_usd"] / trade["notional_usd"], rel=1e-9), case
                else:
                    assert value == ref[key], (case, key)
            if scale == 1.0:
                same_size += 1
                assert trade["pnl_usd"] == pytest.approx(ref["pnl_usd"] - 2.0, abs=1e-8)
                assert trade["commission_usd"] == pytest.approx(ref["commission_usd"] + 2.0, abs=1e-6)
            else:
                smaller += 1
    assert same_size >= 100 and smaller >= 50, (same_size, smaller)  # both kinds really occur


def test_etoro_fees_cost_the_evidence_about_two_hundredths_of_r(computed) -> None:
    """The pooled OOS expectancy the evidence gate reads (1 R = the batch's $100 risk) drops by
    the $2 round trip, give or take the later trades' slightly smaller sizes."""

    from app.performance.strategy_evidence import BACKTEST_RISK_USD

    def pooled(rows):
        oos = [row for _, row in rows if row["file_path"].endswith(":walk_forward_oos")]
        trades = sum(int(row["metrics"]["number_of_trades"]) for row in oos)
        pnl = sum(float(row["metrics"]["expectancy_usd"]) * int(row["metrics"]["number_of_trades"]) for row in oos)
        return trades, pnl / trades / BACKTEST_RISK_USD

    trades, fee_free_r = pooled(computed["fee_free_rows"])
    fee_trades, fee_r = pooled(computed["fee_rows"])
    assert fee_trades == trades >= 150
    assert -0.0205 < fee_r - fee_free_r < -0.0195, fee_r - fee_free_r
