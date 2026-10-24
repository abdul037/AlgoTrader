"""Fakes for the learning-account tests: a scripted learning broker, the main path's own
``PaperAutoTradingService`` on fake safety records (its option B guard collaborators raise if
touched), a fake data provider, a real SQLite runtime_state and a hand-moved clock. No network,
no wall clock."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from app.automation.unattended import PaperAutoTradingService
from app.learning_account import store as records
from app.learning_account.account import LearningAccount
from app.learning_account.market import BoundedReads, MarketReads
from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.performance.strategy_evidence import VERDICTS_KEY
from app.storage.db import Database
from tests.conftest import make_settings

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)  # a Monday, 10:00 in New York
CLOSE = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
NEXT_OPEN = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)
BASE = {
    "execution_mode": "paper",
    "enable_real_trading": False,
    "alpaca_api_key": "main-key",
    "alpaca_secret_key": "main-secret",
    "alpaca_expected_account_number": "PA-MAIN",
    "alpaca_learning_api_key": "learn-key",
    "alpaca_learning_secret_key": "learn-secret",
    "alpaca_learning_expected_account_number": "PA-LEARN",
    "require_strategy_oos_evidence": True,
    "paper_oos_bar_enabled": True,
    "auto_execution_min_score": 65.0,
    "paper_scanner_exploration_enabled": False,
    "auto_propose_risk_based_sizing": True,
    "max_risk_per_trade_pct": 1.0,
    "default_trade_amount_usd": 10_000.0,
    "max_trade_amount_usd": 10_000.0,
    "market_direction_min_drop_pct": 0.3,
}
# The main gate's verdicts (as cached): every key the tests use.
FAILS = {"passed": False, "paper_oos_passed": False, "oos_expectancy_r": -0.01, "oos_trades": 80}
VERDICTS = {
    "momentum_breakout:1d": dict(FAILS),  # fee band (learning passes)
    "ma_crossover:1d": dict(FAILS),  # lane (learning fails, near)
    "trend_following:1d": dict(FAILS),  # outside the lane
    "inside_bar_narrow_range_breakout:1d": {"passed": True, "paper_oos_passed": True, "oos_expectancy_r": 0.04},
    "relative_strength_momentum:1d": {"passed": False, "paper_oos_passed": True, "oos_expectancy_r": 0.05},
    "vwap_reclaim:5m": {**FAILS, "paper_passed": False},
    "rsi_reversal:15m": {"passed": True, "paper_oos_passed": False, "paper_passed": False, "oos_expectancy_r": 0.02},
}
LEARNING = {
    "momentum_breakout:1d": {"basis": "fee_free", "passed": True, "oos_trades": 90, "oos_expectancy_r": 0.012},
    "ma_crossover:1d": {"basis": "fee_free", "passed": False, "oos_trades": 60, "oos_expectancy_r": -0.02},
    "trend_following:1d": {"basis": "fee_free", "passed": False, "oos_trades": 60, "oos_expectancy_r": -0.08},
    "relative_strength_momentum:1d": {"basis": "fee_free", "passed": True, "oos_trades": 90, "oos_expectancy_r": 0.07},
    "vwap_reclaim:5m": {"basis": "alpaca", "passed": True, "oos_trades": 120, "oos_expectancy_r": 0.03},
    "rsi_reversal:15m": {"basis": "alpaca", "passed": True, "oos_trades": 200, "oos_expectancy_r": 0.04},
}


class Clock:
    def __init__(self, now: datetime = NOW):
        self.value = now

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> datetime:
        self.value = self.value + timedelta(seconds=seconds)
        return self.value


class Boom:
    """A collaborator the learning thread must never touch (option B's guard reads)."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(f"learning thread touched {self._name}.{attribute}")


class FakeState(dict):
    def __init__(self, values: dict | None = None):
        super().__init__(values or {})
        self.reads: list[str] = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class FakeSafety:
    def __init__(self, blacklisted=(), inactive=()):
        self.blacklisted, self.inactive = set(blacklisted), set(inactive)

    def is_blacklisted(self, symbol):
        return symbol in self.blacklisted

    def strategy_active(self, strategy):
        return strategy not in self.inactive


class FakeAutomation:
    def __init__(self, blockers=(), fail=False):
        self.blockers, self.fail = list(blockers), fail

    def scan_blockers(self):
        if self.fail:
            raise RuntimeError("automation down")
        return list(self.blockers)


class FakeLogs:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event, payload):
        self.events.append((event, json.loads(json.dumps(payload, default=str))))

    def named(self, event):
        return [payload for name, payload in self.events if name == event]


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []

    def send_text(self, text, **_):
        self.texts.append(text)
        return True


