from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.phase_c_validation_status import (
    broker_filled_qty,
    derive_checks,
    market_window_status,
)


def test_market_window_status_identifies_regular_hours() -> None:
    now_ny = datetime(2026, 5, 18, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    assert market_window_status(now_ny) == "open_regular_hours"


def test_market_window_status_identifies_weekend_closed() -> None:
    now_ny = datetime(2026, 5, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    assert market_window_status(now_ny) == "closed_weekend"


def test_broker_filled_qty_reads_nested_alpaca_payload() -> None:
    response = {
        "broker_execution": {
            "response_payload": {
                "filled_qty": 0.25,
            }
        }
    }

    assert broker_filled_qty(response) == 0.25


def test_derive_checks_marks_smoke_pass_but_strategy_pending() -> None:
    checks = derive_checks(
        queue_rows=[
            {
                "status": "executed",
                "strategy_name": "manual_smoke",
            }
        ],
        execution_rows=[
            {
                "broker_order_id": "broker-1",
                "request_json": '{"strategy_name": "manual_smoke"}',
                "response_json": '{"broker": "alpaca"}',
            }
        ],
        log_rows=[],
    )
    by_name = {check.name: check for check in checks}

    assert by_name["Alpaca paper smoke routing proof"].status == "PASS"
    assert by_name["Strategy-approved Alpaca paper order"].status == "PENDING"
    assert by_name["Market-hours paper fill"].status == "PENDING"


def test_derive_checks_marks_strategy_order_and_fill_pass() -> None:
    checks = derive_checks(
        queue_rows=[],
        execution_rows=[
            {
                "broker_order_id": "broker-1",
                "request_json": '{"strategy_name": "ema_trend_stack"}',
                "response_json": (
                    '{"broker": "alpaca", "broker_execution": '
                    '{"response_payload": {"filled_qty": 0.5}}}'
                ),
            }
        ],
        log_rows=[
            {
                "event_type": "kill_switch_emergency_stop",
                "payload_json": '{"reason": "phase c drill"}',
            }
        ],
    )
    by_name = {check.name: check for check in checks}

    assert by_name["Strategy-approved Alpaca paper order"].status == "PASS"
    assert by_name["Market-hours paper fill"].status == "PASS"
    assert by_name["Kill switch drill after strategy order"].status == "PASS"
