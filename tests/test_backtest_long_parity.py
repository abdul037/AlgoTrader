"""Long backtests stay byte-identical to the pre-short engine (golden digests from bc82efd).

Short research (``EngineConfig.short_entries``, the short cost legs, the
``:walk_forward_oos_short`` rows) must not move one long number: the live evidence gate,
the screener's backtest validation and the eToro copy bar all read the long rows. So every
daily strategy runs on two seeded synthetic frames through ``BatchBacktestService.run``
(walk-forward on both, in-sample on one), and two intraday strategies run on a seeded 15m
frame with the live session flatten and the intraday variants (overnight hold, stop floor).

Each digest is a sha256 of exactly what ``BacktestRepository.create`` would write
(``json.dumps`` without ``sort_keys``, so key order counts too), minus the row id and the
wall-clock stamps, plus the summary dict the run returns and the logged variant metrics.
The trade count and exit reasons sit next to each digest so a failure shows what moved.

Regenerate only at a commit whose long engine is known-good, and say so in the commit:
``python -m tests.test_backtest_long_parity``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

import numpy as np
import pandas as pd
import pytest

from app.backtesting import engine as engine_module
from app.backtesting.batch import BatchBacktestService
from app.strategies import get_strategy_specs
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

    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False):
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


def _run(settings, frame, seed, *, timeframe, walk_forward, strategies=None):
    rows, logs = _Rows(), _Logs()
    service = BatchBacktestService(
        settings=settings, market_data_engine=_Bars(frame, seed), backtest_repository=rows, run_log_repository=logs
    )
    summary = service.run(
        symbols=[SYMBOL], timeframes=[timeframe], strategy_names=strategies, walk_forward=walk_forward
    )
    return summary, rows.rows, logs.events


def _digest(name, rows, results, variants) -> tuple[str, str]:
    mine = [row for row in rows if row["strategy_name"] == name]
    parts = []
    for row in mine:  # exactly the bytes BacktestRepository.create persists, minus id and stamps
        parts += [row["symbol"], row["strategy_name"], row["file_path"], json.dumps(row["metrics"]), json.dumps(row["trades"])]
    parts += [json.dumps(item) for item in results if item.get("strategy_name") == name]
    parts += [json.dumps(payload) for payload in variants if payload.get("strategy_name") == name]
    trades = [trade for row in mine for trade in row["trades"]]
    reasons = Counter(trade["reason"] for trade in trades)
    shape = f"rows={len(mine)} trades={len(trades)} " + " ".join(f"{k}={reasons[k]}" for k in sorted(reasons))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest(), shape


def compute_digests(tmp_dir) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """{"<case>|<strategy>": (sha256, shape)} plus coverage counters for the vacuity guard."""

    daily = make_settings(tmp_dir, walk_forward_intraday_variants=[])
    intraday = make_settings(tmp_dir, walk_forward_intraday_variants=["hold_overnight", "stop_floor_1.0"])
    names = [spec.name for spec in get_strategy_specs(timeframe="1d")]
    cases = [
        ("daily7_wf", daily, _daily_frame(7), 7, "1d", True, None, names),
        ("daily11_wf", daily, _daily_frame(11), 11, "1d", True, list(SELL_EMITTERS), list(SELL_EMITTERS)),
        ("daily7_insample", daily, _daily_frame(7), 7, "1d", False, None, names),
        ("intraday15m_wf", intraday, _intraday_frame(5), 5, "15m", True, list(INTRADAY_SPECS), list(INTRADAY_SPECS)),
    ]
    floors = {"calls": 0, "widened": 0}
    original_floor = engine_module.floored_stop_and_target

    def counting_floor(**kwargs):
        stop, target = original_floor(**kwargs)
        floors["calls"] += 1
        floors["widened"] += int(stop != kwargs["stop"])
        return stop, target

    digests: dict[str, tuple[str, str]] = {}
    reasons: Counter = Counter()
    traded: set[str] = set()
    engine_module.floored_stop_and_target = counting_floor
    try:
        for case, settings, frame, seed, timeframe, walk_forward, requested, case_names in cases:
            summary, rows, events = _run(
                settings, frame, seed, timeframe=timeframe, walk_forward=walk_forward, strategies=requested
            )
            variants = [payload for event, payload in events if event == "backtest_variant_result"]
            for name in case_names:
                digests[f"{case}|{name}"] = _digest(name, rows, summary.results, variants)
            for row in rows:
                reasons.update(trade["reason"] for trade in row["trades"])
                if row["trades"] and timeframe == "1d":
                    traded.add(row["strategy_name"])
            dumped = summary.model_dump(exclude={"generated_at"})
            digests[f"{case}|summary"] = (hashlib.sha256(json.dumps(dumped).encode()).hexdigest(), f"variants={len(variants)}")
    finally:
        engine_module.floored_stop_and_target = original_floor
    coverage = {**{f"reason:{k}": v for k, v in reasons.items()}, "daily_traded": len(traded), **floors}
    return digests, coverage


# Generated at bc82efd (before any short-research edit).
GOLDEN: dict[str, tuple[str, str]] = {
    'daily11_wf|ema_trend_stack': ('5c78bad69166a72b3e40e57f509aef476a759d80b281b55e6482230eaa8e2a14', 'rows=1 trades=8 end_of_data=5 stop_loss=2 strategy_exit=1'),
    'daily11_wf|ma_crossover': ('45b67f55ae468ff8362e527eedc8e38328f5ed634857cd57fad2ed767eb6b481', 'rows=1 trades=8 end_of_data=5 stop_loss=2 strategy_exit=1'),
    'daily11_wf|mean_reversion': ('421eec762593913dbc8978368b6106a6fdca3cebe386f87b6016a990466d661d', 'rows=1 trades=1 end_of_data=1'),
    'daily11_wf|momentum_breakout': ('6c8971f3302e563a3c5506f3ccf7e7c7e8acd87a71d322d2f1c91927bb1043c6', 'rows=1 trades=5 end_of_data=1 stop_loss=3 take_profit=1'),
    'daily11_wf|pullback_trend': ('00d173b5dec486c2b9578a43ace9d64c54fecb37b0d538a910470f8a8371c09d', 'rows=1 trades=9 end_of_data=6 strategy_exit=2 take_profit=1'),
    'daily11_wf|rsi_trend_continuation': ('e39f575bb577025b85b7a27dd9896fd86031e1f7ce79eaa57faab72341bb6fe4', 'rows=1 trades=1 end_of_data=1'),
    'daily11_wf|rsi_vwap_ema_confluence': ('8849ca45c76b7b3a47cccabf5e83f74bcd68fc2296f7a459b7f9ec4635a19b4e', 'rows=1 trades=0 '),
    'daily11_wf|summary': ('bc67fcd48022dc6ebd7fbfec72183511ce84bab6f637e90662d0531a01d95964', 'variants=0'),
    'daily11_wf|trend_following': ('535cf8335a1b1788d7bcc732015ef8794d9b8c2fc70062718925833d816faeb1', 'rows=1 trades=6 end_of_data=3 stop_loss=3'),
    'daily7_insample|atr_donchian_trend_breakout': ('71b30474c494d4170e212d02c1d5a377af84107a240de661d0fd2fe0d560fb7d', 'rows=1 trades=3 stop_loss=3'),
    'daily7_insample|connors_rsi2_reversion': ('e51a4f2f9f5313beab0261dfed164daab7b74e1d8afc860f19f7366d40e132b5', 'rows=1 trades=1 stop_loss=1'),
    'daily7_insample|ema_trend_stack': ('25e275d1ef5f80e5f2a16034855af12d0fc11b3bdbdc32001afd8335561ecce7', 'rows=1 trades=4 stop_loss=2 strategy_exit=1 take_profit=1'),
    'daily7_insample|etf_mega_cap_relative_strength_rotation': ('e28a827c265ec636ee5bd0edc1be8c3006b8f6a0808a372a11f2d0542076f29d', 'rows=1 trades=2 stop_loss=2'),
    'daily7_insample|inside_bar_narrow_range_breakout': ('3f962ab49ec5dd4c01e808ac0ab04dd93ebca9cc2b702135aa780c2cf55e5538', 'rows=1 trades=17 stop_loss=11 take_profit=6'),
    'daily7_insample|ma_crossover': ('8ca09d7105700d8bed2586f0f0038d2fcca2c91d3820fa21bae5f3fc801c1e5c', 'rows=1 trades=11 stop_loss=3 strategy_exit=7 take_profit=1'),
    'daily7_insample|mean_reversion': ('b24f179d89276bf0e2e54c78ad62ed23cf9671dcb5d2f6e663caf8315be853f9', 'rows=1 trades=0 '),
    'daily7_insample|momentum_breakout': ('02248931d9a3851fc18ab4fae38df4306f21a126ecf585a3a48779f62913e815', 'rows=1 trades=6 stop_loss=4 take_profit=2'),
    'daily7_insample|multi_timeframe_trend_pullback': ('da1eb48b2bea2d28ec30c1e2e17410049974a492629123c1870b22d0683f577f', 'rows=1 trades=2 stop_loss=2'),
    'daily7_insample|overnight_drift': ('8df976a3c954db565729370b862cbbc9507682e5771c6a47718442e436b0e01d', 'rows=1 trades=17 stop_loss=3 take_profit=6 time_stop=8'),
    'daily7_insample|pairs_stat_arb': ('cfb7320c393e227f6fa35844140ddd62ec050776c5acba8200b629025575165d', 'rows=1 trades=4 stop_loss=2 take_profit=2'),
    'daily7_insample|post_earnings_drift': ('1b4748e44e4082b99141e36bc8fbed0d916328174b7ec35141c2447aa5af43a5', 'rows=1 trades=0 '),
    'daily7_insample|pullback_trend': ('7f557af481fd9ff2e5930ddc452252cbe31f58198b5791b181d4cfb6987d4b3f', 'rows=1 trades=2 strategy_exit=2'),
    'daily7_insample|regime_aligned_trend_continuation': ('0ff532c1a7c3055b71eb02fafc445257c09c7d7415b3a4df49d5d7f0e243bb18', 'rows=1 trades=3 stop_loss=3'),
    'daily7_insample|regime_filtered_mean_reversion': ('ecd1bf87c15fe514b541af3e1a82651756540a4b198825ae2ea38a6a33daad17', 'rows=1 trades=1 take_profit=1'),
    'daily7_insample|relative_strength_momentum': ('fc591f158f3c5286ce57f127eb83d86da372f99226ba37301234c35f35f2fe5b', 'rows=1 trades=3 stop_loss=3'),
    'daily7_insample|rsi_trend_continuation': ('831a415b8653519725913e53c3d4274188b0a6677d6ecd16383578258435ed2a', 'rows=1 trades=0 '),
    'daily7_insample|rsi_vwap_ema_confluence': ('66d08bee6f018c5d2ee690c0769690dea7faaf806651957807a0182ae3e15de5', 'rows=1 trades=0 '),
    'daily7_insample|summary': ('7fffb8f6324b5be07fbd2b15a6b8d4a476b05ccdfb70e1123c9059157361db1e', 'variants=0'),
    'daily7_insample|trend_following': ('2351f687795347f284099eb6d487a8594c702105532262d071c45d3b9c649404', 'rows=1 trades=4 stop_loss=2 take_profit=2'),
    'daily7_insample|volatility_contraction_breakout': ('1bbb56926e83dd497e6cd519dd3d3a50cd7cec485854209ba4cee68fe6f93021', 'rows=1 trades=1 stop_loss=1'),
    'daily7_wf|atr_donchian_trend_breakout': ('ff94f04c74003fca825ced778ac2063ec6e193ebbef5939fc147b5c279a10564', 'rows=1 trades=4 end_of_data=2 stop_loss=2'),
    'daily7_wf|connors_rsi2_reversion': ('b98a57201840846b9ee4bd1215fed3751d196bf74ac3387970b3c4abf898c18e', 'rows=1 trades=0 '),
    'daily7_wf|ema_trend_stack': ('1d318124508836e6dd7c6cdb4b8f43713554d1a2a4461eb81ba64af55287a1c6', 'rows=1 trades=5 end_of_data=4 stop_loss=1'),
    'daily7_wf|etf_mega_cap_relative_strength_rotation': ('457b5cf9b83e71ef3e3ed38347f191edd3420d48e2706fe6897ac1303d2dff20', 'rows=1 trades=7 end_of_data=5 stop_loss=2'),
    'daily7_wf|inside_bar_narrow_range_breakout': ('d32df28de1ef1b0297e56902359a42df9fa54cc599f0da9c91669f177c68517a', 'rows=1 trades=13 end_of_data=4 stop_loss=7 take_profit=2'),
    'daily7_wf|ma_crossover': ('485aaf2f1c3da75146d431dc14e000730f586a6749ea69f089b6675f79d8e92f', 'rows=1 trades=7 end_of_data=4 stop_loss=1 strategy_exit=2'),
    'daily7_wf|mean_reversion': ('0af6c20b5a9d353fa73b512f8a3191e8f0635a7e88b65ce03970dec5ca328700', 'rows=1 trades=0 '),
    'daily7_wf|momentum_breakout': ('c40567053ee6775c7c245c59fc903e3fc63c962270fbcc136acd5994106e2bc0', 'rows=1 trades=2 end_of_data=1 stop_loss=1'),
    'daily7_wf|multi_timeframe_trend_pullback': ('daadc59b0ae50dc986a2457d49021e382d79b4de076840b1f7357004e0f44700', 'rows=1 trades=5 end_of_data=1 stop_loss=3 take_profit=1'),
    'daily7_wf|overnight_drift': ('1b3fed3a6575e059317e580b6492609d3b05fad71c4ea73a90091d716c8f0760', 'rows=1 trades=0 '),
    'daily7_wf|pairs_stat_arb': ('6eb6aab8205c037b520cb8ba4fac7d871be5ca0ffff4436210a50d6035821643', 'rows=1 trades=4 end_of_data=3 stop_loss=1'),
    'daily7_wf|post_earnings_drift': ('b595fb507de0a3abf95d31387a6f765e0e3eb03744f6a7b51417b561f754dcc4', 'rows=1 trades=0 '),
    'daily7_wf|pullback_trend': ('b9cda69ead6ebb268c880d42fc198142108a9119935900cfdde85def627e1cbd', 'rows=1 trades=5 end_of_data=5'),
    'daily7_wf|regime_aligned_trend_continuation': ('74ed36e85b9f9217d8033039d0579eeba02887cf0150540cb12ccbb7271b1ad5', 'rows=1 trades=6 end_of_data=5 stop_loss=1'),
    'daily7_wf|regime_filtered_mean_reversion': ('9976f799019b9b1931ecd625a02ff92471c728e7317ab4b32cf55400ee28bd12', 'rows=1 trades=3 stop_loss=2 take_profit=1'),
    'daily7_wf|relative_strength_momentum': ('c06ec889dc4d82929c837ec3d2a6c80edc34dd22c02452f6846a28cca4e8f8e5', 'rows=1 trades=5 end_of_data=4 stop_loss=1'),
    'daily7_wf|rsi_trend_continuation': ('0ad461226004cd901d87d75b6f248d6e4d673f8aba8d695a610085d60b1acb77', 'rows=1 trades=0 '),
    'daily7_wf|rsi_vwap_ema_confluence': ('8849ca45c76b7b3a47cccabf5e83f74bcd68fc2296f7a459b7f9ec4635a19b4e', 'rows=1 trades=0 '),
    'daily7_wf|summary': ('354341e07150ecf9581ee92ccbda91b1bc8d5a3291e8779f51e659113d52938d', 'variants=0'),
    'daily7_wf|trend_following': ('6861404db318204384b572a240615b65d0b1948325172d820a0fc9226913bd2d', 'rows=1 trades=4 end_of_data=3 stop_loss=1'),
    'daily7_wf|volatility_contraction_breakout': ('320157832f56702fd76fec2a60ea6814555029000747452e132fcd1ba5138af6', 'rows=1 trades=0 '),
    'intraday15m_wf|ema_trend_stack': ('6fb917e049b9b22752bfa6c0f958fc9da329065cb3e2298bc8743c27ee33277d', 'rows=1 trades=22 session_close=11 stop_loss=3 strategy_exit=7 take_profit=1'),
    'intraday15m_wf|summary': ('336a1d82a683fd8f99e6d19b162c89528e88e956e5ceb08b6ce40bfbee53578b', 'variants=4'),
    'intraday15m_wf|vwap_reclaim': ('e69f79c6d7a782fb702bb23f69eba0c2cd03bdfed6fe1aa03cecb4cef8aa99bd', 'rows=1 trades=39 session_close=15 stop_loss=13 strategy_exit=3 take_profit=8'),
}


@pytest.fixture(scope="module")
def computed(tmp_path_factory):
    return compute_digests(tmp_path_factory.mktemp("parity"))


def test_long_backtests_match_golden_digests(computed) -> None:
    digests, _ = computed
    assert set(digests) == set(GOLDEN)
    moved = {key: (digests[key], GOLDEN[key]) for key in GOLDEN if digests[key] != GOLDEN[key]}
    assert not moved, json.dumps(moved, indent=1)


def test_parity_cases_are_not_vacuous(computed) -> None:
    """The golden must exercise every long exit path the engine edits could touch."""

    _, coverage = computed
    assert coverage["daily_traded"] >= 12
    for reason in ("stop_loss", "take_profit", "strategy_exit", "time_stop", "end_of_data", "session_close"):
        assert coverage.get(f"reason:{reason}", 0) > 0, reason
    assert coverage["widened"] > 0  # the stop_floor variant really widened long stops


if __name__ == "__main__":  # pragma: no cover - golden regeneration helper
    import tempfile
    from pathlib import Path

    found, cover = compute_digests(Path(tempfile.mkdtemp()))
    print("GOLDEN: dict[str, tuple[str, str]] = {")
    for key in sorted(found):
        print(f"    {key!r}: {found[key]!r},")
    print("}")
    print(cover)
