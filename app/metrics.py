"""Minimal Prometheus-compatible operational metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["observability"])


def _age_seconds(value: Any) -> float:
    if not value:
        return -1.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return -1.0


@router.get("/metrics", response_class=Response)
def metrics(request: Request) -> Response:
    db = request.app.state.db
    automation = request.app.state.automation_service.status()
    with db.connect() as connection:
        counts = {}
        for name, query in {
            "algobot_proposals_total": "SELECT COUNT(*) FROM approvals",
            "algobot_executions_total": "SELECT COUNT(*) FROM executions",
            "algobot_queue_open": "SELECT COUNT(*) FROM execution_queue WHERE status IN ('queued','processing')",
            "algobot_orders_total": "SELECT COUNT(*) FROM broker_order_snapshots",
            "algobot_fills_total": "SELECT COUNT(*) FROM broker_order_snapshots WHERE status = 'filled'",
            "algobot_positions_open": "SELECT COUNT(*) FROM broker_position_snapshots WHERE active = 1",
            "algobot_reconciliation_failures_total": "SELECT COUNT(*) FROM reconciliation_runs WHERE status = 'error'",
            "algobot_blacklisted_symbols": "SELECT COUNT(*) FROM instrument_blacklist WHERE active = 1",
            "algobot_inactive_strategies": "SELECT COUNT(*) FROM strategy_health WHERE active = 0",
        }.items():
            counts[name] = int(connection.execute(query).fetchone()[0])
        realized_row = connection.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS value FROM executions"
        ).fetchone()
        data_row = connection.execute("SELECT MAX(generated_at) AS value FROM signal_states").fetchone()
        reconciliation_row = connection.execute(
            "SELECT status, account_json, created_at FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        scheduler_rows = connection.execute(
            """
            SELECT state_key, state_value
            FROM runtime_state
            WHERE state_key LIKE 'workflow:last_%_at'
            """
        ).fetchall()

    latest_reconciliation = dict(reconciliation_row) if reconciliation_row is not None else {}
    account = json.loads(latest_reconciliation.get("account_json") or "{}")
    equity = float(account.get("equity") or 0.0)
    last_equity = float(account.get("last_equity") or equity)
    counts.update(
        {
            "algobot_realized_pnl_usd": float(realized_row["value"] if realized_row else 0.0),
            "algobot_account_equity_usd": equity,
            "algobot_account_drawdown_usd": max(last_equity - equity, 0.0),
            "algobot_reconciliation_healthy": int(latest_reconciliation.get("status") == "ok"),
            "algobot_reconciliation_age_seconds": _age_seconds(latest_reconciliation.get("created_at")),
            "algobot_data_freshness_age_seconds": _age_seconds(data_row["value"] if data_row else None),
            "algobot_automation_paused": int(automation.paused),
            "algobot_kill_switch_enabled": int(automation.kill_switch_enabled),
            "algobot_real_trading_enabled": int(automation.enable_real_trading),
            "algobot_paper_auto_approve_enabled": int(automation.paper_auto_approve_proposals),
            "algobot_auto_execution_worker_enabled": int(automation.auto_execution_worker_enabled),
            "algobot_alpaca_account_verified": int(bool(automation.account_verified)),
        }
    )
    lines = [f"{name} {value}" for name, value in sorted(counts.items())]
    for row in scheduler_rows:
        bucket = str(row["state_key"]).removeprefix("workflow:last_").removesuffix("_at")
        lines.append(
            f'algobot_scheduler_bucket_age_seconds{{bucket="{bucket}"}} {_age_seconds(row["state_value"])}'
        )
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
