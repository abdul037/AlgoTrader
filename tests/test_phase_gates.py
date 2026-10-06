"""Phases 2-4 of the operator plan (2026-10-03).

Phase 2: pooled walk-forward OOS evidence decides which strategies may create
proposals (when enabled). Phase 3: the go-live readiness bar. Phase 4: live
trading is locked until that bar is met, the 0.1% micro-live cap is active and
the operator has set the acknowledgement phrase.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT, AutomationService
from app.performance.go_live_readiness import READINESS_KEY, compute_readiness, readiness_ready
from app.performance.strategy_evidence import compute_verdicts, evidence_blocker, refresh_verdicts
from app.storage.db import Database
from app.storage.repositories import RunLogRepository, RuntimeStateRepository
from app.utils.time import utc_now
from tests.conftest import make_settings


def _db(tmp_path):
    db = Database(make_settings(tmp_path))
    db.initialize()
    return db


def _backtest(
    db,
    *,
    symbol,
    strategy,
    tf,
    trades,
    expectancy,
    holdout_trades,
    holdout_expectancy,
    age_days=0.0,
):
    completed = (utc_now() - timedelta(days=age_days)).isoformat()
    metrics = {
        "number_of_trades": trades,
        "expectancy_usd": expectancy,
        "holdout_trades": holdout_trades,
        "holdout_expectancy_usd": holdout_expectancy,
        "out_of_sample": True,
    }
    with db.connect() as c:
        c.execute(
            "INSERT INTO backtests (id, symbol, strategy_name, file_path, started_at, completed_at, metrics_json, trades_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"bt_{symbol}_{strategy}_{tf}_{age_days}",
                symbol,
                strategy,
                f"alpaca:{tf}:{symbol}:walk_forward_oos",
                completed,
                completed,
                json.dumps(metrics),
                "[]",
            ),
        )


# --- Phase 2 -------------------------------------------------------------------


def test_verdicts_pool_across_symbols_and_use_latest_row(tmp_path) -> None:
    db = _db(tmp_path)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        _backtest(
            db,
            symbol=symbol,
            strategy="vwap_reclaim",
            tf="15m",
            trades=20,
            expectancy=10.0,
            holdout_trades=5,
            holdout_expectancy=4.0,
        )
    # An older, losing row for AAPL must be superseded by the newest one.
    _backtest(
        db,
        symbol="AAPL",
        strategy="vwap_reclaim",
        tf="15m",
        trades=20,
        expectancy=-50.0,
        holdout_trades=5,
        holdout_expectancy=-9.0,
        age_days=2,
    )
    _backtest(
        db,
        symbol="AAPL",
        strategy="momentum_breakout",
        tf="15m",
        trades=60,
        expectancy=-3.0,
        holdout_trades=12,
        holdout_expectancy=1.0,
    )
    _backtest(
        db,
        symbol="AAPL",
        strategy="rsi_reversal",
        tf="5m",
        trades=12,
        expectancy=8.0,
        holdout_trades=2,
        holdout_expectancy=3.0,
    )

    verdicts = compute_verdicts(db, SimpleNamespace())

    good = verdicts["vwap_reclaim:15m"]
    assert good["passed"] and good["symbols"] == 3 and good["oos_trades"] == 60
    assert good["oos_expectancy_usd"] == 10.0 and good["holdout_trades"] == 15
    assert verdicts["momentum_breakout:15m"]["reasons"] == ["oos_expectancy_not_positive"]
    assert verdicts["rsi_reversal:5m"]["reasons"] == [
        "too_few_oos_trades",
        "too_few_holdout_trades",
    ]
    assert good["oos_expectancy_r"] == 0.1
    # Operator bar 2026-10-06: a minimum expectancy in R (1 R = $100 backtest risk).
    strict = compute_verdicts(db, SimpleNamespace(strategy_evidence_min_expectancy_r=0.15))
    assert strict["vwap_reclaim:15m"]["reasons"] == ["oos_expectancy_below_0.15R"]
    assert compute_verdicts(db, SimpleNamespace(strategy_evidence_min_expectancy_r=0.1))[
        "vwap_reclaim:15m"
    ]["passed"]


def test_evidence_gate_is_off_by_default_and_fails_closed_when_on(tmp_path) -> None:
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    _backtest(
        db,
        symbol="AAPL",
        strategy="vwap_reclaim",
        tf="15m",
        trades=50,
        expectancy=5.0,
        holdout_trades=12,
        holdout_expectancy=2.0,
    )
    refresh_verdicts(db, SimpleNamespace(), state)

    off = SimpleNamespace(require_strategy_oos_evidence=False)
    on = SimpleNamespace(require_strategy_oos_evidence=True)
    assert evidence_blocker(off, state, strategy="momentum_breakout", timeframe="15m") is None
    assert evidence_blocker(on, state, strategy="vwap_reclaim", timeframe="15m") is None
    assert (
        evidence_blocker(on, state, strategy="vwap_reclaim", timeframe="5m")
        == "strategy_lacks_oos_evidence"
    )
    assert (
        evidence_blocker(on, state, strategy="never_tested", timeframe="15m")
        == "strategy_lacks_oos_evidence"
    )


# --- Phase 3 -------------------------------------------------------------------


def _execution(db, *, when, pnl, strategy="vwap_reclaim", entry=100.0, stop=99.0, qty=100.0):
    request = {
        "symbol": "AAPL",
        "strategy_name": strategy,
        "stop_loss": stop,
        "metadata": {"timeframe": "15m"},
    }
    response = {"broker_execution": {"filled_avg_price": entry, "filled_qty": qty}}
    with db.connect() as c:
        c.execute(
            "INSERT INTO executions (id, proposal_id, status, mode, request_json, response_json, realized_pnl_usd, created_at, updated_at) "
            "VALUES (?, ?, 'filled', 'alpaca_paper', ?, ?, ?, ?, ?)",
            (
                f"e{when.timestamp()}",
                "p",
                json.dumps(request),
                json.dumps(response),
                pnl,
                when.isoformat(),
                when.isoformat(),
            ),
        )


def _log(db, event, when, payload=None):
    with db.connect() as c:
        c.execute(
            "INSERT INTO run_logs (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event, json.dumps(payload or {}), when.isoformat()),
        )


def test_readiness_bar_and_scorecard(tmp_path) -> None:
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    start = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday
    now = start + timedelta(weeks=5, days=1)
    settings = SimpleNamespace(go_live_phase3_start_date="2026-10-05")
    for i in range(60):  # 30 wins of +$150 (1.5R), 30 losses of -$100 (1R): PF 1.5
        _execution(db, when=start + timedelta(hours=i * 6), pnl=150.0 if i % 2 else -100.0)
    for i, equity in enumerate([100_000, 101_000, 98_500, 102_000]):  # 2.48% dip from 101k
        _log(
            db,
            "alpaca_reconciliation_ok",
            start + timedelta(days=i),
            {"account": {"equity": equity}},
        )

    report = compute_readiness(db, settings, state, now=now)

    assert report["closed_trades"] == 60 and report["profit_factor"] == 1.5
    assert report["max_drawdown_pct"] == round((101_000 - 98_500) / 101_000 * 100, 4)
    assert report["clean_weeks"] == 5
    assert report["ready"] is True, report["blockers"]
    card = report["scorecard"]["vwap_reclaim:15m"]
    assert card["trades"] == 60 and card["wins"] == 30 and card["avg_r"] == 0.25


def test_readiness_blockers(tmp_path) -> None:
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    start = datetime(2026, 10, 5, tzinfo=UTC)
    now = start + timedelta(weeks=5, days=1)
    settings = SimpleNamespace(go_live_phase3_start_date="2026-10-05")
    for i in range(10):
        _execution(db, when=start + timedelta(hours=i), pnl=-50.0)
    _log(db, "alpaca_reconciliation_ok", start, {"account": {"equity": 100_000}})
    _log(db, "alpaca_reconciliation_ok", start + timedelta(days=1), {"account": {"equity": 96_000}})
    _log(
        db, "kill_switch_emergency_stop", now - timedelta(days=8)
    )  # inside the last completed week

    report = compute_readiness(db, settings, state, now=now)

    assert report["ready"] is False
    assert report["blockers"] == [
        "closed_trades_10_below_50",
        "profit_factor_0.00_below_1.3",
        "max_drawdown_4.00pct_not_below_3.0",
        "clean_weeks_0_below_4",
    ]


def test_phase3_not_started_until_evidence_gate_on(tmp_path) -> None:
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    assert compute_readiness(db, SimpleNamespace(), state)["blockers"] == ["phase3_not_started"]
    started = compute_readiness(db, SimpleNamespace(require_strategy_oos_evidence=True), state)
    assert started["phase3_started_at"] is not None


# --- Phase 4 -------------------------------------------------------------------


def _automation(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        execution_mode="live",
        enable_real_trading=True,
        paper_trading_enabled=False,
        **overrides,
    )
    db = Database(settings)
    db.initialize()
    state = RuntimeStateRepository(db)
    return AutomationService(
        settings=settings, runtime_state=state, run_logs=RunLogRepository(db)
    ), state


GO_LIVE_LOCKS = {
    "go_live_readiness_not_met",
    "micro_live_risk_cap_not_active",
    "micro_live_risk_cap_above_0_1_pct",
    "live_operator_acknowledgement_missing",
}


def test_live_mode_is_locked_by_default(tmp_path) -> None:
    automation, _state = _automation(tmp_path)
    locks = set(automation.execution_blockers()) & GO_LIVE_LOCKS
    assert {"go_live_readiness_not_met", "live_operator_acknowledgement_missing"} <= locks
    assert locks & {"micro_live_risk_cap_not_active", "micro_live_risk_cap_above_0_1_pct"}


def test_each_lock_releases_only_when_its_condition_holds(tmp_path) -> None:
    automation, state = _automation(
        tmp_path,
        institutional_portfolio_controls_enabled=True,
        portfolio_micro_live_max_risk_per_trade_pct=0.1,
        live_operator_acknowledgement=LIVE_OPERATOR_ACKNOWLEDGEMENT,
    )
    assert set(automation.execution_blockers()) & GO_LIVE_LOCKS == {"go_live_readiness_not_met"}

    state.set(READINESS_KEY, json.dumps({"ready": True, "computed_at": utc_now().isoformat()}))
    assert readiness_ready(state)
    assert set(automation.execution_blockers()) & GO_LIVE_LOCKS == set()

    automation.settings.portfolio_micro_live_max_risk_per_trade_pct = 0.5
    assert set(automation.execution_blockers()) & GO_LIVE_LOCKS == {
        "micro_live_risk_cap_above_0_1_pct"
    }


def test_paper_mode_has_no_go_live_locks(tmp_path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    db.initialize()
    automation = AutomationService(
        settings=settings, runtime_state=RuntimeStateRepository(db), run_logs=RunLogRepository(db)
    )
    assert not set(automation.execution_blockers()) & GO_LIVE_LOCKS


def test_go_live_readiness_endpoint(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import MockBroker

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    RuntimeStateRepository(app.state.db).set(
        READINESS_KEY, json.dumps({"ready": False, "blockers": ["phase3_not_started"]})
    )

    client = TestClient(app)
    assert client.get("/performance/go-live-readiness").status_code == 403
    body = client.get(
        "/performance/go-live-readiness", headers={"X-Control-Token": "secret"}
    ).json()
    assert body["readiness"]["blockers"] == ["phase3_not_started"]
    assert body["evidence_gate_enforced"] is False and body["real_trading_enabled"] is False
