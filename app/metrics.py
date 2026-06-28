"""Minimal Prometheus-compatible operational metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

from app.strategies.catalog import build_strategy_catalog_report

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
            "algobot_strategy_versions_total": "SELECT COUNT(*) FROM strategy_versions",
            "algobot_strategy_promotions_approved": (
                "SELECT COUNT(*) FROM promotion_decisions "
                "WHERE approved = 1 AND target_stage = 'production_candidate'"
            ),
            "algobot_rollout_gates_passed": (
                "SELECT COUNT(*) FROM rollout_gate_evidence WHERE status = 'passed'"
            ),
            "algobot_broker_comparisons_completed": (
                "SELECT COUNT(*) FROM broker_comparisons WHERE status = 'completed'"
            ),
            "algobot_broker_reconciliation_failures_total": (
                "SELECT COUNT(*) FROM broker_reconciliation_results WHERE status = 'error'"
            ),
            "algobot_learning_decisions_total": "SELECT COUNT(*) FROM learning_decision_snapshots",
            "algobot_learning_real_labels_total": (
                "SELECT COUNT(*) FROM learning_outcome_labels WHERE label_type = 'real'"
            ),
            "algobot_learning_counterfactual_labels_total": (
                "SELECT COUNT(*) FROM learning_outcome_labels WHERE label_type = 'counterfactual'"
            ),
            "algobot_learning_reviews_total": "SELECT COUNT(*) FROM learning_trade_reviews",
            "algobot_learning_experiments_total": "SELECT COUNT(*) FROM learning_experiments",
            "algobot_learning_models_total": "SELECT COUNT(*) FROM learning_meta_model_versions",
            "algobot_learning_review_backlog": (
                "SELECT COUNT(*) FROM learning_jobs WHERE job_type = 'review_trade' AND status = 'pending'"
            ),
            "algobot_learning_jobs_failed": (
                "SELECT COUNT(*) FROM learning_jobs WHERE status = 'failed'"
            ),
            "algobot_learning_excessive_drift_total": (
                "SELECT COUNT(*) FROM learning_drift_snapshots WHERE excessive = 1"
            ),
            "algobot_learning_promotions_approved": (
                "SELECT COUNT(*) FROM learning_model_promotions WHERE approved = 1"
            ),
            "algobot_learning_rollbacks_total": (
                "SELECT COUNT(*) FROM learning_model_promotions "
                "WHERE blockers_json LIKE '%operator_rollback%'"
            ),
            "algobot_learning_champion_active": (
                "SELECT COUNT(*) FROM learning_meta_model_versions "
                "WHERE status = 'champion' AND deployment_mode IN ('paper','live')"
            ),
            "algobot_rl_policy_versions_total": "SELECT COUNT(*) FROM rl_policy_versions",
            "algobot_rl_policy_proposals_total": "SELECT COUNT(*) FROM rl_policy_proposals WHERE status IN ('proposed','queued')",
            "algobot_rl_policy_rejections_total": "SELECT COUNT(*) FROM rl_policy_proposals WHERE status IN ('rejected','blocked')",
            "algobot_strategy_lab_generated_total": "SELECT COUNT(*) FROM strategy_lab_generated_strategies",
            "algobot_strategy_lab_paper_generated_total": (
                "SELECT COUNT(*) FROM strategy_lab_generated_strategies WHERE status = 'paper_generated'"
            ),
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
        risk_row = connection.execute(
            """
            SELECT drawdown_pct, gross_exposure_pct
            FROM portfolio_risk_snapshots
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        etoro_reconciliation_row = connection.execute(
            """
            SELECT status
            FROM broker_reconciliation_results
            WHERE broker = 'etoro'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        learning_cost_row = connection.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS value
            FROM learning_trade_reviews
            WHERE created_at >= ?
            """,
            (datetime.now(UTC).date().isoformat(),),
        ).fetchone()

    latest_reconciliation = dict(reconciliation_row) if reconciliation_row is not None else {}
    institutional_readiness = request.app.state.institutional_service.readiness()
    strategy_catalog = build_strategy_catalog_report(
        settings=request.app.state.settings,
        governance=request.app.state.strategy_governance_repository,
    )
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
            "algobot_portfolio_drawdown_pct": float(risk_row["drawdown_pct"] if risk_row else 0.0),
            "algobot_portfolio_gross_exposure_pct": float(
                risk_row["gross_exposure_pct"] if risk_row else 0.0
            ),
            "algobot_etoro_demo_reconciliation_healthy": int(
                bool(etoro_reconciliation_row and etoro_reconciliation_row["status"] == "ok")
            ),
            "algobot_institutional_rollout_ready": int(institutional_readiness["ready"]),
            "algobot_strategy_families_total": strategy_catalog["total_strategy_families"],
            "algobot_strategy_specs_total": strategy_catalog["total_strategy_specs"],
            "algobot_strategy_specs_active": strategy_catalog["total_active_specs"],
            "algobot_strategy_families_enhanced_total": strategy_catalog[
                "enhanced_research_strategy_families"
            ],
            "algobot_strategy_specs_enhanced_total": strategy_catalog[
                "enhanced_research_strategy_specs"
            ],
            "algobot_paper_approved_strategy_families": strategy_catalog["paper_approved_count"],
            "algobot_production_qualified_strategy_families": strategy_catalog[
                "production_qualified_count"
            ],
            "algobot_learning_review_cost_today_usd": float(
                learning_cost_row["value"] if learning_cost_row else 0.0
            ),
            "algobot_learning_capture_enabled": int(request.app.state.settings.learning_capture_enabled),
            "algobot_learning_worker_enabled": int(request.app.state.settings.learning_worker_enabled),
            "algobot_learning_reviews_enabled": int(request.app.state.settings.learning_reviews_enabled),
            "algobot_learning_training_enabled": int(request.app.state.settings.learning_training_enabled),
            "algobot_learning_openai_enabled": int(request.app.state.settings.learning_openai_enabled),
            "algobot_learning_auto_promote_paper_enabled": int(
                request.app.state.settings.learning_auto_promote_paper_enabled
            ),
            "algobot_learning_model_gating_enabled": int(
                request.app.state.settings.model_deployment_mode == "gating"
            ),
            "algobot_rl_policy_enabled": int(request.app.state.settings.rl_policy_enabled),
            "algobot_rl_policy_paper_proposals_enabled": int(
                request.app.state.settings.rl_policy_paper_proposals_enabled
            ),
        }
    )
    lines = [f"{name} {value}" for name, value in sorted(counts.items())]
    for row in scheduler_rows:
        bucket = str(row["state_key"]).removeprefix("workflow:last_").removesuffix("_at")
        lines.append(
            f'algobot_scheduler_bucket_age_seconds{{bucket="{bucket}"}} {_age_seconds(row["state_value"])}'
        )
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
