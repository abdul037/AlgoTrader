"""The parity scenario: the main account's and eToro's decisions with the learning account
configured must equal d2c7faf's without it (tests/test_learning_account_parity.py).

One app (``create_app``, real SQLite, every repository and service real) on a fake main Alpaca
paper client and a fake eToro, a frozen clock (``app.utils.time``) and deterministic ids
(``app.utils.ids``). Backtest rows give the main gate realistic verdicts, OOS-only ones
included (1d pooled OOS passes / holdout fails, 1h too few holdout trades, 15m stress OOS fails,
5m all OOS parts pass / holdouts fail), and realistic ``LiveSignalSnapshot`` candidates go
through the real swing and intraday scans (``run_swing_scan`` / ``run_intraday_scan``, so
``run_scan_task`` and option B's ``PaperOnlyPass`` with its eToro guards). With ``learning`` the
learning account is configured and its thread's passes run between the phases.

This file must run unchanged in a ``git archive d2c7faf`` tree (no learning account there):
``python -m tests.learning_account_parity <out.json>`` from that tree writes the golden.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)  # a Monday, 10:00 in New York
LEARNING_KEYS = ("learning_account:", "strategy_evidence:learning_verdicts")
SYMBOLS = ["NVDA", "AMD", "JPM", "META", "MSFT", "TSLA", "XOM", "UNH", "ORCL", "AVGO"]
PRICES = {"NVDA": 120.0, "AMD": 150.0, "JPM": 200.0, "META": 500.0, "MSFT": 400.0, "TSLA": 250.0,
          "XOM": 110.0, "UNH": 300.0, "ORCL": 180.0, "AVGO": 170.0}
# (strategy, timeframe, symbol, score): one candidate each, ranked by score.
CANDIDATES = {
    "swing": [
        ("inside_bar_narrow_range_breakout", "1d", "NVDA", 92.0),  # passes: the main (eToro) entry
        ("momentum_breakout", "1d", "AMD", 90.0),  # OOS-only: option B, but eToro holds AMD
        ("momentum_breakout", "1d", "JPM", 89.0),  # OOS-only: option B places it (paper-only)
        ("ma_crossover", "1d", "META", 88.0),  # fails with fees, passes without: fee band
        ("trend_following", "1d", "MSFT", 87.0),  # fails both, near: lane
        ("mean_reversion", "1d", "TSLA", 86.0),  # far from it: nobody
        ("ema_trend_stack", "1h", "XOM", 85.0),  # OOS passes, too few holdout trades: B's (one a pass)
    ],
    "intraday": [
        ("vwap_reclaim", "5m", "UNH", 91.0),  # every OOS part passes, holdouts fail: option B
        ("rsi_reversal", "15m", "ORCL", 90.0),  # passes with fees; stress OOS fails: fee band
        ("ma_crossover", "1d", "AVGO", 70.0),  # below the auto score floor
    ],
}


class _FrozenDatetime(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - utc_now's only call
        return cls.current if tz is not None else cls.current.replace(tzinfo=None)


class MainAlpaca:
    """The MAIN account's Alpaca paper client: every call recorded, nothing real."""

    paper = True

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.positions: dict[str, float] = {}

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append([name, *args])

    def get_quote(self, symbol, *, force_refresh=False, timeframe="1d"):
        from app.live_signal_schema import MarketQuote

        symbol = symbol.upper()
        self._call("get_quote", symbol)
        price = PRICES.get(symbol, 100.0)
        return MarketQuote(symbol=symbol, bid=price - 0.01, ask=price + 0.01, last_execution=price,
                           timestamp=_FrozenDatetime.current.isoformat(), source="alpaca", is_primary=True,
                           data_age_seconds=0.5)

    def get_bars(self, symbol, *, timeframe, start, end):
        import pandas as pd

        self._call("get_bars", symbol.upper(), timeframe)
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "vwap"])

    def get_portfolio(self):
        from app.models.execution import AccountSummary, PortfolioPosition, PortfolioSummary

        self._call("get_portfolio")
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=100_000.0, equity=100_000.0, daily_pnl=0.0),
            positions=[
                PortfolioPosition(symbol=s, is_buy=True, leverage=1, quantity=q, average_price=PRICES.get(s, 100.0),
                                  market_value=q * PRICES.get(s, 100.0), unrealized_pnl=0.0)
                for s, q in sorted(self.positions.items())
            ],
        )

    def get_balance(self):
        return self.get_portfolio().account

    def get_account_identity(self):
        self._call("get_account_identity")
        return {"id": "main", "account_number": "PA-MAIN", "status": "ACTIVE", "trading_blocked": False,
                "equity": 100_000.0, "last_equity": 100_000.0, "cash": 100_000.0}

    def is_regular_market_open(self):
        self._call("is_regular_market_open")
        return True

    def market_clock(self):
        self._call("market_clock")
        return True, NOW + timedelta(hours=6)

    def is_supported_equity(self, symbol):
        self._call("is_supported_equity", symbol.upper())
        return True

    def get_capabilities(self):
        self._call("get_capabilities")
        return {"supports_equities": True, "supports_native_protection": True, "paper": True, "verified": False}

    def submit_bracket_order(self, *, symbol, side, qty, take_profit_price, stop_loss_price, time_in_force="day",
                             client_order_id=None, extended_hours=False):
        from app.models.execution import ExecutionRecord

        order = {"symbol": symbol, "side": side, "qty": qty, "take_profit_price": take_profit_price,
                 "stop_loss_price": stop_loss_price, "time_in_force": time_in_force, "client_order_id": client_order_id}
        self._call("submit_bracket_order", order)
        self.orders.append(order)
        self.positions[symbol.upper()] = float(qty)  # filled at once (paper)
        broker_id = f"main-{len(self.orders)}"
        return ExecutionRecord(proposal_id=f"alpaca:{client_order_id}", status="accepted", mode="alpaca_paper",
                               broker_order_id=broker_id,
                               response_payload={"broker_order_id": broker_id, "client_order_id": client_order_id,
                                                 "status": "accepted", "order_class": "bracket", "legs": []})

    def submit_order(self, **kwargs):
        self._call("submit_order", kwargs)
        raise RuntimeError("the scenario sends brackets only")

    def get_all_orders(self, since=None):
        self._call("get_all_orders")
        return []

    def get_executions(self, since=None):
        self._call("get_executions")
        return []

    def get_order(self, broker_order_id):
        self._call("get_order", broker_order_id)
        raise RuntimeError("not found")

    def cancel_order(self, broker_order_id):
        self._call("cancel_order", broker_order_id)
        return True

    def cancel_all_orders(self):
        self._call("cancel_all_orders")
        return 0

    def close_all_positions(self):
        self._call("close_all_positions")
        return 0

    def close_position(self, symbol):
        self._call("close_position", symbol)
        raise RuntimeError("the scenario never closes on the main account")

    def list_supported_instruments(self):
        return []


