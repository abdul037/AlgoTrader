"""Unattended paper trading baseline.

Revision ID: 20260611_01
"""

from alembic import op

from app.storage.db import SCHEMA

revision = "20260611_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    for statement in (part.strip() for part in schema.split(";")):
        if statement:
            op.execute(statement)


def downgrade() -> None:
    for table in (
        "strategy_health",
        "instrument_blacklist",
        "reconciliation_runs",
        "broker_position_snapshots",
        "broker_order_snapshots",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
