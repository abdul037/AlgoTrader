"""Stock-path proposal blockers found 2026-09-27: risk-cap sizing and the loss cooldown.

Found 2026-09-27: with institutional portfolio controls on, the gate capped
per-trade risk at 0.50% while auto-proposals were sized to 1.0%, so proposals
were rejected with "Estimated trade risk 0.53-1.00% exceeds the 0.50% cap".
"""

from __future__ import annotations

from types import SimpleNamespace

from app.risk.proposal_sizing import risk_based_proposal_notional
from app.risk.rules import effective_max_risk_per_trade_pct, estimate_risk_amount


def _settings(**overrides):
    base = dict(
        max_risk_per_trade_pct=1.0,
        institutional_portfolio_controls_enabled=True,
        portfolio_future_max_risk_per_trade_pct=0.5,
        portfolio_micro_live_max_risk_per_trade_pct=0.1,
        default_trade_amount_usd=100_000.0,
        max_trade_amount_usd=100_000.0,
        auto_propose_risk_based_sizing=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_effective_cap_follows_institutional_controls() -> None:
    assert effective_max_risk_per_trade_pct(_settings()) == 0.5
    assert effective_max_risk_per_trade_pct(_settings(), live=True) == 0.1
    assert effective_max_risk_per_trade_pct(_settings(institutional_portfolio_controls_enabled=False)) == 1.0
    # The tighter cap never loosens the base cap.
    assert effective_max_risk_per_trade_pct(_settings(portfolio_future_max_risk_per_trade_pct=2.0)) == 1.0


def test_risk_based_proposal_fits_under_the_gate_cap() -> None:
    settings = _settings()
    equity, entry, stop = 99_607.60, 80.0, 78.0
    amount, details = risk_based_proposal_notional(settings, entry_price=entry, stop_price=stop, equity_usd=equity)
    risk_pct = estimate_risk_amount(entry, stop, amount, 1) / equity * 100.0
    assert details["sizing_mode"] == "risk_based"
    assert risk_pct <= effective_max_risk_per_trade_pct(settings)
    assert risk_pct > 0.49  # still uses the whole budget


class _Executions:
    """Yesterday ended on a 4-loss streak; today has had `today_losses` losses."""

    def __init__(self, today_losses: int) -> None:
        self.today_losses = today_losses

    def count_since(self, _since):
        return 0

    def daily_loss_stats(self):
        return -10.0 * self.today_losses, self.today_losses

    def consecutive_losses(self):  # all-history streak: must not drive the cooldown
        return 4 + self.today_losses

    def period_realized_pnl(self, *, days):
        return 0.0


def test_loss_streak_cooldown_resets_each_trading_day() -> None:
    from app.risk.context import build_risk_context

    settings = SimpleNamespace(execution_mode="paper", paper_broker="self_simulated", paper_account_balance_usd=100_000.0)

    assert build_risk_context(settings, None, _Executions(0)).consecutive_losses_today == 0
    assert build_risk_context(settings, None, _Executions(4)).consecutive_losses_today == 4


def test_live_mode_sizes_to_the_micro_live_cap() -> None:
    settings = _settings(execution_mode="live")
    equity, entry, stop = 100_000.0, 100.0, 98.0
    amount, _details = risk_based_proposal_notional(settings, entry_price=entry, stop_price=stop, equity_usd=equity)
    assert estimate_risk_amount(entry, stop, amount, 1) / equity * 100.0 <= 0.1
