"""Wiring of the separate paper learning account: settings, the client, attach, the thread's
owner process, the one hook at the end of ``run_scan_task``, the boot snapshot and the
live-scorecard section. No network: the Alpaca SDK's HTTP session is faked where it is used."""

from __future__ import annotations

import json
import sys
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.learning_account as learning_package
from app.learning_account import account as account_module
from app.learning_account import route as routes
from app.learning_account import store as records
from app.learning_account.client import PAPER_URL, TIMEOUT_SECONDS, LearningClient, build_client, order_dict
from app.learning_account.report import STALLED_SECONDS, learning_account_report, status
from app.live_signal_schema import MarketQuote
from tests.learning_account_fakes import BASE, NOW, FakeState, Rig, settings_for, snapshot


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, routes.PENDING),
        ({"learning_account_enabled": False}, routes.SETTING_OFF),
        ({"alpaca_learning_api_key": ""}, routes.NOT_CONFIGURED),
        ({"alpaca_learning_secret_key": " "}, routes.NOT_CONFIGURED),
        ({"enable_real_trading": True}, routes.NOT_PAPER),
        ({"execution_mode": "live"}, routes.NOT_PAPER),
        ({"alpaca_expected_account_number": ""}, routes.MAIN_ACCOUNT_UNKNOWN),
        ({"alpaca_learning_expected_account_number": ""}, routes.LEARNING_ACCOUNT_UNKNOWN),
        ({"alpaca_learning_expected_account_number": "pa-main"}, routes.SAME_ACCOUNT),
        ({"alpaca_learning_api_key": "main-key"}, routes.SAME_KEYS),
        ({"alpaca_learning_secret_key": "main-secret"}, routes.SAME_KEYS),
    ],
)
def test_the_settings_route(tmp_path, overrides, expected) -> None:
    assert routes.settings_route(settings_for(tmp_path, **overrides)) == expected


def test_the_identity_route(tmp_path) -> None:
    settings = settings_for(tmp_path)
    ok = {"account_number": "PA-LEARN", "status": "ACTIVE", "trading_blocked": False}
    assert routes.identity_route(settings, ok) == routes.RUNNING
    assert routes.identity_route(settings, {**ok, "account_number": "PA-OTHER"}) == routes.ACCOUNT_MISMATCH
    assert routes.identity_route(settings, {**ok, "account_number": ""}) == routes.VERIFICATION_FAILED
    assert routes.identity_route(settings, None) == routes.VERIFICATION_FAILED
    assert routes.identity_route(settings, {**ok, "trading_blocked": True}) == routes.TRADING_BLOCKED
    assert routes.identity_route(settings, {**ok, "status": "ACCOUNT_CLOSED"}) == routes.TRADING_BLOCKED
    assert routes.retry_seconds(routes.VERIFICATION_FAILED) == 120.0
    assert routes.retry_seconds(routes.ACCOUNT_MISMATCH) == routes.retry_seconds(routes.TRADING_BLOCKED) == 900.0


def test_the_settings_default_on_and_inert_without_keys() -> None:
    from app.runtime_settings import AppSettings

    fields = AppSettings.model_fields
    assert fields["learning_account_enabled"].default is True
    for name in ("alpaca_learning_api_key", "alpaca_learning_secret_key", "alpaca_learning_expected_account_number"):
        assert fields[name].default == ""
    assert not any(name.startswith("alpaca_learning") and "url" in name for name in fields)  # no base URL


