"""Schema + verdict-ranking tests for the validation CLI's structured output."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import validate_gated_features as cli


def _app(**settings):
    base = {
        "cross_sectional_momentum_enabled": False,
        "cross_sectional_momentum_top_pct": 30.0,
        "regime_router_enabled": False,
        "drawdown_governor_enabled": False,
    }
    base.update(settings)
    state = SimpleNamespace(
        settings=SimpleNamespace(**base),
        screener_service=None,
        market_screener_service=None,
        paper_trade_repository=None,
    )
    return SimpleNamespace(state=state)


def test_momentum_result_schema():
    result = cli._momentum_result(_app())
    assert result["feature"] == "cross_sectional_momentum"
    assert result["flag"] == "cross_sectional_momentum_enabled"
    assert result["verdict"] == "GO"
    assert result["metrics"]["top_pct"] == 30.0
    assert set(result) == {"feature", "flag", "enabled", "verdict", "detail", "metrics"}


def test_collect_reports_overall_as_most_blocking_verdict():
    # Both data-dependent features are NEED-DATA (no screener / no trades), and
    # momentum is GO -> overall must be the most conservative (NEED-DATA).
    report = cli.collect(_app())
    assert report["overall"] == "NEED-DATA"
    assert [f["feature"] for f in report["features"]] == [
        "regime_router",
        "drawdown_governor",
        "cross_sectional_momentum",
    ]
    # Serializable as JSON (the dashboard/notification contract).
    json.dumps(report, default=str)


def test_verdict_rank_orders_blocking_before_go():
    assert cli.VERDICT_RANK["NO-GO"] < cli.VERDICT_RANK["REVIEW"] < cli.VERDICT_RANK["GO"]
    assert cli.VERDICT_RANK["NEED-DATA"] < cli.VERDICT_RANK["GO"]


def test_momentum_review_when_top_pct_out_of_range():
    result = cli._momentum_result(_app(cross_sectional_momentum_top_pct=100.0))
    assert result["verdict"] == "REVIEW"
