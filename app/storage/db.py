"""Database helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.runtime_settings import AppSettings


SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_name TEXT,
    status TEXT NOT NULL,
    order_json TEXT NOT NULL,
    signal_json TEXT,
    notes TEXT,
    decision_notes TEXT,
    approved_by TEXT,
    execution_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    executed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_symbol ON approvals(symbol);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    broker_order_id TEXT,
    request_json TEXT,
    response_json TEXT,
    error_message TEXT,
    realized_pnl_usd REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_proposal_id ON executions(proposal_id);
CREATE INDEX IF NOT EXISTS idx_executions_created_at ON executions(created_at);

CREATE TABLE IF NOT EXISTS execution_queue (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT,
    timeframe TEXT,
    mode TEXT NOT NULL,
    client_order_id TEXT,
    status TEXT NOT NULL,
    approval_required INTEGER NOT NULL DEFAULT 1,
    ready_for_execution INTEGER NOT NULL DEFAULT 0,
    requested_entry_price REAL,
    latest_quote_price REAL,
    latest_quote_timestamp TEXT,
    validation_reason TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    executed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_queue_status ON execution_queue(status);
CREATE INDEX IF NOT EXISTS idx_execution_queue_symbol ON execution_queue(symbol);
CREATE INDEX IF NOT EXISTS idx_execution_queue_proposal ON execution_queue(proposal_id);

CREATE TABLE IF NOT EXISTS backtests (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    trades_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_states (
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    state TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    candle_timestamp TEXT,
    rate_timestamp TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (symbol, strategy_name, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_signal_states_symbol ON signal_states(symbol);

CREATE TABLE IF NOT EXISTS runtime_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    last_price REAL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracked_signals_status ON tracked_signals(status);
CREATE INDEX IF NOT EXISTS idx_tracked_signals_symbol ON tracked_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_tracked_signals_opened_at ON tracked_signals(opened_at);

CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    symbol TEXT,
    strategy_name TEXT,
    timeframe TEXT,
    status TEXT NOT NULL,
    message_text TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_history_category ON alert_history(category);
CREATE INDEX IF NOT EXISTS idx_alert_history_symbol ON alert_history(symbol);
CREATE INDEX IF NOT EXISTS idx_alert_history_created_at ON alert_history(created_at);

CREATE TABLE IF NOT EXISTS scan_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_task TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    final_score REAL,
    alert_eligible INTEGER NOT NULL DEFAULT 0,
    freshness TEXT,
    reason_codes_json TEXT NOT NULL,
    rejection_reasons_json TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_decisions_symbol ON scan_decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_scan_decisions_status ON scan_decisions(status);
CREATE INDEX IF NOT EXISTS idx_scan_decisions_lookup ON scan_decisions(symbol, strategy_name, timeframe, created_at);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    regime_label TEXT,
    hold_style TEXT,
    status TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    stop_loss REAL,
    target_1 REAL,
    target_2 REAL,
    target_3 REAL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl_usd REAL DEFAULT 0,
    unrealized_pnl_usd REAL DEFAULT 0,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);
CREATE INDEX IF NOT EXISTS idx_paper_positions_symbol ON paper_positions(symbol);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    proposal_id TEXT,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    regime_label TEXT,
    hold_style TEXT,
    outcome TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    realized_pnl_pct REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_trades_closed_at ON paper_trades(closed_at);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL,
    position_count INTEGER NOT NULL DEFAULT 0,
    credit REAL,
    unrealized_pnl_usd REAL,
    positions_json TEXT NOT NULL,
    raw_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(snapshot_ts);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_source TEXT NOT NULL,
    alert_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT,
    timeframe TEXT,
    alert_created_at TEXT NOT NULL,
    alert_entry_price REAL,
    alert_stop REAL,
    alert_target REAL,
    alert_score REAL,
    alert_payload_json TEXT,
    matched_position_id INTEGER,
    matched_at TEXT,
    position_open_at TEXT,
    position_open_rate REAL,
    position_amount_usd REAL,
    position_units REAL,
    position_is_buy INTEGER,
    position_leverage INTEGER,
    position_stop_loss_rate REAL,
    position_take_profit_rate REAL,
    closed_at TEXT,
    close_rate REAL,
    realized_pnl_usd REAL,
    realized_r_multiple REAL,
    outcome_status TEXT NOT NULL DEFAULT 'pending_match',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_status ON signal_outcomes(outcome_status);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_position ON signal_outcomes(matched_position_id);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_alert_ts ON signal_outcomes(alert_created_at);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_alert_id ON signal_outcomes(alert_id);
"""


class Database:
    """SQLite database wrapper."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.path = settings.database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create all required tables."""

        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._apply_schema_upgrades(connection)

    def exists(self) -> bool:
        """Return whether the backing SQLite file already exists."""

        return Path(self.path).exists()

    def _apply_schema_upgrades(self, connection: sqlite3.Connection) -> None:
        """Apply idempotent schema upgrades for existing SQLite files."""

        if not self._column_exists(connection, "execution_queue", "client_order_id"):
            connection.execute("ALTER TABLE execution_queue ADD COLUMN client_order_id TEXT")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_unique_open_per_symbol
            ON execution_queue(symbol)
            WHERE status IN ('queued','processing')
            """
        )

    @staticmethod
    def _column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(str(row["name"]) == column_name for row in rows)