class Notifier:
    enabled = True

    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, message, *, chat_id=None):
        self.texts.append(str(message))
        return True

    def __getattr__(self, name):  # formatters and the rest: a plain stand-in
        if name.startswith("format_"):
            return lambda *a, **k: name
        raise AttributeError(name)


class Screener:
    """Returns the phase's candidates for the scan's timeframes and records its decisions."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.batch: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.auto_trading = None

    def scan_universe(self, **kwargs):
        from app.models.screener import ScreenerRunResponse

        timeframes = [str(tf).lower() for tf in kwargs.get("timeframes") or []]
        self.calls.append({"timeframes": timeframes, "symbols": list(kwargs.get("symbols") or []),
                           "scan_task": kwargs.get("scan_task"), "spec_keys": kwargs.get("strategy_spec_keys")})
        wanted = set(kwargs.get("strategy_spec_keys") or [])
        chosen = [c for c in self.batch if c.timeframe in timeframes
                  and (not wanted or f"{c.strategy_name}:{c.timeframe}" in wanted)]
        repo = self.app.state.scan_decision_repository
        for candidate in chosen:
            repo.create(scan_task=str(kwargs.get("scan_task") or "scan"), symbol=candidate.symbol,
                        strategy_name=candidate.strategy_name, timeframe=candidate.timeframe, status="passed",
                        final_score=candidate.score, alert_eligible=True, freshness="fresh", reason_codes=[],
                        rejection_reasons=[], payload={"rank": candidate.rank})
        return ScreenerRunResponse(
            generated_at=_FrozenDatetime.current.isoformat(), universe_name="parity", timeframes=timeframes,
            evaluated_symbols=len(kwargs.get("symbols") or []), evaluated_strategy_runs=len(chosen),
            candidates=[c.model_copy(deep=True) for c in chosen],
        )

    def strategy_spec_keys_for_timeframes(self, timeframes):
        wanted = {str(tf).lower() for tf in timeframes}
        keys = {f"{s}:{tf}" for group in CANDIDATES.values() for s, tf, _, _ in group}
        return sorted(key for key in keys if key.rsplit(":", 1)[1] in wanted)


def _snapshot(strategy: str, timeframe: str, symbol: str, score: float, rank: int):
    from app.live_signal_schema import LiveSignalSnapshot, SignalState

    price = PRICES[symbol]
    stop, target = round(price * 0.97, 2), round(price * 1.06, 2)
    generated = _FrozenDatetime.current.isoformat()
    return LiveSignalSnapshot(
        symbol=symbol, strategy_name=strategy, state=SignalState.BUY, timeframe=timeframe, generated_at=generated,
        signal_generated_at=generated, candle_timestamp=generated, current_price=price, current_bid=price - 0.01,
        current_ask=price + 0.01, entry_price=price, stop_loss=stop, take_profit=target, targets=[target],
        risk_reward_ratio=2.0, signal_role="entry_long", direction_label="long", confidence_label="high",
        freshness="fresh", rank=rank, rationale=f"{strategy} setup on {timeframe}", score=score,
        confidence=0.7, tradable=True, execution_ready=True, supported=True, asset_class="equity",
        pass_reasons=["trend_aligned", "volume_confirmed"], reject_reasons=[],
        indicators={"rsi_14": 58.0, "atr_14": round(price * 0.012, 4), "relative_volume": 1.6},
        metadata={"alert_eligible": True, "backtest_validated": True, "signal_classification": "strict_valid",
                  "timeframe": timeframe, "source": "scanner_strategy", "planner": {"timing_label": "immediate"}},
        backtest_snapshot={"oos_trades": 60, "expectancy_r": 0.01},
    )


def _rows(app: Any) -> None:
    """Walk-forward OOS rows behind the main gate's verdicts (eToro-priced, as the batch writes)."""

    from app.storage.repositories import BacktestRepository

    repo = BacktestRepository(app.state.db)
    done = (NOW - timedelta(days=1)).isoformat()

    def row(strategy, timeframe, symbol, trades, expectancy, holdout, holdout_expectancy, *, fees=True, profiles=None):
        metrics = {"number_of_trades": trades, "expectancy_usd": expectancy, "holdout_trades": holdout,
                   "holdout_expectancy_usd": holdout_expectancy, "out_of_sample": True, "fold_count": 9}
        if profiles is not None:
            metrics["cost_profiles"] = profiles
        if fees:
            metrics["etoro_fees_usd"], metrics["holdout_etoro_fees_usd"] = 2.0 * trades, 2.0 * holdout
        repo.create(backtest_id=f"bt-{strategy}-{timeframe}-{symbol}", symbol=symbol, strategy_name=strategy,
                    file_path=f"alpaca:{timeframe}:{symbol}:walk_forward_oos", started_at=done, completed_at=done,
                    metrics=metrics, trades=[])

    def profile(trades, expectancy, holdout, holdout_expectancy):
        return {"number_of_trades": trades, "expectancy_usd": expectancy, "holdout_trades": holdout,
                "holdout_expectancy_usd": holdout_expectancy}

    for symbol in ("SPY", "QQQ"):
        row("inside_bar_narrow_range_breakout", "1d", symbol, 30, 6.0, 8, 4.0)  # passes, +0.06R
        row("momentum_breakout", "1d", symbol, 30, 5.0, 8, -3.0)  # 1d: OOS passes, holdout fails
        row("ma_crossover", "1d", symbol, 30, -0.5, 8, -1.0)  # -0.005R with fees, +0.015R without
        row("trend_following", "1d", symbol, 30, -3.0, 8, -2.0)  # -0.03R: the lane
        row("mean_reversion", "1d", symbol, 30, -9.0, 8, -5.0)  # -0.09R: neither
        row("ema_trend_stack", "1h", symbol, 30, 4.0, 3, 6.0)  # 1h: too few holdout trades
        good, bad = profile(30, 3.0, 8, 2.0), profile(30, -1.0, 8, -1.0)
        row("rsi_reversal", "15m", symbol, 30, 2.0, 8, 1.0, profiles={"alpaca": good, "stress": bad})  # stress OOS fails
        row("vwap_reclaim", "5m", symbol, 30, 3.0, 8, -1.0,
            profiles={"alpaca": profile(30, 4.0, 8, -0.5), "stress": profile(30, 1.0, 8, -2.0)})  # holdouts fail


