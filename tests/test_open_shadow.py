"""Phase 3 shadow (operator 2026-10-18): best-first entries at the open on the completed bar.

The morning watchlist's top setups are re-checked on completed bars with the live build, the
entry is re-anchored to a fresh quote, the live gates and the eToro copy rules are re-checked
there, and the ranked result is logged. Shadow only: never a proposal, order, scan decision,
alert or Telegram message.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.broker.etoro_live_mirror as mirror
import app.performance.strategy_evidence as evidence
import app.workflow.open_shadow as shadow
import app.workflow.premarket_deep_scan as deep
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.live_signal_schema import MarketQuote
from app.performance.strategy_evidence import VERDICTS_KEY
from tests.conftest import make_settings
from tests.test_premarket_deep_scan import _Boom, _frame
from tests.test_workflow_service import FakeLogs, FakeState

DAY = "2026-10-12"  # a Monday: the completed bars end on Friday 10-09
DEEP_AT = datetime(2026, 10, 12, 12, 35, tzinfo=UTC)  # 08:35 EDT
OPEN_AT = datetime(2026, 10, 12, 13, 40, tzinfo=UTC)  # 09:40 EDT
RECORD = shadow.RECORD_KEY.format(date=DAY)
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
VERDICTS = {
    "alpha:1d": {"passed": True, "oos_expectancy_r": 0.05, "oos_trades": 300},
    "beta:1d": {"passed": True, "oos_expectancy_r": 0.02, "oos_trades": 200},  # below +0.03 R
}
SIGNALS = {  # (strategy, symbol): the daily signal on the completed bars
    ("alpha", "AAA"): {"price": 100.0, "stop": 95.0, "target": 112.0},
    ("alpha", "BBB"): {"price": 50.0, "stop": 48.0, "target": 55.0, "weak": True},
    ("beta", "CCC"): {"price": 20.0, "stop": 19.0, "target": 23.0},
    ("alpha", "DDD"): {"price": 200.0, "stop": 190.0, "target": 230.0},
    ("gamma", "EEE"): {"price": 10.0, "stop": 9.5, "target": 11.5},  # no verdict: never a pick
}


def _quote(price, *, spread_bps=2.0, age=1.0, source="alpaca", derived=False) -> MarketQuote:
    half = price * spread_bps / 20_000.0
    return MarketQuote(
        symbol="X",
        bid=price - half,
        ask=price + half,
        last_execution=price,
        timestamp="2026-10-12T13:40:00+00:00",
        source=source,
        data_age_seconds=age,
        quote_derived_from_history=derived,
    )


class _Strategy:
    def __init__(self, env, name):
        self.env, self.name = env, name

    def generate_signal(self, frame, symbol):
        self.env.seen_bars.append(str(frame["timestamp"].iloc[-1])[:10])
        spec = self.env.signals.get((self.name, symbol))
        if spec is None:
            return None
        metadata = {"weak_signal_kind": "pullback_watch"} if spec.get("weak") else {}
        return SimpleNamespace(
            action="buy",
            price=spec["price"],
            stop_loss=spec["stop"],
            take_profit=spec["target"],
            metadata=metadata,
        )


class _Screener:
    def __init__(self, env):
        self.env = env

    def _strategy_specs_for_timeframe(self, timeframe, strategy_spec_keys=None):
        return [SimpleNamespace(name=n, timeframe="1d") for n in self.env.strategies]

    def _build_strategy(self, spec):
        return _Strategy(self.env, spec.name)

    @staticmethod
    def _bars_for_timeframe(timeframe):
        return 400

    def scan_universe(self, **kwargs):
        raise AssertionError("the live scan must not run")


class _Market:
    def __init__(self, env):
        self.env = env
        self.history_calls: list[str] = []
        self.history_cache_writes: list[bool] = []
        self.quote_calls: list[tuple] = []

    def get_history(
        self, symbol, *, timeframe="1d", bars=250, force_refresh=False, write_cache=True
    ):
        self.history_calls.append(symbol)
        self.history_cache_writes.append(write_cache)
        self.env.mono["t"] += self.env.seconds_per_history
        failure = self.env.history_errors.get(symbol)
        if failure is not None:
            raise failure
        return _frame(1)  # Friday's completed bar plus Monday's forming bar

    def get_quote(
        self, symbol, *, timeframe="1d", provider=None, force_refresh=False, write_cache=True
    ):
        self.quote_calls.append((symbol, provider, force_refresh, write_cache))
        quote = self.env.quotes.get(symbol)
        if isinstance(quote, Exception):
            raise quote
        if quote is None:
            spec = next(s for (_, sym), s in self.env.signals.items() if sym == symbol)
            quote = _quote(round(spec["price"] * 1.001, 4))  # 10 bps above the signal
        return quote.model_copy(update={"symbol": symbol})


class _Env(SimpleNamespace):
    def run(self, *, started_at=None, deferred=()):
        start = self.mono["t"] if started_at is None else started_at
        return shadow.run_open_shadow(
            self.service, started_at=start, deferred=deferred, clock=lambda: self.mono["t"]
        )

    def at(self, moment):
        self.now["t"] = moment

    def record(self):
        return json.loads(self.service.runtime_state.get(RECORD) or "null")

    def events(self):
        return [event for event, _ in self.service.run_logs.items]

    def by_symbol(self):
        return {(r["symbol"], r["strategy"]): r for r in self.record()["results"]}


def _env(
    tmp_path, monkeypatch, *, signals=None, symbols=SYMBOLS, etoro=None, deep_scan=True, **overrides
):
    now = {"t": DEEP_AT}
    for module in (deep, shadow, evidence, mirror):
        monkeypatch.setattr(module, "utc_now", lambda: now["t"])
    values = dict(
        premarket_deep_scan_enabled=True,
        broker_for_equities="alpaca",
        etoro_live_mirror_enabled=True,
        market_universe_symbols=list(symbols),
        market_universe_limit=200,
        allowed_instruments=list(symbols),
    )
    values.update(overrides)
    env = _Env(
        now=now,
        mono={"t": 1000.0},
        seconds_per_history=0.0,
        history_errors={},
        quotes={},
        seen_bars=[],
        signals=dict(SIGNALS if signals is None else signals),
        strategies=["alpha", "beta", "gamma"],
    )
    state = FakeState()
    state.set(VERDICTS_KEY, json.dumps({"computed_at": DEEP_AT.isoformat(), "verdicts": VERDICTS}))
    account = {"day": DAY, "last_equity": 10_000.0, "last_cash": 6_000.0, "open_symbols": []}
    state.set(
        mirror.STATE_KEY, json.dumps({**account, "day_start_equity": 10_000.0, **(etoro or {})})
    )
    env.service = SimpleNamespace(
        settings=make_settings(tmp_path, **values),
        market_screener=_Screener(env),
        market_data=_Market(env),
        runtime_state=state,
        run_logs=FakeLogs(),
        proposal_service=None,
        reconciliation=None,
    )
    if deep_scan:
        deep.run_premarket_deep_scan(
            env.service, state_key="workflow:last_premarket_scan_at", force_refresh=True
        )
        assert deep.load_watchlist(state, DAY)["status"] == "complete"
    # From here on nothing may touch the trading side: proposals, orders, alerts, Telegram.
    env.service.proposal_service = env.service.auto_trading = _Boom()
    env.service.notifier = env.service.tracked_signals = env.service.alert_history = _Boom()
    env.service.run_logs = FakeLogs()
    env.service.market_data.history_calls.clear()
    env.service.market_data.history_cache_writes.clear()
    env.seen_bars.clear()
    env.at(OPEN_AT)
    state.set(shadow.MARKET_OPEN_KEY, datetime(2026, 10, 12, 13, 36, tzinfo=UTC).isoformat())
    return env


def test_best_first_entries_take_the_two_etoro_slots_and_the_rest_are_paper_only(
    tmp_path, monkeypatch
) -> None:
    env = _env(tmp_path, monkeypatch)
    keys_before = dict(env.service.runtime_state.values)
    summary = env.run()
    record = env.record()
    assert summary is not None and record["status"] == "complete"
    # Copyable strict (same edge, R:R at the quote decides), then copyable weak, then the rest.
    assert [(e["symbol"], e["slot"]) for e in record["entries"]] == [
        ("DDD", "etoro"),
        ("AAA", "etoro"),
        ("BBB", "paper_only"),
        ("CCC", "paper_only"),
    ]
    assert record["etoro_slots"] == ["DDD", "AAA"] and record["paper_only"] == ["BBB", "CCC"]
    assert record["drift_gated"]["etoro_slots"] == ["DDD", "AAA"]  # 10 bps: within 35 either way
    rows = env.by_symbol()
    assert ("EEE", "gamma") not in rows  # no evidence: never a candidate
    aaa = rows[("AAA", "alpha")]
    assert aaa["entry_at_quote"] == 100.1 and (aaa["stop"], aaa["target"]) == (95.0, 112.0)
    assert aaa["rr_at_quote"] == round((112 - 100.1) / (100.1 - 95), 3)
    assert aaa["entry_drift_bps"] == 10.0 and aaa["gate_reasons"] == []
    assert aaa["drift_from_signal_ok"] is True
    assert rows[("BBB", "alpha")]["etoro_room_hints"] == ["etoro_daily_slots_taken"]
    assert rows[("CCC", "beta")]["etoro_copy_reasons"] == ["strategy_edge_below_bar"]
    # The order path's quote: the equities broker as provider, always fresh. Neither request
    # stores what it fetched in the shared market-data cache (review 10-19).
    market = env.service.market_data
    assert {(p, f, w) for _, p, f, w in market.quote_calls} == {("alpaca", True, False)}
    assert market.history_cache_writes and set(market.history_cache_writes) == {False}
    assert env.events() == ["premarket_open_shadow_completed"]  # one run log
    logged = env.service.run_logs.items[0][1]
    assert logged["shadow_only"] is True and logged["etoro_slots"] == ["DDD", "AAA"]
    changed = {k for k, v in env.service.runtime_state.values.items() if keys_before.get(k) != v}
    assert changed == {RECORD}  # nothing but its own record
    assert env.run() is None and env.events() == ["premarket_open_shadow_completed"]  # done


def test_rank_strict_before_weak_only_among_the_etoro_copyable() -> None:
    rows = [  # symbol, copyable, kind, expectancy, rr at the quote
        ("A", True, "weak", 0.9, 3.0),
        ("B", True, "strict", 0.04, 1.5),
        ("C", True, "strict", 0.04, 2.5),
        ("D", False, "strict", 0.02, 2.0),
        ("E", False, "weak", 0.09, 1.0),
        ("F", True, "strict", None, 9.0),
    ]
    items = [
        {"symbol": s, "spec_key": "x", "etoro_copy_rules_ok": c, "kind": k, "oos_expectancy_r": e}
        | {"rr_at_quote": r}
        for s, c, k, e, r in rows
    ]
    ranked = [i["symbol"] for i in sorted(items, key=shadow.rank_key)]
    assert ranked == ["C", "B", "F", "A", "E", "D"]  # paper-only: expectancy, weak or strict


def test_a_setup_that_changed_since_the_deep_scan_is_skipped_and_logged(
    tmp_path, monkeypatch
) -> None:
    env = _env(tmp_path, monkeypatch)
    env.signals[("alpha", "AAA")] = {"price": 100.0, "stop": 96.0, "target": 112.0}
    env.signals[("alpha", "BBB")] = {"price": 50.0, "stop": 48.0, "target": 55.0}  # now strict
    del env.signals[("alpha", "DDD")]  # gone
    env.strategies.remove("beta")  # demoted since 08:30
    env.run()
    rows = env.by_symbol()
    assert rows[("AAA", "alpha")]["mismatch"] == ["stop"]
    assert rows[("BBB", "alpha")]["mismatch"] == ["kind"]
    assert rows[("DDD", "alpha")]["mismatch"] == ["no_buy_signal"]
    assert rows[("CCC", "beta")]["mismatch"] == ["spec_unavailable"]
    assert all(r["status"] == "mismatch" for r in rows.values())
    assert env.service.market_data.quote_calls == []  # a mismatch is never quoted
    record = env.record()
    assert record["status"] == "complete" and record["entries"] == []
    logged = env.service.run_logs.items[0][1]["mismatches"]
    assert {(m["symbol"], tuple(m["mismatch"])) for m in logged} == {
        ("AAA", ("stop",)),
        ("BBB", ("kind",)),
        ("DDD", ("no_buy_signal",)),
        ("CCC", ("spec_unavailable",)),
    }


def test_the_recheck_sees_completed_bars_only(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.run()
    assert env.seen_bars and set(env.seen_bars) == {"2026-10-09"}  # never Monday's forming bar
    assert all(r["status"] == "entered" for r in env.record()["results"])
    now_ny = OPEN_AT.astimezone(deep._zone(env.service.settings))
    raw = SimpleNamespace(get_history=lambda symbol, **kw: _frame(1), other=lambda: "same")
    adapter = shadow.CompletedBarsMarketData(raw, now_ny)
    assert (
        str(adapter.get_history("AAA", timeframe="1d")["timestamp"].iloc[-1])[:10] == "2026-10-09"
    )
    assert len(adapter.get_history("AAA", timeframe="5m")) == len(_frame(1))  # intraday untouched
    assert adapter.other() == "same"


def test_the_shadow_reads_the_shared_cache_but_never_feeds_a_live_scan(
    tmp_path, monkeypatch
) -> None:
    """Review 10-19: the live swing scan reads its 1d frames from the engine's cache
    (force_refresh=False), so a frame the shadow fetched must never be stored there. The real
    engine; only its provider requests and Alpaca quotes are fakes."""

    from app.data.engine import MarketDataEngine

    env = _env(
        tmp_path,
        monkeypatch,
        market_data_cache_dir=str(tmp_path / "cache"),
        market_data_retry_attempts=1,
        market_data_retry_backoff_seconds=0.0,
    )
    prices = {"AAA": 100.1, "BBB": 50.05, "DDD": 200.2}

    class _Alpaca:
        @staticmethod
        def get_quote(symbol):
            if symbol not in prices:  # CCC: the engine falls back to a quote from history
                raise RuntimeError("no quote")
            return _quote(prices[symbol]).model_copy(update={"symbol": symbol})

    engine = MarketDataEngine(env.service.settings, alpaca_provider=_Alpaca())
    fetches: list[str] = []

    def fetch(symbol, *, timeframe, bars, provider):
        fetches.append(symbol)
        return _frame(1)

    monkeypatch.setattr(engine, "_fetch_history_from_provider", fetch)
    env.service.market_data = engine
    engine.get_history("DDD", timeframe="1d", bars=400)  # a live scan's frame, stored as ever
    cache = tmp_path / "cache"
    assert fetches == ["DDD"] and [p.name for p in cache.rglob("*.csv")] == ["DDD.csv"]
    env.run()
    rows = env.by_symbol()
    assert env.record()["status"] == "complete"
    assert {k[0]: r["status"] for k, r in rows.items()} == {
        "AAA": "entered",
        "BBB": "entered",
        "DDD": "entered",
        "CCC": "gate_failed",  # its quote came from history
    }
    # DDD came from the live scan's fresh frame; CCC's history and its quote's history fetched.
    assert sorted(fetches[1:]) == ["AAA", "BBB", "CCC", "CCC"]
    assert [p.name for p in cache.rglob("*.csv")] == ["DDD.csv"]  # nothing of the shadow's
    # The live swing scan's own call a few minutes later fetches its own frame and stores it.
    frame = engine.get_history("AAA", timeframe="1d", bars=400, force_refresh=False)
    assert fetches[-1] == "AAA" and len(fetches) == 6 and frame.attrs["from_cache"] is False
    assert (cache / "yfinance" / "1d" / "AAA.csv").exists()


def test_a_bar_newer_or_older_than_the_signals_is_a_mismatch(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    watchlist = deep.load_watchlist(env.service.runtime_state, DAY)
    for item in watchlist["signals"]:
        item["signal_bar_ts"] = "2026-10-08 04:00:00+00:00"
    env.service.runtime_state.set(deep.WATCHLIST_KEY.format(date=DAY), json.dumps(watchlist))
    env.run()
    assert {tuple(r["mismatch"]) for r in env.record()["results"]} == {("signal_bar_changed",)}


@pytest.mark.parametrize(
    ("quote", "settings", "reasons"),
    [
        (_quote(100.1, age=500.0), {}, ["quote_too_old"]),
        (_quote(100.1, spread_bps=80.0), {}, ["spread_too_wide"]),
        (_quote(100.1, source="yfinance"), {}, ["quote_provider_not_alpaca"]),
        (_quote(100.1, source="alpaca", derived=True), {}, ["quote_not_direct"]),
        (_quote(106.0), {}, ["reward_to_risk_below_min_at_quote"]),
        (_quote(94.0), {}, ["stop_loss_not_below_entry"]),
        (_quote(113.0), {}, ["take_profit_not_above_entry"]),
    ],
)
def test_the_live_gates_at_the_quote(tmp_path, monkeypatch, quote, settings, reasons) -> None:
    env = _env(tmp_path, monkeypatch, **settings)
    env.quotes["AAA"] = quote
    env.run()
    row = env.by_symbol()[("AAA", "alpha")]
    assert row["status"] == "gate_failed" and row["gate_reasons"] == reasons
    assert "AAA" not in env.record()["etoro_slots"] + env.record()["paper_only"]
    assert env.record()["etoro_slots"] == ["DDD", "BBB"]  # the next copyable takes the slot


@pytest.mark.parametrize("gap_bps", [50, 100])
def test_a_normal_opening_gap_is_recorded_and_ranked_both_ways(
    tmp_path, monkeypatch, gap_bps
) -> None:
    # Every setup opens 50-100 bps above its signal's close. The order would be priced at the
    # quote (no drift at the coordinator's check), so the headline keeps them; drift_gated,
    # where the gap from the signal's price is the 35 bps drift gate, has none.
    env = _env(tmp_path, monkeypatch)
    for (_, symbol), spec in env.signals.items():
        env.quotes[symbol] = _quote(round(spec["price"] * (1 + gap_bps / 10_000.0), 4))
    env.run()
    record, rows = env.record(), env.by_symbol()
    assert all(r["status"] == "entered" and r["gate_reasons"] == [] for r in rows.values())
    assert {r["entry_drift_bps"] for r in rows.values()} == {float(gap_bps)}
    assert not any(r["drift_from_signal_ok"] for r in rows.values())
    assert record["etoro_slots"] == ["DDD", "AAA"] and record["paper_only"] == ["BBB", "CCC"]
    assert [e["entry_drift_bps"] for e in record["entries"]] == [float(gap_bps)] * 4
    assert record["drift_gated"] == {"entries": [], "etoro_slots": [], "paper_only": []}
    logged = env.service.run_logs.items[0][1]
    assert logged["etoro_slots"] == ["DDD", "AAA"] and logged["drift_gated_etoro_slots"] == []


def test_the_drift_gated_ranking_hands_out_its_own_slots(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.quotes["DDD"] = _quote(201.0)  # 50 bps: out of drift_gated only
    env.run()
    record = env.record()
    assert record["etoro_slots"] == ["DDD", "AAA"]
    gated = record["drift_gated"]
    assert gated["etoro_slots"] == ["AAA", "BBB"] and gated["paper_only"] == ["CCC"]
    ranked = [(e["rank"], e["symbol"]) for e in gated["entries"]]
    assert ranked == [(1, "AAA"), (2, "BBB"), (3, "CCC")]
    assert env.service.run_logs.items[0][1]["drift_gated_etoro_slots"] == ["AAA", "BBB"]
    rows = env.by_symbol()  # the rows keep the headline's ranks and slots
    assert (rows[("DDD", "alpha")]["rank"], rows[("BBB", "alpha")]["slot"]) == (1, "paper_only")


def test_a_quote_without_a_price_is_a_gate_failure(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.quotes["AAA"] = MarketQuote(symbol="AAA", source="alpaca", data_age_seconds=1.0)
    env.run()
    row = env.by_symbol()[("AAA", "alpha")]
    assert row["status"] == "gate_failed" and row["gate_reasons"] == ["quote_missing"]


def test_the_etoro_copy_rules_are_checked_at_the_quote(tmp_path, monkeypatch) -> None:
    # 7.9% below the signal's price, 8.3% below a quote 0.4% higher: the copy is refused there.
    signals = {
        ("alpha", "AAA"): {"price": 100.0, "stop": 92.1, "target": 130.0},
        ("alpha", "DDD"): {"price": 200.0, "stop": 190.0, "target": 230.0},
    }
    env = _env(tmp_path, monkeypatch, signals=signals)
    env.quotes["AAA"] = _quote(100.4)
    env.run()
    rows = env.by_symbol()
    aaa, ddd = rows[("AAA", "alpha")], rows[("DDD", "alpha")]
    assert aaa["status"] == "entered" and aaa["etoro_copy_reasons"] == ["etoro_live_stop_too_wide"]
    assert aaa["slot"] == "paper_only" and ddd["slot"] == "etoro"
    # Sized so the stop costs 0.5% of equity: $10,000 x 0.5% = $50 at the stop.
    stop_pct = (200.2 - 190.0) / 200.2 * 100.0
    assert ddd["etoro_amount_usd"] == round(10_000 * 0.5 / stop_pct, 2)
    assert ddd["etoro_risk_usd"] == pytest.approx(50.0, abs=0.01)


def test_a_copy_below_etoros_minimum_is_paper_only(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch, etoro={"last_equity": 50.0, "day_start_equity": 50.0})
    env.run()
    rows = env.by_symbol()
    assert rows[("DDD", "alpha")]["etoro_copy_reasons"] == ["etoro_live_trade_below_minimum"]
    assert env.record()["etoro_slots"] == []


def test_an_unknown_equity_is_an_account_hint_not_a_copy_rule(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch, etoro={"last_equity": None, "day_start_equity": None})
    env.service.runtime_state.set(mirror.HALTED_KEY, json.dumps({"reason": "test"}))
    env.run()
    record = env.record()
    assert record["etoro"]["account_hints"] == [
        "etoro_live_mirror_halted",
        "etoro_live_equity_unknown",
    ]
    assert record["etoro_slots"] == ["DDD", "AAA"]  # ranked as if the account could copy
    assert (
        env.service.run_logs.items[0][1]["etoro_account_hints"] == record["etoro"]["account_hints"]
    )


def test_room_hints_come_from_stored_state_without_the_copies_opened_that_day(
    tmp_path, monkeypatch
) -> None:
    tickers = ["NVDA", "AMD", "MSFT", "META", "GOOGL", "JPM"]
    signals = {
        ("alpha", "NVDA"): {"price": 100.0, "stop": 95.0, "target": 120.0},
        ("alpha", "GOOGL"): {"price": 100.0, "stop": 95.0, "target": 115.0},
        ("alpha", "JPM"): {"price": 100.0, "stop": 95.0, "target": 110.0},
    }
    account = {"open_symbols": ["NVDA", "AMD", "MSFT", "META"], "last_cash": 600.0}
    env = _env(tmp_path, monkeypatch, signals=signals, symbols=tickers, etoro=account)
    # The live bot copied META at 09:36: these entries would have taken its place.
    meta = {"symbol": "META", "status": "open", "amount_usd": 500.0}
    old = {"symbol": "AMD", "status": "open", "amount_usd": 500.0}
    cards = {
        "e1": {**meta, "opened_at": "2026-10-12T13:36:00+00:00"},
        "e0": {**old, "opened_at": "2026-10-09T15:00:00+00:00"},
    }
    env.service.runtime_state.set(SCORECARD_KEY, json.dumps(cards))
    env.run()
    rows, record = env.by_symbol(), env.record()
    assert record["etoro"]["live_copies_today"] == ["META"]
    assert record["etoro"]["open_symbols"] == ["AMD", "MSFT", "NVDA"]
    assert record["etoro"]["cash"] == 1_100.0  # META's $500 back
    assert rows[("NVDA", "alpha")]["etoro_room_hints"] == ["etoro_live_symbol_already_open"]
    assert rows[("NVDA", "alpha")]["slot"] == "paper_only"
    assert rows[("GOOGL", "alpha")]["slot"] == "etoro"  # tech 3 of 4 without META
    # GOOGL took the 4th tech place and $1,000 of the cash: JPM has no cash left.
    assert rows[("JPM", "alpha")]["etoro_room_hints"] == ["etoro_live_insufficient_cash"]
    assert record["etoro_slots"] == ["GOOGL"]


_ROOM_SIGNALS = {  # all strict and copyable; ranked by R:R at the quote: X1, X2, X3
    "X1": {"price": 100.0, "stop": 95.0, "target": 120.0},
    "X2": {"price": 100.0, "stop": 95.0, "target": 115.0},
    "X3": {"price": 100.0, "stop": 95.0, "target": 110.0},
}
_NOT_TECH = ["XOM", "CVX", "JPM", "BAC", "GS", "COST", "WMT", "UNH", "JNJ"]  # <= 3 per group


def _room_env(tmp_path, monkeypatch, tickers, held):
    picks = zip(tickers, _ROOM_SIGNALS, strict=False)
    signals = {("alpha", ticker): _ROOM_SIGNALS[x] for ticker, x in picks}
    return _env(
        tmp_path, monkeypatch, signals=signals, symbols=tickers, etoro={"open_symbols": held}
    )


@pytest.mark.parametrize(
    ("held", "slots", "capped"),
    [
        # 3 tech held: GOOGL takes the 4th tech place, so AAPL finds the group full.
        (["NVDA", "AMD", "MSFT"], ["GOOGL", "BAC"], ["AAPL"]),
        # 4 tech held before today: no tech setup fits.
        (["NVDA", "AMD", "MSFT", "META"], ["BAC"], ["GOOGL", "AAPL"]),
    ],
)
def test_the_correlation_group_cap_counts_the_slots_already_handed_out(
    tmp_path, monkeypatch, held, slots, capped
) -> None:
    env = _room_env(tmp_path, monkeypatch, ["GOOGL", "AAPL", "BAC"], held)
    env.run()
    rows, record = env.by_symbol(), env.record()
    assert record["etoro_slots"] == slots and record["paper_only"] == capped
    for symbol in capped:
        row = rows[(symbol, "alpha")]
        assert row["etoro_room_hints"] == ["etoro_live_bucket_cap"] and row["slot"] == "paper_only"


@pytest.mark.parametrize(
    ("held", "slots", "capped"),
    [
        (_NOT_TECH, [], ["NVDA", "AAPL"]),  # 9 open: no room at all
        (_NOT_TECH[:-1], ["NVDA"], ["AAPL"]),  # 8 open: NVDA takes the 9th place
    ],
)
def test_the_open_position_cap_counts_the_slots_already_handed_out(
    tmp_path, monkeypatch, held, slots, capped
) -> None:
    env = _room_env(tmp_path, monkeypatch, ["NVDA", "AAPL"], held)
    env.run()
    rows, record = env.by_symbol(), env.record()
    assert record["etoro_slots"] == slots and record["paper_only"] == capped
    for symbol in capped:
        assert rows[(symbol, "alpha")]["etoro_room_hints"] == ["etoro_live_open_position_cap"]


@pytest.mark.parametrize(
    ("etoro", "settings", "hints"),
    [
        ({"last_equity": 9_500.0}, {}, ["etoro_live_daily_loss_stop"]),  # 5% down on the day
        ({"last_equity": 9_501.0}, {}, []),
        ({}, {"etoro_live_mirror_enabled": False}, ["etoro_live_mirror_disabled"]),
    ],
)
def test_the_account_hints_are_hints_only(tmp_path, monkeypatch, etoro, settings, hints) -> None:
    env = _env(tmp_path, monkeypatch, etoro=etoro, **settings)
    env.run()
    record = env.record()
    assert record["etoro"]["account_hints"] == hints
    assert env.service.run_logs.items[0][1]["etoro_account_hints"] == hints
    assert record["etoro_slots"] == ["DDD", "AAA"]  # ranked as if the account could copy


def test_one_entry_per_symbol(tmp_path, monkeypatch) -> None:
    signals = {
        ("alpha", "AAA"): {"price": 100.0, "stop": 95.0, "target": 112.0},
        ("beta", "AAA"): {"price": 100.0, "stop": 95.0, "target": 120.0},
    }
    env = _env(tmp_path, monkeypatch, signals=signals)
    env.run()
    rows = env.by_symbol()
    assert rows[("AAA", "alpha")]["slot"] == "etoro"
    assert rows[("AAA", "beta")]["slot"] == "duplicate_symbol"
    assert [e["symbol"] for e in env.record()["entries"]] == ["AAA"]
    assert len(env.service.market_data.quote_calls) == 1  # one quote per symbol


def test_only_the_top_setups_are_checked(tmp_path, monkeypatch) -> None:
    symbols = [f"S{i:02d}" for i in range(14)]
    signals = {
        ("alpha", s): {"price": 100.0, "stop": 95.0, "target": 110.0 + i}
        for i, s in enumerate(symbols)
    }
    env = _env(tmp_path, monkeypatch, signals=signals, symbols=symbols)
    env.run()
    checked = [r["symbol"] for r in env.record()["results"]]
    assert checked == symbols[::-1][: shadow.TOP_ITEMS]  # best R:R first


def test_setups_the_live_bot_could_not_take_are_never_candidates(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch, deep_scan=False)
    item = {"strategy": "alpha", "spec_key": "alpha:1d", "kind": "strict", "allowed": True}
    item.update(entry=100.0, stop=95.0, target=110.0, signal_bar_ts="2026-10-09 04:00:00+00:00")
    signals = [
        {**item, "symbol": "AAA"},
        {**item, "symbol": "BBB", "allowed": False},
        {**item, "symbol": "CCC", "blacklisted": True},
        {**item, "symbol": "DDD", "held_paper": True},
        {**item, "symbol": "EEE", "pending_proposal": True},
        {**item, "symbol": "FFF", "stop": 101.0},  # not a bracket
        {**item, "symbol": "GGG", "strategy": "gamma"},  # no evidence
    ]
    watchlist = {"schema_version": 1, "session_date": DAY, "status": "complete", "signals": signals}
    env.service.runtime_state.set(deep.WATCHLIST_KEY.format(date=DAY), json.dumps(watchlist))
    env.run()
    assert [c["symbol"] for c in env.record()["candidates"]] == ["AAA"]


@pytest.mark.parametrize(
    ("edit", "status"),
    [
        (None, "no_watchlist"),
        ({"session_date": "2026-10-09"}, "watchlist_wrong_date"),
        ({"schema_version": 2}, "watchlist_unreadable"),
        ({"signals": "x"}, "watchlist_unreadable"),
        ({"status": "partial"}, "watchlist_not_final"),
        ({"status": "missed_cutoff"}, "watchlist_missed_cutoff"),
    ],
)
def test_a_wrong_watchlist_fails_closed(tmp_path, monkeypatch, edit, status) -> None:
    env = _env(tmp_path, monkeypatch)
    key = deep.WATCHLIST_KEY.format(date=DAY)
    if edit is None:
        env.service.runtime_state.values.pop(key)
    else:
        watchlist = deep.load_watchlist(env.service.runtime_state, DAY)
        env.service.runtime_state.set(key, json.dumps({**watchlist, **edit}))
    env.run()
    record = env.record()
    assert record["status"] == status and record["results"] == [] and record["candidates"] == []
    assert env.service.market_data.history_calls == env.service.market_data.quote_calls == []
    assert env.events() == ["premarket_open_shadow_completed"]
    env.service.runtime_state = _Boom()  # done for the day: no more reads
    assert env.run() is None and env.events() == ["premarket_open_shadow_completed"]


def test_it_waits_for_the_live_market_open_scan(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.runtime_state.set(shadow.MARKET_OPEN_KEY, "2026-10-09T13:36:00+00:00")  # Friday
    assert env.run() is None and env.record() is None and env.service.market_data.quote_calls == []
    env.service.runtime_state.set(shadow.MARKET_OPEN_KEY, "2026-10-12T13:41:00+00:00")
    env.run()
    assert env.record()["status"] == "complete"


def test_with_the_market_open_scan_off_it_does_not_wait(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch, market_open_scan_enabled=False)
    env.service.runtime_state.values.pop(shadow.MARKET_OPEN_KEY)
    env.run()
    assert env.record()["status"] == "complete"


class _CountingState(FakeState):
    def __init__(self, values):
        super().__init__()
        self.values, self.reads = dict(values), 0

    def get(self, key):
        self.reads += 1
        return super().get(key)


def test_it_never_runs_while_a_live_bucket_waits_or_without_budget(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.runtime_state = _CountingState(env.service.runtime_state.values)
    assert env.run(deferred=["swing_hourly"]) is None
    assert shadow.MIN_START_SECONDS == 55.0  # the cadence backstop's margin: setup + useful scan
    job = env.service.settings.scheduler_job_timeout_seconds
    late = env.mono["t"] - (job - 60.0 - 55.0 + 0.5)  # 54.5 s of scan budget left
    assert env.run(started_at=late) is None
    assert env.service.runtime_state.reads == 0 and env.service.market_data.quote_calls == []
    env.run(started_at=late + 1.0)  # 55.5 s left
    assert env.record()["status"] == "complete"


@pytest.mark.parametrize(
    "settings", [{"premarket_open_shadow_enabled": False}, {"premarket_deep_scan_enabled": False}]
)
def test_off_switches(tmp_path, monkeypatch, settings) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.settings = env.service.settings.model_copy(update=settings)
    env.service.runtime_state = _Boom()  # not one read (a read would log an error)
    assert env.run() is None and env.events() == []


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 12, 13, 34, tzinfo=UTC),  # 09:34 EDT, before the market-open scan
        datetime(2026, 10, 12, 20, 0, tzinfo=UTC),  # 16:00 EDT
        datetime(2026, 10, 10, 13, 40, tzinfo=UTC),  # a Saturday
    ],
)
def test_outside_its_window_it_reads_nothing(tmp_path, monkeypatch, moment) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.runtime_state = _Boom()
    env.at(moment)
    assert env.run() is None and env.events() == []


def test_a_tick_out_of_budget_saves_a_cursor_and_the_next_tick_resumes(
    tmp_path, monkeypatch
) -> None:
    env = _env(tmp_path, monkeypatch)
    env.seconds_per_history = 12.0  # each history request takes 12 s of the 30 s per tick
    assert env.run()["status"] == "running"
    partial = env.record()
    assert partial["status"] == "running" and 0 < partial["cursor"] < len(partial["candidates"])
    assert env.events() == []  # the run log comes once, when the day is done
    done_before = [r["symbol"] for r in partial["results"]]
    env.run()
    record = env.record()
    assert record["status"] == "complete" and record["ticks"] == 2
    assert [r["symbol"] for r in record["results"]][: len(done_before)] == done_before
    checked = [(r["symbol"], r["strategy"]) for r in record["results"]]
    assert len(checked) == len(set(checked)) == len(record["candidates"])  # each once
    # The one whose quote the first tick's budget cut off is fetched again, nothing else.
    calls = env.service.market_data.history_calls
    assert len(calls) == len(record["candidates"]) + 1 and calls.count(calls[len(done_before)]) == 2
    assert env.events() == ["premarket_open_shadow_completed"]


def test_a_fetch_cut_short_by_the_budget_is_retried_on_the_next_tick(tmp_path, monkeypatch) -> None:
    import app.screener.scan_support as support

    env = _env(tmp_path, monkeypatch)
    real = support._bounded_call

    def bounded(label, timeout, func, *args, **kwargs):
        if timeout < 20.0:
            raise support.ScanTimeoutError(f"{label}_timeout")
        return real(label, timeout, func, *args, **kwargs)

    monkeypatch.setattr(support, "_bounded_call", bounded)
    env.seconds_per_history = 12.0  # the quote after it has 18 s of the tick's 30: cut short
    env.run()
    record = env.record()
    assert record["status"] == "running" and record["cursor"] == 0 and record["results"] == []
    assert env.service.market_data.quote_calls == []
    env.seconds_per_history = 0.0
    env.run()
    assert env.record()["status"] == "complete"


def test_a_full_timeout_is_that_symbols_failure_only(tmp_path, monkeypatch) -> None:
    import app.screener.scan_support as support

    env = _env(tmp_path, monkeypatch)
    real = support._bounded_call

    def bounded(label, timeout, func, *args, **kwargs):
        if "AAA" in label:
            raise support.ScanTimeoutError(f"{label}_timeout_after_{timeout:g}s")
        return real(label, timeout, func, *args, **kwargs)

    monkeypatch.setattr(support, "_bounded_call", bounded)
    env.quotes["DDD"] = RuntimeError("quote feed down")
    env.run()
    rows = env.by_symbol()
    assert rows[("AAA", "alpha")]["status"] == "unavailable"
    assert rows[("AAA", "alpha")]["reason"].startswith("history: ")
    assert rows[("DDD", "alpha")]["reason"] == "quote: quote feed down"
    assert env.record()["etoro_slots"] == ["BBB"]


def test_an_etoro_rate_limit_pauses_until_the_next_tick(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.history_errors["DDD"] = EToroRateLimitError("cooldown")
    env.run()
    record = env.record()
    assert record["status"] == "running" and record["paused_reason"] == "etoro_rate_limit"
    assert record["cursor"] == 0  # DDD ranks first: nothing burned
    env.history_errors.clear()
    env.run()
    assert env.record()["status"] == "complete" and "paused_reason" not in env.record()


def test_at_the_deadline_an_open_record_is_closed_with_what_it_has(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.seconds_per_history = 12.0
    env.run()
    checked = env.record()["cursor"]
    calls = len(env.service.market_data.history_calls)
    env.at(datetime(2026, 10, 12, 14, 31, tzinfo=UTC))  # 10:31 EDT
    env.run()
    record = env.record()
    assert record["status"] == "incomplete_at_deadline" and len(record["results"]) == checked
    assert len(env.service.market_data.history_calls) == calls  # nothing new after 10:30
    assert record["entries"] and env.events() == ["premarket_open_shadow_completed"]


@pytest.mark.parametrize(
    ("open_done", "reason"), [(False, "market_open_scan_not_completed"), (True, "not_started")]
)
def test_a_day_that_never_started_is_recorded_after_the_deadline(
    tmp_path, monkeypatch, open_done, reason
) -> None:
    env = _env(tmp_path, monkeypatch)
    if not open_done:
        env.service.runtime_state.values.pop(shadow.MARKET_OPEN_KEY)
    env.at(datetime(2026, 10, 12, 15, 0, tzinfo=UTC))  # 11:00 EDT
    env.run()
    assert (env.record()["status"], env.record()["reason"]) == ("missed_deadline", reason)
    assert env.service.market_data.history_calls == []


def test_a_record_it_cannot_resume_starts_over(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.runtime_state.set(RECORD, json.dumps({"status": "running", "cursor": "x"}))
    env.run()
    assert env.record()["status"] == "complete" and env.record()["etoro_slots"] == ["DDD", "AAA"]


def test_a_day_another_run_closed_meanwhile_is_left_as_it_is(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    other = {"schema_version": 1, "session_date": DAY, "status": "complete", "run_id": "other"}
    quote = env.service.market_data.get_quote

    def quote_while_another_run_closes(symbol, **kwargs):
        env.service.runtime_state.set(RECORD, json.dumps(other))
        return quote(symbol, **kwargs)

    env.service.market_data.get_quote = quote_while_another_run_closes
    assert env.run() is None
    assert env.record() == other and env.events() == []  # not replaced, no second run log
    env.service.runtime_state = _Boom()
    assert env.run() is None and env.events() == []  # and done for the day


def test_it_never_raises_and_logs_a_failure_once_a_day(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    def refuse(key, value):
        raise RuntimeError("database down")

    env.service.runtime_state.set = refuse
    assert env.run() is None and env.run() is None
    assert env.events() == ["premarket_open_shadow_error"]
    env.service.run_logs.log = refuse  # even the log failing stays inside
    env.at(OPEN_AT + timedelta(days=1))
    assert env.run() is None


def test_the_record_is_strict_json(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    nan = {
        "alpha:1d": {"passed": True, "oos_expectancy_r": float("nan")},
        "beta:1d": VERDICTS["beta:1d"],
    }
    env.service.runtime_state.set(
        VERDICTS_KEY, json.dumps({"computed_at": DEEP_AT.isoformat(), "verdicts": nan})
    )
    env.run()

    def refuse(token):
        raise ValueError(token)

    record = json.loads(env.service.runtime_state.get(RECORD), parse_constant=refuse)
    assert record["status"] == "complete"


def test_the_cadence_tick_runs_it_last_and_passes_the_deferred_buckets(
    tmp_path, monkeypatch
) -> None:
    import app.workflow.service as workflow_service
    from tests.test_workflow_service import (
        FakeAlertHistory,
        FakeMarketDataEngine,
        FakeMarketScreener,
        FakeNotifier,
        FakeTrackedSignals,
    )

    calls: list = []
    monkeypatch.setattr(
        workflow_service,
        "run_open_shadow",
        lambda service, **kw: calls.append(("shadow", kw["deferred"], kw["started_at"])),
    )
    flow = workflow_service.SignalWorkflowService(
        settings=make_settings(tmp_path, screener_scheduler_enabled=True),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )
    monkeypatch.setattr(
        flow, "_bucket_due", lambda name: name in {"market_open_scan", "swing_hourly"}
    )

    def ran(name, **kwargs):
        calls.append(name)
        return SimpleNamespace(status="ok", alerts_sent=0)

    monkeypatch.setattr(flow, "run_bucket", ran)
    flow.run_scheduled_tasks()
    assert [c if isinstance(c, str) else c[0] for c in calls] == [
        "market_open_scan",
        "swing_hourly",
        "shadow",
    ]
    assert calls[-1][1] == [] and isinstance(calls[-1][2], float)
    calls.clear()
    budget = SimpleNamespace(
        admits=lambda name, settings: name != "swing_hourly", describe=lambda: {}
    )
    monkeypatch.setattr(
        workflow_service.CadenceBudget, "for_tick", lambda settings, started: budget
    )
    flow.run_scheduled_tasks()
    assert calls[0] == "market_open_scan" and calls[-1][1] == ["swing_hourly"]


def test_after_the_cadence_backstop_skipped_a_bucket_it_does_not_run(tmp_path, monkeypatch) -> None:
    # The swing scan is admitted with 58.5 s of scan budget left, its guard runs 1 s over its
    # 3 s estimate and run_scan_task's backstop skips it (it stays due for the next tick). It is
    # not in the deferred list, yet the shadow must not add its 30 s to that bucket's wait.
    import app.workflow.cadence_budget as cadence
    import app.workflow.service as workflow_service
    from tests.test_workflow_service import FakeAlertHistory, FakeNotifier, FakeTrackedSignals

    env = _env(tmp_path, monkeypatch, screener_scheduler_enabled=True)
    clock = {"t": 5_000.0}
    fake_time = SimpleNamespace(monotonic=lambda: clock["t"])
    monkeypatch.setattr(cadence, "time", fake_time)
    monkeypatch.setattr(workflow_service, "time", fake_time)
    shadow_tick, passed = workflow_service.run_open_shadow, []

    def run_shadow(service, **kwargs):
        passed.append(kwargs["deferred"])
        return shadow_tick(service, clock=lambda: clock["t"], **kwargs)

    monkeypatch.setattr(workflow_service, "run_open_shadow", run_shadow)
    flow = workflow_service.SignalWorkflowService(
        settings=env.service.settings,
        market_screener=env.service.market_screener,
        market_data_engine=env.service.market_data,
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=env.service.runtime_state,
        run_logs=env.service.run_logs,
    )
    scan_budget = env.service.settings.scheduler_job_timeout_seconds - 60.0

    def due(name):
        if name == "swing_hourly":
            clock["t"] += scan_budget - 58.5  # what ran before it in this tick
        return name == "swing_hourly"

    lock = flow._acquire_lock

    def slow_lock(task):
        clock["t"] += 4.0
        return lock(task)

    monkeypatch.setattr(flow, "_bucket_due", due)
    monkeypatch.setattr(flow, "_acquire_lock", slow_lock)
    flow.run_scheduled_tasks()
    assert "workflow_scan_budget_deferred" in env.events()  # the real backstop
    assert "workflow_cadence_deferred" not in env.events() and passed == [[]]
    assert env.record() is None and env.service.market_data.quote_calls == []
    clock["t"] += 600.0  # the next tick, with its whole budget: the shadow runs
    monkeypatch.setattr(flow, "_bucket_due", lambda name: False)
    flow.run_scheduled_tasks()
    assert env.record()["status"] == "complete"


def test_the_boot_snapshot_shows_the_switch(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy

    assert (
        effective_execution_policy(make_settings(tmp_path))["premarket_open_shadow_enabled"] is True
    )
    off = make_settings(tmp_path, premarket_open_shadow_enabled=False)
    assert effective_execution_policy(off)["premarket_open_shadow_enabled"] is False


def _table_rows(db_path) -> dict[str, list]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{t}"')) for t in tables}


def test_on_the_real_wiring_it_writes_only_its_record(tmp_path, monkeypatch) -> None:
    # The real app: the live screener's specs and builders, real repositories (SQLite).
    import sqlite3

    from app.main import create_app
    from tests.conftest import MockBroker
    from tests.test_premarket_deep_scan import _completed, _direct_buys, _no_network

    _no_network(monkeypatch)
    now = {"t": DEEP_AT}
    for module in (deep, shadow, evidence, mirror):
        monkeypatch.setattr(module, "utc_now", lambda: now["t"])
    symbols = ["S00", "S01", "S02"]
    settings = make_settings(
        tmp_path,
        premarket_deep_scan_enabled=True,
        market_universe_symbols=symbols,
        allowed_instruments=symbols,
        screener_scheduler_enabled=False,
        ledger_cycle_enabled=False,
        learning_worker_enabled=False,
        paper_position_refresh_enabled=False,
        execution_max_entry_drift_bps=1000.0,
    )
    app = create_app(settings, broker=MockBroker(), enable_background_jobs=False)
    engine = app.state.market_data_engine
    monkeypatch.setattr(engine, "get_history", lambda symbol, **kw: _frame(17))
    flow = app.state.workflow_service
    assert flow.run_premarket_scan(notify=False).status == "ok"
    watchlist = deep.load_watchlist(flow.runtime_state, DAY)
    names = sorted({i["strategy"] for i in watchlist["signals"]})
    assert names, "the frame gives daily buys, so the parity check is not vacuous"
    verdicts = {f"{n}:1d": {"passed": True, "oos_expectancy_r": 0.05} for n in names}
    flow.runtime_state.set(
        VERDICTS_KEY, json.dumps({"computed_at": DEEP_AT.isoformat(), "verdicts": verdicts})
    )
    flow.runtime_state.set(shadow.MARKET_OPEN_KEY, OPEN_AT.isoformat())
    close = float(_completed(17)["close"].iloc[-1])
    quote = MarketQuote(
        symbol="X", bid=close, ask=close, last_execution=close, source="etoro", data_age_seconds=1.0
    )
    monkeypatch.setattr(engine, "get_quote", lambda symbol, **kw: quote)
    now["t"] = OPEN_AT
    db_path = tmp_path / "test.db"
    before = _table_rows(db_path)
    with sqlite3.connect(db_path) as conn:
        last_log = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM run_logs").fetchone()[0]
        keys_before = {row[0] for row in conn.execute("SELECT state_key FROM runtime_state")}

    summary = shadow.run_open_shadow(flow, started_at=time.monotonic())

    assert summary is not None and summary["status"] == "complete", summary
    after = _table_rows(db_path)
    assert {t for t in after if after[t] != before.get(t)} <= {"runtime_state", "run_logs"}
    with sqlite3.connect(db_path) as conn:
        events = [
            r[0]
            for r in conn.execute("SELECT event_type FROM run_logs WHERE rowid > ?", (last_log,))
        ]
        new_keys = {r[0] for r in conn.execute("SELECT state_key FROM runtime_state")} - keys_before
    assert events == ["premarket_open_shadow_completed"] and new_keys == {RECORD}
    record = json.loads(flow.runtime_state.get(RECORD))
    assert record["candidates"] and summary["mismatches"] == []  # the live build agrees
    assert all(r["status"] in {"entered", "gate_failed"} for r in record["results"])
    screener = app.state.market_screener_service
    specs = [s for s in screener._strategy_specs_for_timeframe("1d") if s.name != "pairs_stat_arb"]
    direct = _direct_buys(_completed(17), specs, build=screener._build_strategy)
    assert {r["strategy"] for r in record["results"] if r["symbol"] == "S00"} <= set(direct)


def test_the_live_scorecard_shows_phase3_next_to_the_live_entries(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models.execution import ExecutionRecord
    from app.storage.repositories import ExecutionRepository, RuntimeStateRepository
    from tests.conftest import MockBroker

    monkeypatch.setattr(shadow, "utc_now", lambda: datetime(2026, 10, 13, 15, 0, tzinfo=UTC))
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    state = RuntimeStateRepository(app.state.db)
    client = TestClient(app)
    assert client.get("/performance/live-scorecard").json()["open_shadow"]["days"] == []
    entries = [
        {"symbol": "AAA", "strategy": "alpha", "slot": "etoro"},
        {"symbol": "DDD", "strategy": "alpha", "slot": "paper_only"},
    ]
    record = {"session_date": DAY, "status": "complete", "entries": entries}
    record.update(etoro_slots=["AAA"], paper_only=["DDD"], results=[{"status": "mismatch"}])
    record["drift_gated"] = {"entries": entries[1:], "etoro_slots": ["DDD"], "paper_only": []}
    state.set(RECORD, json.dumps(record))
    executions = ExecutionRepository(app.state.db)
    for symbol, at, status, side in (
        ("AAA", "2026-10-12T13:37:00+00:00", "submitted", "buy"),
        ("MSFT", "2026-10-12T17:00:00+00:00", "submitted", "buy"),
        ("NVDA", "2026-10-12T18:00:00+00:00", "failed", "buy"),  # not an entry
        ("COST", "2026-10-12T19:00:00+00:00", "submitted", "sell"),  # an exit, not an entry
        ("JPM", "2026-10-13T03:00:00+00:00", "submitted", "buy"),  # 23:00 New York: still 10-12
        ("XOM", "2026-10-13T14:00:00+00:00", "submitted", "buy"),  # the next day
    ):
        order = {
            "symbol": symbol,
            "side": side,
            "strategy_name": "live",
            "metadata": {"timeframe": "1d"},
        }
        executions.create(
            ExecutionRecord(
                proposal_id=f"p-{symbol}", status=status, mode="paper", request_payload=order
            ).model_copy(update={"created_at": at})
        )
    cards = {
        "e1": {"symbol": "COST", "strategy_name": "x", "opened_at": "2026-10-12T14:00:00+00:00"}
    }
    state.set(SCORECARD_KEY, json.dumps(cards))
    day = client.get("/performance/live-scorecard").json()["open_shadow"]["days"][0]
    assert day["session_date"] == DAY and day["phase3_etoro"] == ["AAA"]
    assert day["phase3_drift_gated"] == {"etoro_slots": ["DDD"], "paper_only": []}
    assert [e["symbol"] for e in day["live_paper_entries"]] == ["AAA", "MSFT", "JPM"]
    assert [c["symbol"] for c in day["live_etoro_entries"]] == ["COST"]
    assert day["same_symbols"] == ["AAA"] and day["phase3_only"] == ["DDD"]
    assert day["live_only"] == ["COST", "JPM", "MSFT"] and day["mismatches"] == 1
    state.set(RECORD, "not json")  # a bad record is skipped, the page still answers
    assert client.get("/performance/live-scorecard").json()["open_shadow"]["days"] == []
