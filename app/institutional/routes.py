"""Institutional governance and rollout readiness API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models.institutional import (
    BrokerAccountIdentity,
    BrokerCapability,
    BrokerComparison,
    BrokerReconciliationResult,
    PortfolioRiskSnapshot,
    RolloutGateEvidence,
    StrategyAudit,
    StrategyVersion,
)
from app.strategies.catalog import build_strategy_catalog_report

router = APIRouter(prefix="/institutional", tags=["institutional"])


class AssessmentRequest(BaseModel):
    decided_by: str = "system"


def _service(request: Request):
    return request.app.state.institutional_service


@router.get("/readiness")
def readiness(request: Request):
    return _service(request).readiness()


@router.get("/strategies")
def strategies(request: Request):
    repository = request.app.state.strategy_governance_repository
    return {
        "versions": repository.list_versions(),
        "audits": repository.list_audits(),
        "catalog": build_strategy_catalog_report(
            settings=request.app.state.settings,
            governance=repository,
        ),
    }


@router.post("/strategies")
def strategy_create(request: Request, payload: StrategyVersion):
    return request.app.state.strategy_governance_repository.create_version(payload)


@router.post("/strategy-audits")
def strategy_audit_create(request: Request, payload: StrategyAudit):
    try:
        request.app.state.strategy_governance_repository.get_version(payload.strategy_version_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return request.app.state.strategy_governance_repository.record_audit(payload)


@router.post("/strategies/{version_id}/assess")
def strategy_assess(version_id: str, request: Request, payload: AssessmentRequest | None = None):
    try:
        return _service(request).assess_strategy(
            version_id,
            decided_by=payload.decided_by if payload else "system",
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/promotions")
def promotions(request: Request):
    return request.app.state.strategy_governance_repository.list_decisions()


@router.get("/portfolio-risk")
def portfolio_risk(request: Request):
    snapshot = request.app.state.portfolio_risk_repository.latest()
    return snapshot or {"status": "never_recorded"}


@router.post("/portfolio-risk/evaluate")
def portfolio_risk_evaluate(request: Request, payload: PortfolioRiskSnapshot):
    return _service(request).evaluate_portfolio(payload)


@router.get("/brokers")
def brokers(request: Request):
    repository = request.app.state.broker_governance_repository
    return {
        "capabilities": repository.list_capabilities(),
        "identities": repository.list_identities(),
        "reconciliations": repository.list_reconciliations(),
    }


@router.post("/brokers/capabilities")
def broker_capability(request: Request, payload: BrokerCapability):
    return request.app.state.broker_governance_repository.upsert_capability(payload)


@router.post("/brokers/identities")
def broker_identity(request: Request, payload: BrokerAccountIdentity):
    return request.app.state.broker_governance_repository.upsert_identity(payload)


@router.post("/brokers/reconciliations")
def broker_reconciliation(request: Request, payload: BrokerReconciliationResult):
    return request.app.state.broker_governance_repository.record_reconciliation(payload)


@router.post("/brokers/reconciliations/etoro-demo/run")
def etoro_demo_reconciliation_run(request: Request):
    service = request.app.state.etoro_demo_reconciliation_service
    if service is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="eToro Demo v2 is disabled")
    return service.reconcile()


@router.get("/broker-comparisons")
def broker_comparisons(request: Request):
    return request.app.state.broker_governance_repository.list_comparisons()


@router.post("/broker-comparisons")
def broker_comparison(request: Request, payload: BrokerComparison):
    return request.app.state.broker_governance_repository.record_comparison(payload)


@router.get("/rollout-gates")
def rollout_gates(request: Request, stage: str | None = None):
    return request.app.state.rollout_gate_repository.list(stage=stage)


@router.post("/rollout-gates")
def rollout_gate_record(request: Request, payload: RolloutGateEvidence):
    try:
        return _service(request).record_gate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
