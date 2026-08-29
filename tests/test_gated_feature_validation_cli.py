"""Schema + verdict-ranking tests for the shared gated-feature verdict collector.

The collector feeds both the Monday CLI (text/JSON) and the operator dashboard,
so its output shape is a contract worth pinning.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.performance.gated_feature_report import (
    VERDICT_RANK,
    collect_feature_verdicts,
    momentum_verdict,
)


def _state(**settings):
    base = {
        "cross_sectional_momentum_enabled": False,
        "cross_sectional_momentum_top_pct": 30.0,
        "regime_router_enabled": False,
        "drawdown_governor_enabled": False,
        "drawdown_governor_soft_pct": 2.0,
        "drawdown_governor_hard_pct": 5.0,
        "drawdown_governor_floor": 0.25,
    }
    base.update(settings)
    return SimpleNamespace(
        settings=SimpleNamespace(**base),
        screener_service=None,
        market_screener_service=None,
        paper_trade_repository=None,
    )


def test_momentum_verdict_schema():
    result = momentum_verdict(_state())
    assert result["feature"] == "cross_sectional_momentum"
    assert result["flag"] == "cross_sectional_momentum_enabled"
    assert result["verdict"] == "GO"
    assert result["metrics"]["top_pct"] == 30.0
    assert set(result) == {"feature", "flag", "enabled", "verdict", "detail", "metrics"}


def test_collect_reports_overall_as_most_blocking_verdict():
    # Both data-dependent features are NEED-DATA (no screener / no trades), and
    # momentum is GO -> overall must be the most conservative (NEED-DATA).
    report = collect_feature_verdicts(_state())
    assert report["overall"] == "NEED-DATA"
    assert [f["feature"] for f in report["features"]] == [
        "regime_router",
        "drawdown_governor",
        "cross_sectional_momentum",
    ]
    # Serializable as JSON (the dashboard/notification/CLI contract).
    json.dumps(report, default=str)


def test_verdict_rank_orders_blocking_before_go():
    assert VERDICT_RANK["NO-GO"] < VERDICT_RANK["REVIEW"] < VERDICT_RANK["GO"]
    assert VERDICT_RANK["NEED-DATA"] < VERDICT_RANK["GO"]


def test_momentum_review_when_top_pct_out_of_range():
    result = momentum_verdict(_state(cross_sectional_momentum_top_pct=100.0))
    assert result["verdict"] == "REVIEW"
