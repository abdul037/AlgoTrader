"""Continuous-learning trade review persistence.

Revision ID: 20260615_02
Revises: 20260615_01
"""

from alembic import op

from app.storage.db import SCHEMA

revision = "20260615_02"
down_revision = "20260615_01"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "learning_decision_snapshots",
    "learning_outcome_labels",
    "learning_lifecycle_events",
    "learning_trade_reviews",
    "learning_experiments",
    "learning_dataset_versions",
    "learning_meta_model_versions",
    "learning_model_evaluations",
    "learning_model_promotions",
    "learning_drift_snapshots",
    "learning_jobs",
)


def upgrade() -> None:
    schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    for statement in (part.strip() for part in schema.split(";")):
        if statement and any(table in statement for table in NEW_TABLES):
            op.execute(statement)


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