def _seed(app: Any) -> None:
    from app.broker.etoro_live_mirror import STATE_KEY
    from app.performance.strategy_evidence import refresh_verdicts
    from app.storage.repositories import RuntimeStateRepository

    state = RuntimeStateRepository(app.state.db)
    app.state.automation_service.set_account_verified(True)
    state.set(STATE_KEY, json.dumps({"open_symbols": ["AMD"]}))  # the real eToro account holds AMD
    with app.state.db.connect() as connection:
        for strategy in sorted({s for group in CANDIDATES.values() for s, _, _, _ in group}):
            connection.execute(
                "INSERT INTO strategy_versions (id, strategy_name, code_version, parameters_json, dataset_version,"
                " timeframe, status, created_at) VALUES (?, ?, 'v1', '{}', 'd1', 'any', 'paper_exploration', ?)",
                (f"sv-{strategy}", strategy, NOW.isoformat()),
            )
            connection.execute(
                "INSERT INTO promotion_decisions (id, strategy_version_id, strategy_audit_id, target_stage, approved,"
                " blockers_json, evidence_json, decided_by, created_at) VALUES (?, ?, NULL, 'paper_exploration', 1,"
                " '[]', '{}', 'operator', ?)",
                (f"pd-{strategy}", f"sv-{strategy}", NOW.isoformat()),
            )
    _rows(app)
    refresh_verdicts(app.state.db, app.state.settings, state, app.state.run_log_repository)