def test_the_boot_snapshot_shows_booleans_and_the_route_never_a_value(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy

    snapshot_ = effective_execution_policy(settings_for(tmp_path))
    assert snapshot_["learning_account_enabled"] is True
    assert snapshot_["learning_account_configured"] is True
    assert snapshot_["learning_account_expected_account_configured"] is True
    assert snapshot_["learning_account_route"] == routes.PENDING
    text = repr(snapshot_)
    assert "learn-key" not in text and "learn-secret" not in text and "PA-LEARN" not in text
    off = effective_execution_policy(settings_for(tmp_path, alpaca_learning_api_key="", alpaca_learning_expected_account_number=""))
    assert (off["learning_account_configured"], off["learning_account_expected_account_configured"]) == (False, False)
    assert off["learning_account_route"] == routes.NOT_CONFIGURED


class _Response:
    status_code = 200
    text = '{"is_open": true, "timestamp": "2026-10-26T10:00:00-04:00", "next_open": "2026-10-27T09:30:00-04:00", "next_close": "2026-10-26T16:00:00-04:00"}'

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.text)


def test_the_client_is_paper_only_on_its_own_keys_with_a_ten_second_timeout(tmp_path) -> None:
    client = build_client(settings_for(tmp_path))
    trading = client.trading
    assert trading._base_url == PAPER_URL == "https://paper-api.alpaca.markets" and trading._sandbox is True
    assert (trading._api_key, trading._secret_key) == ("learn-key", "learn-secret")
    seen: list[dict] = []
    trading._session = SimpleNamespace(request=lambda method, url, **opts: seen.append({"url": url, **opts}) or _Response())
    clock = client.clock()
    assert clock["is_open"] is True
    assert seen[0]["timeout"] == TIMEOUT_SECONDS == 10.0
    assert seen[0]["url"].startswith(PAPER_URL)


class _Trading:
    """The SDK client's surface the learning client uses (no HTTP)."""

    def __init__(self):
        self.requests: list = []
        legs = [SimpleNamespace(id="tp", client_order_id="", symbol="NVDA", side="sell", type="limit", status="new",
                                qty="5", filled_qty="0", filled_avg_price=None, stop_price=None, limit_price="110", filled_at=None)]
        self.nested = SimpleNamespace(id="o1", client_order_id="la-e-x", symbol="NVDA", side="buy", type="market",
                                      status="filled", qty="5", filled_qty="5", filled_avg_price="100.1",
                                      stop_price=None, limit_price=None, filled_at="2026-10-26T14:00:01+00:00", legs=legs)

    def submit_order(self, request):
        self.requests.append(request)
        return self.nested

    def get_order_by_client_id(self, client_order_id):
        self.requests.append(("by_client", client_order_id))
        if client_order_id == "missing":
            raise RuntimeError('{"code":40410000,"message":"order not found"}')
        if client_order_id == "down":
            raise TimeoutError("read timed out")
        return SimpleNamespace(id="o1", legs=None)  # not nested

    def get_order_by_id(self, broker_order_id, filter=None):
        self.requests.append(("by_id", broker_order_id, getattr(filter, "nested", None)))
        return self.nested

    def cancel_order_by_id(self, broker_order_id):
        raise RuntimeError("order is already in filled state")


def test_the_client_sends_a_gtc_bracket_and_re_reads_by_broker_id_nested() -> None:
    trading = _Trading()
    client = LearningClient(trading)
    order = client.submit_bracket(symbol="nvda", qty=5, take_profit=110.004, stop_loss=94.996, client_order_id="la-e-x")
    (request,) = trading.requests
    assert request.symbol == "NVDA" and request.qty == 5 and request.client_order_id == "la-e-x"
    assert str(request.order_class.value) == "bracket" and str(request.time_in_force.value) == "gtc"
    assert request.take_profit.limit_price == 110.0 and request.stop_loss.stop_price == 95.0
    assert order["filled_avg_price"] == 100.1 and order["legs"][0]["type"] == "limit"
    trading.requests.clear()
    assert client.order_by_client_id("la-e-x")["legs"]  # the nested re-read
    assert trading.requests == [("by_client", "la-e-x"), ("by_id", "o1", True)]
    assert client.order_by_client_id("missing") is None
    with pytest.raises(TimeoutError):
        client.order_by_client_id("down")  # a failed read is not "no such order"
    client.cancel("tp")  # already done: not an error
    close = client.submit_close(symbol="NVDA", qty=5, client_order_id="la-x-1")
    assert close["id"] == "o1" and str(trading.requests[-1].side.value) == "sell"
    assert str(trading.requests[-1].time_in_force.value) == "day"
    assert order_dict(SimpleNamespace())["legs"] == []


