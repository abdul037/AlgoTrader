from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.automation.reliability import (
    AUTO_TIER_STRICT_VALID,
    PAPER_NEAR_MISS,
    STRICT_VALID,
    SUPERVISED_WEAK_VALID,
    aggregate_scan_funnel,
    auto_approval_tier_blockers,
    candidate_propose_drop_reason,
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


def test_candidate_propose_drop_reason_maps_each_stage() -> None:
    # A fully eligible candidate proceeds (no drop reason).
    assert candidate_propose_drop_reason(_candidate()) is None
    # Each ineligibility maps to a stable funnel label, in precedence order.
    assert candidate_propose_drop_reason(_candidate(execution_ready=False)) == "not_execution_ready"
    assert (
        candidate_propose_drop_reason(_candidate(metadata={"alert_eligible": False}))
        == "not_alert_eligible"
    )
    assert (
        candidate_propose_drop_reason(_candidate(signal_role="entry_short")) == "entry_short"
    )
    assert candidate_propose_drop_reason(_candidate(stop_loss=None)) == "missing_stop"
    # Precedence: readiness is checked before eligibility.
    assert (
        candidate_propose_drop_reason(
            _candidate(execution_ready=False, metadata={"alert_eligible": False})
        )
        == "not_execution_ready"
    )


def test_aggregate_scan_funnel_rolls_up_stages_across_scans() -> None:
    rows = [
        {
            "payload": {
                "origin": "intraday_scan",
                "created": 1,
                "entered": 5,
                "proposals_created": 1,
                "executed": 0,
                "dropped:not_execution_ready": 2,
                "safety_blocked": 1,
                "safety_blocked:symbol_blacklisted": 1,
                "exec_blocked_candidates": 1,
                "exec_blocked:near_miss_requires_human_approval": 1,
            }
        },
        # A bare payload dict (not wrapped in {"payload": ...}) is also accepted.
        {
            "origin": "swing_scan",
            "entered": 3,
            "proposals_created": 1,
            "executed": 1,
            "dropped:missing_stop": 1,
        },
        "not-a-dict",  # defensively skipped, never raised on
    ]

    summary = aggregate_scan_funnel(rows)

    assert summary["scans"] == 2
    assert summary["entered"] == 8
    assert summary["proposals_created"] == 2
    assert summary["executed"] == 1
    assert summary["dropped"] == {"missing_stop": 1, "not_execution_ready": 2}
    assert summary["safety_blocked"] == {"symbol_blacklisted": 1}
    assert summary["safety_blocked_total"] == 1
    assert summary["exec_blocked"] == {"near_miss_requires_human_approval": 1}
    assert summary["exec_blocked_candidates"] == 1
    assert summary["origins"] == {"intraday_scan": 1, "swing_scan": 1}


def test_aggregate_scan_funnel_empty() -> None:
    summary = aggregate_scan_funnel([])
    assert summary["scans"] == 0
    assert summary["entered"] == 0
    assert summary["executed"] == 0
    assert summary["dropped"] == {}
    assert summary["exec_blocked"] == {}


def test_run_log_list_by_event_reads_back_only_matching_event(tmp_path) -> None:
    from app.storage.db import Database
    from app.storage.repositories import RunLogRepository

    db = Database(make_settings(tmp_path))
    db.initialize()
    logs = RunLogRepository(db)
    logs.log("auto_propose_funnel", {"origin": "intraday_scan", "entered": 3, "executed": 1})
    logs.log("some_other_event", {"noise": 1})

    rows = logs.list_by_event("auto_propose_funnel", limit=10)

    assert len(rows) == 1
    assert rows[0]["payload"]["entered"] == 3
    # A future-dated since_iso filters everything out (created_at is today).
    assert logs.list_by_event("auto_propose_funnel", since_iso="2999-01-01") == []
    assert aggregate_scan_funnel(rows)["executed"] == 1


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
    scan_funnel = payload["proposal_flow"]["scan_funnel"]
    assert scan_funnel["scans"] == 0
    assert scan_funnel["executed"] == 0
    assert scan_funnel["dropped"] == {}


def _near_miss_candidate():
    return _candidate(metadata={"alert_eligible": True, "signal_classification": PAPER_NEAR_MISS})


def test_paper_unattended_near_miss_bypasses_approval_policy(tmp_path) -> None:
    near_miss = _near_miss_candidate()

    off = make_settings(
        tmp_path,
        paper_auto_approval_tier="tier1_supervised_only",
        paper_auto_operation_mode="unattended",
        paper_auto_min_clean_supervised_lifecycles=10,
        paper_unattended_near_miss_auto_exec_enabled=False,
    )
    off_blockers = auto_approval_tier_blockers(settings=off, candidate=near_miss, lifecycles=[])
    assert "near_miss_requires_human_approval" in off_blockers
    assert "paper_auto_tier_supervised_only" in off_blockers
    assert "insufficient_clean_supervised_lifecycles" in off_blockers

    on = make_settings(
        tmp_path,
        paper_auto_approval_tier="tier1_supervised_only",
        paper_auto_operation_mode="unattended",
        paper_auto_min_clean_supervised_lifecycles=10,
        paper_unattended_near_miss_auto_exec_enabled=True,
    )
    on_blockers = auto_approval_tier_blockers(settings=on, candidate=near_miss, lifecycles=[])
    assert "near_miss_requires_human_approval" not in on_blockers
    assert "paper_auto_tier_supervised_only" not in on_blockers
    assert "insufficient_clean_supervised_lifecycles" not in on_blockers


def test_paper_unattended_near_miss_bypass_requires_paper_mode(tmp_path) -> None:
    # In live mode the bypass must NOT apply even with the flag set.
    live = make_settings(
        tmp_path,
        paper_auto_approval_tier="tier1_supervised_only",
        paper_auto_operation_mode="unattended",
        paper_unattended_near_miss_auto_exec_enabled=True,
        execution_mode="live",
        enable_real_trading=False,
    )
    blockers = auto_approval_tier_blockers(settings=live, candidate=_near_miss_candidate(), lifecycles=[])
    assert "near_miss_requires_human_approval" in blockers


def test_paper_unattended_near_miss_bypass_requires_unattended_mode(tmp_path) -> None:
    # Supervised mode keeps the human-approval requirement even with the flag.
    supervised = make_settings(
        tmp_path,
        paper_auto_approval_tier="tier1_supervised_only",
        paper_auto_operation_mode="supervised",
        paper_unattended_near_miss_auto_exec_enabled=True,
    )
    blockers = auto_approval_tier_blockers(settings=supervised, candidate=_near_miss_candidate(), lifecycles=[])
    assert "near_miss_requires_human_approval" in blockers
