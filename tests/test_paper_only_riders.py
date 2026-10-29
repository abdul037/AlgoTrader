"""Paper-only riders (option B): app/screener/scan_riders.py and app/workflow/paper_only_riders.py.

* the run's own scan is the same with its bars kept: candidates, decisions, reads, database
  transactions and deadline behaviour; while it runs every read passes through, and only the
  (symbol, timeframe) pairs it evaluated are kept;
* riders run only after the run's own alerts, tracking and auto-propose pass, on the kept bars
  (no fetch, no database write), each guarded; a data-fetching strategy never rides;
* at most two queued rider signals get the full candidate checks (never a crypto, held, in-flight
  or proposed symbol, one a symbol, the next best once one was checked), only while the run's
  deadline leaves time for them, only when paper has room; their candidates get their own pass;
* a rider's error is counted, never raised: the run's candidates and proposals stand.

Deterministic: in-memory bars, stub strategies, controlled filters and scores, patched clocks.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.screener.scan_riders as scan_riders
import app.screener.service_scan as service_scan
import app.workflow.paper_only_riders as paper_only_riders
from app.live_signal_schema import MarketQuote
from app.models.signal import Signal, SignalAction
from app.performance.strategy_evidence import VERDICTS_KEY
from app.screener.filters import FilterOutcome
from app.screener.scan_riders import KeptBars, rider_signals
from app.screener.service import MarketScreenerService
from app.workflow.paper_only import PROPOSAL_SECONDS
from app.workflow.paper_only_riders import RIDER_CHECK_SECONDS, RIDER_CHECKS
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings
from tests.test_paper_only_pass import BOOK, EXECUTED, FULL, OOS_ONLY, _auto, _Broker
from tests.test_screener_service import (
    FakeBacktestRepository,
    FakeMarketDataEngine,
    FakeRunLogRepository,
    FakeScanDecisionRepository,
    FakeSignalStateRepository,
    FakeTelegramNotifier,
    _frame,
)
from tests.test_workflow_service import (
    FakeAlertHistory,
    FakeLogs,
    FakeNotifier,
    FakeProposalService,
    FakeTrackedSignals,
)


@pytest.fixture(autouse=True)
def _memo(monkeypatch):
    monkeypatch.setattr(paper_only_riders, "_CHECKED", {})  # the in-process checked memo


SYMBOLS = ["AAA", "BBB", "CCC"]
VERDICTS = {  # the run's own keys pass the full check; the riders' pass paper's OOS bar alone
    "trend_following:1h": FULL,
    "trend_following:1d": FULL,
    "momentum_breakout:1h": OOS_ONLY,
    "momentum_breakout:1d": OOS_ONLY,
}
AUTOMATIC = dict(
    require_strategy_oos_evidence=True,
    auto_propose_enabled=True,
    paper_auto_approve_proposals=True,
    paper_auto_operation_mode="unattended",
)


class _Engine(FakeMarketDataEngine):
    """Every symbol's 1d/1h/1w bars and quote in memory (and the benchmarks'); reads recorded."""

    def __init__(self):
        frame = _frame([100 + index * 0.2 for index in range(130)])
        names = [*SYMBOLS, "SPY", "QQQ", "IWM"]
        frames = {(name, tf): frame for name in names for tf in ("1d", "1h", "1w")}
        quotes = {
            name: MarketQuote(
                symbol=name, bid=125.9, ask=126.0, last_execution=125.95, timestamp="2026-04-11T10:00:00Z"
            )
            for name in names
        }
        super().__init__(frames, quotes)
        self.reads: list[tuple] = []

    def get_history(self, symbol, **kwargs):
        self.reads.append(("bars", symbol.upper(), kwargs.get("timeframe"), kwargs.get("bars")))
        return super().get_history(symbol, **kwargs)

    def get_quote(self, symbol, **kwargs):
        self.reads.append(("quote", symbol.upper()))
        return super().get_quote(symbol, **kwargs)


class _Stub:
    """A strategy: records each call; fires on ``fire`` symbols, raises on ``boom`` ones."""

    def __init__(self, spec, events, fire, boom=(), clock=None):
        self.name, self.timeframe = spec.name, spec.timeframe
        self.events, self.fire, self.boom, self.clock = events, set(fire), set(boom), clock

    last_diagnostics = None

    def generate_signal(self, _history, symbol):
        self.events.append(("eval", symbol, self.timeframe, self.name))
        if self.clock is not None:
            self.clock.now += 1.0
        if symbol in self.boom:
            raise RuntimeError(f"{self.name} {symbol}")
        if symbol not in self.fire:
            return None
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            confidence=0.9 if symbol == "CCC" else 0.8,
            price=125.95,
            stop_loss=120.0,
            take_profit=140.0,
            rationale="rider fixture",
            metadata={"signal_role": "entry_long", "risk_reward_ratio": 2.5},
        )


def _screener(tmp_path, monkeypatch, events, *, fire=None, boom=None, clock=None, **settings):
    """A real screener on in-memory bars; every signal passes the filters and scores 80."""

    engine = _Engine()
    screener = MarketScreenerService(
        settings=make_settings(tmp_path, market_universe_symbols=SYMBOLS, **settings),
        market_data_engine=engine,
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=None),
        scan_decision_repository=FakeScanDecisionRepository(),
        telegram_notifier=FakeTelegramNotifier(),
    )
    fire = fire or {}
    boom = boom or {}
    monkeypatch.setattr(
        screener,
        "_build_strategy",
        lambda spec: _Stub(spec, events, fire.get(spec.name, ()), boom.get(spec.name, ()), clock),
    )
    passed = FilterOutcome(passed=True, pass_reasons=["ok"], rejection_reasons=[], reason_codes=[])
    monkeypatch.setattr(screener.filters, "evaluate", lambda **_k: passed)
    monkeypatch.setattr(
        service_scan,
        "rank_live_signal",
        lambda **_k: {
            "final_score": 80.0,
            "score_breakdown": {},
            "confidence_label": "high",
            "direction_label": "buy",
            "actionability": "alert",
        },
    )
    return screener, engine


def _project(response):
    return [
        (c.rank, c.symbol, c.strategy_name, c.timeframe, c.score, bool(c.metadata.get("alert_eligible")))
        for c in response.candidates
    ]


# --- the run's own scan, with its bars kept ---------------------------------------------------


def test_kept_bars_pass_through_while_the_scan_runs_and_replay_only_what_it_kept() -> None:
    engine = _Engine()
    kept = KeptBars(engine)
    first = kept.get_history("aaa", timeframe="1d", bars=400, force_refresh=False)
    kept.get_history("AAA", timeframe="1d", bars=400, force_refresh=False)
    quote = kept.get_quote("AAA", timeframe="1d", force_refresh=False)
    kept.get_quote("AAA", timeframe="1d", force_refresh=False)
    assert engine.reads == [("bars", "AAA", "1d", 400)] * 2 + [("quote", "AAA")] * 2
    assert kept.bars == {}  # a read alone keeps nothing: a late one cannot be served
    kept.keep("aaa", "1D", first, quote)  # the scan evaluated AAA on 1d with these
    assert kept.get_history("AAA", timeframe="1d") is not first  # still passing through
    kept.replay = True
    reads = len(engine.reads)
    assert kept.get_history("AAA", timeframe="1d", bars=400, force_refresh=False) is first
    assert kept.get_quote("AAA", timeframe="1d", force_refresh=False) is quote
    with pytest.raises(KeyError):
        kept.get_quote("AAA", timeframe="1h")  # never kept: never fetched either
    with pytest.raises(KeyError):
        kept.get_history("BBB", timeframe="1d")
    assert len(engine.reads) == reads


ALL_PAIRS = {(s, tf) for s in SYMBOLS for tf in ("1h", "1d")}


@pytest.mark.parametrize(
    ("deadline", "read_seconds", "evaluated"),
    [
        (0.0, 0.0, ALL_PAIRS),  # BBB's spec raised, but it ran: kept
        (5.0, 0.0, ALL_PAIRS - {("CCC", "1d")}),  # stops before CCC's 1d bars are read
        (4.5, 1.0, {("AAA", "1h"), ("AAA", "1d")}),  # BBB's 1h bars read, its spec never ran
    ],
)
def test_the_scan_is_the_same_with_its_bars_kept(
    tmp_path, monkeypatch, deadline, read_seconds, evaluated
) -> None:
    holder = {}

    def scan(kept):
        events: list[tuple] = []
        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(service_scan.time, "monotonic", lambda: clock.now)
        screener, engine = _screener(
            tmp_path,
            monkeypatch,
            events,
            fire={"trend_following": {"AAA", "CCC"}},
            boom={"trend_following": {"BBB"}},
            clock=clock,
            screener_batch_deadline_seconds=deadline,
        )
        read = engine.get_history

        def slow_read(symbol, **kwargs):
            if kwargs.get("bars") in {320, 400}:  # the scan's own 1h/1d bars, not intelligence's
                clock.now += read_seconds
            return read(symbol, **kwargs)

        monkeypatch.setattr(engine, "get_history", slow_read)
        holder["kept"] = KeptBars(screener.market_data) if kept else None
        response = screener.scan_universe(
            symbols=SYMBOLS,
            timeframes=["1h", "1d"],
            scan_task="swing_scan",
            strategy_spec_keys=["trend_following:1h", "trend_following:1d"],
            kept_bars=holder["kept"],
        )
        decisions = [
            (d.symbol, d.strategy_name, d.timeframe, d.status) for d in screener.scan_decisions.items
        ]
        coverage = {k: v for k, v in response.coverage.items()}
        return (
            _project(response), response.errors, coverage, decisions, events, engine.reads,
            response.evaluated_symbols,
        )

    alone, kept = scan(False), scan(True)
    assert kept == alone
    assert bool(deadline) is ("scan_deadline_exceeded" in alone[1])
    assert set(holder["kept"].bars) == evaluated  # only where every spec of the run ran


def test_the_scan_makes_the_same_database_transactions_with_its_bars_kept(tmp_path, monkeypatch) -> None:
    from app.storage.db import Database
    from app.storage.repositories import (
        BacktestRepository,
        RunLogRepository,
        ScanDecisionRepository,
        SignalStateRepository,
    )

    def run(kept):
        settings = make_settings(Path(tempfile.mkdtemp(dir=tmp_path)), market_universe_symbols=SYMBOLS)
        db = Database(settings)
        db.initialize()
        count = Counter()
        connect = db.connect
        monkeypatch.setattr(db, "connect", lambda: count.update(["tx"]) or connect())
        events: list[tuple] = []
        screener = MarketScreenerService(
            settings=settings,
            market_data_engine=_Engine(),
            signal_state_repository=SignalStateRepository(db),
            run_log_repository=RunLogRepository(db),
            backtest_repository=BacktestRepository(db),
            scan_decision_repository=ScanDecisionRepository(db),
            telegram_notifier=FakeTelegramNotifier(),
        )
        fire = {"trend_following": SYMBOLS, "momentum_breakout": SYMBOLS}  # every rider fires
        monkeypatch.setattr(
            screener, "_build_strategy", lambda spec: _Stub(spec, events, fire.get(spec.name, ()))
        )
        bars = KeptBars(screener.market_data) if kept else None
        count.clear()
        screener.scan_universe(
            symbols=SYMBOLS,
            timeframes=["1d"],
            scan_task="swing_scan",
            strategy_spec_keys=["trend_following:1d"],
            kept_bars=bars,
        )
        scan = count["tx"]
        if bars is None:
            return scan, None
        count.clear()
        riders = rider_signals(screener, bars, ["momentum_breakout:1d"], stop_at=float("inf"))
        assert len(riders.fired) == 3
        return scan, count["tx"]

    alone, (kept, riding) = run(False)[0], run(True)
    assert kept == alone > 0  # the run's scan: not one transaction more
    assert riding == 0  # the riders' signals: none at all


def test_a_rider_check_bounds_its_intelligence_step_and_reads_only_the_kept_bars(
    tmp_path, monkeypatch
) -> None:
    events: list[tuple] = []
    screener, engine = _screener(
        tmp_path, monkeypatch, events, fire={"trend_following": SYMBOLS, "momentum_breakout": SYMBOLS}
    )
    bounds: list[tuple[str, float]] = []
    bounded = service_scan._bounded_call

    def record(label, timeout, *args, **kwargs):
        bounds.append((label.rsplit("_", 1)[-1], timeout))
        return bounded(label, timeout, *args, **kwargs)

    monkeypatch.setattr(service_scan, "_bounded_call", record)
    kept = KeptBars(screener.market_data)
    screener.scan_universe(
        symbols=SYMBOLS, timeframes=["1d"], strategy_spec_keys=["trend_following:1d"], kept_bars=kept
    )
    assert {t for kind, t in bounds if kind == "intelligence"} == {20.0}  # the scan's own bound
    bounds.clear()
    before = len(engine.reads)
    kept.replay = True
    kept.intelligence_timeout = paper_only_riders.RIDER_INTELLIGENCE_SECONDS
    checked = screener.scan_universe(
        symbols=["BBB"], timeframes=["1d"], strategy_spec_keys=["momentum_breakout:1d"], kept_bars=kept
    )
    assert [c.strategy_name for c in checked.candidates] == ["momentum_breakout"]
    assert ("intelligence", 5.0) in bounds and ("intelligence", 20.0) not in bounds
    check_reads = engine.reads[before:]  # only its intelligence step's (benchmarks, 1w, 1h)
    assert ("bars", "BBB", "1d", 400) not in check_reads and ("quote", "BBB") not in check_reads


# --- the riders' signals ---------------------------------------------------------------------


def test_riders_run_on_the_kept_bars_alone_guarded_and_bounded(tmp_path, monkeypatch) -> None:
    events: list[tuple] = []
    screener, engine = _screener(
        tmp_path,
        monkeypatch,
        events,
        fire={"momentum_breakout": {"AAA", "CCC"}, "pairs_stat_arb": SYMBOLS},
        boom={"momentum_breakout": {"BBB"}},
    )
    kept = KeptBars(screener.market_data)
    screener.scan_universe(
        symbols=SYMBOLS, timeframes=["1d"], strategy_spec_keys=["trend_following:1d"], kept_bars=kept
    )
    reads, events[:] = list(engine.reads), []
    build = screener._build_strategy

    class _Truncates:  # a rider that empties the frame it was given
        last_diagnostics = None

        def generate_signal(self, history, _symbol):
            history.drop(history.index, inplace=True)

    def built(spec):
        if spec.name == "mean_reversion":
            raise RuntimeError("cannot build")  # a built-in that cannot build: counted, skipped
        if spec.name == "ema_trend_stack":
            return _Truncates()
        strategy = build(spec)
        if spec.name == "pairs_stat_arb":  # its hedge leg comes from a provider: it would fetch
            strategy.set_hedge_provider = lambda _provider: None
        return strategy

    monkeypatch.setattr(screener, "_build_strategy", built)
    keys = [
        "momentum_breakout:1d",
        "pairs_stat_arb:1d",
        "mean_reversion:1d",
        "ema_trend_stack:1d",
        "momentum_breakout:1h",  # no 1h bars were kept: never evaluated
    ]
    kept.keep("DDD", "1d", kept.bars[("AAA", "1d")][0], None)  # bars without a quote: never
    lengths = {key: len(bars[0]) for key, bars in kept.bars.items()}
    riders = rider_signals(screener, kept, keys, stop_at=float("inf"))
    assert engine.reads == reads  # nothing fetched
    assert events == [("eval", s, "1d", "momentum_breakout") for s in SYMBOLS]
    assert {key: len(bars[0]) for key, bars in kept.bars.items()} == lengths  # copies only
    assert [(f.symbol, f.key) for f in riders.fired] == [
        ("AAA", "momentum_breakout:1d"),
        ("CCC", "momentum_breakout:1d"),
    ]
    assert (riders.excluded, riders.errors, riders.stopped) == (1, 2, False)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(scan_riders.time, "monotonic", lambda: clock.now)
    events.clear()
    stopped = rider_signals(screener, kept, keys, stop_at=100.0)
    assert stopped.stopped and stopped.fired == [] and events == []


# --- the run, end to end ---------------------------------------------------------------------


def _workflow(
    tmp_path, monkeypatch, *, riders=True, fire=None, boom=None, clock=None, deadline=None, **settings
):
    events: list[tuple] = []
    if clock is not None:  # one clock for the scan, the riders and their step
        monkeypatch.setattr(service_scan.time, "monotonic", lambda: clock.now)
    if deadline is not None:  # the scan's and the run's batch deadline
        settings["screener_batch_deadline_seconds"] = deadline
    screener, engine = _screener(
        tmp_path / "screener", monkeypatch, events, fire=fire, boom=boom, clock=clock,
        **({} if deadline is None else {"screener_batch_deadline_seconds": deadline}),
    )
    auto, state = _auto(tmp_path / "auto", verdicts=VERDICTS)
    auto.runtime_state = state
    auto.approve_enqueue_execute = (
        lambda proposal, candidate, **_k: events.append(
            ("execute", candidate.symbol, candidate.strategy_name)
        )
        or EXECUTED
    )
    proposals = FakeProposalService()
    create = proposals.create_proposal

    def create_proposal(request):
        events.append(("propose", request.symbol, request.strategy_name, bool(request.metadata.get("paper_only"))))
        return create(request)

    proposals.create_proposal = create_proposal
    values = dict(
        AUTOMATIC,
        screener_spec_coverage_mode="scheduled_all",  # the swing run asks for 1h and 1d
        market_universe_symbols=SYMBOLS,
        allowed_instruments=SYMBOLS,
        paper_oos_bar_enabled=riders,
    )
    values.update(settings)
    workflow_settings = make_settings(tmp_path / "workflow", **values)
    auto.settings = workflow_settings
    workflow = SignalWorkflowService(
        settings=workflow_settings,
        market_screener=screener,
        market_data_engine=engine,
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
        proposal_service=proposals,
        auto_trading_service=auto,
    )
    return SimpleNamespace(workflow=workflow, events=events, state=state, auto=auto, proposals=proposals)


RIDERS_FIRE = {"trend_following": {"AAA", "BBB"}, "momentum_breakout": SYMBOLS}


def _own(events):
    """The run's own events: its specs' evaluations, its proposals and executions."""

    return [e for e in events if "momentum_breakout" not in e]


def _plain(coverage):
    """coverage.paper_only without its timings (wall clock)."""

    out = {k: v for k, v in coverage.items() if k not in {"seconds", "seconds_left"}}
    if "checks" in out:
        out["checks"] = [{k: v for k, v in c.items() if k != "seconds"} for c in out["checks"]]
    return out


def _check(key, symbol, outcome="candidate"):
    return {"key": key, "symbol": symbol, "timeframe": key.rsplit(":", 1)[-1], "outcome": outcome}


def test_a_fired_rider_never_delays_an_etoro_spec_or_proposal(tmp_path, monkeypatch) -> None:
    alone = _workflow(tmp_path / "alone", monkeypatch, riders=False, fire=RIDERS_FIRE)
    alone.workflow.run_swing_scan(notify=False)
    run = _workflow(tmp_path / "ride", monkeypatch, fire=RIDERS_FIRE)
    bounds: list[tuple[str, float]] = []
    bounded = service_scan._bounded_call

    def record(label, timeout, *args, **kwargs):
        if label.endswith("_intelligence"):
            bounds.append((label.split("_")[2], timeout))
        return bounded(label, timeout, *args, **kwargs)

    monkeypatch.setattr(service_scan, "_bounded_call", record)
    result = run.workflow.run_swing_scan(notify=False)
    assert result.status == "ok"
    first_rider = next(i for i, e in enumerate(run.events) if "momentum_breakout" in e)
    assert run.events[:first_rider] == alone.events  # every own spec, proposal and execution first
    assert _own(run.events) == _own(alone.events)
    own_evals = [e for e in alone.events if e[0] == "eval"]
    assert {(e[1], e[2]) for e in own_evals} == {(s, tf) for s in SYMBOLS for tf in ("1h", "1d")}
    riders = run.events[first_rider:]
    assert riders[: 2 * len(SYMBOLS)] == [
        ("eval", s, tf, "momentum_breakout") for s in SYMBOLS for tf in ("1h", "1d")
    ]  # on the kept bars, each symbol's timeframes in the scan's order
    checks = [e[1:3] for e in riders[2 * len(SYMBOLS):] if e[0] == "eval"]
    assert checks == [("CCC", "1h"), ("AAA", "1h")]  # best first (0.9), one a symbol, two
    assert len(checks) == RIDER_CHECKS
    proposed = [e for e in riders if e[0] == "propose"]
    assert proposed == [("propose", "CCC", "momentum_breakout", True)]  # tagged, one a pass
    assert run.auto.executions.paper_reads == (2, 2)  # room's read, then the pass's fresh one
    coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])
    assert coverage["paper_only"]["seconds_left"] > RIDER_CHECK_SECONDS
    assert _plain(coverage["paper_only"]) == {
        "specs": 2,
        "excluded": 0,
        "signals_fired": 6,
        "queued": 3,
        "checked": 2,
        "candidates": 2,
        "errors": 0,
        "checks": [_check("momentum_breakout:1h", "CCC"), _check("momentum_breakout:1h", "AAA")],
        "proposals_created": 1,
    }
    assert coverage["requested_spec_keys"] == ["trend_following:1d", "trend_following:1h"]
    assert run.state["workflow:last_swing_scan_at"]  # stamped by the run, before its riders
    assert {(name, t) for name, t in bounds} == {("trend", 20.0), ("momentum", 5.0)}
    workflow = run.workflow
    created = [p["origin"] for e, p in workflow.run_logs.items if e == "auto_proposal_created"]
    assert created == ["swing_scan", "swing_scan", "swing_scan:paper_only"]
    tracked = {(t.symbol, t.strategy_name): t.origin for t in workflow.tracked_signals.items}
    assert tracked[("CCC", "momentum_breakout")] == "swing_scan:paper_only"
    assert tracked[("AAA", "trend_following")] == "swing_scan"
    rows = workflow.market_screener.scan_decisions.items
    assert {d.scan_task for d in rows if d.strategy_name == "momentum_breakout"} == {
        "swing_scan:paper_only"  # its two checks' rows only: its other signals wrote none
    }
    assert len([d for d in rows if d.strategy_name == "momentum_breakout"]) == 2


def test_run_after_run_the_next_best_riders_are_checked(tmp_path, monkeypatch) -> None:
    run = _workflow(tmp_path, monkeypatch, fire=RIDERS_FIRE)
    checked = []
    for _ in range(3):  # daily-bar signals persist: the same six fire every run
        run.workflow.run_swing_scan(notify=False)
        coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])["paper_only"]
        checked.append([(c["symbol"], c["timeframe"]) for c in coverage["checks"]])
    assert checked == [
        [("CCC", "1h"), ("AAA", "1h")],
        [("CCC", "1d"), ("AAA", "1d")],  # within the repeat window: the next best rotate in
        [("BBB", "1h")],
    ]


def test_the_queue_leaves_out_what_could_never_be_a_paper_only_entry(monkeypatch) -> None:
    def fired(symbol, confidence, timeframe="1d", key="momentum_breakout:1d"):
        return scan_riders.Fired(key, symbol, timeframe, SimpleNamespace(confidence=confidence))

    signals = [
        fired("BTC/USD", 0.99),  # crypto: never paper-only
        fired("NVDA", 0.98),  # held or in flight (eToro or paper): taken
        fired("UBER", 0.97),  # an open proposal: the pass skips it
        fired("AAPL", 0.6),
        fired("AAPL", 0.9, "1h", "momentum_breakout:1h"),  # AAPL's best
        fired("MSFT", 0.8),  # checked within the window
        fired("ADP", 0.7),
    ]
    monkeypatch.setattr(paper_only_riders.time, "monotonic", lambda: 100_000.0)
    memo = {("momentum_breakout:1d", "MSFT", "1d"): 100_000.0 - 60.0, ("x", "ADP", "1d"): 0.0}
    monkeypatch.setattr(paper_only_riders, "_CHECKED", memo)
    queue = paper_only_riders._queue(signals, {"NVDA", "UBER"}, 240 * 60.0)
    assert [(f.symbol, f.timeframe) for f in queue] == [("AAPL", "1h"), ("ADP", "1d")]
    assert ("x", "ADP", "1d") not in memo  # past the window: forgotten
    assert [f.symbol for f in paper_only_riders._queue(signals, set(), 30.0)][:3] == [
        "NVDA",
        "UBER",
        "AAPL",
    ]  # a 30 s window: MSFT's check has expired too


def test_the_riders_pass_counts_what_it_created_even_when_it_raises(tmp_path, monkeypatch) -> None:
    run = _workflow(tmp_path, monkeypatch, fire=RIDERS_FIRE)
    execute = run.auto.approve_enqueue_execute

    def flaky(proposal, candidate, **kwargs):
        if candidate.strategy_name == "momentum_breakout":
            raise RuntimeError("broker down after the proposal")
        return execute(proposal, candidate, **kwargs)

    run.auto.approve_enqueue_execute = flaky
    result = run.workflow.run_swing_scan(notify=False)
    assert result.status == "ok"
    coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])
    assert coverage["paper_only"]["proposals_created"] == 1
    assert coverage["paper_only"]["error"] == "broker down after the proposal"
    assert coverage["proposals_created"] == 3  # the run's two and the riders' one


def test_the_riders_pass_leaves_out_a_rider_that_passes_the_full_check_since(
    tmp_path, monkeypatch
) -> None:
    run = _workflow(tmp_path, monkeypatch, fire=RIDERS_FIRE)
    execute = run.auto.approve_enqueue_execute

    def refresh(proposal, candidate, **kwargs):  # the verdicts refresh during the run's pass
        verdicts = {**VERDICTS, "momentum_breakout:1h": FULL, "momentum_breakout:1d": FULL}
        run.state[VERDICTS_KEY] = json.dumps({"verdicts": verdicts})
        return execute(proposal, candidate, **kwargs)

    run.auto.approve_enqueue_execute = refresh
    run.workflow.run_swing_scan(notify=False)
    assert [e[1:3] for e in run.events if e[0] == "propose"] == [
        ("AAA", "trend_following"),
        ("BBB", "trend_following"),
    ]  # the rider is eToro-eligible now: never proposed outside the run's own pass
    coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])["paper_only"]
    assert (coverage["candidates"], coverage["proposals_created"]) == (2, 0)


def test_a_deadline_inside_the_scan_leaves_it_as_without_riders(tmp_path, monkeypatch) -> None:
    def run(riders):
        clock = SimpleNamespace(now=0.0)
        outcome = _workflow(
            tmp_path / str(riders),
            monkeypatch,
            riders=riders,
            fire=RIDERS_FIRE,
            clock=clock,
            deadline=5.0,  # each evaluation takes 1 s: the scan stops before CCC's 1d specs
        )
        outcome.workflow.run_swing_scan(notify=False)
        return outcome

    alone, riding = run(False), run(True)
    assert riding.events == alone.events  # the same specs, symbols and proposals; no rider ran
    assert ("eval", "CCC", "1d", "trend_following") not in alone.events
    coverage = json.loads(riding.state["workflow:swing_scan:last_scan_coverage"])
    assert coverage["paper_only"] == {
        "specs": 2,
        "seconds_left": 0.0,
        "skipped": "run_deadline_too_close",
        "proposals_created": 0,
        "seconds": 0.0,
    }


def test_a_rider_error_never_costs_the_run_its_candidates_or_proposals(tmp_path, monkeypatch) -> None:
    run = _workflow(
        tmp_path,
        monkeypatch,
        fire=RIDERS_FIRE,
        boom={"momentum_breakout": {"BBB"}},  # its generate_signal raises on BBB
    )
    screener = run.workflow.market_screener
    validation = screener._backtest_validation

    def backtests(symbol, strategy, timeframe=None):  # a rider's checks fail: e.g. a pool timeout
        if strategy == "momentum_breakout":
            raise RuntimeError("QueuePool limit reached")
        return validation(symbol, strategy, timeframe)

    monkeypatch.setattr(screener, "_backtest_validation", backtests)
    result = run.workflow.run_swing_scan(notify=False)
    assert result.status == "ok"
    assert [e for e in run.events if e[0] == "propose"] == [
        ("propose", "AAA", "trend_following", False),
        ("propose", "BBB", "trend_following", False),
    ]
    coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])["paper_only"]
    assert (coverage["errors"], coverage["checked"], coverage["candidates"]) == (4, 2, 0)
    assert coverage["proposals_created"] == 0  # 2 generate_signal calls + 2 checks raised
    error = "error: QueuePool limit reached"
    assert _plain(coverage)["checks"] == [
        _check("momentum_breakout:1h", "CCC", error),
        _check("momentum_breakout:1h", "AAA", error),
    ]
    assert paper_only_riders._CHECKED == {}  # a failed check is tried again next run


def test_no_rider_is_checked_without_room_for_a_paper_only_entry(tmp_path, monkeypatch) -> None:
    room = dict(BOOK, max_open_positions=4)  # 2 held + this one + the 2 left for eToro > 4
    run = _workflow(tmp_path, monkeypatch, fire=RIDERS_FIRE, **room)
    run.auto.proposals.broker = _Broker(["AAPL", "MSFT"])
    run.workflow.run_swing_scan(notify=False)
    coverage = json.loads(run.state["workflow:swing_scan:last_scan_coverage"])["paper_only"]
    assert coverage["skipped"] == "paper_only_headroom_open_positions"
    assert coverage["checked"] == 0 and coverage["signals_fired"] == 6
    rider_evals = [e for e in run.events if "momentum_breakout" in e]
    assert len(rider_evals) == 6  # the riders' signals only: no candidate checks


# --- the riders' step: bounded by the run's own deadline -------------------------------------


def _step(
    monkeypatch,
    *,
    now,
    started=1_000.0,
    batch=120.0,
    cadence_stop=None,
    fired=3,
    room=None,
    generation=0.0,
    check_seconds=10.0,
    found=False,
    taken=(),
    proposed=(),
):
    """``ride_along`` with stub riders: returns its coverage, the checks it started and what else
    it did (generation stop, room reads, tracking)."""

    from tests.test_workflow_service import _snapshot

    clock = SimpleNamespace(now=now)
    monkeypatch.setattr(paper_only_riders.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(paper_only_riders, "_CHECKED", {})  # each call its own process
    seen: dict = {"room": 0, "tracked": 0}

    def riders(_screener, _kept, _keys, *, stop_at):
        seen["generation_stop"] = stop_at
        clock.now += generation
        signals = [
            scan_riders.Fired("momentum_breakout:1d", s, "1d", SimpleNamespace(confidence=c))
            for s, c in zip(SYMBOLS, (0.5, 0.9, 0.7), strict=True)
        ][:fired]
        return scan_riders.Riders(fired=signals)

    monkeypatch.setattr(scan_riders, "rider_signals", riders)
    checks: list[str] = []

    def check(**kwargs):
        checks.append(kwargs["symbols"][0])
        clock.now += check_seconds
        candidate = _snapshot().model_copy(update={"symbol": kwargs["symbols"][0]})
        return SimpleNamespace(candidates=[candidate] if found else [])

    import app.workflow.paper_only as paper_only_module

    def read_room(_self):
        seen["room"] += 1
        return room

    monkeypatch.setattr(paper_only_module.PaperOnlyPass, "room", read_room)
    monkeypatch.setattr(paper_only_module.PaperOnlyPass, "taken", lambda _self: set(taken))
    open_proposals = [SimpleNamespace(order=SimpleNamespace(symbol=s)) for s in proposed]
    service = SimpleNamespace(
        settings=SimpleNamespace(screener_batch_deadline_seconds=batch),
        market_screener=SimpleNamespace(scan_universe=check),
        auto_trading=None,
        proposal_service=SimpleNamespace(
            list_proposals=lambda *, status: open_proposals if status == "pending" else []
        ),
        _track_candidates=lambda *_a, **_k: seen.update(tracked=seen["tracked"] + 1),
    )
    kwargs = {"kept_bars": SimpleNamespace(started=started)}
    if cadence_stop is not None:
        from app.workflow.cadence_budget import ScanDeadline

        kwargs["cancel_event"] = ScanDeadline(cadence_stop)
    response = SimpleNamespace(coverage={})
    batch_spec = {"paper_only_spec_keys": ["momentum_breakout:1d"]}
    paper_only_riders.ride_along(service, kwargs, response, batch_spec, origin="swing_scan", notify=False)
    return response.coverage["paper_only"], checks, seen


def test_the_riders_step_runs_only_in_what_is_left_of_the_runs_deadline(monkeypatch) -> None:
    assert RIDER_CHECK_SECONDS == 13.0 and PROPOSAL_SECONDS == 35.0
    # Batch deadline from the scan's start (1_000 + 120): 26 s left, two checks of 10 s each.
    coverage, checks, seen = _step(monkeypatch, now=1_094.0)
    assert seen["generation_stop"] == 1_120.0 - RIDER_CHECK_SECONDS  # a check's time kept back
    assert checks == ["BBB", "CCC"]  # best first: confidence 0.9, then 0.7
    assert coverage["checked"] == 2 and "skipped" not in coverage
    assert (coverage["seconds_left"], coverage["seconds"]) == (26.0, 20.0)
    # 22 s left: one check (10 s) leaves 12 s, under a check's 13: the second waits for a run.
    coverage, checks, _ = _step(monkeypatch, now=1_098.0)
    assert checks == ["BBB"] and coverage["skipped"] == "run_deadline"
    # The cadence tick's scan stop less one paper-only proposal comes first: 1_180 - 35 = 1_145.
    _coverage, checks, seen = _step(monkeypatch, now=1_100.0, batch=500.0, cadence_stop=1_180.0)
    assert seen["generation_stop"] == 1_145.0 - RIDER_CHECK_SECONDS and len(checks) == 2
    coverage, checks, seen = _step(monkeypatch, now=1_107.1)  # 12.9 s left: nothing at all
    assert coverage["skipped"] == "run_deadline_too_close" and checks == []
    assert "generation_stop" not in seen
    coverage, checks, seen = _step(monkeypatch, now=1_000.0, batch=0.0)  # no deadline: never
    assert coverage["skipped"] == "no_run_deadline" and checks == [] and seen["room"] == 0
    coverage, checks, seen = _step(monkeypatch, now=1_000.0, room="paper_only_positions_cap")
    assert coverage["skipped"] == "paper_only_positions_cap" and checks == []


def test_the_riders_step_reads_nothing_more_once_no_check_can_start(monkeypatch) -> None:
    # The signals took 14 s of 26: 12 s left, under a check's 13: paper is not even read.
    coverage, checks, seen = _step(monkeypatch, now=1_094.0, generation=14.0)
    assert coverage["skipped"] == "run_deadline" and checks == [] and seen["room"] == 0
    # A check slower than planned (14 s): no second one, though 12 s would be left... or more.
    coverage, checks, _ = _step(monkeypatch, now=1_000.0, check_seconds=14.0)
    assert checks == ["BBB"] and coverage["skipped"] == "check_slower_than_planned"
    # A check that ends past the stop: its candidate is neither tracked nor proposed.
    coverage, checks, seen = _step(monkeypatch, now=1_094.0, check_seconds=27.0, found=True)
    assert coverage["skipped"] == "run_deadline" and seen["tracked"] == 0
    assert coverage["candidates"] == 1
    _coverage, _checks, seen = _step(monkeypatch, now=1_094.0, found=True)
    assert seen["tracked"] == 1  # in time: tracked, then its pass


def test_no_check_goes_to_a_taken_or_proposed_symbol(monkeypatch) -> None:
    # Fired: AAA 0.5, BBB 0.9, CCC 0.7. BBB is held or in flight; CCC has an open proposal.
    coverage, checks, _ = _step(monkeypatch, now=1_000.0, taken={"BBB"}, proposed=("CCC",))
    assert checks == ["AAA"] and coverage["queued"] == 1


def test_the_legacy_cadence_budget_never_rides(monkeypatch) -> None:
    from app.workflow.cadence_budget import CadenceBudget, tick_budget

    legacy = CadenceBudget(started_at=1_000.0, job_timeout=240.0, legacy_soft_budget=110.0)
    with tick_budget(legacy):  # no scan stop: nothing bounds a rider's work in the tick
        coverage, checks, seen = _step(monkeypatch, now=1_000.0)
    assert coverage["skipped"] == "legacy_cadence_budget" and checks == []
    assert "generation_stop" not in seen


def test_the_riders_step_never_raises(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("riders down")

    monkeypatch.setattr(scan_riders, "rider_signals", broken)
    response = SimpleNamespace(coverage={})
    service = SimpleNamespace(
        settings=SimpleNamespace(screener_batch_deadline_seconds=120.0), market_screener=None
    )
    kept = SimpleNamespace(started=paper_only_riders.time.monotonic(), replay=False)
    batch = {"paper_only_spec_keys": ["momentum_breakout:1d"]}
    created = paper_only_riders.ride_along(
        service, {"kept_bars": kept}, response, batch, origin="swing_scan", notify=False
    )
    assert created == 0 and kept.replay  # replay is set before any rider work
    assert _plain(response.coverage["paper_only"]) == {
        "specs": 1,
        "errors": 1,
        "error": "riders down",
        "proposals_created": 0,
    }
