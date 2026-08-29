"""Reconciliation must stamp signed fill slippage (vs the decision price) onto the
execution's response payload so execution quality is visible."""

from __future__ import annotations

from types import SimpleNamespace

from app.automation.reconciliation import AlpacaReconciliationService
from app.models.execution import ExecutionRecord


def _bind():
    obj = SimpleNamespace()
    obj.executions = SimpleNamespace(update=lambda e: None)
    obj.learning = None
    obj.broker_governance = None
    obj._realized_pnl = lambda payload: 0.0
    obj._update_execution = AlpacaReconciliationService._update_execution.__get__(obj)
    return obj


def test_positive_slippage_on_paying_up() -> None:
    recon = _bind()
    rec = ExecutionRecord(proposal_id="p", mode="paper", request_payload={"proposed_price": 100.0})
    recon._update_execution(rec, {"status": "filled", "filled_avg_price": 100.5})
    # Paid 100.5 vs decision 100 -> +50 bps.
    assert rec.response_payload["slippage_bps"] == 50.0
    assert rec.response_payload["fill_price"] == 100.5


def test_negative_slippage_on_price_improvement() -> None:
    recon = _bind()
    rec = ExecutionRecord(proposal_id="p", mode="paper", request_payload={"proposed_price": 200.0})
    recon._update_execution(rec, {"status": "filled", "filled_avg_price": 199.0})
    assert rec.response_payload["slippage_bps"] == -50.0


def test_no_slippage_without_fill_price() -> None:
    recon = _bind()
    rec = ExecutionRecord(proposal_id="p", mode="paper", request_payload={"proposed_price": 100.0})
    recon._update_execution(rec, {"status": "submitted"})  # no filled_avg_price yet
    assert "slippage_bps" not in rec.response_payload
