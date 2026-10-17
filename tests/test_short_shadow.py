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