def _app(tmp_path, **overrides):
    from app.main import create_app

    settings = settings_for(tmp_path, alpaca_enabled=False, **overrides)
    return create_app(settings=settings, enable_background_jobs=False)


def test_attach_builds_one_account_for_the_workflow_and_background_off_never_starts_it(tmp_path, monkeypatch) -> None:
    started: list = []
    monkeypatch.setattr(account_module.LearningAccount, "start", lambda self: started.append(self) or True)
    app = _app(tmp_path)
    account = app.state.learning_account
    assert account is not None and app.state.workflow_service.learning_account is account
    with TestClient(app):
        pass  # startup and shutdown ran: no cadence worker in this process, so no thread
    assert started == [] and account._finished  # shutdown stopped it for good


def test_it_starts_only_with_the_cadence_and_never_after_stop(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path)
    account = app.state.learning_account
    calls: list = []
    monkeypatch.setattr(account, "start", lambda: calls.append("start") or True)
    app.state.scheduler_worker = object()  # this process runs the cadence
    handler = app.router.on_startup[-1]
    handler()
    assert calls == ["start"]
    fresh = Rig(tmp_path / "real")
    fresh.account.stop()
    assert fresh.account.start() is False and fresh.account._thread is None


def test_polling_mode_runs_it_from_the_bot_process(tmp_path, monkeypatch) -> None:
    started: list = []
    monkeypatch.setattr(account_module.LearningAccount, "start", lambda self: started.append(self) or True)
    app = _app(tmp_path, telegram_enabled=True, telegram_polling_enabled=True)
    with TestClient(app):
        pass
    assert started == []
    stop = learning_package.run_learning_account_here(app)
    assert started == [app.state.learning_account] and stop == app.state.learning_account.stop
    assert learning_package.run_learning_account_here(SimpleNamespace(state=SimpleNamespace(learning_account=None)))() is None
    script = open("scripts/run_telegram_bot.py").read()
    assert "run_learning_account_here(app)" in script and "stop_learning_account()" in script


def test_the_thread_waits_thirty_seconds_then_ticks_and_releases_the_lease_on_exit(tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path)
    waits: list = []
    monkeypatch.setattr(rig.account._stopping, "wait", lambda seconds: waits.append(seconds) or False)

    def one_tick():
        rig.account.lease.ensure(work=True)
        rig.account.stop()

    monkeypatch.setattr(rig.account, "tick", one_tick)
    rig.account._run()
    assert waits == [account_module.FIRST_PASS_DELAY_SECONDS] == [30.0]
    assert not rig.account.lease.held
    assert json.loads(records.Store(rig.db).read(records.LEASE_KEY))["released"] is True


def test_a_real_thread_stops_promptly(tmp_path) -> None:
    rig = Rig(tmp_path)
    assert rig.account.start() is True and rig.account.start() is False
    assert any(thread.name == "learning-account" for thread in threading.enumerate())
    rig.account.stop()
    assert not rig.account._thread.is_alive()
    assert rig.store.reads == [] and rig.broker.calls == []  # stopped within its first-pass delay


def test_inert_without_keys(tmp_path) -> None:
    app = _app(tmp_path, alpaca_learning_api_key="")
    assert app.state.learning_account is None
    assert getattr(app.state.workflow_service, "learning_account", None) is None
    assert learning_package.offer(app.state.workflow_service, SimpleNamespace(candidates=[snapshot()], generated_at=NOW.isoformat())) == 0


