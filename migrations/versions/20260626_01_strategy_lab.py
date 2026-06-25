"""Strategy Lab generated strategy persistence.

Revision ID: 20260626_01
Revises: 20260620_01
"""

from alembic import op

from app.storage.db import SCHEMA

revision = "20260626_01"
down_revision = "20260620_01"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "strategy_lab_generated_strategies",
    "strategy_lab_backtests",
)


def upgrade() -> None:
    schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    for statement in (part.strip() for part in schema.split(";")):
        if statement and any(table in statement for table in NEW_TABLES):
            op.execute(statement)


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