def _settings(workdir: Path, learning: bool):
    from tests.conftest import make_settings

    values: dict[str, Any] = dict(
        alpaca_enabled=True, alpaca_api_key="main-key", alpaca_secret_key="main-secret",
        alpaca_expected_account_number="PA-MAIN", broker_for_equities="alpaca", paper_broker="alpaca",
        execution_mode="paper", enable_real_trading=False, market_universe_symbols=SYMBOLS,
        allowed_instruments=SYMBOLS, require_strategy_oos_evidence=True, paper_oos_bar_enabled=True,
        auto_propose_enabled=True, paper_auto_approve_proposals=True, auto_execution_worker_enabled=True,
        paper_auto_operation_mode="unattended", paper_auto_approval_tier="tier2_strict_valid",
        paper_auto_min_clean_supervised_lifecycles=0, paper_scanner_exploration_enabled=True,
        paper_scanner_bypass_production_approval=True, paper_scanner_allowed_strategies=["all"],
        auto_execution_min_score=80.0, paper_exploration_auto_execution_min_score=80.0,
        auto_propose_risk_based_sizing=True, max_open_positions=12, max_trades_per_day=10,
        default_trade_amount_usd=10_000.0, max_trade_amount_usd=10_000.0, market_direction_min_drop_pct=0.0,
        screener_intraday_timeframes=["5m", "15m", "1h", "1d"], swing_scan_timeframes=["1h", "1d"],
        track_alerted_signals=True, market_data_cache_dir=str(workdir / "cache"),
        primary_market_data_provider="alpaca", fallback_market_data_provider="none",
        intraday_active_mover_shortlist_enabled=False, swing_scan_evidence_focus=False,
        screener_spec_batch_mode="off", screener_spec_batch_size=0,
    )
    if learning:
        values.update(alpaca_learning_api_key="learn-key", alpaca_learning_secret_key="learn-secret",
                      alpaca_learning_expected_account_number="PA-LEARN")
    return make_settings(workdir, **values)


