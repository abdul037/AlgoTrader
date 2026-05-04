"""Risk guardrail orchestration."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.broker.instrument_resolver import InstrumentResolver
from app.config import AppSettings
from app.models.trade import TradeOrder
from app.risk.rules import RiskValidationResult, estimate_risk_amount, leverage_cap_for_asset


class RiskContext(BaseModel):
    """Live account context for risk checks."""

    account_balance: float = Field(gt=0)
    daily_realized_pnl_usd: float = 0.0
    weekly_realized_pnl_usd: float = 0.0
    open_positions: int = 0
    positions_by_symbol: dict[str, int] = Field(default_factory=dict)
    consecutive_losses_today: int = 0
    trades_today: int = 0
    mode: str = "demo"


class RiskManager:
    """Validate orders against hard trading guardrails."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.resolver = InstrumentResolver(settings)

    def validate_order(self, order: TradeOrder, context: RiskContext) -> RiskValidationResult:
        """Validate an order against configured guardrails."""

        reasons: list[str] = []
        risk_amount = 0.0
        risk_pct = 0.0

        try:
            instrument = self.resolver.resolve(order.symbol)
        except ValueError as exc:
            return RiskValidationResult(passed=False, reasons=[str(exc)])

        leverage_cap = leverage_cap_for_asset(
            asset_class=instrument.asset_class.value,
            max_equity_leverage=self.settings.max_equity_leverage,
            max_gold_leverage=self.settings.max_gold_leverage,
        )
        if order.leverage > leverage_cap:
            reasons.append(
                f"Leverage {order.leverage} exceeds the cap of {leverage_cap} for {instrument.asset_class.value}"
            )

        if context.open_positions >= self.settings.max_open_positions:
            reasons.append("Maximum number of open positions reached")

        if context.trades_today >= int(getattr(self.settings, "max_trades_per_day", 999999)):
            reasons.append("Maximum number of trades for today reached")

        symbol_positions = int(context.positions_by_symbol.get(order.symbol.upper(), 0))
        if symbol_positions >= self.settings.per_symbol_position_limit:
            reasons.append("Per-symbol position limit reached")

        if context.daily_realized_pnl_usd <= -abs(self.settings.max_daily_loss_usd):
            reasons.append("Daily loss limit has already been reached")

        if context.weekly_realized_pnl_usd <= -abs(self.settings.max_weekly_loss_usd):
            reasons.append("Weekly loss limit has already been reached")

        if context.consecutive_losses_today >= self.settings.max_consecutive_losses_before_cooldown:
            reasons.append(
                f"Trading halted after {self.settings.max_consecutive_losses_before_cooldown} consecutive losses today"
            )

        if self.settings.kill_switch_enabled:
            reasons.append("Kill switch is enabled")

        if order.stop_loss is None:
            reasons.append("A stop loss is required before submitting any order")
        else:
            risk_amount = estimate_risk_amount(
                entry_price=order.proposed_price,
                stop_loss=order.stop_loss,
                amount_usd=order.amount_usd,
                leverage=order.leverage,
            )
            risk_pct = (risk_amount / context.account_balance) * 100
            if risk_pct > self.settings.max_risk_per_trade_pct:
                reasons.append(
                    f"Estimated trade risk {risk_pct:.2f}% exceeds the {self.settings.max_risk_per_trade_pct:.2f}% cap"
                )

        if context.mode == "real" and not self.settings.enable_real_trading:
            reasons.append("Real trading is disabled by configuration")

        return RiskValidationResult(
            passed=not reasons,
            reasons=reasons,
            risk_amount_usd=round(risk_amount, 2),
            risk_pct_of_balance=round(risk_pct, 4),
        )