def test_an_attach_failure_never_stops_the_app(tmp_path, monkeypatch) -> None:
    def broken(app):
        raise RuntimeError("boom")

    monkeypatch.setattr(learning_package, "attach", broken)
    app = _app(tmp_path)
    assert app.state.learning_account_attach_error == "RuntimeError: boom"
    with TestClient(app) as client:
        body = client.get("/performance/live-scorecard").json()
    assert body["learning_account"]["route"] == routes.ATTACH_FAILED
    from app.storage.repositories import RunLogRepository

    logs = RunLogRepository(app.state.db).list_by_event("learning_account_status")
    assert logs[0]["payload"] == {"route": routes.ATTACH_FAILED, "error": "RuntimeError: boom", "learning_account": True}
    monkeypatch.setitem(sys.modules, "app.learning_account", None)  # an import failure too
    app = _app(tmp_path / "import")
    assert app.state.learning_account_attach_error.startswith("ModuleNotFoundError")  # an ImportError


def _scan_service(tmp_path, candidates, learning_account):
    from tests.test_workflow_service import (
        FakeAlertHistory,
        FakeLogs,
        FakeMarketDataEngine,
        FakeMarketScreener,
        FakeNotifier,
        FakeState,
        FakeTrackedSignals,
    )
    from app.workflow.service import SignalWorkflowService
    from tests.conftest import make_settings

    service = SignalWorkflowService(
        settings=make_settings(tmp_path),
        market_screener=FakeMarketScreener(candidates),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
    )
    service.learning_account = learning_account
    return service


def _run(service):
    from app.workflow.operations import run_scan_task

    return run_scan_task(service, task="intraday_scan", state_key="workflow:last_intraday_scan", origin="test",
                         timeframes=["5m"], notify=False, force_refresh=False, symbols=["NVDA"])


def test_the_scan_offers_its_candidates_last_stamped_with_its_time(tmp_path) -> None:
    offers: list = []

    class Recorder:
        def offer(self, candidates, generated_at):
            offers.append((list(candidates), generated_at, [event for event, _ in service.run_logs.items]))
            return len(candidates)

    service = _scan_service(tmp_path, [snapshot("NVDA", "vwap_reclaim", "5m")], Recorder())
    result = _run(service)
    assert result.status == "ok"
    ((candidates, generated_at, events_before),) = offers
    assert [c.symbol for c in candidates] == ["NVDA"] and generated_at == "2026-04-11T00:00:00+00:00"
    assert events_before[-1] == "workflow_intraday_scan_completed"  # after every main-path step


def test_a_learning_failure_never_fails_a_scan(tmp_path, monkeypatch) -> None:
    class Broken:
        def offer(self, candidates, generated_at):
            raise RuntimeError("learning account broke")

    service = _scan_service(tmp_path, [snapshot("NVDA", "vwap_reclaim", "5m")], Broken())
    assert _run(service).status == "ok"
    monkeypatch.setitem(sys.modules, "app.learning_account", None)
    assert _run(_scan_service(tmp_path / "import", [snapshot()], Broken())).status == "ok"


def test_the_scorecard_status_follows_the_current_settings(tmp_path) -> None:
    settings = settings_for(tmp_path)
    running = {"route": "running", "owner": "me"}
    lease = {"owner": "me", "heartbeat_at": NOW.isoformat()}
    assert status(settings, running, lease, now=NOW, owner="me") == "running"
    assert STALLED_SECONDS == 420.0  # 7 min
    assert status(settings, running, lease, now=NOW + timedelta(seconds=420), owner="me") == "running"
    assert status(settings, running, lease, now=NOW + timedelta(seconds=421), owner="me") == "stalled"
    assert status(settings, running, {**lease, "released": True}, now=NOW, owner="me") == "stalled"
    assert status(settings, running, {**lease, "owner": "else"}, now=NOW, owner="me") == "stalled"
    assert status(settings, running, lease, now=NOW, owner="a-new-deploy") == routes.PENDING  # an older record
    assert status(settings, {}, lease, now=NOW, owner="me") == routes.PENDING
    assert status(settings, {"route": "account_mismatch", "owner": "me"}, {}, now=NOW, owner="me") == "account_mismatch"
    off = settings_for(tmp_path, learning_account_enabled=False)
    assert status(off, running, lease, now=NOW, owner="me") == routes.SETTING_OFF
    removed = settings_for(tmp_path, alpaca_learning_api_key="", alpaca_learning_secret_key="")
    assert status(removed, running, lease, now=NOW, owner="me") == routes.NOT_CONFIGURED
    polling = settings_for(tmp_path, telegram_polling_enabled=True)
    assert status(polling, running, lease, now=NOW, owner="another-process") == "running"
    assert status(settings, running, lease, now=NOW, owner="me", attach_error="x") == routes.ATTACH_FAILED


