from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.automation.reliability import (
    AUTO_TIER_STRICT_VALID,
    PAPER_NEAR_MISS,
    STRICT_VALID,
    SUPERVISED_WEAK_VALID,
    auto_approval_tier_blockers,
    proposal_quality_label,
)
from app.main import create_app
from app.models.paper import (
    PaperBrokerExecutionRecord,
    PaperLifecycleFlags,
    PaperTradeLifecycleRecord,
)
from tests.conftest import MockBroker, make_settings


def _candidate(**overrides):
    payload = {
        "symbol": "NVDA",
        "strategy_name": "momentum_breakout",
        "execution_ready": True,
        "signal_role": "entry_long",
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "score": 80.0,
        "metadata": {"alert_eligible": True, "backtest_validated": True},
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _lifecycle(*, complete: bool = True, source: str = "scanner_strategy") -> PaperTradeLifecycleRecord:
    flags = PaperLifecycleFlags(
        entry_submitted=True,
        entry_filled=True,
        bracket_legs_verified=complete,
        exit_filled_or_position_flat=True,
        reconciled=True,
        review_created=True,
        duplicate_order_absent=True,
    )
    execution = PaperBrokerExecutionRecord(
        execution_id="exec_auto_1",
        proposal_id="prop_auto_1",
        symbol="NVDA",
        strategy_name="momentum_breakout",
        source=source,
        mode="alpaca_paper",
        status="filled",
        broker_order_id="parent",
        client_order_id="client",
        created_at="2026-07-13T14:00:00+00:00",
        updated_at="2026-07-13T15:00:00+00:00",
        filled_qty=1.0,
        entry_fill_price=100.0,
        exit_fill_price=105.0,
        realized_pnl_usd=5.0,
    )
    return PaperTradeLifecycleRecord(
        id="exec_auto_1",
        execution_id="exec_auto_1",
        proposal_id="prop_auto_1",
        symbol="NVDA",
        strategy_name="momentum_breakout",
        source=source,
        autonomous=source in {"scanner_strategy", "generated_strategy", "rl_policy"},
        status="filled",
        broker_order_id="parent",
        client_order_id="client",
        entry_fill_price=100.0,
        exit_fill_price=105.0,
        realized_pnl_usd=5.0,
        created_at="2026-07-13T14:00:00+00:00",
        updated_at="2026-07-13T15:00:00+00:00",
        flags=flags,
        blockers=[],
        execution=execution,
    )


def test_proposal_quality_labels_guard_supervised_paths() -> None:
    assert proposal_quality_label(_candidate()) == STRICT_VALID
    assert (
        proposal_quality_label(
            _candidate(metadata={"alert_eligible": True, "signal_classification": SUPERVISED_WEAK_VALID})
        )
        == SUPERVISED_WEAK_VALID
    )
    assert (
        proposal_quality_label(
            _candidate(metadata={"alert_eligible": True, "signal_classification": PAPER_NEAR_MISS})
        )
        == PAPER_NEAR_MISS
    )
    assert proposal_quality_label(_candidate(take_profit=None)) == "not_tradeable"


def test_tier_two_auto_approval_blocks_weak_valid_and_requires_lifecycle_evidence(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        paper_auto_approval_tier=AUTO_TIER_STRICT_VALID,
        paper_auto_min_clean_supervised_lifecycles=10,
    )
    weak_candidate = _candidate(
        metadata={"alert_eligible": True, "signal_classification": SUPERVISED_WEAK_VALID}
    )

    blockers = auto_approval_tier_blockers(
        settings=settings,
        candidate=weak_candidate,
        lifecycles=[_lifecycle() for _ in range(10)],
    )

    assert "weak_valid_requires_human_approval" in blockers
    assert "paper_auto_requires_strict_valid_quality" in blockers

    strict_blockers = auto_approval_tier_blockers(
        settings=settings,
        candidate=_candidate(),
        lifecycles=[_lifecycle() for _ in range(9)],
    )

    assert "insufficient_clean_supervised_lifecycles" in strict_blockers


def test_reliability_endpoint_reports_supervised_and_auto_blockers(tmp_path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            control_api_token="secret",
            auto_propose_enabled=True,
            paper_auto_operation_mode="supervised",
            paper_auto_approve_proposals=False,
        ),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    assert client.get("/automation/reliability").status_code == 403

    response = client.get("/automation/reliability", headers={"X-Control-Token": "secret"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "paper_reliability"
    assert payload["ready_for_auto_approval"] is False
    assert "daily_proposal_target_not_met" in payload["proposal_flow"]["proposal_blockers"]
    assert "paper_auto_tier_supervised_only" in payload["auto_approval"]["blockers"]
    assert "paper_auto_approve_disabled" in payload["auto_approval"]["blockers"]
