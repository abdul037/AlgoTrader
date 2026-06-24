from __future__ import annotations

from fastapi.testclient import TestClient

from app.institutional.service import ROLLOUT_GATES, InstitutionalGovernanceService
from app.models.institutional import (
    PortfolioRiskSnapshot,
    PromotionDecision,
    RolloutGateEvidence,
    StrategyAudit,
    StrategyVersion,
)
from app.storage.db import Database
from app.storage.repositories import (
    BrokerGovernanceRepository,
    PortfolioRiskRepository,
    RolloutGateRepository,
    StrategyGovernanceRepository,
)
from tests.conftest import MockBroker, make_settings


def _service(tmp_path):
    settings = make_settings(tmp_path, rollout_stage="stage_1_validation")
    db = Database(settings)
    db.initialize()
    strategies = StrategyGovernanceRepository(db)
    brokers = BrokerGovernanceRepository(db)
    risk = PortfolioRiskRepository(db)
    gates = RolloutGateRepository(db)
    service = InstitutionalGovernanceService(
        settings=settings,
        strategies=strategies,
        brokers=brokers,
        portfolio_risk=risk,
        rollout_gates=gates,
    )
    return service, strategies


def test_strategy_promotion_requires_every_production_threshold(tmp_path):
    service, strategies = _service(tmp_path)
    version = strategies.create_version(
        StrategyVersion(
            strategy_name="swing_trend",
            code_version="abc123",
            dataset_version="pit-sp500-v1",
            timeframe="1d",
        )
    )
    strategies.record_audit(
        StrategyAudit(
            strategy_version_id=version.id,
            dataset_version="pit-sp500-v1",
            timeframe="1d",
            out_of_sample_trades=250,
            deflated_sharpe=1.01,
            rolling_sharpe=1.30,
            profit_factor=1.35,
            expectancy_after_costs=12.0,
            max_drawdown_pct=7.0,
            strategy_drawdown_pct=6.0,
            unexplained_errors=0,
            protected_exit_coverage_pct=100.0,
        )
    )

    decision = service.assess_strategy(version.id, decided_by="research-review")

    assert decision.approved is True
    assert decision.blockers == []


def test_strategy_promotion_rejects_invalid_audit(tmp_path):
    service, strategies = _service(tmp_path)
    version = strategies.create_version(
        StrategyVersion(
            strategy_name="weak_strategy",
            code_version="def456",
            dataset_version="dataset-v1",
            timeframe="1d",
        )
    )
    strategies.record_audit(
        StrategyAudit(
            strategy_version_id=version.id,
            dataset_version="dataset-v1",
            timeframe="1d",
            out_of_sample_trades=20,
            profit_factor=0.8,
            expectancy_after_costs=-1.0,
            unexplained_errors=2,
        )
    )

    decision = service.assess_strategy(version.id)

    assert decision.approved is False
    assert "insufficient_out_of_sample_trades" in decision.blockers
    assert "unexplained_audit_errors" in decision.blockers
    assert "non_positive_expectancy_after_costs" in decision.blockers


def test_failed_reassessment_revokes_production_approval(tmp_path):
    service, strategies = _service(tmp_path)
    version = strategies.create_version(
        StrategyVersion(
            strategy_name="reassessed",
            code_version="abc",
            dataset_version="dataset",
            timeframe="1d",
        )
    )
    strategies.record_audit(
        StrategyAudit(
            strategy_version_id=version.id,
            dataset_version="dataset",
            timeframe="1d",
            out_of_sample_trades=250,
            deflated_sharpe=1,
            rolling_sharpe=1.3,
            profit_factor=1.4,
            expectancy_after_costs=1,
            protected_exit_coverage_pct=100,
        )
    )
    assert service.assess_strategy(version.id).approved is True
    assert strategies.strategy_production_approved("reassessed") is True
    strategies.record_audit(
        StrategyAudit(
            strategy_version_id=version.id,
            dataset_version="dataset",
            timeframe="1d",
            out_of_sample_trades=250,
            profit_factor=0.5,
            expectancy_after_costs=-1,
            protected_exit_coverage_pct=100,
        )
    )

    assert service.assess_strategy(version.id).approved is False
    assert strategies.strategy_production_approved("reassessed") is False


def test_paper_exploration_approval_is_not_production_approval(tmp_path):
    _service_instance, strategies = _service(tmp_path)
    version = strategies.create_version(
        StrategyVersion(
            strategy_name="scanner_strategy",
            code_version="abc",
            dataset_version="paper-exploration",
            timeframe="multi",
            status="paper_exploration",
        )
    )
    strategies.record_decision(
        PromotionDecision(
            strategy_version_id=version.id,
            target_stage="paper_exploration",
            approved=True,
            decided_by="operator",
            evidence={"scope": "alpaca_paper_only"},
        )
    )

    assert strategies.strategy_paper_exploration_approved("scanner_strategy") is True
    assert strategies.strategy_production_approved("scanner_strategy") is False
    assert strategies.approved_paper_exploration_strategies() == ["scanner_strategy"]


def test_rollout_readiness_requires_signed_gates_strategy_and_clean_risk(tmp_path):
    service, strategies = _service(tmp_path)
    version = strategies.create_version(
        StrategyVersion(
            strategy_name="qualified",
            code_version="abc",
            dataset_version="dataset",
            timeframe="1d",
        )
    )
    strategies.record_audit(
        StrategyAudit(
            strategy_version_id=version.id,
            dataset_version="dataset",
            timeframe="1d",
            out_of_sample_trades=250,
            deflated_sharpe=1.0,
            rolling_sharpe=1.3,
            profit_factor=1.4,
            expectancy_after_costs=1,
            max_drawdown_pct=5,
            strategy_drawdown_pct=5,
            protected_exit_coverage_pct=100,
        )
    )
    service.assess_strategy(version.id)
    service.evaluate_portfolio(
        PortfolioRiskSnapshot(
            equity_usd=100_000,
            peak_equity_usd=100_000,
            drawdown_pct=0,
            gross_exposure_pct=0,
        )
    )
    for gate_name in ROLLOUT_GATES["stage_1_validation"]:
        service.record_gate(
            RolloutGateEvidence(
                stage="stage_1_validation",
                gate_name=gate_name,
                status="passed",
                signed_by="operator",
                evidence={"test": True},
            )
        )

    assert service.readiness()["ready"] is True


def test_portfolio_hard_drawdown_triggers_kill_switch_status(tmp_path):
    service, _strategies = _service(tmp_path)

    result = service.evaluate_portfolio(
        PortfolioRiskSnapshot(
            equity_usd=90_000,
            peak_equity_usd=100_000,
            drawdown_pct=10,
            gross_exposure_pct=20,
        )
    )

    assert result.status == "kill_switch"
    assert "portfolio_hard_drawdown_limit" in result.blockers


def test_passed_rollout_gate_requires_signer_and_evidence(tmp_path):
    service, _strategies = _service(tmp_path)

    try:
        service.record_gate(
            RolloutGateEvidence(
                stage="stage_1_validation",
                gate_name="observation_48h",
                status="passed",
            )
        )
    except ValueError as exc:
        assert "signed_by" in str(exc)
    else:
        raise AssertionError("Unsigned passed gate must be rejected")


def test_institutional_mutations_use_control_api_token(tmp_path):
    from app.main import create_app

    app = create_app(
        make_settings(tmp_path, control_api_token="control-secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    assert client.get("/institutional/readiness").status_code == 200
    assert client.post("/institutional/rollout-gates", json={}).status_code == 403
