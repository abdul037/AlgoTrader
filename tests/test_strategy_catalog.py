from __future__ import annotations

from app.models.institutional import PromotionDecision, StrategyAudit, StrategyVersion
from app.storage.db import Database
from app.storage.repositories import StrategyGovernanceRepository
from app.strategies import CORE_STRATEGY_NAMES, ENHANCED_RESEARCH_STRATEGY_NAMES, STRATEGY_SPECS
from app.strategies.catalog import build_strategy_catalog_report
from tests.conftest import make_settings


def _repository(tmp_path):
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()
    return settings, StrategyGovernanceRepository(database)


def test_strategy_catalog_reports_core_enhanced_and_governance_counts(tmp_path):
    settings, repository = _repository(tmp_path)
    paper_version = repository.create_version(
        StrategyVersion(
            strategy_name="relative_strength_momentum",
            code_version="abc123",
            dataset_version="research-adjusted-v1",
            timeframe="1d",
            status="paper_exploration",
        )
    )
    repository.record_decision(
        PromotionDecision(
            strategy_version_id=paper_version.id,
            target_stage="paper_exploration",
            approved=True,
            decided_by="test",
        )
    )
    production_version = repository.create_version(
        StrategyVersion(
            strategy_name="trend_following",
            code_version="abc123",
            dataset_version="pit-sp500-v1",
            timeframe="1d",
            status="production_candidate",
        )
    )
    repository.record_audit(
        StrategyAudit(
            strategy_version_id=production_version.id,
            dataset_version="pit-sp500-v1",
            timeframe="1d",
            out_of_sample_trades=240,
            deflated_sharpe=1.05,
            rolling_sharpe=1.4,
            profit_factor=1.45,
            expectancy_after_costs=8.0,
            max_drawdown_pct=6.0,
            unexplained_errors=0,
        )
    )
    repository.record_decision(
        PromotionDecision(
            strategy_version_id=production_version.id,
            target_stage="production_candidate",
            approved=True,
            decided_by="test",
        )
    )

    report = build_strategy_catalog_report(settings=settings, governance=repository)

    assert report["total_strategy_families"] == 28
    assert report["total_strategy_specs"] == 70
    assert report["total_active_specs"] == 70
    assert report["core_strategy_families"] == 12
    assert report["core_strategy_specs"] == 36
    assert report["enhanced_research_strategy_families"] == 16
    assert report["enhanced_research_strategy_specs"] == 34
    assert report["paper_approved_count"] == 1
    assert report["paper_approved_strategies"] == ["relative_strength_momentum"]
    assert report["production_qualified_count"] == 1
    assert report["production_qualified_strategies"] == ["trend_following"]
    assert report["top_ranked_strategies"][0]["strategy_name"] == "trend_following"
    assert report["learning_scope"] == "ranking_rejection_only"


def test_strategy_catalog_respects_configured_active_strategy_subset(tmp_path):
    settings = make_settings(tmp_path, screener_active_strategy_names=["relative_strength_momentum"])

    report = build_strategy_catalog_report(settings=settings)

    assert report["total_strategy_specs"] == 70
    assert report["total_active_specs"] == 2
    assert report["active_specs_by_pack"] == {"enhanced_research": 2}


def test_scheduled_all_buckets_cover_core_and_enhanced_specs():
    scheduled_timeframes = {"1m", "5m", "10m", "15m", "1h", "1d", "1w"}
    covered_specs = [spec for spec in STRATEGY_SPECS if spec.timeframe in scheduled_timeframes]

    assert len(covered_specs) == 70
    assert len([spec for spec in covered_specs if spec.name in CORE_STRATEGY_NAMES]) == 36
    assert len([spec for spec in covered_specs if spec.name in ENHANCED_RESEARCH_STRATEGY_NAMES]) == 34
