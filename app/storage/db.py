"""Database helpers."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_broker_order_id
ON executions(broker_order_id)
WHERE broker_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS broker_order_snapshots (
    broker_order_id TEXT PRIMARY KEY,
    execution_id TEXT,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT,
    order_class TEXT,
    status TEXT NOT NULL,
    filled_qty REAL DEFAULT 0,
    filled_avg_price REAL,
    parent_order_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broker_order_snapshots_execution
ON broker_order_snapshots(execution_id);
CREATE INDEX IF NOT EXISTS idx_broker_order_snapshots_symbol
ON broker_order_snapshots(symbol);

CREATE TABLE IF NOT EXISTS broker_position_snapshots (
    symbol TEXT PRIMARY KEY,
    account_number TEXT,
    quantity REAL NOT NULL DEFAULT 0,
    average_price REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broker_position_snapshots_active
ON broker_position_snapshots(active);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    account_number TEXT,
    orders_seen INTEGER NOT NULL DEFAULT 0,
    positions_seen INTEGER NOT NULL DEFAULT 0,
    issues_json TEXT NOT NULL,
    account_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument_blacklist (
    symbol TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_health (
    strategy_name TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    closed_trades INTEGER NOT NULL DEFAULT 0,
    expectancy_usd REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    reason TEXT,
    updated_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS strategy_versions (
    id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    code_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_versions_name ON strategy_versions(strategy_name);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_status ON strategy_versions(status);

CREATE TABLE IF NOT EXISTS strategy_audits (
    id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    out_of_sample_trades INTEGER NOT NULL DEFAULT 0,
    deflated_sharpe REAL NOT NULL DEFAULT 0,
    rolling_sharpe REAL NOT NULL DEFAULT 0,
    profit_factor REAL NOT NULL DEFAULT 0,
    expectancy_after_costs REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    strategy_drawdown_pct REAL NOT NULL DEFAULT 0,
    unexplained_errors INTEGER NOT NULL DEFAULT 0,
    protected_exit_coverage_pct REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_audits_version
ON strategy_audits(strategy_version_id, created_at);

CREATE TABLE IF NOT EXISTS promotion_decisions (
    id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    strategy_audit_id TEXT,
    target_stage TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    blockers_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_promotion_decisions_version
ON promotion_decisions(strategy_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_promotion_decisions_approved
ON promotion_decisions(approved, target_stage);

CREATE TABLE IF NOT EXISTS broker_capabilities (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_mode TEXT NOT NULL,
    supports_equities INTEGER NOT NULL DEFAULT 0,
    supports_native_protection INTEGER NOT NULL DEFAULT 0,
    supports_client_idempotency INTEGER NOT NULL DEFAULT 0,
    supports_shorting INTEGER NOT NULL DEFAULT 0,
    supports_borrow_checks INTEGER NOT NULL DEFAULT 0,
    supports_financing_costs INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_capabilities_unique
ON broker_capabilities(broker, account_mode);

CREATE TABLE IF NOT EXISTS broker_account_identities (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_mode TEXT NOT NULL,
    account_id TEXT,
    account_number TEXT,
    expected_account_number TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_identities_unique
ON broker_account_identities(broker, account_mode);

CREATE TABLE IF NOT EXISTS broker_reconciliation_results (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_id TEXT,
    status TEXT NOT NULL,
    orders_seen INTEGER NOT NULL DEFAULT 0,
    positions_seen INTEGER NOT NULL DEFAULT 0,
    unknown_positions INTEGER NOT NULL DEFAULT 0,
    unprotected_positions INTEGER NOT NULL DEFAULT 0,
    issues_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broker_reconciliation_lookup
ON broker_reconciliation_results(broker, created_at);

CREATE TABLE IF NOT EXISTS broker_comparisons (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    primary_broker TEXT NOT NULL,
    comparison_broker TEXT NOT NULL,
    primary_order_id TEXT,
    comparison_order_id TEXT,
    status TEXT NOT NULL,
    primary_fill_price REAL,
    comparison_fill_price REAL,
    primary_cost_usd REAL NOT NULL DEFAULT 0,
    comparison_cost_usd REAL NOT NULL DEFAULT 0,
    primary_slippage_bps REAL,
    comparison_slippage_bps REAL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broker_comparisons_lookup
ON broker_comparisons(symbol, strategy_name, created_at);
CREATE INDEX IF NOT EXISTS idx_broker_comparisons_primary_order
ON broker_comparisons(primary_order_id);
CREATE INDEX IF NOT EXISTS idx_broker_comparisons_comparison_order
ON broker_comparisons(comparison_order_id);

CREATE TABLE IF NOT EXISTS etoro_demo_order_requests (
    client_order_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_etoro_demo_request_id
ON etoro_demo_order_requests(request_id);

CREATE TABLE IF NOT EXISTS portfolio_risk_snapshots (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    equity_usd REAL NOT NULL,
    peak_equity_usd REAL NOT NULL,
    drawdown_pct REAL NOT NULL DEFAULT 0,
    gross_exposure_pct REAL NOT NULL DEFAULT 0,
    largest_symbol_exposure_pct REAL NOT NULL DEFAULT 0,
    largest_sector_exposure_pct REAL NOT NULL DEFAULT 0,
    largest_correlated_exposure_pct REAL NOT NULL DEFAULT 0,
    open_positions INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_risk_created
ON portfolio_risk_snapshots(created_at);

CREATE TABLE IF NOT EXISTS rollout_gate_evidence (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    signed_by TEXT,
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rollout_gate_unique
ON rollout_gate_evidence(stage, gate_name);

CREATE TABLE IF NOT EXISTS learning_decision_snapshots (
    id TEXT PRIMARY KEY,
    decision_key TEXT NOT NULL,
    signal_id TEXT,
    execution_id TEXT,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    stage TEXT NOT NULL,
    deterministic_eligible INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    deterministic_score REAL,
    adjusted_score REAL,
    model_version_id TEXT,
    features_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_decision_key
ON learning_decision_snapshots(decision_key);
CREATE INDEX IF NOT EXISTS idx_learning_decision_lookup
ON learning_decision_snapshots(symbol, strategy_name, timeframe, created_at);

CREATE TABLE IF NOT EXISTS learning_outcome_labels (
    id TEXT PRIMARY KEY,
    decision_snapshot_id TEXT NOT NULL,
    label_type TEXT NOT NULL,
    status TEXT NOT NULL,
    net_pnl_usd REAL,
    net_r REAL,
    profitable INTEGER,
    source TEXT NOT NULL,
    horizon TEXT,
    details_json TEXT NOT NULL,
    labeled_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_outcome_unique
ON learning_outcome_labels(decision_snapshot_id, label_type, source, horizon);

CREATE TABLE IF NOT EXISTS learning_lifecycle_events (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    execution_id TEXT,
    proposal_id TEXT,
    broker_order_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_lifecycle_event_key
ON learning_lifecycle_events(event_key);
CREATE INDEX IF NOT EXISTS idx_learning_lifecycle_execution
ON learning_lifecycle_events(execution_id, occurred_at);

CREATE TABLE IF NOT EXISTS learning_trade_reviews (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    outcome_label_id TEXT,
    status TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    failure_categories_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    critic_model TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_review_execution
ON learning_trade_reviews(execution_id);

CREATE TABLE IF NOT EXISTS learning_experiments (
    id TEXT PRIMARY KEY,
    trade_review_id TEXT,
    title TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_dataset_versions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    accepted_oos_trades INTEGER NOT NULL DEFAULT 0,
    feature_schema_hash TEXT NOT NULL,
    source_cutoff_at TEXT NOT NULL,
    train_start_at TEXT,
    train_end_at TEXT,
    holdout_start_at TEXT,
    holdout_end_at TEXT,
    artifact_uri TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_meta_model_versions (
    id TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL,
    parent_version_id TEXT,
    model_type TEXT NOT NULL,
    status TEXT NOT NULL,
    deployment_mode TEXT NOT NULL,
    feature_names_json TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_model_status
ON learning_meta_model_versions(status, deployment_mode, created_at);

CREATE TABLE IF NOT EXISTS learning_model_evaluations (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL,
    champion_version_id TEXT,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    shadow_sessions INTEGER NOT NULL DEFAULT 0,
    leakage_passed INTEGER NOT NULL DEFAULT 0,
    schema_passed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_model_promotions (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL,
    target_mode TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    signed_by TEXT,
    blockers_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_drift_snapshots (
    id TEXT PRIMARY KEY,
    model_version_id TEXT,
    drift_score REAL NOT NULL DEFAULT 0,
    excessive INTEGER NOT NULL DEFAULT 0,
    feature_drift_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_job_idempotency
ON learning_jobs(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_learning_job_status
ON learning_jobs(status, scheduled_at);
"""


