"""The daily summary should include a today-focused P&L block."""

from __future__ import annotations

from types import SimpleNamespace

from app.utils.time import utc_now
from app.workflow.operations import _daily_pnl_lines, _gated_feature_lines


def _trade(strategy: str, pnl: float, day: str):
    return SimpleNamespace(strategy_name=strategy, realized_pnl_usd=pnl, closed_at=f"{day}T15:00:00Z", payload={})


def _service(trades):
    trade_repo = SimpleNamespace(list=lambda limit=2000: trades)
    return SimpleNamespace(paper_trading=SimpleNamespace(trades=trade_repo))


def test_today_block_reports_pnl_and_best_worst() -> None:
    today = utc_now().strftime("%Y-%m-%d")
    trades = [
        _trade("connors_rsi2_reversion", 120.0, today),
        _trade("mean_reversion", -40.0, today),
        _trade("trend_following", 50.0, "2020-01-01"),  # old, ignored for "today"
    ]
    lines = _daily_pnl_lines(_service(trades))
    text = "\n".join(lines)
    assert "Today's paper P&L:" in text
    assert "Realized today: +80.00 (2 trades)" in text  # 120 - 40
    assert "Best: connors_rsi2_reversion +120.00" in text
    assert "Worst: mean_reversion -40.00" in text


def test_no_trades_today_reports_zero() -> None:
    trades = [_trade("s", 10.0, "2020-01-01")]
    lines = _daily_pnl_lines(_service(trades))
    assert "Realized today: +0.00 (0 trades)" in "\n".join(lines)


def test_empty_when_no_paper_trading() -> None:
    assert _daily_pnl_lines(SimpleNamespace()) == []
    assert _daily_pnl_lines(_service([])) == []


def _settings(**over):
    base = {
        "regime_router_enabled": False,
        "drawdown_governor_enabled": False,
        "cross_sectional_momentum_enabled": False,
        "cross_sectional_momentum_top_pct": 30.0,
        "drawdown_governor_soft_pct": 2.0,
        "drawdown_governor_hard_pct": 5.0,
        "drawdown_governor_floor": 0.25,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_gated_feature_lines_reports_state_and_verdicts() -> None:
    service = SimpleNamespace(
        settings=_settings(),
        market_screener=None,  # regime -> NEED-DATA
        paper_trading=SimpleNamespace(trades=None),  # governor -> NEED-DATA
    )
    lines = _gated_feature_lines(service)
    text = "\n".join(lines)
    assert "Gated features (default-off):" in text
    assert "regime router: off — NEED-DATA" in text
    assert "drawdown governor: off — NEED-DATA" in text
    assert "cross-sectional momentum: off — GO" in text
    assert "Overall: NEED-DATA" in text


def test_gated_feature_lines_reflects_enabled_flag() -> None:
    service = SimpleNamespace(
        settings=_settings(cross_sectional_momentum_enabled=True),
        market_screener=None,
        paper_trading=SimpleNamespace(trades=None),
    )
    text = "\n".join(_gated_feature_lines(service))
    assert "cross-sectional momentum: ON — GO" in text


def test_gated_feature_lines_empty_without_settings() -> None:
    assert _gated_feature_lines(SimpleNamespace()) == []
