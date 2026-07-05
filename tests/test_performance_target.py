from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.paper import (
    PaperBrokerExecutionRecord,
    PaperBrokerOrderLeg,
    PaperLifecycleFlags,
    PaperTradeLifecycleRecord,
)
from tests.conftest import MockBroker, make_settings


class FakePaperService:
    def __init__(self, lifecycles):
        self._lifecycles = lifecycles

    def lifecycles(self, *, limit: int = 100, source: str | None = None, autonomous_only: bool = False):
        records = list(self._lifecycles)
        if source:
            records = [item for item in records if item.source == source]
        if autonomous_only:
            records = [item for item in records if item.autonomous]
        return records[:limit]


def _lifecycle(*, realized_pnl_usd: float = 13000.0) -> PaperTradeLifecycleRecord:
    execution = PaperBrokerExecutionRecord(
        execution_id="exec_auto_1",
        proposal_id="prop_auto_1",
        queue_id="queue_auto_1",
        symbol="NVDA",
        strategy_name="opening_range_breakout",
        source="scanner_strategy",
        mode="alpaca_paper",
        status="filled",
        broker_order_id="parent",
        client_order_id="client",
        side="buy",
        order_class="bracket",
        quantity=100.0,
        filled_qty=100.0,
        entry_fill_price=100.0,
        exit_order_id="target",
        exit_fill_price=230.0,
        realized_pnl_usd=realized_pnl_usd,
        created_at="2026-07-02T14:30:00+00:00",
        updated_at="2026-07-02T20:00:00+00:00",
        legs=[
            PaperBrokerOrderLeg(side="sell", order_type="limit", limit_price=230.0, status="filled"),
            PaperBrokerOrderLeg(side="sell", order_type="stop", stop_price=90.0, status="canceled"),
        ],
    )
    return PaperTradeLifecycleRecord(
        id="exec_auto_1",
        execution_id="exec_auto_1",
        proposal_id="prop_auto_1",
        queue_id="queue_auto_1",
        symbol="NVDA",
        strategy_name="opening_range_breakout",
        source="scanner_strategy",
        autonomous=True,
        status="filled",
        broker_order_id="parent",
        client_order_id="client",
        entry_fill_price=100.0,
        exit_fill_price=230.0,
        realized_pnl_usd=realized_pnl_usd,
        created_at="2026-07-02T14:30:00+00:00",
        updated_at="2026-07-02T20:00:00+00:00",
        flags=PaperLifecycleFlags(
            entry_submitted=True,
            entry_filled=True,
            bracket_legs_verified=True,
            exit_filled_or_position_flat=True,
            reconciled=True,
            review_created=False,
            duplicate_order_absent=True,
        ),
        blockers=[],
        execution=execution,
    )


def test_weekly_target_readiness_requires_control_token_and_reports_blockers(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    assert client.get("/performance/weekly-target-readiness").status_code == 403

    response = client.get("/performance/weekly-target-readiness", headers={"X-Control-Token": "secret"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is False
    assert "insufficient_clean_autonomous_closed_trades" in payload["blockers"]
    assert "weekly_target_not_met" in payload["blockers"]
    assert payload["target"]["weekly_profit_usd"] == 1000.0


def test_weekly_target_readiness_scales_clean_autonomous_lifecycles(tmp_path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            control_api_token="secret",
            weekly_target_min_closed_autonomous_trades=1,
            weekly_target_min_clean_sessions=1,
            weekly_target_capital_scenarios_usd=[100000.0],
            paper_account_balance_usd=100000.0,
        ),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    app.state.paper_trading_service = FakePaperService([_lifecycle()])
    app.state.safety_state_repository.record_reconciliation(
        status="ok",
        account_number="PA3B287XBZYU",
        orders_seen=1,
        positions_seen=0,
        issues=[],
        account={"equity": 100000},
    )
    client = TestClient(app)

    response = client.get("/performance/weekly-target-readiness", headers={"X-Control-Token": "secret"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["blockers"] == []
    assert payload["evidence"]["clean_window_closed_trade_count"] == 1
    assert payload["actual"]["average_r_multiple"] == 13.0
    assert payload["scenarios"][0]["target_met"] is True
    assert payload["scenarios"][0]["scaled_weekly_profit_usd"] == 1000.0