class FakeProvider:
    """The shared engine's Alpaca provider (the main account's data client)."""

    def __init__(self, price: float = 100.0):
        self.price = price
        self.quote_error: Exception | None = None
        self.calls: list[tuple[str, Any]] = []
        self.bars: dict[str, pd.DataFrame] = {}

    def get_quote(self, symbol):
        self.calls.append(("get_quote", symbol))
        if self.quote_error is not None:
            raise self.quote_error
        return MarketQuote(
            symbol=symbol, bid=self.price - 0.01, ask=self.price + 0.01, last_execution=self.price,
            source="alpaca", data_age_seconds=1.0,
        )

    def get_bars(self, symbol, *, timeframe, start, end, cache=True):
        self.calls.append(("get_bars", (symbol, timeframe, cache)))
        if symbol not in self.bars:
            raise RuntimeError("no bars")
        return self.bars[symbol]


class FakeEngine:
    def __init__(self, provider: Any):
        self.alpaca_provider = provider


class APIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FakeBroker:
    """The learning account at Alpaca: identity, clock, positions and orders (scripted)."""

    def __init__(self, account_number: str = "PA-LEARN", equity: float = 50_000.0):
        self.account = {"account_number": account_number, "status": "ACTIVE", "trading_blocked": False, "equity": equity}
        self.identity_error: Exception | None = None
        self.clock_value = {"is_open": True, "next_open": NEXT_OPEN, "next_close": CLOSE}
        self.orders: dict[str, dict[str, Any]] = {}
        self.by_client: dict[str, str] = {}
        self.held: dict[str, float] = {}
        self.calls: list[tuple[str, Any]] = []
        self.submit_error: Exception | None = None
        self.close_error: Exception | None = None
        self.fill_closes = False
        self.positions_error: Exception | None = None
        self.on_submit: Any = None

    # reads
    def identity(self):
        self.calls.append(("identity", None))
        if self.identity_error is not None:
            raise self.identity_error
        return dict(self.account)

    def clock(self):
        self.calls.append(("clock", None))
        return dict(self.clock_value)

    def positions(self):
        self.calls.append(("positions", None))
        if self.positions_error is not None:
            raise self.positions_error
        return dict(self.held)

    def order(self, broker_order_id):
        self.calls.append(("order", broker_order_id))
        return copy.deepcopy(self.orders[broker_order_id])

    def order_by_client_id(self, client_order_id):
        self.calls.append(("order_by_client_id", client_order_id))
        broker_id = self.by_client.get(client_order_id)
        return None if broker_id is None else copy.deepcopy(self.orders[broker_id])

    # writes
    def _new(self, client_order_id, **fields):
        broker_id = f"o{len(self.orders) + 1}"
        order = {"id": broker_id, "client_order_id": client_order_id, "filled_qty": 0.0, "filled_avg_price": None,
                 "filled_at": None, "legs": [], **fields}
        self.orders[broker_id] = order
        self.by_client[client_order_id] = broker_id
        return order

    def submit_bracket(self, *, symbol, qty, take_profit, stop_loss, client_order_id):
        self.calls.append(("submit_bracket", (symbol, qty, take_profit, stop_loss, client_order_id)))
        if self.on_submit is not None:
            self.on_submit(symbol)
        if self.submit_error is not None:
            raise self.submit_error
        order = self._new(client_order_id, symbol=symbol, side="buy", type="market", status="accepted", qty=float(qty))
        order["legs"] = [
            {"id": f"{order['id']}-tp", "type": "limit", "status": "new", "limit_price": take_profit, "filled_avg_price": None, "filled_at": None},
            {"id": f"{order['id']}-sl", "type": "stop", "status": "held", "stop_price": stop_loss, "filled_avg_price": None, "filled_at": None},
        ]
        return copy.deepcopy(order)

    def submit_close(self, *, symbol, qty, client_order_id):
        self.calls.append(("submit_close", (symbol, qty, client_order_id)))
        if self.close_error is not None:
            raise self.close_error
        order = self._new(client_order_id, symbol=symbol, side="sell", type="market", status="accepted", qty=float(qty))
        if self.fill_closes:
            self.fill_close(order["id"], 101.0)
        return copy.deepcopy(self.orders[order["id"]])

    def cancel(self, broker_order_id):
        self.calls.append(("cancel", broker_order_id))
        for order in self.orders.values():
            for leg in order["legs"]:
                if leg["id"] == broker_order_id and leg["status"] not in {"filled", "canceled"}:
                    leg["status"] = "canceled"

    # the market
    def entry(self, symbol):
        return next(o for o in self.orders.values() if o["symbol"] == symbol and o["side"] == "buy")

    def fill_entry(self, symbol, price=100.0, at=NOW):
        order = self.entry(symbol)
        order.update(status="filled", filled_qty=order["qty"], filled_avg_price=price, filled_at=at.isoformat())
        self.held[symbol] = order["qty"]

    def fill_leg(self, symbol, kind, price, at=NOW):
        order = self.entry(symbol)
        for leg in order["legs"]:
            if leg["type"] == kind:
                leg.update(status="filled", filled_avg_price=price, filled_at=at.isoformat())
            else:
                leg["status"] = "canceled"
        self.held.pop(symbol, None)

    def fill_close(self, broker_id, price, at=NOW):
        order = self.orders[broker_id]
        order.update(status="filled", filled_qty=order["qty"], filled_avg_price=price, filled_at=at.isoformat())
        self.held.pop(order["symbol"], None)

    def sent(self, name):
        return [args for call, args in self.calls if call == name]


