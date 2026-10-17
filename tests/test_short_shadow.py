"""Shorts step 2 (operator 2026-10-17): short setups are recorded in shadow, never traded.

The deep scan lists bracketed ``entry_short`` SELLs in ``short_signals`` (never in ``signals``
or the ranked items) and a separate off-hours ledger follows them. The long watchlist and the
long ledger must come out exactly as before: the digests below were generated at bc82efd,
before any short code existed, on the same inputs (short setups and passing short verdicts
included, which that code ignored).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.workflow.premarket_deep_scan as deep
import app.workflow.shadow_ledger as shadow
from tests import test_premarket_deep_scan as scan_tests
from tests import test_shadow_ledger as ledger_tests

FEW = scan_tests.SYMBOLS[:6]
SHORT_KEY = "strategy_evidence:short_verdicts"  # the short research verdicts (step 1)
SHORT_SPECS = ("momentum_breakout", "mean_reversion")  # stubbed to emit shorts below

# sha256 of the long watchlist, coverage record and completed run log; and of the long ledger,
# its state, run logs and fetches; both at bc82efd.
GOLDEN_DEEP_SCAN = "4a5bfca0d670c02b35f352a07eaa19fb9d327a197670039dbc012091330b6299"
GOLDEN_LEDGER = "7ae57206a5aebc98367dfddee84d3f6d96e2e02c7bd190d27dcc272f9e36d893"
GOLDEN_REPORT = "f0907d76c01480637e5a5c3251ab818e1f3ac1dcb8aac399165feabf153bac87"


def _short_signal(price: float, *, role: str = "entry_short", bracket: bool = True):
    stop, target = (price * 1.04, price * 0.92) if bracket else (None, None)
    return SimpleNamespace(
        action="sell",
        price=price,
        stop_loss=stop,
        take_profit=target,
        metadata={"signal_role": role},
    )


def _with_shorts(service, names=SHORT_SPECS):
    """``names`` emit a bracketed entry_short SELL on even symbols; odd ones keep the real call."""

    real = service.market_screener._build_strategy

    def build(spec):
        strategy = real(spec)
        if spec.name not in names:
            return strategy

        def generate(frame, symbol):
            if int(symbol[1:]) % 2:
                return strategy.generate_signal(frame, symbol)
            return _short_signal(float(frame["close"].iloc[-1]))

        return SimpleNamespace(generate_signal=generate)

    service.market_screener._build_strategy = build
    return service


def _passing_short_verdicts(service, now) -> None:
    """Passing, high-expectancy short verdicts for every daily spec, in both key shapes."""

    verdict = {"passed": True, "oos_expectancy_r": 0.5, "oos_trades": 400, "holdout_trades": 50}
    names = [spec.name for spec in scan_tests._specs(service.settings)]
    verdicts = {f"{name}:short:1d": dict(verdict) for name in names}
    verdicts.update({f"{name}:1d": dict(verdict) for name in names})
    payload = {"computed_at": now.isoformat(), "research_only": True, "verdicts": verdicts}
    service.runtime_state.set(SHORT_KEY, json.dumps(payload))


def _scan(tmp_path, monkeypatch, *, short_verdicts: bool, **overrides):
    service = scan_tests._service(
        tmp_path,
        monkeypatch,
        market=scan_tests._MarketData(fail={"S03"}),
        market_universe_symbols=[*FEW, "BTC/USD"],
        allowed_instruments=FEW,
        **overrides,
    )
    _with_shorts(service)
    if short_verdicts:
        _passing_short_verdicts(service, scan_tests.MONDAY_0835_NY)
    scan_tests._run(service)
    return service


def _long_scan_view(service) -> list:
    watchlist = scan_tests._watchlist(service)
    long_only = {
        k: v for k, v in watchlist.items() if k not in {"run_id", "short_signals", "short_errors"}
    }
    coverage = json.loads(service.runtime_state.get(deep.COVERAGE_KEY))
    logs = [
        [event, {k: v for k, v in payload.items() if not k.startswith("short_")}]
        for event, payload in service.run_logs.items
    ]
    return [long_only, coverage, logs]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()  # no sort_keys: order counts


def test_long_watchlist_is_unchanged_by_short_setups_and_short_verdicts(
    tmp_path, monkeypatch
) -> None:
    without = _scan(tmp_path / "a", monkeypatch, short_verdicts=False)
    with_verdicts = _scan(tmp_path / "b", monkeypatch, short_verdicts=True)
    view = _long_scan_view(without)
    assert view[0]["signals"] and view[0]["items"], "the long list is not empty: not vacuous"
    assert _long_scan_view(with_verdicts) == view
    assert _digest(view) == GOLDEN_DEEP_SCAN


def _ledger_service(tmp_path, monkeypatch, *, now=ledger_tests.FRIDAY_1000_NY):
    service = ledger_tests._service(tmp_path, monkeypatch, now=now)
    shorts = {
        "2026-10-12": [_short_item("S00"), _short_item("S02")],  # S02 has no long setup
        "2026-10-13": [
            _short_item("S00", bar="2026-10-12"),  # while the S00 short is still open
            _short_item("S01", bar="2026-10-12", stop=100.8),  # the quiet bars reach 102
        ],
    }
    for day, items in shorts.items():
        key = f"premarket:watchlist:{day}"
        watchlist = json.loads(service.runtime_state.get(key))
        watchlist["short_signals"] = items
        service.runtime_state.set(key, json.dumps(watchlist))
    service.market_data.frames["S02"] = service.market_data.frames["S00"]
    return service


def _short_item(symbol, spec="trend_following:1d", *, bar="2026-10-09", stop=103.0, target=95.0):
    return {
        **ledger_tests._signal(symbol, spec, bar=bar, stop=stop, target=target),
        "side": "short",
        "oos_expectancy_r": 0.2,
        "tradeable": False,
        "shadow_only": True,
    }


def _long_ledger_view(service) -> list:
    state = service.runtime_state
    return [
        json.loads(state.get(shadow.LEDGER_KEY)),
        json.loads(state.get(shadow.STATE_KEY)),
        service.run_logs.items,
        service.market_data.calls,
    ]


def test_long_ledger_is_unchanged_by_short_setups_in_the_watchlists(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch)
    assert shadow.refresh_shadow_ledger(service)["done_for_today"]
    view = _long_ledger_view(service)
    assert {call[0] for call in service.market_data.calls} == {"S00", "S01"}  # never S02
    assert _digest(view) == GOLDEN_LEDGER
    report = shadow.shadow_ledger_report(service.runtime_state)
    assert report["taken"] == 2
    assert _digest({k: v for k, v in report.items() if k != "short"}) == GOLDEN_REPORT


def test_golden_inputs_carry_short_setups(tmp_path, monkeypatch) -> None:
    # The goldens are only meaningful if their inputs hold shorts the old code ignored.
    service = _ledger_service(tmp_path, monkeypatch, now=datetime(2026, 10, 16, 14, tzinfo=UTC))
    raw = json.loads(service.runtime_state.get("premarket:watchlist:2026-10-12"))
    assert [i["symbol"] for i in raw["short_signals"]] == ["S00", "S02"]


# --------------------------------------------------------------------------- #
# Deep scan: short setups are listed apart and never tradeable
# --------------------------------------------------------------------------- #
def _shorts(service) -> list[dict]:
    return scan_tests._watchlist(service)["short_signals"]


def test_short_setups_are_listed_apart_and_never_tradeable(tmp_path, monkeypatch) -> None:
    service = _scan(tmp_path, monkeypatch, short_verdicts=True)
    watchlist = scan_tests._watchlist(service)
    shorts = _shorts(service)
    stubbed = {(s, f"{n}:1d") for s in ("S00", "S02", "S04") for n in SHORT_SPECS}  # S03: no data
    assert stubbed <= {(i["symbol"], i["spec_key"]) for i in shorts}
    for item in shorts:
        assert item["side"] == "short" and item["stop"] > item["entry"] > item["target"] > 0
        assert item["tradeable"] is False and item["shadow_only"] is True
        reward, risk = item["entry"] - item["target"], item["stop"] - item["entry"]
        assert item["rr"] == round(reward / risk, 3)
        assert (item["oos_expectancy_r"], item["oos_trades"]) == (0.5, 400)  # the short verdict
    longs = [*watchlist["signals"], *watchlist["items"], *watchlist["shadow_items"]]
    assert longs and not any(i.get("side") == "short" for i in longs)
    assert not stubbed & {(i["symbol"], i["spec_key"]) for i in longs}
    assert shorts == sorted(shorts, key=deep._short_rank_key)
    payload = service.run_logs.items[-1][1]
    assert payload["short_signals"] == len(shorts) and payload["short_errors"] == 0
    coverage = json.loads(service.runtime_state.get(deep.COVERAGE_KEY))
    assert coverage["candidates_found"] == len(watchlist["signals"])  # shorts are not candidates


def _variant(**changes):
    return SimpleNamespace(**{**vars(_short_signal(100.0)), **changes})


def _one(signal, verdicts=None, name="mean_reversion"):
    spec = SimpleNamespace(name=name)
    last = {"timestamp": "2026-10-09 04:00:00+00:00", "close": 100.0}
    context = {"short_verdicts": lambda: verdicts or {}}
    return deep._short_item(signal, "S00", spec, f"{name}:1d", last, 1.2, "tech", context)


def test_only_a_bracketed_entry_short_sell_is_a_short_setup() -> None:
    item = _one(_short_signal(100.0))
    assert (item["entry"], item["stop"], item["target"]) == (100.0, 104.0, 92.0)
    assert (item["stop_pct"], item["kind"], item["oos_expectancy_r"]) == (4.0, "strict", None)
    rejected = [
        None,
        _short_signal(100.0, role="entry_short_watch"),  # a hint without a stop and target
        _variant(metadata={}),  # an exit SELL (gold_momentum's bare SELL)
        _variant(metadata=None),
        _short_signal(100.0, bracket=False),
        _variant(action="buy"),
        _variant(stop_loss=96.0),  # the stop below the entry: not a short's bracket
        _variant(take_profit=float("nan")),
        _variant(stop_loss=float("inf")),
    ]
    assert [_one(signal) for signal in rejected] == [None] * len(rejected)
    weak = _variant(metadata={"signal_role": "entry_short", "weak_signal_kind": "x"})
    assert _one(weak)["kind"] == "weak" and _one(weak)["tradeable"] is False


def test_short_figures_come_only_from_the_native_short_verdict_key() -> None:
    native = {"mean_reversion:short:1d": {"oos_expectancy_r": 0.12, "oos_trades": 80}}
    # '<name>:1d' is the live gate's key shape: a short verdict is never read under it.
    renamed = {"mean_reversion:1d": {"oos_expectancy_r": 0.07, "holdout_trades": 11}}
    assert _one(_short_signal(100.0), native)["oos_expectancy_r"] == 0.12
    assert _one(_short_signal(100.0), {**renamed, **native})["holdout_trades"] is None
    for odd in (renamed, {"mean_reversion:short:1d": "n/a"}, {"mean_reversion:short:1d": None}):
        item = _one(_short_signal(100.0), odd)  # still listed, without figures
        assert item is not None and item["oos_expectancy_r"] is item["oos_trades"] is None


class _CountingState(scan_tests.FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get(self, key: str):
        self.reads.append(key)
        return super().get(key)


def test_short_verdicts_are_read_once_and_only_for_shorts(tmp_path, monkeypatch) -> None:
    def reads(**overrides):
        state = _CountingState()
        with monkeypatch.context() as patch:
            patch.setattr(scan_tests, "FakeState", lambda: state)
            service = _scan(
                tmp_path / str(len(overrides)), monkeypatch, short_verdicts=True, **overrides
            )
        return service, state.reads.count(SHORT_KEY)

    on, reads_on = reads()
    assert _shorts(on) and reads_on == 1  # one cached read for the whole run
    off, reads_off = reads(premarket_short_shadow_enabled=False)
    assert _shorts(off) == [] and reads_off == 0


def test_a_failing_short_never_touches_the_long_scan(tmp_path, monkeypatch) -> None:
    def boom(*args):
        raise RuntimeError("short boom")

    monkeypatch.setattr(deep, "_short_item", boom)
    service = _scan(tmp_path, monkeypatch, short_verdicts=True)
    watchlist = scan_tests._watchlist(service)
    assert watchlist["short_signals"] == [] and watchlist["short_errors"] > 0
    assert not any("short boom" in e for e in watchlist["errors"])  # _rewind counts on errors
    assert _digest(_long_scan_view(service)) == GOLDEN_DEEP_SCAN


def test_the_short_switch_stops_recording_and_leaves_the_long_scan(tmp_path, monkeypatch) -> None:
    off = {"premarket_short_shadow_enabled": False}
    service = _scan(tmp_path, monkeypatch, short_verdicts=True, **off)
    assert _shorts(service) == []
    assert _digest(_long_scan_view(service)) == GOLDEN_DEEP_SCAN


def test_shorts_never_reach_the_telegram_note(tmp_path, monkeypatch) -> None:
    loud = {"premarket_deep_scan_notify": True}
    on = _scan(tmp_path / "on", monkeypatch, short_verdicts=True, **loud)
    off = _scan(
        tmp_path / "off",
        monkeypatch,
        short_verdicts=True,
        premarket_short_shadow_enabled=False,
        **loud,
    )
    assert _shorts(on) and _shorts(off) == []
    assert on.notifier.messages == off.notifier.messages  # the same note, shorts or not
    [text] = on.notifier.messages
    lines = text.splitlines()
    assert lines[0].startswith("SHADOW pre-market setups") and len(lines) > 1  # not vacuous
    assert "short" not in text.lower()
    for item in _shorts(on):  # a stubbed spec on an even symbol has no long setup to name
        assert f"- {item['symbol']} {item['strategy']} " not in text


def test_odd_short_fields_in_a_stored_list_never_stop_the_long_scan(tmp_path, monkeypatch) -> None:
    def resumed(path, **odd):
        service = scan_tests._service(
            path, monkeypatch, market_universe_symbols=FEW, allowed_instruments=FEW
        )
        _with_shorts(service)
        scan_tests._run(service, stop=scan_tests._StopAfter(3))
        key = deep.WATCHLIST_KEY.format(date="2026-10-12")
        partial = json.loads(service.runtime_state.get(key))
        partial.update(odd)  # hand-edited, or stored by something else
        service.runtime_state.set(key, json.dumps(partial))
        scan_tests._run(service)
        return service

    clean = resumed(tmp_path / "clean")
    odd = resumed(tmp_path / "odd", short_signals=7, short_errors="x")
    done = scan_tests._watchlist(odd)
    assert done["status"] == "complete" and done["short_errors"] == 0
    assert {i["symbol"] for i in done["short_signals"]} == {"S04"}  # scanned after the resume
    assert _long_scan_view(odd) == _long_scan_view(clean)
    payload = odd.run_logs.items[-1][1]
    assert (payload["short_signals"], payload["short_errors"]) == (len(done["short_signals"]), 0)
    swapped_in = {"signals": [], "failed_symbols": [], "short_signals": 5, "short_errors": -1}
    deep._finalize(swapped_in, odd.settings, complete=True)  # e.g. handed over by _ahead_of
    assert (swapped_in["short_signals"], swapped_in["short_errors"]) == ([], 0)
    assert swapped_in["status"] == "complete"

    def boom(*args):
        raise RuntimeError("short boom")

    monkeypatch.setattr(deep, "_short_item", boom)
    edited = {"short_signals": 7, "short_errors": "x"}
    deep._record_short(edited, ())  # never raises, whatever the list held
    assert edited == {"short_signals": [], "short_errors": 1}


def test_a_list_stored_without_short_fields_resumes_and_finishes(tmp_path, monkeypatch) -> None:
    service = scan_tests._service(
        tmp_path, monkeypatch, market_universe_symbols=FEW, allowed_instruments=FEW
    )
    _with_shorts(service)
    scan_tests._run(service, stop=scan_tests._StopAfter(3))
    key = deep.WATCHLIST_KEY.format(date="2026-10-12")
    partial = json.loads(service.runtime_state.get(key))
    assert partial["status"] == "partial" and partial["cursor"] == 3
    for field in ("short_signals", "short_errors"):  # as the code before shorts stored it
        partial.pop(field)
    service.runtime_state.set(key, json.dumps(partial))
    scan_tests._run(service)
    done = scan_tests._watchlist(service)
    assert done["status"] == "complete"
    assert {i["symbol"] for i in done["short_signals"]} == {"S04"}  # scanned after the resume
    swapped_in = {"signals": [], "failed_symbols": []}  # e.g. _ahead_of hands an older list over
    deep._finalize(swapped_in, service.settings, complete=True)
    assert swapped_in["short_signals"] == [] and swapped_in["status"] == "complete"


def test_the_saved_watchlist_with_shorts_is_strict_json(tmp_path, monkeypatch) -> None:
    service = scan_tests._service(
        tmp_path, monkeypatch, market_universe_symbols=FEW, allowed_instruments=FEW
    )
    _with_shorts(service)
    nan = {"oos_expectancy_r": float("nan"), "oos_trades": float("nan")}
    payload = {"verdicts": {f"{n}:short:1d": nan for n in SHORT_SPECS}}
    service.runtime_state.set(SHORT_KEY, json.dumps(payload))
    scan_tests._run(service)

    def refuse(token):
        raise ValueError(token)

    raw = service.runtime_state.get(deep.WATCHLIST_KEY.format(date="2026-10-12"))
    shorts = json.loads(raw, parse_constant=refuse)["short_signals"]
    assert shorts and all(i["oos_expectancy_r"] is None and i["oos_trades"] is None for i in shorts)


def test_short_verdicts_never_reach_the_long_evidence(tmp_path, monkeypatch) -> None:
    from app.performance.strategy_evidence import VERDICTS_KEY, evidence_blocker

    service = scan_tests._service(tmp_path, monkeypatch)
    names = [spec.name for spec in scan_tests._specs(service.settings)]

    def gate(context):
        return (
            context["verdicts"],
            [context["evidence_ok"](n) for n in names],
            [context["copy_edge_ok"](n) for n in names],
        )

    without = gate(deep._prefetch(service))
    _passing_short_verdicts(service, scan_tests.MONDAY_0835_NY)
    assert gate(deep._prefetch(service)) == without
    assert without[1].count(True) == 1  # only the long trend_following verdict passes
    # Written under the live key by mistake, the short payload's own keys still unlock nothing.
    native = json.loads(service.runtime_state.get(SHORT_KEY))
    native["verdicts"] = {k: v for k, v in native["verdicts"].items() if ":short:" in k}
    service.runtime_state.set(VERDICTS_KEY, json.dumps(native))
    strict = SimpleNamespace(require_strategy_oos_evidence=True)
    blocked = evidence_blocker(
        strict, service.runtime_state, strategy="mean_reversion", timeframe="1d"
    )
    assert blocked == "strategy_lacks_oos_evidence"


def test_the_short_verdict_key_is_the_research_key(monkeypatch) -> None:
    from app.performance import strategy_evidence

    assert deep.SHORT_VERDICTS_KEY == SHORT_KEY != strategy_evidence.VERDICTS_KEY
    # Step 1 (shorts research) names this key strategy_evidence.SHORT_VERDICTS_KEY. A short key
    # constant there under any name must be this one, and so must the key its writer sets.
    named = {
        name: value
        for name, value in vars(strategy_evidence).items()
        if name.endswith("_KEY") and isinstance(value, str) and "short" in f"{name}{value}".lower()
    }
    assert set(named.values()) <= {deep.SHORT_VERDICTS_KEY}, named
    writer = getattr(strategy_evidence, "refresh_short_verdicts", None)
    if writer is not None:  # step 1 is in: what it writes is what the deep scan reads
        monkeypatch.setattr(strategy_evidence, "compute_verdicts", lambda *args, **kwargs: {})
        state = scan_tests.FakeState()
        writer(None, SimpleNamespace(), state)
        assert list(state.values) == [deep.SHORT_VERDICTS_KEY]


def test_the_live_gate_functions_never_name_shorts() -> None:
    import inspect

    from app.performance import strategy_evidence as evidence

    # strategy_evidence.py may host the short research key (step 1); the functions the live
    # gate runs must still never mention shorts at all.
    for func in (
        evidence.evidence_blocker,
        evidence.compute_verdicts,
        evidence.refresh_verdicts,
        evidence._edge_r,
        evidence._stale,
    ):
        assert "short" not in inspect.getsource(func).lower(), func.__name__


def test_nothing_on_the_trading_path_reads_short_shadow_data() -> None:
    from pathlib import Path

    app_root = Path(deep.__file__).resolve().parents[1]
    step2 = ("short_signals", "short_verdicts", "SHORT_VERDICTS_KEY", "shadow_ledger:short")
    step2 += ("SHORT_LEDGER_KEY",)
    research = ("walk_forward_oos_short", "short_research")  # step 1's research rows
    readers = {  # step 1 may add the last three
        "workflow/premarket_deep_scan.py",
        "workflow/shadow_ledger.py",
        "performance/strategy_evidence.py",
        "performance/routes.py",
        "backtesting/short_research.py",
    }
    trading = ("broker/", "automation/", "risk/", "execution/", "approvals/", "screener/")
    trading += ("signals/", "paper/", "workflow/service.py", "workflow/operations.py")
    for path in app_root.rglob("*.py"):
        rel = path.relative_to(app_root).as_posix()
        text = path.read_text()
        if any(token in text for token in step2):
            assert rel in readers, rel
        if rel.startswith(trading):
            assert not any(token in text for token in (*step2, *research)), rel


# --------------------------------------------------------------------------- #
# Ledger: shorts are followed apart, off-hours, with the engine's short rules
# --------------------------------------------------------------------------- #
COST = ledger_tests.COST
SIGNAL = ledger_tests.SIGNAL
FRIDAY_1700_NY = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)  # 17:00 EDT


def _bid(raw: float) -> float:  # a short's entry
    return COST.entry_fill_price(raw, side="sell", extended_hours=False)


def _ask(raw: float) -> float:  # a short's cover
    return COST.exit_fill_price(raw, side="sell", extended_hours=False)


def _short(signal_bar="2026-10-09", *, stop=105.0, target=90.0):
    return {**ledger_tests._entry(signal_bar, stop=stop, target=target), "side": "short"}


def test_short_entry_at_the_next_open_bid_and_the_target_at_the_target() -> None:
    entry = _short()
    bars = ledger_tests._bars(
        SIGNAL,
        ("2026-10-12", 99.5, 100, 98, 99),
        ("2026-10-13", 99, 99.5, 96, 97),
        ("2026-10-14", 97, 98, 89, 90),
    )
    shadow.score_entry(entry, bars, datetime(2026, 10, 15).date())
    fill = _bid(99.5)
    assert entry["entry_date"] == "2026-10-12" and entry["entry_price"] == round(fill, 4)
    leg = entry["stop_target"]
    assert (leg["status"], leg["reason"], leg["exit_date"], leg["bars"]) == (
        "closed",
        "target",
        "2026-10-14",
        3,
    )
    assert leg["exit_price"] == round(_ask(90), 4)
    assert leg["r"] == round((fill - _ask(90)) / (105 - fill), 3) > 0  # a falling price wins
    assert entry["time_14d"] == leg and entry["status"] == "closed"


def test_short_stop_wins_a_bar_that_touches_both_and_a_gap_up_fills_at_the_open() -> None:
    today = datetime(2026, 10, 14).date()
    both = _short()
    shadow.score_entry(both, ledger_tests._bars(SIGNAL, ("2026-10-12", 99.5, 106, 89, 100)), today)
    assert both["stop_target"]["reason"] == "stop"
    assert both["stop_target"]["exit_price"] == round(_ask(105), 4)
    gap = _short()
    bars = ledger_tests._bars(
        SIGNAL, ("2026-10-12", 99.5, 101, 99, 100), ("2026-10-13", 108, 109, 107, 108)
    )
    shadow.score_entry(gap, bars, today)
    fill = _bid(99.5)
    assert gap["stop_target"]["exit_price"] == round(_ask(108), 4)  # the worse of stop and open
    assert gap["stop_target"]["r"] == round((fill - _ask(108)) / (105 - fill), 3) < -1


def test_a_quiet_short_gets_the_fold_length_time_exit() -> None:
    entry = _short()
    quiet = ledger_tests._quiet("2026-10-12", "2026-10-30")  # open 100, high 102, low 99, close 101
    shadow.score_entry(
        entry, [ledger_tests._bars(SIGNAL)[0], *quiet], datetime(2026, 10, 31).date()
    )
    fill = _bid(100)
    timed = entry["time_14d"]
    assert (timed["status"], timed["reason"], timed["exit_date"]) == (
        "closed",
        "time_exit",
        "2026-10-23",
    )
    assert timed["r"] == round((fill - _ask(101)) / (105 - fill), 3)
    assert entry["stop_target"]["status"] == "open" and entry["status"] == "open"


def test_a_short_waits_for_the_next_session_and_skips_a_stop_at_the_fill() -> None:
    entry = _short()
    shadow.score_entry(entry, ledger_tests._bars(SIGNAL), datetime(2026, 10, 12).date())
    assert entry["status"] == "pending"
    flat = _short(stop=_bid(100.5))
    bars = ledger_tests._bars(SIGNAL, ("2026-10-12", 100.5, 102, 99, 101))
    shadow.score_entry(flat, bars, datetime(2026, 10, 13).date())
    assert flat["status"] == "no_risk"


def test_one_short_per_symbol_and_strategy_at_a_time() -> None:
    first, second = _short(), _short("2026-10-13")
    quiet = [ledger_tests._bars(SIGNAL)[0], *ledger_tests._quiet("2026-10-12", "2026-10-16")]
    shadow._score_symbol([first, second], quiet, datetime(2026, 10, 17).date())
    assert first["status"] == "open" and second["status"] == "skipped_open"


def test_a_short_entry_needs_a_short_bracket_and_the_short_side() -> None:
    good = _short_item("S00")
    entry = shadow._short_entry(good, "2026-10-12")
    assert entry["key"] == "2026-10-12|S00|trend_following:1d|short"
    assert (entry["side"], entry["tradeable"], entry["expected_r"]) == ("short", False, 0.2)
    assert shadow._entry(good, "2026-10-12") is None  # never a long either
    for bad in (
        {**good, "stop": 95.0, "target": 110.0},  # a long's bracket
        {**good, "side": None},
        {**good, "stop": float("nan")},
        {**good, "signal_bar_ts": None},
    ):
        assert shadow._short_entry(bad, "2026-10-12") is None
    assert (
        shadow._short_entry({**good, "oos_expectancy_r": float("nan")}, "x")["expected_r"] is None
    )


def test_off_hours_the_short_ledger_follows_the_short_setups(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    before = dict(service.runtime_state.values)
    summary = shadow.refresh_short_shadow_ledger(service)
    assert summary["added"] == 4 and summary["done_for_today"] and summary["side"] == "short"
    entries = json.loads(service.runtime_state.get(shadow.SHORT_LEDGER_KEY))["entries"]
    status = {key: entry["status"] for key, entry in entries.items()}
    assert status == {
        "2026-10-12|S00|trend_following:1d|short": "open",
        "2026-10-12|S02|trend_following:1d|short": "open",  # no long setup: fetched here
        "2026-10-13|S00|trend_following:1d|short": "skipped_open",
        "2026-10-13|S01|trend_following:1d|short": "closed",
    }
    stopped = entries["2026-10-13|S01|trend_following:1d|short"]["stop_target"]
    assert stopped["reason"] == "stop" and stopped["r"] < -1  # the quiet bars reach 102
    changed = {k for k, v in service.runtime_state.values.items() if before.get(k) != v}
    assert changed == {shadow.SHORT_LEDGER_KEY, shadow.SHORT_STATE_KEY}  # never the long keys
    assert [e for e, _ in service.run_logs.items] == ["premarket_short_shadow_ledger_updated"]
    assert sorted(c[0] for c in service.market_data.calls) == ["S00", "S01", "S02"]
    assert all(call[3] is False for call in service.market_data.calls)
    assert shadow.refresh_short_shadow_ledger(service) is None  # once a day


def test_a_long_and_a_short_on_one_symbol_and_strategy_are_both_followed(
    tmp_path, monkeypatch
) -> None:
    service = _ledger_service(tmp_path, monkeypatch)  # Friday 10:00 ET: the long ledger
    shadow.refresh_shadow_ledger(service)
    monkeypatch.setattr(shadow, "utc_now", lambda: FRIDAY_1700_NY)
    shadow.refresh_short_shadow_ledger(service)
    longs = json.loads(service.runtime_state.get(shadow.LEDGER_KEY))["entries"]
    shorts = json.loads(service.runtime_state.get(shadow.SHORT_LEDGER_KEY))["entries"]
    assert longs["2026-10-12|S00|trend_following:1d"]["status"] == "open"
    assert shorts["2026-10-12|S00|trend_following:1d|short"]["status"] == "open"
    assert not any(e.get("side") == "short" for e in longs.values())


class _Watched:
    """A runtime_state that records every call."""

    def __init__(self, inner) -> None:
        self.inner, self.calls = inner, []

    def get(self, key):
        self.calls.append(("get", key))
        return self.inner.get(key)

    def set(self, key, value):
        self.calls.append(("set", key))
        self.inner.set(key, value)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 12, 12, 0, tzinfo=UTC),  # Mon 08:00 ET: the pre-market scan's hour
        datetime(2026, 10, 12, 12, 30, tzinfo=UTC),  # 08:30 ET
        datetime(2026, 10, 12, 13, 40, tzinfo=UTC),  # 09:40 ET, after the long ledger's 09:35
        datetime(2026, 10, 12, 14, 0, tzinfo=UTC),  # 10:00 ET
        datetime(2026, 10, 16, 20, 29, tzinfo=UTC),  # Fri 16:29 ET
    ],
)
def test_in_session_the_short_ledger_never_touches_the_database(tmp_path, monkeypatch, now) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=now)
    service.runtime_state = _Watched(service.runtime_state)
    assert shadow.refresh_short_shadow_ledger(service) is None
    assert service.runtime_state.calls == [] and service.market_data.calls == []
    assert service.run_logs.items == []


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 12, 11, 59, tzinfo=UTC),  # Mon 07:59 ET
        datetime(2026, 10, 16, 20, 30, tzinfo=UTC),  # Fri 16:30 ET
        datetime(2026, 10, 17, 14, 0, tzinfo=UTC),  # Sat 10:00 ET
    ],
)
def test_off_hours_the_short_ledger_runs(tmp_path, monkeypatch, now) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=now)
    assert shadow.refresh_short_shadow_ledger(service) is not None


def test_the_quiet_hours_are_new_york_hours_whatever_the_schedule_zone(tmp_path) -> None:
    from tests.conftest import make_settings

    london = make_settings(tmp_path, schedule_timezone="Europe/London")
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    assert shadow._short_quiet(monday.replace(hour=14), london)  # 10:00 ET, 15:00 London
    assert shadow._short_quiet(monday.replace(hour=19), london)  # 15:00 ET, 20:00 London
    assert shadow._short_quiet(monday.replace(hour=7, minute=30), london)  # London 08:30 scan
    assert not shadow._short_quiet(monday.replace(hour=21), london)  # 17:00 ET, 22:00 London
    early = make_settings(tmp_path, premarket_scan_time_local="07:15")
    assert shadow._short_quiet(monday.replace(hour=11, minute=20), early)  # 07:20 ET scan
    assert not shadow._short_quiet(monday.replace(hour=11, minute=20), make_settings(tmp_path))


@pytest.mark.parametrize(
    "overrides",
    [
        {"crypto_trading_enabled": True},  # crypto trades around the clock
        {"premarket_short_shadow_enabled": False},
        {"premarket_deep_scan_enabled": False},
    ],
)
def test_switches_that_keep_the_short_ledger_off(tmp_path, monkeypatch, overrides) -> None:
    from tests.conftest import make_settings

    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    values = {"premarket_deep_scan_enabled": True, **overrides}
    service.settings = make_settings(tmp_path, **values)
    service.runtime_state = _Watched(service.runtime_state)
    assert shadow.refresh_short_shadow_ledger(service) is None
    assert service.runtime_state.calls == [] and service.market_data.calls == []


def test_the_short_ledger_bounds_its_fetches_by_its_own_budget(tmp_path, monkeypatch) -> None:
    import app.screener.scan_support as support

    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    clock = {"t": 0.0}
    timeouts: list[float] = []

    def slow(label, timeout, func, *args, **kwargs):  # every fetch takes 10 s
        timeouts.append(timeout)
        clock["t"] += 10.0
        return func(*args, **kwargs)

    monkeypatch.setattr(support, "_bounded_call", slow)
    first = shadow.refresh_short_shadow_ledger(service, clock=lambda: clock["t"])
    assert timeouts == [20.0, 10.0]  # the fetch timeout, then what was left of the 20 s
    assert first["scored_symbols"] == 2 and not first["done_for_today"]
    second = shadow.refresh_short_shadow_ledger(service, clock=lambda: clock["t"])
    assert second["scored_symbols"] == 1 and second["done_for_today"]  # S02, the rest
    assert sorted(c[0] for c in service.market_data.calls) == ["S00", "S01", "S02"]


def _timed_fetches(monkeypatch, seconds: float):
    """Every fetch takes ``seconds`` on a fake clock; a shorter timeout fires, as it would."""

    import app.screener.scan_support as support

    clock = {"t": 0.0}
    timeouts: list[float] = []

    def timed(label, timeout, func, *args, **kwargs):
        timeouts.append(timeout)
        clock["t"] += min(timeout, seconds)
        if timeout < seconds:
            raise support.ScanTimeoutError(f"{label}_timeout_after_{timeout:g}s")
        return func(*args, **kwargs)

    monkeypatch.setattr(support, "_bounded_call", timed)
    return clock, timeouts


def test_a_fetch_the_budget_cut_short_is_tried_again_the_same_day(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    clock, timeouts = _timed_fetches(monkeypatch, 8.0)
    first = shadow.refresh_short_shadow_ledger(service, clock=lambda: clock["t"])
    assert timeouts == [20.0, 12.0, 4.0]  # S02 had 4 s left of the 20 s: cut short
    assert (first["scored_symbols"], first["failed_symbols"]) == (2, [])  # not a failure
    assert not first["done_for_today"]
    second = shadow.refresh_short_shadow_ledger(service, clock=lambda: clock["t"])
    assert timeouts[3:] == [20.0] and second["scored_symbols"] == 1 and second["done_for_today"]
    entries = json.loads(service.runtime_state.get(shadow.SHORT_LEDGER_KEY))["entries"]
    assert entries["2026-10-12|S02|trend_following:1d|short"]["status"] == "open"  # scored today


def test_a_fetch_that_always_times_out_still_ends_the_day(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    clock, timeouts = _timed_fetches(monkeypatch, 60.0)  # the feed never answers

    def ticking():  # the reads before the first fetch take time too
        clock["t"] += 0.5
        return clock["t"]

    runs = [shadow.refresh_short_shadow_ledger(service, clock=ticking) for _ in range(3)]
    assert timeouts == [19.5] * 3  # each run's first fetch had all the run could give it
    assert [run["failed_symbols"] for run in runs] == [["S00"], ["S01"], ["S02"]]
    assert [run["done_for_today"] for run in runs] == [False, False, True]
    assert shadow.refresh_short_shadow_ledger(service, clock=ticking) is None


@pytest.mark.parametrize("broken", ["no_timestamp_column", "a_none_close"])
def test_one_symbols_malformed_bars_never_stop_the_short_ledger(
    tmp_path, monkeypatch, broken
) -> None:
    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    frames = service.market_data.frames
    bad = frames["S00"].copy()  # S00 sorts first: every later run would hit it first
    if broken == "no_timestamp_column":
        bad = bad.drop(columns=["timestamp"])
    else:
        bad["close"] = bad["close"].astype(object)
        bad.loc[2, "close"] = None
    frames["S00"] = bad
    summary = shadow.refresh_short_shadow_ledger(service)
    assert summary["failed_symbols"] == ["S00"] and summary["scored_symbols"] == 2
    assert summary["done_for_today"]  # S00 is tried again tomorrow
    state = json.loads(service.runtime_state.get(shadow.SHORT_STATE_KEY))
    assert state["tried_symbols"] == ["S00", "S01", "S02"]
    entries = json.loads(service.runtime_state.get(shadow.SHORT_LEDGER_KEY))["entries"]
    assert entries["2026-10-12|S00|trend_following:1d|short"]["status"] == "pending"
    assert entries["2026-10-13|S01|trend_following:1d|short"]["status"] == "closed"
    assert entries["2026-10-12|S02|trend_following:1d|short"]["status"] == "open"


def test_a_rate_limit_cooldown_never_burns_the_short_list(tmp_path, monkeypatch) -> None:
    from app.broker.etoro_rate_limit import EToroRateLimitError

    service = _ledger_service(tmp_path, monkeypatch, now=FRIDAY_1700_NY)
    market = service.market_data
    real, cooldown = market.get_history, {"on": True}

    def get_history(symbol, **kwargs):
        if cooldown["on"]:
            raise EToroRateLimitError("eToro API temporarily rate-limited")
        return real(symbol, **kwargs)

    market.get_history = get_history
    paused = shadow.refresh_short_shadow_ledger(service)
    assert (paused["scored_symbols"], paused["failed_symbols"]) == (0, [])
    assert not paused["done_for_today"]
    assert json.loads(service.runtime_state.get(shadow.SHORT_STATE_KEY))["tried_symbols"] == []
    cooldown["on"] = False  # the next run, 30 minutes on
    after = shadow.refresh_short_shadow_ledger(service)
    assert after["scored_symbols"] == 3 and after["done_for_today"]


def test_a_failing_short_symbol_never_holds_up_either_ledger(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch)
    assert shadow.refresh_shadow_ledger(service)["done_for_today"]  # the long ledger, 10:00
    long_state = service.runtime_state.get(shadow.STATE_KEY)
    long_ledger = service.runtime_state.get(shadow.LEDGER_KEY)
    frames = service.market_data.frames
    frames["S02"] = None  # no data for the short-only symbol
    real = shadow._score_symbol

    def score(own, bars, today):
        if own[0]["symbol"] == "S01":
            raise RuntimeError("short boom")
        return real(own, bars, today)

    monkeypatch.setattr(shadow, "_score_symbol", score)
    monkeypatch.setattr(shadow, "utc_now", lambda: FRIDAY_1700_NY)
    summary = shadow.refresh_short_shadow_ledger(service)
    assert sorted(summary["failed_symbols"]) == ["S01", "S02"] and summary["scored_symbols"] == 1
    assert summary["done_for_today"]  # tried: the next day's run tries them again
    assert service.runtime_state.get(shadow.STATE_KEY) == long_state
    assert service.runtime_state.get(shadow.LEDGER_KEY) == long_ledger


def _maintenance(tmp_path):
    from tests.conftest import make_settings
    from tests.test_workflow_service import FakeLogs, FakeState

    return SimpleNamespace(
        settings=make_settings(tmp_path),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        market_screener=SimpleNamespace(),
        auto_trading=SimpleNamespace(),
        _is_due=lambda key, minutes: True,
    )


def test_maintenance_runs_the_short_ledger_after_the_long_one_in_isolation(
    tmp_path, monkeypatch
) -> None:
    from app.workflow.operations import refresh_phase_gates

    calls = []

    def boom(service):
        calls.append("short")
        raise RuntimeError("short boom")

    monkeypatch.setattr(shadow, "refresh_shadow_ledger", lambda service: calls.append("long") or {})
    monkeypatch.setattr(shadow, "refresh_short_shadow_ledger", boom)
    completed, errors = [], []
    refresh_phase_gates(_maintenance(tmp_path), completed, errors)
    assert calls == ["long", "short"] and "premarket_shadow_ledger" in completed
    assert "premarket_short_shadow_ledger" not in completed
    assert errors[-1] == "phase_gates_refresh:premarket_short_shadow_ledger:short boom"


def test_the_report_keeps_the_long_figures_and_adds_the_shorts_apart(tmp_path, monkeypatch) -> None:
    service = _ledger_service(tmp_path, monkeypatch)
    shadow.refresh_shadow_ledger(service)
    empty = shadow.shadow_ledger_report(service.runtime_state)["short"]
    assert (empty["signals"], empty["taken"]) == (0, 0)
    monkeypatch.setattr(shadow, "utc_now", lambda: FRIDAY_1700_NY)
    shadow.refresh_short_shadow_ledger(service)
    report = shadow.shadow_ledger_report(service.runtime_state)
    assert _digest({k: v for k, v in report.items() if k != "short"}) == GOLDEN_REPORT
    short = report["short"]
    assert (short["signals"], short["taken"], short["skipped_open"]) == (4, 3, 1)
    trend = short["strategies"]["trend_following:1d"]
    assert (trend["stop_target"]["closed"], trend["open"]) == (1, 2)
    assert trend["stop_target"]["win_rate"] == 0.0 and trend["backtest_expected_r"] == 0.2
    assert short["cohorts"]["tradeable"]["taken"] == 0 and short["rules"].startswith("short")
    assert all(row.get("side") == "short" for row in short["recent"])


@pytest.mark.parametrize(
    "ledger",
    [
        {"entries": {"x": {"status": "closed", "stop_target": {"status": "closed"}}}},  # no r
        {"entries": ["not", "a", "dict"]},
    ],
)
def test_a_bad_short_ledger_never_takes_the_long_report_down(tmp_path, monkeypatch, ledger) -> None:
    service = _ledger_service(tmp_path, monkeypatch)
    shadow.refresh_shadow_ledger(service)
    service.runtime_state.set(shadow.SHORT_LEDGER_KEY, json.dumps(ledger))
    report = shadow.shadow_ledger_report(service.runtime_state)
    assert _digest({k: v for k, v in report.items() if k != "short"}) == GOLDEN_REPORT
    assert report["short"]["error"] == "short_ledger_unreadable"


def test_a_gap_through_the_bracket_at_the_entry_open_is_scored_as_the_engine_does() -> None:
    # Like the long ledger and the backtest engine, the entry has no gap check: such an entry
    # is scored as is (a near-zero risk gives an extreme R) and shows in the report's mean_r.
    today = datetime(2026, 10, 14).date()
    above = _short()  # stop 105: the next open is just over it
    shadow.score_entry(
        above, ledger_tests._bars(SIGNAL, ("2026-10-12", 105.0525, 106, 104, 105)), today
    )
    fill, leg = _bid(105.0525), above["stop_target"]
    assert (leg["reason"], leg["exit_price"]) == ("stop", round(_ask(105.0525), 4))
    assert leg["r"] == round((fill - _ask(105.0525)) / abs(fill - 105), 3) < -100
    below = _short()  # target 90: the next open is under it
    shadow.score_entry(below, ledger_tests._bars(SIGNAL, ("2026-10-12", 89, 91, 88, 90)), today)
    fill, leg = _bid(89), below["stop_target"]
    assert (leg["reason"], leg["exit_price"]) == ("target", round(_ask(90), 4))
    assert leg["r"] == round((fill - _ask(90)) / (105 - fill), 3) < 0  # a losing "target"


def test_scorecard_endpoint_carries_the_short_ledger(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.storage.repositories import RuntimeStateRepository
    from tests.conftest import MockBroker, make_settings

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    entry = {**_short(), "status": "pending"}
    ledger = {"entries": {"2026-10-12|S00|trend_following:1d|short": entry}}
    RuntimeStateRepository(app.state.db).set(shadow.SHORT_LEDGER_KEY, json.dumps(ledger))
    body = (
        TestClient(app)
        .get("/performance/live-scorecard", headers={"X-Control-Token": "secret"})
        .json()
    )
    assert body["shadow_ledger"]["signals"] == 0  # the long ledger is empty
    assert body["shadow_ledger"]["short"]["signals"] == 1
    assert body["shadow_ledger"]["short"]["waiting_for_entry"] == 1
