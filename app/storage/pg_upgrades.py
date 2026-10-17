"""Postgres-only schema upgrades, applied at boot (operator-approved 2026-10-10).

The per-symbol open-queue unique index that ``ExecutionCoordinator.enqueue`` treats as the
source of truth was only ever created for SQLite (``Database._apply_schema_upgrades``), so
production relied on application-level checks alone. Each upgrade runs in its own
transaction and a failure is logged, never raised: it must not stop the bot from booting.
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
