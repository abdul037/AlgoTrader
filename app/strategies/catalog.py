"""Strategy catalog reporting helpers."""

from __future__ import annotations

from math import isfinite
from typing import Any

from app.strategies import (
    CORE_STRATEGY_NAMES,
    ENHANCED_RESEARCH_STRATEGY_NAMES,
    STRATEGY_SPECS,
)


def build_strategy_catalog_report(
    *,
    settings: Any | None = None,
    governance: Any | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Return registry, approval, and audit-ranking status for operators."""

    active_specs = _active_specs(settings)
    paper_approved = _paper_approved(governance)
    production_qualified = _production_qualified(governance)
    rankings = _top_ranked_strategies(governance, top_n=top_n)
    return {
        "total_strategy_families": len(CORE_STRATEGY_NAMES) + len(ENHANCED_RESEARCH_STRATEGY_NAMES),
        "total_active_specs": len(active_specs),
        "total_strategy_specs": len(STRATEGY_SPECS),
        "core_strategy_families": len(CORE_STRATEGY_NAMES),
        "core_strategy_specs": len([spec for spec in STRATEGY_SPECS if spec.name in CORE_STRATEGY_NAMES]),
        "enhanced_research_strategy_families": len(ENHANCED_RESEARCH_STRATEGY_NAMES),
        "enhanced_research_strategy_specs": len(
            [spec for spec in STRATEGY_SPECS if spec.name in ENHANCED_RESEARCH_STRATEGY_NAMES]
        ),
        "active_specs_by_timeframe": _count_by_timeframe(active_specs),
        "specs_by_pack": _count_by_pack(STRATEGY_SPECS),
        "active_specs_by_pack": _count_by_pack(active_specs),
        "paper_approved_count": len(paper_approved),
        "paper_approved_strategies": paper_approved,
        "production_qualified_count": len(production_qualified),
        "production_qualified_strategies": production_qualified,
        "top_ranked_strategies": rankings,
        "learning_scope": "ranking_rejection_only",
        "live_trading_blocked_until_production_qualification": True,
    }


def _active_specs(settings: Any | None) -> list[Any]:
    configured = {
        item.strip().lower()
        for item in (getattr(settings, "screener_active_strategy_names", []) or [])
        if str(item).strip()
    }
    if not configured or "all" in configured:
        return list(STRATEGY_SPECS)
    return [spec for spec in STRATEGY_SPECS if spec.name.lower() in configured]


def _paper_approved(governance: Any | None) -> list[str]:
    if governance is None:
        return []
    try:
        return sorted({str(item) for item in governance.approved_paper_exploration_strategies()})
    except Exception:
        return []


def _production_qualified(governance: Any | None) -> list[str]:
    if governance is None:
        return []
    try:
        versions = {version.id: version for version in governance.list_versions(limit=1000)}
        decisions = governance.list_decisions(limit=1000)
    except Exception:
        return []
    qualified: set[str] = set()
    for decision in decisions:
        version = versions.get(decision.strategy_version_id)
        if (
            version is not None
            and bool(decision.approved)
            and decision.target_stage == "production_candidate"
            and version.status == "production_candidate"
        ):
            qualified.add(version.strategy_name)
    return sorted(qualified)


def _top_ranked_strategies(governance: Any | None, *, top_n: int) -> list[dict[str, Any]]:
    if governance is None:
        return []
    try:
        versions = {version.id: version for version in governance.list_versions(limit=1000)}
        audits = governance.list_audits(limit=1000)
    except Exception:
        return []

    latest_by_version: dict[str, Any] = {}
    for audit in audits:
        latest_by_version.setdefault(audit.strategy_version_id, audit)

    rows: list[dict[str, Any]] = []
    for version_id, audit in latest_by_version.items():
        version = versions.get(version_id)
        if version is None:
            continue
        row = {
            "strategy_name": version.strategy_name,
            "timeframe": audit.timeframe or version.timeframe,
            "stage": version.status,
            "pack": _pack_for_strategy(version.strategy_name),
            "out_of_sample_trades": int(audit.out_of_sample_trades),
            "deflated_sharpe": _finite(audit.deflated_sharpe),
            "rolling_sharpe": _finite(audit.rolling_sharpe),
            "profit_factor": _finite(audit.profit_factor),
            "expectancy_after_costs": _finite(audit.expectancy_after_costs),
            "max_drawdown_pct": _finite(audit.max_drawdown_pct),
            "unexplained_errors": int(audit.unexplained_errors),
            "rank_score": _rank_score(audit),
            "created_at": audit.created_at,
        }
        rows.append(row)
    return sorted(rows, key=lambda item: item["rank_score"], reverse=True)[: max(top_n, 1)]


def _rank_score(audit: Any) -> float:
    pf = min(_finite(audit.profit_factor), 5.0)
    return round(
        (_finite(audit.deflated_sharpe) * 40.0)
        + (_finite(audit.rolling_sharpe) * 20.0)
        + (pf * 8.0)
        + (_finite(audit.expectancy_after_costs) * 2.0)
        + min(float(audit.out_of_sample_trades), 300.0) * 0.05
        - (_finite(audit.max_drawdown_pct) * 1.5)
        - (float(audit.unexplained_errors) * 20.0),
        4,
    )


def _finite(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if isfinite(result) else 0.0


def _pack_for_strategy(strategy_name: str) -> str:
    if strategy_name in ENHANCED_RESEARCH_STRATEGY_NAMES:
        return "enhanced_research"
    if strategy_name in CORE_STRATEGY_NAMES:
        return "core_scanner"
    return "generated_or_external"


def _count_by_timeframe(specs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.timeframe] = counts.get(spec.timeframe, 0) + 1
    return dict(sorted(counts.items()))


def _count_by_pack(specs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in specs:
        pack = str((spec.metadata or {}).get("pack") or _pack_for_strategy(spec.name))
        counts[pack] = counts.get(pack, 0) + 1
    return dict(sorted(counts.items()))
