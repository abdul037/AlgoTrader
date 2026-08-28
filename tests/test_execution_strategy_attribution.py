"""The executions table must carry first-class strategy attribution, populated
from the order payload even when the caller does not set it explicitly."""

from __future__ import annotations

from pathlib import Path

from app.models.execution import ExecutionRecord, ExecutionStatus
from app.storage.db import Database
from app.storage.repositories import ExecutionRepository
from tests.conftest import make_settings


def _repo(tmp_path: Path) -> ExecutionRepository:
    db = Database(make_settings(tmp_path))
    db.initialize()
    return ExecutionRepository(db)


def test_strategy_name_persisted_from_explicit_field(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = ExecutionRecord(
        proposal_id="prop-1",
        mode="paper",
        status=ExecutionStatus.SUBMITTED,
        strategy_name="opening_range_breakout",
        request_payload={"symbol": "NVDA"},
    )
    repo.create(record)
    fetched = repo.get(record.id)
    assert fetched is not None
    assert fetched.strategy_name == "opening_range_breakout"


def test_strategy_name_backfilled_from_request_payload(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Callers build request_payload from proposal.order.model_dump(), which
    # carries strategy_name -- the repository should lift it into the column.
    record = ExecutionRecord(
        proposal_id="prop-2",
        mode="paper",
        status=ExecutionStatus.SUBMITTED,
        request_payload={"symbol": "AAPL", "strategy_name": "rsi_reversal"},
    )
    repo.create(record)
    fetched = repo.list(limit=10)[0]
    assert fetched.strategy_name == "rsi_reversal"


def test_missing_strategy_is_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = ExecutionRecord(proposal_id="prop-3", mode="paper", request_payload={"symbol": "SPY"})
    repo.create(record)
    fetched = repo.get(record.id)
    assert fetched is not None
    assert fetched.strategy_name is None
