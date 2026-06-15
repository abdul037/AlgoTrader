"""Institutional governance and multi-broker persistence.

Revision ID: 20260615_01
Revises: 20260611_01
"""

from alembic import op

from app.storage.db import SCHEMA

revision = "20260615_01"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "strategy_versions",
    "strategy_audits",
    "promotion_decisions",
    "broker_capabilities",
    "broker_account_identities",
    "broker_reconciliation_results",
    "broker_comparisons",
    "etoro_demo_order_requests",
    "portfolio_risk_snapshots",
    "rollout_gate_evidence",
)


def upgrade() -> None:
    """Create additive institutional tables and indexes."""

    schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    for statement in (part.strip() for part in schema.split(";")):
        if statement and any(table in statement for table in NEW_TABLES):
            op.execute(statement)


def downgrade() -> None:
    """Drop additive institutional tables."""

    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
