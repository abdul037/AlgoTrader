"""Backtest lookup indexes.

The scan looks up the latest backtest for (symbol, strategy) on every strategy
check, and the ranker / decay monitor read the newest rows by completed_at.
With only the primary key, both were full scans of a ~1.4M-row table in
production (7.7s and 11.8s mean), which is why scheduled scans hit their
wall-clock deadline after ~6 symbols.

The baseline migration executes the whole ``app.storage.db.SCHEMA`` string, so a
fresh database already has these indexes; ``IF NOT EXISTS`` keeps this
idempotent on both SQLite (the schema-drift test target) and Postgres. On the
live Postgres deploy the indexes were built ``CONCURRENTLY`` before this shipped,
so this is a no-op there.

Revision ID: 20260926_01
Revises: 20260828_01
"""

from alembic import op

revision = "20260926_01"
down_revision = "20260828_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtests_symbol_strategy_completed "
        "ON backtests(symbol, strategy_name, completed_at)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_backtests_completed_at ON backtests(completed_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_scan_decisions_created_at ON scan_decisions(created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_scan_decisions_created_at")
    op.execute("DROP INDEX IF EXISTS idx_backtests_completed_at")
    op.execute("DROP INDEX IF EXISTS idx_backtests_symbol_strategy_completed")
