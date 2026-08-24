from __future__ import annotations

from types import SimpleNamespace

from app.models.trade import TradeOrder
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskContext, RiskManager
from app.risk.sectors import correlation_bucket_for_symbol, sector_for_symbol
from tests.conftest import make_settings

# -- mapping -----------------------------------------------------------------


def test_sector_and_bucket_mapping() -> None:
    assert sector_for_symbol("NVDA") == "SMH"
    assert sector_for_symbol("GOOG") == "XLC"
    assert correlation_bucket_for_symbol("NVDA") == "tech_complex"
    assert correlation_bucket_for_symbol("GOOG") == "tech_complex"  # different sector, same bucket
    assert correlation_bucket_for_symbol("JPM") == "financials"
    assert sector_for_symbol("ZZZZ") == "unknown"
    assert correlation_bucket_for_symbol("ZZZZ") == "unclassified"


# -- build_risk_context populates the accumulation maps ----------------------


class _Executions:
    def count_since(self, _since):
        return 0

    def daily_loss_stats(self):
        return 0.0, 0

    def consecutive_losses(self):
        return 0

    def period_realized_pnl(self, *, days):
        return 0.0


def _broker(positions):
    account = SimpleNamespace(equity=10_000.0, cash_balance=10_000.0)
    return SimpleNamespace(get_portfolio=lambda: SimpleNamespace(account=account, positions=positions))


def test_build_risk_context_accumulates_sector_and_bucket_exposure() -> None:
    settings = SimpleNamespace(
        execution_mode="paper",
        paper_broker="alpaca",  # not self_simulated -> takes the broker portfolio path
        etoro_account_mode="demo",
    )
    positions = [
        SimpleNamespace(symbol="NVDA", market_value=1500.0),  # SMH / tech_complex
        SimpleNamespace(symbol="AMD", market_value=1000.0),  # SMH / tech_complex
        SimpleNamespace(symbol="GOOG", market_value=1000.0),  # XLC / tech_complex
        SimpleNamespace(symbol="JPM", market_value=500.0),  # XLF / financials
    ]
    ctx = build_risk_context(settings, _broker(positions), _Executions())

    # NVDA + AMD = 25% in SMH; GOOG = 10% in XLC.
    assert ctx.exposure_by_sector_pct["SMH"] == 25.0
    assert ctx.exposure_by_sector_pct["XLC"] == 10.0
    # tech_complex accumulates SMH + XLC = 35%; financials = 5%.
    assert ctx.exposure_by_correlation_bucket_pct["tech_complex"] == 35.0
    assert ctx.exposure_by_correlation_bucket_pct["financials"] == 5.0
    assert ctx.correlated_exposure_pct == 35.0  # most concentrated bucket


# -- guardrail now enforces accumulated exposure -----------------------------


def _order(symbol: str, amount: float = 1000.0) -> TradeOrder:
    # No sector in metadata -> the guardrail must derive it from the symbol.
    return TradeOrder(symbol=symbol, amount_usd=amount, proposed_price=100, stop_loss=99)


def test_sector_cap_fires_from_accumulated_positions_without_metadata(tmp_path) -> None:
    settings = make_settings(tmp_path, institutional_portfolio_controls_enabled=True)
    manager = RiskManager(settings)
    # 20% already in SMH; a 10% NVDA order (also SMH) -> 30% > 25% cap.
    ctx = RiskContext(
        account_balance=10_000,
        exposure_by_sector_pct={"SMH": 20.0},
        exposure_by_correlation_bucket_pct={"tech_complex": 20.0},
        mode="paper",
    )
    result = manager.validate_order(_order("NVDA"), ctx)
    assert "Projected sector exposure exceeds the portfolio limit" in result.reasons


def test_correlated_cap_catches_cross_sector_tech_concentration(tmp_path) -> None:
    settings = make_settings(tmp_path, institutional_portfolio_controls_enabled=True)
    manager = RiskManager(settings)
    # Split across two tech sectors so neither breaches the 25% sector cap, but
    # the tech complex is at 25% -> a 10% NVDA order breaches the 30% correlated cap.
    ctx = RiskContext(
        account_balance=10_000,
        exposure_by_sector_pct={"SMH": 15.0, "XLC": 10.0},
        exposure_by_correlation_bucket_pct={"tech_complex": 25.0},
        mode="paper",
    )
    result = manager.validate_order(_order("NVDA"), ctx)
    assert "Projected correlated exposure exceeds the portfolio limit" in result.reasons
    assert "Projected sector exposure exceeds the portfolio limit" not in result.reasons


def test_diversifying_trade_not_over_blocked(tmp_path) -> None:
    settings = make_settings(tmp_path, institutional_portfolio_controls_enabled=True)
    manager = RiskManager(settings)
    # Tech complex is heavily concentrated, but a financials order is diversifying
    # and must not be blocked by the tech concentration.
    ctx = RiskContext(
        account_balance=10_000,
        gross_exposure_pct=28.0,
        exposure_by_sector_pct={"SMH": 15.0, "XLC": 13.0},
        exposure_by_correlation_bucket_pct={"tech_complex": 28.0},
        correlated_exposure_pct=28.0,
        mode="paper",
    )
    result = manager.validate_order(_order("JPM"), ctx)
    assert "Projected correlated exposure exceeds the portfolio limit" not in result.reasons
    assert "Projected sector exposure exceeds the portfolio limit" not in result.reasons
