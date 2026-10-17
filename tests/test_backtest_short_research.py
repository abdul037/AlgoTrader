"""Short research (step 1, research only): isolated from the long evidence and bounded in time.

Covers ``app/backtesting/short_research.py``: the rows it writes can never be read as long
evidence (evidence gate, decay baseline, screener / signal backtest validation, live ranker),
its verdicts sit under their own key that the gate never reads, and the pass never runs in
market hours, never outlives the scheduler's job cap and keeps to its own cursor.
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.backtesting import short_research
from app.backtesting.batch import BatchBacktestService
from app.backtesting.intraday_plan import splitter_for
from app.backtesting.short_research import (
    CURSOR_KEY,
    MARGIN_SECONDS,
    SHORT_OOS_SUFFIX,
    SHORT_STRATEGIES,
    in_quiet_hours,
    run_short_pass,
    short_budget_seconds,
)
from app.models.signal import Signal, SignalAction
from app.performance import strategy_evidence
from app.performance.strategy_evidence import (
    SHORT_OOS_PATTERN,
    SHORT_VERDICTS_KEY,
    VERDICTS_KEY,
    compute_verdicts,
    evidence_blocker,
    refresh_short_verdicts,
    refresh_verdicts,
)
from app.screener.scan_support import ScanTimeoutError
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, RunLogRepository, RuntimeStateRepository
from app.strategies import get_strategy, get_strategy_specs
from app.utils.time import utc_now
from tests.conftest import make_settings
from tests.test_backtest_long_parity import _daily_frame

APP = Path(__file__).resolve().parent.parent / "app"
SATURDAY_10_ET = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
MONDAY = datetime(2026, 10, 19, tzinfo=UTC)  # EDT: New York = UTC-4


def _et(hour: int, minute: int = 0, day: datetime = MONDAY) -> datetime:
    return day + timedelta(hours=hour + 4, minutes=minute)


# --------------------------------------------------------------------------- isolation


def _db(tmp_path, **overrides):
    settings = make_settings(tmp_path, backtest_short_research_enabled=True, **overrides)
    db = Database(settings)
    db.initialize()
    return settings, db


def _row(repo, *, symbol, name, suffix, trades=25, expectancy=30.0, holdout=6, holdout_exp=10.0, age_hours=0.0):
    completed = (utc_now() - timedelta(hours=age_hours)).isoformat()
    repo.create(
        backtest_id=f"bt_{symbol}_{name}_{suffix}_{age_hours}",
        symbol=symbol,
        strategy_name=name,
        file_path=f"alpaca:1d:{symbol}{suffix}",
        started_at=completed,
        completed_at=completed,
        metrics={
            "number_of_trades": trades, "expectancy_usd": expectancy, "holdout_trades": holdout,
            "holdout_expectancy_usd": holdout_exp, "out_of_sample": True, "profit_factor": 1.5,
            "annualized_return_pct": 12.0, "max_drawdown_pct": 5.0,
        },
        trades=[],
    )


def test_passing_short_rows_never_reach_the_long_evidence_gate(tmp_path) -> None:
    settings, db = _db(tmp_path)
    repo, state = BacktestRepository(db), RuntimeStateRepository(db)
    for symbol in ("AAA", "BBB"):  # pooled: 50 OOS trades, 12 holdout trades, all positive
        _row(repo, symbol=symbol, name="mean_reversion:short", suffix=SHORT_OOS_SUFFIX)

    assert compute_verdicts(db, settings) == {}
    refresh_verdicts(db, settings, state)
    assert json.loads(state.get(VERDICTS_KEY))["verdicts"] == {}

    short = refresh_short_verdicts(db, settings, state)
    assert list(short) == ["mean_reversion:short:1d"] and short["mean_reversion:short:1d"]["passed"]
    stored = json.loads(state.get(SHORT_VERDICTS_KEY))
    assert stored["research_only"] is True and stored["verdicts"] == short

    gate = make_settings(tmp_path, require_strategy_oos_evidence=True)
    assert evidence_blocker(gate, state, strategy="mean_reversion", timeframe="1d") == "strategy_lacks_oos_evidence"
    # Even written under the long key by mistake, the native ":short" keys unlock nothing.
    state.set(VERDICTS_KEY, state.get(SHORT_VERDICTS_KEY))
    assert evidence_blocker(gate, state, strategy="mean_reversion", timeframe="1d") == "strategy_lacks_oos_evidence"


def test_a_newer_short_row_never_replaces_the_long_summary(tmp_path) -> None:
    from app.screener.service_backtests import backtest_validation as screener_validation
    from app.signals.backtests import backtest_validation as signal_validation

    settings, db = _db(tmp_path, min_backtest_trades_for_alerts=1, min_backtest_profit_factor=1.0)
    repo = BacktestRepository(db)
    _row(repo, symbol="AAA", name="mean_reversion", suffix=":walk_forward_oos", expectancy=7.0, age_hours=5)
    _row(repo, symbol="AAA", name="mean_reversion:short", suffix=SHORT_OOS_SUFFIX, expectancy=-50.0)

    for summary in (repo.get_latest_summary("AAA", "mean_reversion"),
                    repo.get_latest_summary("AAA", "mean_reversion", timeframe="1d")):
        assert summary["strategy_name"] == "mean_reversion"
        assert summary["file_path"].endswith(":walk_forward_oos")
        assert summary["metrics"]["expectancy_usd"] == 7.0
    service = SimpleNamespace(backtests=repo, settings=settings)
    for result in (screener_validation(service, "AAA", "mean_reversion", "1d"),
                   signal_validation(service, SimpleNamespace(symbol="AAA", strategy_name="mean_reversion"))):
        assert result["summary"]["strategy_name"] == "mean_reversion"
    assert repo.expectancy_by_strategy() == {"mean_reversion": 7.0}
    assert compute_verdicts(db, settings, pattern=SHORT_OOS_PATTERN).keys() == {"mean_reversion:short:1d"}
    assert compute_verdicts(db, settings).keys() == {"mean_reversion:1d"}


class _Bars:
    def __init__(self):
        self.calls: list[tuple] = []

    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False):
        self.calls.append((symbol, timeframe, bars, provider, force_refresh))
        return _daily_frame(7)


def _long_snapshot(db, settings, state, repo):
    from app.screener.service_backtests import backtest_validation as screener_validation
    from app.signals.backtests import backtest_validation as signal_validation

    refresh_verdicts(db, settings, state)
    names = [spec.name for spec in get_strategy_specs(timeframe="1d")]
    service = SimpleNamespace(backtests=repo, settings=settings)
    return json.dumps(
        {
            "verdicts": compute_verdicts(db, settings),
            "cached": json.loads(state.get(VERDICTS_KEY))["verdicts"],
            "baseline": repo.expectancy_by_strategy(),
            "latest": {n: repo.get_latest_summary("AAA", n) for n in names},
            "latest_1d": {n: repo.get_latest_summary("AAA", n, timeframe="1d") for n in names},
            "screener": {n: screener_validation(service, "AAA", n, "1d") for n in names},
            "signals": {
                n: signal_validation(service, SimpleNamespace(symbol="AAA", strategy_name=n)) for n in names
            },
        },
        sort_keys=True,
    )


@pytest.fixture(scope="module")
def real_pass(tmp_path_factory):
    """One real short pass on a DB that already holds long rows for every daily strategy."""

    tmp = tmp_path_factory.mktemp("short_pass")
    settings, db = _db(tmp, market_universe_symbols=["AAA"])
    repo, state, logs = BacktestRepository(db), RuntimeStateRepository(db), RunLogRepository(db)
    for spec in get_strategy_specs(timeframe="1d"):
        _row(repo, symbol="AAA", name=spec.name, suffix=":walk_forward_oos", age_hours=1, trades=60)
    before = _long_snapshot(db, settings, state, repo)
    with db.connect() as connection:
        old_ids = {row["id"] for row in connection.execute("SELECT id FROM backtests").fetchall()}
    bars = _Bars()
    service = BatchBacktestService(
        settings=settings, market_data_engine=bars, backtest_repository=repo, run_log_repository=logs
    )
    patch = pytest.MonkeyPatch()
    patch.setattr("app.broker.etoro_rate_limit.etoro_cooldown_remaining", lambda _settings: 0.0)
    try:
        out = run_short_pass(service, state, pass_started=time.monotonic(), wall=lambda: SATURDAY_10_ET)
    finally:
        patch.undo()
    after = _long_snapshot(db, settings, state, repo)
    with db.connect() as connection:
        rows = connection.execute("SELECT id, strategy_name, file_path, trades_json FROM backtests").fetchall()
    new = [dict(row) for row in rows if row["id"] not in old_ids]
    return SimpleNamespace(out=out, before=before, after=after, new=new, bars=bars, state=state)


def test_a_real_pass_writes_only_relabelled_short_rows(real_pass) -> None:
    assert real_pass.out["rows"] == 6 and real_pass.out["stop"] == "complete"
    assert sorted(row["strategy_name"] for row in real_pass.new) == sorted(f"{n}:short" for n in SHORT_STRATEGIES)
    assert all(row["file_path"] == f"auto:1d:AAA{SHORT_OOS_SUFFIX}" for row in real_pass.new)
    trades = [trade for row in real_pass.new for trade in json.loads(row["trades_json"])]
    assert trades and {trade["side"] for trade in trades} == {"short"}
    assert real_pass.bars.calls == [("AAA", "1d", 500, None, False)]


def test_a_real_pass_leaves_every_long_reader_byte_identical(real_pass) -> None:
    assert real_pass.after == real_pass.before


def test_a_real_pass_stores_research_only_short_verdicts(real_pass) -> None:
    stored = json.loads(real_pass.state.get(SHORT_VERDICTS_KEY))
    assert stored["research_only"] is True
    assert set(stored["verdicts"]) == {f"{n}:short:1d" for n in SHORT_STRATEGIES}
    assert real_pass.out["verdicts"] == "refreshed"
    assert real_pass.state.get(CURSOR_KEY) == "0"  # one symbol: the six units wrapped around


def test_the_live_scorecard_reports_short_evidence(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(tmp_path)
    app = create_app(settings=settings, enable_background_jobs=False)
    state = RuntimeStateRepository(app.state.db)
    client = TestClient(app)
    assert client.get("/performance/live-scorecard").json()["short_evidence"] == {}
    state.set(SHORT_VERDICTS_KEY, json.dumps({"research_only": True, "verdicts": {"x:short:1d": {"passed": False}}}))
    assert client.get("/performance/live-scorecard").json()["short_evidence"]["verdicts"] == {
        "x:short:1d": {"passed": False}
    }
    state.set(SHORT_VERDICTS_KEY, "not json")
    assert client.get("/performance/live-scorecard").json()["short_evidence"] == {}


class _OneShort:
    """One bracketed entry_short (stop 0.8% above, target 10% below) on one bar's close."""

    name = "one_short"

    def __init__(self, at):
        self.at = at

    def generate_signal(self, data, symbol):
        last = data.iloc[-1]
        if pd.Timestamp(last["timestamp"]) != self.at:
            return None
        price = float(last["close"])
        return Signal(
            symbol=symbol, strategy_name=self.name, action=SignalAction.SELL, rationale="squeeze",
            price=price, stop_loss=price * 1.008, take_profit=price * 0.9, metadata={"signal_role": "entry_short"},
        )


