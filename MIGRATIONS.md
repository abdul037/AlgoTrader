# Migrations

This project currently uses startup schema initialization rather than a formal
migrations directory.

## Sprint 1 Execution Queue Hardening

Startup now applies an idempotent SQLite upgrade for existing databases:

- Adds `execution_queue.client_order_id` when missing.
- Adds partial unique index `idx_queue_unique_open_per_symbol` on open queue
  statuses: `queued` and `processing`.

The upgrade is safe to run more than once. If an existing database already has
duplicate open queue rows for the same symbol, SQLite will reject the unique
index creation; resolve the duplicate open rows before restarting the app.
