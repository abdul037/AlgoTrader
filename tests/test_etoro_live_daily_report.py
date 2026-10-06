"""Daily eToro LIVE Telegram report: sent once per US trading day after the close."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.broker.etoro_live_daily_report import (
    REPORT_KEY,
    RETRY_MINUTES,
    build_report,
    maybe_send_daily_report,
)
from app.broker.etoro_live_mirror import HALTED_KEY, STATE_KEY
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from tests.test_etoro_live_backup_stop import _Client, _mirror

# Tue 2026-10-06: New York is UTC-4, so 16:10 NY = 20:10 UTC.
BEFORE = datetime(2026, 10, 6, 20, 5, tzinfo=UTC)
AFTER = datetime(2026, 10, 6, 20, 12, tzinfo=UTC)
SATURDAY = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)


class _Notifier:
    def __init__(self, ok=True):
        self.sent, self.ok, self.enabled = [], ok, True

    def send_text(self, text):
        self.sent.append(text)
        return self.ok


def _setup(tmp_path, ok=True):
    client = _Client(rates={"MSFT": 530.0, "AAPL": 104.0})
    mirror, logs = _mirror(tmp_path, client)
    mirror.notifier = _Notifier(ok)
    mirror.state.set(
        STATE_KEY,
        json.dumps({"last_equity": 9_988.0, "day_start_equity": 9_990.0, "last_cash": 6_990.1}),
    )
    cards = {
        "e1": {
            "symbol": "MSFT",
            "strategy_name": "momentum_breakout",
            "timeframe": "1d",
            "status": "open",
            "open_rate": 525.51,
            "stop_loss": 511.0225,
            "take_profit": 544.52,
            "notional_usd": 999.0,
            "amount_usd": 999.0,
            "leverage": 1,
            "expected_r": 0.07,
            "opened_at": "2026-10-05T17:02:55+00:00",
        },
        "e2": {
            "symbol": "AAPL",
            "strategy_name": "momentum_breakout",
            "timeframe": "1d",
            "status": "closed",
            "open_rate": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "notional_usd": 1_000.0,
            "amount_usd": 500.0,
            "leverage": 2,
            "expected_r": 0.07,
            "opened_at": "2026-10-06T14:00:00+00:00",
            "closed_seen_at": "2026-10-06T19:00:00+00:00",
            "exit_reason": "paper_exit",
            "realized_r": 0.8,
            "pnl_usd": 40.0,
        },
    }
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
    return mirror, logs


def test_sent_once_per_trading_day_after_the_close(tmp_path) -> None:
    mirror, logs = _setup(tmp_path)
    assert not maybe_send_daily_report(mirror, now=BEFORE)
    assert maybe_send_daily_report(mirror, now=AFTER)
    assert not maybe_send_daily_report(mirror, now=AFTER)  # once per New York date
    assert len(mirror.notifier.sent) == 1 and logs.events[-1][0] == "etoro_live_daily_report_sent"
    assert json.loads(mirror.state.get(REPORT_KEY))["ny_date"] == "2026-10-06"
    assert not maybe_send_daily_report(mirror, now=SATURDAY)


def test_failed_send_is_retried_after_a_backoff(tmp_path) -> None:
    mirror, _ = _setup(tmp_path, ok=False)
    assert not maybe_send_daily_report(mirror, now=AFTER)
    assert mirror.state.get(REPORT_KEY) is None
    mirror.notifier.ok = True
    assert not maybe_send_daily_report(mirror, now=AFTER + timedelta(minutes=1))
    assert maybe_send_daily_report(mirror, now=AFTER + timedelta(minutes=RETRY_MINUTES))


def test_disabled_telegram_skips_without_reading_etoro_prices(tmp_path) -> None:
    # 2026-10-06: Telegram was off in production, so the report was rebuilt (with fresh
    # eToro price reads) on every guard tick.
    mirror, logs = _setup(tmp_path)
    mirror.notifier.enabled = False
    requests = []
    real_request = mirror.client._request
    mirror.client._request = lambda *a, **kw: requests.append(a) or real_request(*a, **kw)
    for minute in range(3):
        assert not maybe_send_daily_report(mirror, now=AFTER + timedelta(minutes=minute))
    assert mirror.notifier.sent == [] and requests == []
    skips = [e for e in logs.events if e[0] == "etoro_live_daily_report_skipped"]
    assert len(skips) == 1  # logged once per New York date
    mirror.notifier.enabled = True  # the operator turns Telegram on: sent on the next tick
    assert maybe_send_daily_report(mirror, now=AFTER + timedelta(minutes=4))


def test_report_content(tmp_path) -> None:
    mirror, _ = _setup(tmp_path)
    mirror.state.set(HALTED_KEY, json.dumps({"reason": "open_failed:NVDA"}))
    text = build_report(mirror, since="2026-10-05T20:10:00+00:00", now=AFTER)
    assert "US session Tue 06 Oct (sent 00:12 Dubai)" in text
    assert "Balance at cost $9,988 | free cash $6,990 | today -$2.00" in text
    assert "1 opened, 1 closed" in text
    assert "+ Opened AAPL $500 x2 (momentum_breakout)" in text
    assert "- Closed AAPL +0.80R +$40.00 (paper exit), your copy +$2.00" in text
    assert "* MSFT +0.31R +$8.54" in text  # fresh eToro rate 530.00
    assert "momentum_breakout:1d: 1 closed, live avg +0.80R, backtest +0.07R" in text
    assert "2x leverage test placed" in text and "1 paper exit(s) copied" in text
    assert "LIVE TRADING HALTED: open_failed:NVDA" in text
    assert "Your copy = 5.0% of each AlgoBot trade" in text


def test_preview_endpoint(tmp_path) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import MockBroker, make_settings

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    mirror, _ = _setup(tmp_path)
    app.state.execution_coordinator = SimpleNamespace(etoro_live_mirror=mirror)
    client = TestClient(app)
    assert client.get("/performance/live-daily-report").status_code == 403
    body = client.get("/performance/live-daily-report", headers={"X-Control-Token": "secret"})
    assert "eToro LIVE daily report" in body.json()["text"]