def test_the_scorecard_section_in_one_read(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.offer(snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    rig.tick()
    rig.broker.fill_entry("NVDA", 100.0)
    rig.broker.fill_entry("AMD", 50.0)
    rig.tick(60)
    rig.broker.fill_leg("NVDA", "limit", 110.0)
    rig.tick(60)
    state = FakeState()
    store = records.Store(rig.db)
    many: list = []

    class OneRead:
        def get_many(self, keys):
            many.append(list(keys))
            return store.read_many(keys)

    app_state = SimpleNamespace(learning_account=rig.account)
    report = learning_account_report(OneRead(), rig.settings, app_state=app_state, now=rig.clock())
    assert many == [[records.BOOK_KEY, records.CLOSED_KEY, records.STATUS_KEY, records.LEASE_KEY]]
    assert report["status"] == "running" and report["heartbeat_at"]
    assert report["caps"] == {"entries_per_day": 3, "orders_per_day": 6, "max_open": 4, "risk_fraction": 0.5}
    assert report["today"] == {"entries": 2, "orders": 2}
    assert (report["open"], report["closed"], report["closed_unreported"]) == (1, 1, 0)
    assert report["mean_r_by_key"] == {"momentum_breakout:1d": {"closed": 1, "mean_r": 2.0}}
    assert report["mean_r_by_kind"] == {"fee_band": {"closed": 1, "mean_r": 2.0}}
    assert [t["symbol"] for t in report["open_trades"]] == ["AMD"]
    assert report["last_closed"][0]["symbol"] == "NVDA" and report["totals"]["total_r"] == 2.0
    assert report["paper_only"] is True and report["copied_to_etoro"] is False
    assert state.reads == []


def test_the_live_scorecard_route_has_the_section(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/performance/live-scorecard").json()
    assert body["learning_account"]["status"] == routes.PENDING  # configured, not checked yet
    app = _app(tmp_path / "off", learning_account_enabled=False)
    with TestClient(app) as client:
        assert client.get("/performance/live-scorecard").json()["learning_account"]["status"] == routes.SETTING_OFF
    assert BASE["alpaca_learning_api_key"] not in json.dumps(body)


def test_the_keys_and_the_account_number_are_never_logged_or_echoed(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.offer(snapshot("NVDA"))
    rig.tick()
    rig.broker.account["account_number"] = "PA-OTHER"
    rig.offer(snapshot("AMD", "ma_crossover"))
    rig.tick(5)
    store = records.Store(rig.db)
    written = json.dumps([rig.logs.events, rig.notifier.texts, store.read_many(list(records.LEARNING_KEYS))])
    for secret in ("learn-key", "learn-secret", "PA-LEARN", "PA-OTHER", "main-key", "main-secret", "PA-MAIN"):
        assert secret not in written, secret
    report = learning_account_report(SimpleNamespace(get_many=store.read_many), rig.settings, app_state=SimpleNamespace(learning_account=rig.account), now=rig.clock())
    assert "PA-LEARN" not in json.dumps(report) and "learn-key" not in json.dumps(report)