class LearningBroker:
    """The learning account at Alpaca (only with ``learning``): accepts and later fills."""

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.held: dict[str, float] = {}

    def identity(self):
        self.calls.append(["identity"])
        return {"account_number": "PA-LEARN", "status": "ACTIVE", "trading_blocked": False, "equity": 25_000.0}

    def clock(self):
        self.calls.append(["clock"])
        return {"is_open": True, "next_open": NOW + timedelta(days=1), "next_close": NOW + timedelta(hours=6)}

    def positions(self):
        self.calls.append(["positions"])
        return dict(self.held)

    def submit_bracket(self, *, symbol, qty, take_profit, stop_loss, client_order_id):
        self.calls.append(["submit_bracket", symbol, qty, client_order_id])
        oid = f"lo{len(self.orders) + 1}"
        self.orders[oid] = {"id": oid, "client_order_id": client_order_id, "symbol": symbol, "status": "accepted",
                            "qty": float(qty), "filled_qty": 0.0, "filled_avg_price": None, "filled_at": None,
                            "legs": [{"id": f"{oid}-tp", "type": "limit", "status": "new"},
                                     {"id": f"{oid}-sl", "type": "stop", "status": "held"}]}
        return dict(self.orders[oid])

    def fill_all(self):
        for order in self.orders.values():
            if order["status"] == "accepted":
                order.update(status="filled", filled_qty=order["qty"], filled_avg_price=PRICES[order["symbol"]],
                             filled_at=_FrozenDatetime.current.isoformat())
                self.held[order["symbol"]] = order["qty"]

    def order(self, broker_order_id):
        self.calls.append(["order", broker_order_id])
        return json.loads(json.dumps(self.orders[broker_order_id]))

    def order_by_client_id(self, client_order_id):
        self.calls.append(["order_by_client_id", client_order_id])
        for order in self.orders.values():
            if order["client_order_id"] == client_order_id:
                return json.loads(json.dumps(order))
        return None

    def cancel(self, broker_order_id):
        self.calls.append(["cancel", broker_order_id])

    def submit_close(self, *, symbol, qty, client_order_id):
        self.calls.append(["submit_close", symbol, qty, client_order_id])
        raise RuntimeError("no close in this scenario")


def _table(db: Any, name: str, *, order: str = "rowid") -> list[dict[str, Any]]:
    with db.connect() as connection:
        rows = connection.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()  # noqa: S608
    return [dict(row) for row in rows]


def capture(app: Any, main: MainAlpaca, results: list[Any], screener: Screener) -> dict[str, Any]:
    db = app.state.db
    tables = ["approvals", "execution_queue", "executions", "scan_decisions", "paper_positions", "paper_trades",
              "tracked_signals", "alert_history", "instrument_blacklist", "strategy_health", "broker_order_snapshots",
              "broker_position_snapshots", "reconciliation_runs", "learning_decision_snapshots",
              "learning_lifecycle_events", "learning_outcome_labels", "signals", "backtests"]
    out: dict[str, Any] = {name: _table(db, name) for name in tables}
    out["runtime_state"] = [
        row for row in _table(db, "runtime_state", order="state_key") if not row["state_key"].startswith(LEARNING_KEYS)
    ]
    logs = []
    for row in _table(db, "run_logs", order="id"):
        payload = json.loads(row["payload_json"])
        if isinstance(payload, dict) and payload.get("learning_account") is True:
            continue
        logs.append({"event_type": row["event_type"], "payload": payload, "created_at": row["created_at"]})
    out["run_logs"] = logs
    out["main_orders"] = main.orders
    out["main_calls"] = main.calls
    out["results"] = [result.model_dump() for result in results]
    out["screener_calls"] = screener.calls
    out["notices"] = [text for text in app.state.telegram_notifier.texts if not text.startswith("LEARNING (paper")]
    return json.loads(json.dumps(out, default=str, sort_keys=True))


def run_scenario(workdir: Path, *, learning: bool) -> dict[str, Any]:
    """Run every phase; returns the captured state (and, with ``learning``, what it did)."""

    import app.utils.ids as ids
    import app.utils.time as clock

    counter = {"n": 0}

    def next_uuid():
        counter["n"] += 1
        return SimpleNamespace(hex=f"{counter['n']:012x}{'0' * 20}")

    from app.data.market_data import MarketDataService

    def no_network(*args, **kwargs):
        raise RuntimeError("no network in the parity scenario")

    import app.workflow.paper_only as paper_only

    paper_only._LOGGED.clear()  # option B logs a block once a day per process: each run starts afresh
    saved = (clock.datetime, ids.uuid4, MarketDataService.load_yfinance)
    clock.datetime, ids.uuid4 = _FrozenDatetime, next_uuid
    MarketDataService.load_yfinance = no_network
    _FrozenDatetime.current = NOW
    try:
        return _phases(workdir, learning)
    finally:
        clock.datetime, ids.uuid4, MarketDataService.load_yfinance = saved