class CountingStore(records.Store):
    def __init__(self, db):
        super().__init__(db)
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.before_cas: Any = None
        self.fail_cas: set[str] = set()

    def read(self, key):
        self.reads.append(key)
        return super().read(key)

    def read_many(self, keys):
        self.reads.extend(keys)
        return super().read_many(keys)

    def cas(self, key, expected, value):
        self.writes.append(key)
        if self.before_cas is not None:
            self.before_cas(key)
        if key in self.fail_cas:
            self.fail_cas.discard(key)
            raise RuntimeError("write failed")
        return super().cas(key, expected, value)

    def put(self, key, value):
        self.writes.append(key)
        return super().put(key, value)


def settings_for(tmp_path, **overrides):
    return make_settings(tmp_path, **{**BASE, **overrides})


def database(settings):
    db = Database(settings)
    db.initialize()
    return db


def snapshot(symbol="NVDA", strategy="momentum_breakout", timeframe="1d", *, score=80.0, price=100.0,
             stop=95.0, target=110.0, **metadata) -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol=symbol, strategy_name=strategy, state=SignalState.BUY, timeframe=timeframe, score=score,
        execution_ready=True, entry_price=price, current_price=price, stop_loss=stop, take_profit=target,
        signal_role="entry_long",
        metadata={"alert_eligible": True, "backtest_validated": True, "signal_classification": "strict_valid", **metadata},
    )


def main_verdicts(verdicts=None, computed_at=NOW):
    return json.dumps({"computed_at": computed_at.isoformat(), "verdicts": verdicts if verdicts is not None else VERDICTS})


def auto_trading(settings, verdicts=None, *, safety=None, runtime_state=None):
    state = runtime_state if runtime_state is not None else FakeState({VERDICTS_KEY: main_verdicts(verdicts)})
    return PaperAutoTradingService(
        settings=settings,
        proposal_service=Boom("proposals"),
        execution_coordinator=Boom("execution"),
        automation=Boom("main_automation"),
        reconciliation=Boom("reconciliation"),
        safety_state=safety or FakeSafety(),
        executions=Boom("executions"),
        run_logs=Boom("main_run_logs"),
        notifier=Boom("main_notifier"),
        alpaca_client=Boom("main_alpaca"),
        strategy_governance=None,
        institutional_governance=None,
        paper_trading_service=Boom("paper_trading"),
        runtime_state=state,
    )


def sync_reads() -> BoundedReads:
    return BoundedReads(spawn=lambda target: target())


class Rig:
    """One learning account on a real SQLite runtime_state, with every collaborator faked."""

    def __init__(self, tmp_path, *, settings=None, verdicts=None, learning=None, broker=None, clock=None,
                 owner="host-a:1:aaaa", db=None, automation=None, safety=None, provider=None, seed=True, **overrides):
        self.settings = settings or settings_for(tmp_path, **overrides)
        self.db = db or database(self.settings)
        self.store = CountingStore(self.db)
        self.clock = clock or Clock()
        self.broker = broker or FakeBroker()
        self.logs, self.notifier = FakeLogs(), FakeNotifier()
        self.provider = provider or FakeProvider()
        self.automation = automation or FakeAutomation()
        self.auto = auto_trading(self.settings, verdicts, safety=safety)
        if seed and learning is not False:
            payload = {"computed_at": self.clock().isoformat(), "strategies": sorted({k.split(":")[0] for k in (learning or LEARNING)}),
                       "verdicts": learning or LEARNING}
            records.Store(self.db).put(records.LEARNING_VERDICTS_KEY, records.dumps(payload))
        self.account = LearningAccount(
            settings=self.settings, db=self.db, run_logs=self.logs, notifier=self.notifier,
            market_data_engine=FakeEngine(self.provider), auto_trading=self.auto, automation=self.automation,
            client_factory=lambda _settings: self.broker, now=self.clock, owner=owner, store=self.store,
            market=MarketReads(FakeEngine(self.provider), self.settings, now=self.clock, reads=sync_reads()),
        )

    def book(self) -> dict[str, dict[str, Any]]:
        return records.load_book(records.Store(self.db)).trades

    def closed(self) -> list[dict[str, Any]]:
        return json.loads(records.Store(self.db).read(records.CLOSED_KEY) or "[]")

    def offer(self, *candidates, at=None):
        return self.account.offer(list(candidates), (at or self.clock()).isoformat())

    def tick(self, seconds: float = 0.0):
        if seconds:
            self.clock.advance(seconds)
        self.account.tick()

    def skipped(self) -> list[str]:
        return [payload["reason"] for payload in self.logs.named("learning_account_skipped")]