class _Squeeze:
    """500 flat daily bars; inside one fold's test window, two bars after a short's signal,
    the open gaps up 2.6x straight through its stop: the short loses more than the account."""

    def __init__(self, settings):
        stamps = pd.bdate_range("2024-01-02", periods=500, tz="UTC") + pd.Timedelta(hours=4)
        frame = pd.DataFrame({"timestamp": stamps, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
                              "volume": 5_000_000.0})
        windows = list(splitter_for("1d", settings).split(frame))
        window = windows[len(windows) // 2]
        in_test = frame.index[(frame["timestamp"] >= window.test_start) & (frame["timestamp"] <= window.test_end)]
        gap = int(in_test[4])
        frame.loc[gap:, ["open", "high", "low", "close"]] *= 2.6
        self.frame, self.signal_at = frame, frame["timestamp"].iloc[gap - 2]

    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False):
        return self.frame.copy()


def test_a_short_that_loses_more_than_the_account_still_reaches_the_short_evidence(tmp_path, monkeypatch) -> None:
    """The worst shorts used to vanish: the wiped-out run raised (a complex annualized return),
    the pass logged it as a unit error and wrote no row, so the verdicts pooled only survivors."""

    settings, db = _db(tmp_path, market_universe_symbols=["SQZ"])
    repo, state, logs = BacktestRepository(db), RuntimeStateRepository(db), RunLogRepository(db)
    bars = _Squeeze(settings)
    service = BatchBacktestService(settings=settings, market_data_engine=bars, backtest_repository=repo,
                                   run_log_repository=logs)
    monkeypatch.setattr(short_research, "get_strategy", lambda name, **kwargs: _OneShort(bars.signal_at))
    monkeypatch.setattr("app.broker.etoro_rate_limit.etoro_cooldown_remaining", lambda _settings: 0.0)

    out = run_short_pass(service, state, pass_started=time.monotonic(), wall=lambda: SATURDAY_10_ET)

    assert (out["rows"], out["errors"], out["first_error"], out["verdicts"]) == (6, 0, "", "refreshed")
    with db.connect() as connection:
        rows = connection.execute("SELECT metrics_json, trades_json FROM backtests").fetchall()
    assert len(rows) == 6
    for row in rows:
        (trade,) = json.loads(row["trades_json"])
        metrics = json.loads(row["metrics_json"])
        json.dumps(metrics, allow_nan=False)  # finite: the row is valid Postgres JSONB too
        assert trade["reason"] == "stop_loss" and trade["side"] == "short"
        assert trade["pnl_usd"] < -1.5 * trade["notional_usd"]  # the whole 2.6x gap, uncapped
        assert metrics["expectancy_usd"] == pytest.approx(trade["pnl_usd"])
        assert metrics["total_return_pct"] < -100.0 and metrics["annualized_return_pct"] == -100.0
    verdicts = json.loads(state.get(SHORT_VERDICTS_KEY))["verdicts"]
    assert set(verdicts) == {f"{name}:short:1d" for name in SHORT_STRATEGIES}
    for verdict in verdicts.values():  # counted, and it sinks the expectancy
        assert verdict["oos_trades"] == 1 and verdict["oos_expectancy_usd"] < -15_000 and not verdict["passed"]
    assert compute_verdicts(db, settings) == {}  # still nothing for the long gate