def _phases(workdir: Path, learning: bool) -> dict[str, Any]:
    from app.main import create_app
    from tests.conftest import MockBroker

    main = MainAlpaca()
    app = create_app(settings=_settings(workdir, learning), broker=MockBroker(), alpaca_client=main,
                     market_data_client=SimpleNamespace(get_rates=lambda symbols, timeframe=None: {}),
                     telegram_notifier=Notifier(), enable_background_jobs=False)
    screener = Screener(app)
    workflow = app.state.workflow_service
    workflow.market_screener = screener
    _seed(app)
    account = getattr(app.state, "learning_account", None) if learning else None
    broker = LearningBroker()
    marks: list[int] = []  # main-client call counts around each learning pass
    if learning:
        assert account is not None, "the learning account is not attached"
        account._client_factory = lambda settings: broker

    def learning_pass() -> None:
        if account is not None:
            start = len(main.calls)
            account.tick()
            marks.append((start, len(main.calls)))

    results = []
    batch = [_snapshot(s, tf, sym, score, rank) for rank, (s, tf, sym, score) in enumerate(CANDIDATES["swing"], 1)]
    screener.batch = batch
    results.append(workflow.run_swing_scan(notify=False))
    _FrozenDatetime.current = NOW + timedelta(seconds=40)
    learning_pass()  # the learning thread's first pass after the scan's offer
    _FrozenDatetime.current = NOW + timedelta(minutes=2)
    screener.batch = [_snapshot(s, tf, sym, score, rank) for rank, (s, tf, sym, score) in enumerate(CANDIDATES["intraday"], 1)]
    results.append(workflow.run_intraday_scan(notify=False))
    _FrozenDatetime.current = NOW + timedelta(minutes=2, seconds=30)
    learning_pass()
    _FrozenDatetime.current = NOW + timedelta(minutes=4)
    broker.fill_all()
    learning_pass()  # the fills
    _FrozenDatetime.current = NOW + timedelta(minutes=5)
    screener.batch = batch  # the same setups again a few minutes on
    results.append(workflow.run_swing_scan(notify=False))
    _FrozenDatetime.current = NOW + timedelta(minutes=5, seconds=40)
    learning_pass()
    _FrozenDatetime.current = NOW + timedelta(minutes=35)  # the learning verdicts refresh after 30 min
    screener.batch = [_snapshot(s, tf, sym, score, rank) for rank, (s, tf, sym, score) in enumerate(CANDIDATES["intraday"], 1)]
    results.append(workflow.run_intraday_scan(notify=False))
    _FrozenDatetime.current = NOW + timedelta(minutes=35, seconds=30)
    learning_pass()  # its third entry of the day
    _FrozenDatetime.current = NOW + timedelta(minutes=36)
    # The main account's own reconcile: this fake lists no orders, so it sees unprotected
    # positions and trips the main circuit breaker (kill switch and emergency stop).
    app.state.reconciliation_service.reconcile()
    _FrozenDatetime.current = NOW + timedelta(minutes=37)
    screener.batch = batch
    results.append(workflow.run_swing_scan(notify=False))
    _FrozenDatetime.current = NOW + timedelta(minutes=37, seconds=30)
    learning_pass()  # the kill switch stops learning entries; nothing of it is flattened
    out = capture(app, main, results, screener)
    if learning:
        extra = [call for start, end in marks for call in main.calls[start:end]]
        main_calls = [call for i, call in enumerate(main.calls) if not any(s <= i < e for s, e in marks)]
        out["main_calls"] = json.loads(json.dumps(main_calls, default=str))
        out["learning"] = {
            "extra_main_calls": json.loads(json.dumps(extra, default=str)),
            "broker_calls": broker.calls,
            "book": json.loads(_book(app)),
            "skipped": _learning_skips(app),
        }
    return out


def _learning_skips(app: Any) -> list[list[Any]]:
    rows = _table(app.state.db, "run_logs", order="id")
    payloads = [json.loads(row["payload_json"]) for row in rows if row["event_type"] == "learning_account_skipped"]
    return [[p.get("reason"), p.get("symbol")] for p in payloads]


def _book(app: Any) -> str:
    from app.learning_account import store as records

    return records.Store(app.state.db).read(records.BOOK_KEY) or "{}"


def main(argv: list[str]) -> None:
    out = Path(argv[1]) if len(argv) > 1 else Path("learning_account_parity_golden.json")
    with tempfile.TemporaryDirectory() as tmp:
        result = run_scenario(Path(tmp), learning=False)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv)