class Database:
    """Database wrapper supporting SQLite and PostgreSQL through one repository API."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.url = settings.database_url
        self.is_sqlite = self.url.startswith("sqlite:///")
        self.path = settings.database_path if self.is_sqlite else None
        self._engine = None
        if not self.is_sqlite:
            from sqlalchemy import create_engine

            self._engine = create_engine(self.url, pool_pre_ping=True, future=True)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """Yield a configured SQLite connection."""

        if self.is_sqlite:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()
            return

        assert self._engine is not None
        with self._engine.begin() as connection:
            yield _SqlAlchemyConnection(connection)

    def initialize(self) -> None:
        """Create all required tables."""

        with self.connect() as connection:
            schema = SCHEMA
            if not self.is_sqlite:
                schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            connection.executescript(schema)
            if self.is_sqlite:
                self._apply_schema_upgrades(connection)

    def exists(self) -> bool:
        """Return whether the backing SQLite file already exists."""

        return True if not self.is_sqlite else Path(self.path).exists()

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


class _DatabaseRow(Mapping[str, Any]):
    def __init__(self, values: Mapping[str, Any]):
        self._values = dict(values)
        self._keys = list(self._values)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[self._keys[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _SqlAlchemyResult:
    def __init__(self, result: Any, *, returned_id: Any | None = None):
        self._result = result
        self.lastrowid = returned_id
        self.rowcount = int(getattr(result, "rowcount", 0) or 0)

    def fetchone(self) -> _DatabaseRow | None:
        row = self._result.mappings().fetchone()
        return None if row is None else _DatabaseRow(row)

    def fetchall(self) -> list[_DatabaseRow]:
        return [_DatabaseRow(row) for row in self._result.mappings().fetchall()]


class _SqlAlchemyConnection:
    _AUTO_ID_TABLES = {
        "portfolio_snapshots",
        "signal_outcomes",
        "scan_decisions",
        "tracked_signals",
        "alert_history",
        "reconciliation_runs",
        "run_logs",
    }

    def __init__(self, connection: Any):
        self._connection = connection

    def executescript(self, script: str) -> None:
        from sqlalchemy import text

        for statement in (part.strip() for part in script.split(";")):
            if statement:
                self._connection.execute(text(statement))

    def execute(self, query: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> _SqlAlchemyResult:
        from sqlalchemy import text

        sql, values = self._bind_query(query, params)
        returning_id = False
        table = self._insert_table(sql)
        if table in self._AUTO_ID_TABLES and " returning " not in sql.lower():
            sql = sql.rstrip() + " RETURNING id"
            returning_id = True
        result = self._connection.execute(text(sql), values)
        returned_id = None
        if returning_id:
            row = result.fetchone()
            returned_id = row[0] if row is not None else None
        return _SqlAlchemyResult(result, returned_id=returned_id)

    @staticmethod
    def _insert_table(query: str) -> str | None:
        match = re.search(r"^\s*INSERT\s+INTO\s+([a-zA-Z0-9_]+)", query, flags=re.IGNORECASE)
        return match.group(1).lower() if match else None

    @staticmethod
    def _bind_query(
        query: str,
        params: Sequence[Any] | Mapping[str, Any],
    ) -> tuple[str, Mapping[str, Any]]:
        if isinstance(params, Mapping):
            return query, params
        values = list(params)
        index = 0

        def replace(_match: re.Match[str]) -> str:
            nonlocal index
            name = f"p{index}"
            index += 1
            return f":{name}"

        sql = re.sub(r"\?", replace, query)
        return sql, {f"p{position}": value for position, value in enumerate(values)}
