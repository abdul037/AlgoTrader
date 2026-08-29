"""Execution strategy attribution.

Adds a first-class ``strategy_name`` column to the ``executions`` table so
per-strategy P&L can be queried directly instead of being dug out of the
request payload JSON.

The baseline migration executes the whole ``app.storage.db.SCHEMA`` string, so
a database created fresh already has this column and the add below is skipped.
A database migrated before the column existed (e.g. the live Postgres deploy)
does not, so we inspect first and add only when missing -- this keeps the
migration idempotent on both SQLite (the schema-drift test target) and Postgres.

Revision ID: 20260828_01
Revises: 20260628_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20260828_01"
down_revision = "20260628_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("executions", "strategy_name"):
        op.add_column("executions", sa.Column("strategy_name", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("executions", "strategy_name"):
        op.drop_column("executions", "strategy_name")