# --------------------------------------------------------------------------- static guards


def _sources(paths):
    for path in paths:
        files = [path] if path.suffix == ".py" else sorted(path.rglob("*.py"))
        yield from ((file, file.read_text()) for file in files)


def test_only_the_reporting_path_reads_the_short_verdicts() -> None:
    readers = {
        file.relative_to(APP.parent).as_posix()
        for file, text in _sources([APP])
        if "SHORT_VERDICTS_KEY" in text or "short_verdicts" in text
    }
    allowed = {
        "app/performance/strategy_evidence.py",
        "app/performance/routes.py",
        "app/backtesting/short_research.py",
        "app/workflow/premarket_deep_scan.py",  # step 2: shadow only
    }
    assert readers <= allowed and "app/performance/routes.py" in readers


def test_only_the_batch_service_imports_short_research() -> None:
    importers = set()
    for file, text in _sources([APP]):
        for node in ast.walk(ast.parse(text)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            if any(name == "app.backtesting.short_research" for name in names):
                importers.add(file.relative_to(APP.parent).as_posix())
    assert importers == {"app/backtesting/batch.py"}


def test_the_trading_path_never_mentions_short_research() -> None:
    trading = [APP / name for name in ("broker", "automation", "risk", "execution", "approvals", "screener", "signals")]
    trading += [APP / "workflow" / "operations.py", APP / "workflow" / "service.py"]
    needles = ("short_research", "SHORT_VERDICTS_KEY", "short_verdicts", "short_signals",
               "walk_forward_oos_short", "short_entries=")
    hits = [f"{file}: {n}" for file, text in _sources(trading) for n in needles if n in text]
    assert hits == []


def test_only_the_research_path_names_any_short_research_artefact() -> None:
    """All of app/, not a list of directories: a new reader of the short rows, the short engine
    or its cost legs anywhere (notifications, dashboard, workflow, main) must be added here on
    purpose. main.py may hold only the one call after the long pass."""

    needles = ("short_research", "SHORT_OOS", "SHORT_NAME_SUFFIX", "walk_forward_oos_short", "short_entries=",
               ".short_entries", "refresh_short_verdicts", "pattern=SHORT", "short_holding_cost", "short_borrow_fee")
    allowed = {
        "app/backtesting/short_research.py",  # the pass
        "app/backtesting/batch.py",  # run_short_research, its one entry point
        "app/backtesting/engine.py",  # EngineConfig.short_entries
        "app/backtesting/cost_model.py",  # the short-only cost legs
        "app/performance/strategy_evidence.py",  # the short verdicts
        "app/runtime_settings.py",  # its two settings
    }
    hits = {}
    for file, text in _sources([APP]):
        lines = [line.strip() for line in text.splitlines() if any(n in line for n in needles)]
        if lines:
            hits[file.relative_to(APP.parent).as_posix()] = lines
    main = hits.pop("app/main.py", None)
    assert main == ["app.state.batch_backtest_service.run_short_research(runtime_state_repository)"]
    assert set(hits) <= allowed, sorted(set(hits) - allowed)


def test_short_strategies_are_exactly_the_daily_bracketed_entry_short_emitters() -> None:
    emitters = {
        spec.name
        for spec in get_strategy_specs(timeframe="1d")
        if '"signal_role": "entry_short"' in inspect.getsource(type(get_strategy(spec.name, **dict(spec.default_kwargs))))
    }
    assert emitters == SHORT_STRATEGIES


def test_the_short_pattern_matches_only_the_short_suffix(tmp_path) -> None:
    _, db = _db(tmp_path)
    assert SHORT_OOS_PATTERN == "%" + SHORT_OOS_SUFFIX
    with db.connect() as connection:
        row = connection.execute(
            "SELECT ? LIKE ? AS long_hit, ? LIKE ? AS short_hit, ? LIKE ? AS cross_hit",
            ("x:1d:S" + SHORT_OOS_SUFFIX, "%:walk_forward_oos", "x:1d:S" + SHORT_OOS_SUFFIX, SHORT_OOS_PATTERN,
             "x:1d:S:walk_forward_oos", SHORT_OOS_PATTERN),
        ).fetchone()
    assert (row["long_hit"], row["short_hit"], row["cross_hit"]) == (0, 1, 0)


# --------------------------------------------------------------------------- time bounds


class _Clock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Recorder:
    """runtime_state / run_logs / backtests double: records calls, each costs DB time."""

    def __init__(self, clock: _Clock, cost: float, values=None):
        self.clock, self.cost, self.values, self.calls = clock, cost, dict(values or {}), []
        self.db = object()

    def _tick(self, call):
        self.calls.append(call)
        self.clock.advance(self.cost)

    def get(self, key):
        self._tick(("get", key))
        return self.values.get(key)

    def set(self, key, value):
        self._tick(("set", key))
        self.values[key] = value

    def log(self, event, payload):
        self._tick(("log", event, payload))

    def create(self, **kwargs):
        self._tick(("create", kwargs["strategy_name"], kwargs["file_path"]))


class _FakeService:
    def __init__(self, settings, clock, *, unit=2.5, fetch=1.0, db_cost=0.6, fetch_error=None):
        self.settings, self.clock, self.unit, self.fetch, self.fetch_error = settings, clock, unit, fetch, fetch_error
        self.market_data = SimpleNamespace(get_history=self._history)
        self.backtests = _Recorder(clock, db_cost)
        self.logs = _Recorder(clock, db_cost)
        self.fetches: list[tuple[str, float]] = []
        self.units: list[tuple[str, str]] = []

    def _history(self, symbol, **kwargs):
        if self.fetch_error and symbol in self.fetch_error:
            raise self.fetch_error[symbol]
        return _daily_frame(7, 60)

    def _run_strategy(self, **kwargs):
        assert kwargs["engine"].config.short_entries is True and kwargs["engine"].repository is None
        assert kwargs["walk_forward"] is True and kwargs["oos_suffix"] == SHORT_OOS_SUFFIX
        self.clock.advance(self.unit)
        self.units.append((kwargs["symbol"], kwargs["row_name"]))
        self.backtests.create(strategy_name=kwargs["row_name"], file_path=f"x:1d:{kwargs['symbol']}{kwargs['oos_suffix']}")
        return {"number_of_trades": 0}


@pytest.fixture
def bounded(monkeypatch):
    """Fetches go through a fake _bounded_call on the fake clock (it enforces the timeout)."""

    holder: dict = {}

    def fake_bounded(label, timeout, func, *args, **kwargs):
        service = holder["service"]
        service.fetches.append((args[0], round(timeout, 3)))
        if service.fetch > timeout:
            service.clock.advance(timeout)
            raise ScanTimeoutError(f"{label}_timeout_after_{timeout:g}s")
        service.clock.advance(service.fetch)
        return func(*args, **kwargs)

    monkeypatch.setattr("app.screener.scan_support._bounded_call", fake_bounded)
    monkeypatch.setattr("app.broker.etoro_rate_limit.etoro_cooldown_remaining", lambda _settings: 0.0)
    verdict_calls: list = []

    def fake_refresh(db, settings, runtime_state):
        verdict_calls.append(db)
        holder["service"].clock.advance(1.5)

    monkeypatch.setattr(strategy_evidence, "refresh_short_verdicts", fake_refresh)
    holder["verdicts"] = verdict_calls
    return holder


def _setup(tmp_path, bounded, *, symbols=("AAA", "BBB", "CCC"), elapsed=150.0, **service_kwargs):
    overrides = {"backtest_short_research_enabled": True, "market_universe_symbols": list(symbols)}
    settings = make_settings(tmp_path, **{**overrides, **service_kwargs.pop("settings", {})})
    clock = _Clock()
    service = _FakeService(settings, clock, **service_kwargs)
    bounded["service"] = service
    state = _Recorder(clock, service.backtests.cost)
    return service, state, clock, clock.t - elapsed


def _run(service, state, started, when=SATURDAY_10_ET, wall=None):
    return run_short_pass(service, state, pass_started=started, clock=service.clock, wall=wall or (lambda: when))


@pytest.mark.parametrize("when", [_et(8, 30), _et(10, 0), _et(16, 29)], ids=["08:30", "10:00", "16:29"])
def test_weekday_market_hours_do_nothing_at_all(tmp_path, bounded, when) -> None:
    service, state, _, started = _setup(tmp_path, bounded)
    assert _run(service, state, started, when) is None
    assert (service.fetches, service.units, state.calls, service.logs.calls, service.backtests.calls) == ([], [], [], [], [])


@pytest.mark.parametrize("when", [_et(17, 0), _et(7, 59), SATURDAY_10_ET], ids=["mon_17:00", "mon_07:59", "sat_10:00"])
def test_off_hours_and_weekends_run(tmp_path, bounded, when) -> None:
    service, state, _, started = _setup(tmp_path, bounded)
    assert _run(service, state, started, when)["rows"] > 0


def test_quiet_hours_use_new_york_and_the_schedule_clock(tmp_path) -> None:
    london = make_settings(tmp_path, schedule_timezone="Europe/London")
    assert in_quiet_hours(_et(12, 0), london)  # 17:00 London, but the US session is open
    assert in_quiet_hours(_et(4, 0), london)  # 09:00 London: the schedule's own scans run
    assert not in_quiet_hours(_et(17, 0), london)
    assert not in_quiet_hours(_et(10, 0, day=MONDAY - timedelta(days=1)), london)  # Sunday


def test_the_budget_is_what_the_job_cap_leaves(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert short_budget_seconds(settings, 0.0, 150.0) == 45.0  # the pass's own cap
    assert short_budget_seconds(settings, 0.0, 192.0) == pytest.approx(10.0)  # 240 - 192 - 30 - 8
    assert short_budget_seconds(settings, 0.0, 193.0) == 0.0  # below the 10 s minimum
    assert short_budget_seconds(settings, None, 150.0) == 0.0
    assert short_budget_seconds(make_settings(tmp_path, scheduler_job_timeout_seconds=300), 0.0, 230.0) == pytest.approx(32.0)


def test_no_budget_is_logged_once_and_touches_nothing_else(tmp_path, bounded) -> None:
    service, state, _, started = _setup(tmp_path, bounded, elapsed=205.0)
    assert _run(service, state, started) is None
    assert [call[:2] for call in service.logs.calls] == [("log", "backtest_short_research_skipped")]
    assert service.logs.calls[0][2] == {"reason": "no_budget", "long_elapsed_seconds": 205.0}
    assert (service.fetches, state.calls) == ([], [])


@pytest.mark.parametrize("fetch", [1.0, 25.0], ids=["fast_fetch", "hung_fetch"])
@pytest.mark.parametrize("elapsed", [150.0, 185.0, 200.0, 209.0, 215.0])
def test_the_pass_never_outlives_the_job_cap(tmp_path, bounded, elapsed, fetch) -> None:
    service, state, clock, started = _setup(tmp_path, bounded, elapsed=elapsed, fetch=fetch)
    out = _run(service, state, started)
    job_end = clock() - started
    if out is None:  # skipped: at most the one skip log after the long pass
        assert job_end <= elapsed + service.logs.cost + 1e-9
    else:
        assert job_end <= 240.0 - MARGIN_SECONDS
        assert all(timeout <= 20.0 for _, timeout in service.fetches)


def test_a_hung_fetch_is_cut_and_the_next_symbol_skipped(tmp_path, bounded) -> None:
    service, state, _, started = _setup(tmp_path, bounded, elapsed=150.0, fetch=25.0)
    out = _run(service, state, started)
    assert service.fetches == [("AAA", 20.0), ("BBB", 20.0)]  # CCC: no time left for a fetch
    assert out["stop"] == "budget" and out["rows"] == 0 and out["errors"] == 2
    assert out["units"] == 12 and state.values[CURSOR_KEY] == "12"


def test_it_stops_at_its_deadline_and_the_next_pass_resumes_there(tmp_path, bounded) -> None:
    service, state, clock, started = _setup(tmp_path, bounded, elapsed=150.0)
    first = _run(service, state, started)
    assert first["stop"] == "budget" and first["units"] == 12 and state.values[CURSOR_KEY] == "12"
    assert [symbol for symbol, _ in service.units] == ["AAA"] * 6 + ["BBB"] * 6
    assert bounded["verdicts"]  # rows were written and the reserve was left

    service.units.clear()
    second = _run(service, state, clock() - 150.0)
    assert [symbol for symbol, _ in service.units][:6] == ["CCC"] * 6
    assert second["cursor"] == (12 + second["units"]) % 18
    keys = {call[1] for call in state.calls if call[0] == "set"}
    assert keys == {CURSOR_KEY}  # never the long job's backtest_gate_refresh cursor or unit
    assert {name for _, name in service.units} <= {f"{n}:short" for n in SHORT_STRATEGIES}


def test_an_overrunning_unit_defers_the_verdict_refresh(tmp_path, bounded) -> None:
    service, state, clock, started = _setup(tmp_path, bounded, elapsed=150.0, unit=55.0)
    out = _run(service, state, started)
    assert out["rows"] == 1 and out["verdicts"] == "deferred" and bounded["verdicts"] == []
    assert clock() - started <= 240.0  # still inside the hard cap, if not the margin


def test_crossing_into_the_morning_window_stops_the_pass(tmp_path, bounded) -> None:
    service, state, clock, started = _setup(tmp_path, bounded, elapsed=150.0)
    origin, opened = clock(), _et(7, 59) + timedelta(seconds=50)
    out = _run(service, state, started, wall=lambda: opened + timedelta(seconds=clock() - origin))
    assert out["stop"] == "quiet_hours"
    assert 0 < out["units"] < 6 and state.values[CURSOR_KEY] == str(out["units"])


def test_a_rate_limit_stops_the_pass_without_burning_the_cursor(tmp_path, bounded, monkeypatch) -> None:
    from app.broker.etoro_rate_limit import EToroRateLimitError

    service, state, _, started = _setup(tmp_path, bounded, fetch_error={"BBB": EToroRateLimitError("cooldown")})
    out = _run(service, state, started)
    assert out["stop"] == "rate_limited" and out["units"] == 6 and state.values[CURSOR_KEY] == "6"

    monkeypatch.setattr("app.broker.etoro_rate_limit.etoro_cooldown_remaining", lambda _settings: 30.0)
    service, state, _, started = _setup(tmp_path, bounded)
    out = _run(service, state, started)
    assert out["stop"] == "rate_limited" and out["units"] == 0 and service.fetches == []


def test_a_failed_fetch_skips_only_that_symbol(tmp_path, bounded) -> None:
    service, state, _, started = _setup(tmp_path, bounded, symbols=("AAA", "BBB"), fetch_error={"AAA": RuntimeError("no data")})
    out = _run(service, state, started)
    assert [symbol for symbol, _ in service.units] == ["BBB"] * 6
    assert out["units"] == 12 and out["errors"] == 1 and "AAA: history: no data" in out["first_error"]


def test_disabled_crypto_hours_and_no_long_pass_do_nothing(tmp_path, bounded) -> None:
    for settings, started in (
        ({"backtest_short_research_enabled": False}, 0.0),
        ({"crypto_trading_enabled": True}, 0.0),
        ({}, None),
    ):
        service, state, _, _ = _setup(tmp_path, bounded, settings=settings)
        assert _run(service, state, started) is None
        assert (service.fetches, state.calls, service.logs.calls) == ([], [], [])


def test_crypto_symbols_are_never_short_researched(tmp_path, bounded) -> None:
    service, state, _, started = _setup(tmp_path, bounded, symbols=("BTCUSD", "AAA"))
    _run(service, state, started)
    assert {symbol for symbol, _ in service.fetches} == {"AAA"}


def test_run_short_research_swallows_failures_and_reads_only_its_own_threads_pass(tmp_path, monkeypatch) -> None:
    settings, db = _db(tmp_path)
    logs = RunLogRepository(db)
    service = BatchBacktestService(
        settings=settings, market_data_engine=SimpleNamespace(get_history=lambda *a, **k: 1 / 0),
        backtest_repository=BacktestRepository(db), run_log_repository=logs,
    )
    seen: list = []
    monkeypatch.setattr(short_research, "run_short_pass", lambda svc, state, *, pass_started: seen.append(pass_started))

    service.run(symbols=["AAA"], timeframes=["1d"])
    mine = service._pass_started.value
    worker = threading.Thread(target=lambda: service.run(symbols=["BBB"], timeframes=["1d"]))
    worker.start()
    worker.join()
    service.run_short_research(RuntimeStateRepository(db))
    service.run_short_research(RuntimeStateRepository(db))  # consumed: a second call has no pass
    assert seen == [mine, None]

    def boom(*args, **kwargs):
        raise RuntimeError("short research bug")

    monkeypatch.setattr(short_research, "run_short_pass", boom)
    service.run(symbols=["AAA"], timeframes=["1d"])
    assert service.run_short_research(RuntimeStateRepository(db)) is None
    with db.connect() as connection:
        events = [row["event_type"] for row in connection.execute("SELECT event_type FROM run_logs").fetchall()]
    assert "backtest_short_research_failed" in events
