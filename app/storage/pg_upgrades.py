"""Postgres-only schema upgrades, applied at boot (operator-approved 2026-10-10).

The per-symbol open-queue unique index that ``ExecutionCoordinator.enqueue`` treats as the
source of truth was only ever created for SQLite (``Database._apply_schema_upgrades``), so
production relied on application-level checks alone. Each upgrade runs in its own
transaction and a failure is logged, never raised: it must not stop the bot from booting.

``idx_run_logs_event_created`` (2026-11-23): ``run_logs`` had only its primary key, so every read
by event type scanned the whole table (about 400,000 rows, 167 MB). The Live Scorecard's query
makes three such reads; it took 13 to 17 s on 11-22 and passed its 20 s statement timeout on
11-23, so the page showed no data. With the index the query takes about 2 s. Production's copy
was built on 11-23 with ``CREATE INDEX CONCURRENTLY`` (no write lock), so this statement is a
no-op there; on a database without it, it builds the index at boot.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

POSTGRES_UPGRADES: tuple[tuple[str, str], ...] = (
    (
        "idx_queue_unique_open_per_symbol",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_unique_open_per_symbol "
        "ON execution_queue(symbol) WHERE status IN ('queued','processing')",
    ),
    (
        "idx_run_logs_event_created",
        "CREATE INDEX IF NOT EXISTS idx_run_logs_event_created ON run_logs(event_type, created_at)",
    ),
)


def apply_postgres_upgrades(database: Any) -> list[str]:
    """Apply each upgrade in its own transaction; return the names that succeeded."""

    applied: list[str] = []
    for name, statement in POSTGRES_UPGRADES:
        try:
            with database.connect() as connection:
                connection.execute(statement)
            applied.append(name)
        except Exception:  # noqa: BLE001 - never block the boot; app-level checks still run
            logger.warning("postgres schema upgrade %s failed", name, exc_info=True)
    return applied
