"""Portfolio-level institutional risk evaluation."""

from __future__ import annotations

from typing import Any

from app.models.institutional import PortfolioRiskSnapshot


class PortfolioRiskEvaluator:
    """Evaluate portfolio drawdown and concentration against configured limits."""

    def __init__(self, settings: Any):
        self.settings = settings

    def evaluate(self, snapshot: PortfolioRiskSnapshot) -> PortfolioRiskSnapshot:
        calculated_drawdown = max(
            (snapshot.peak_equity_usd - snapshot.equity_usd)
            / snapshot.peak_equity_usd
            * 100.0,
            0.0,
        )
        drawdown_pct = max(snapshot.drawdown_pct, calculated_drawdown)
        blockers: list[str] = []
        status = "ok"
        if drawdown_pct >= self.settings.portfolio_hard_drawdown_pct:
            blockers.append("portfolio_hard_drawdown_limit")
            status = "kill_switch"
        elif drawdown_pct >= self.settings.portfolio_soft_drawdown_pct:
            blockers.append("portfolio_soft_drawdown_pause")
            status = "pause"
        if snapshot.gross_exposure_pct > self.settings.portfolio_max_gross_exposure_pct:
            blockers.append("gross_exposure_limit")
        if snapshot.largest_symbol_exposure_pct > self.settings.portfolio_max_symbol_exposure_pct:
            blockers.append("symbol_exposure_limit")
        if snapshot.largest_sector_exposure_pct > self.settings.portfolio_max_sector_exposure_pct:
            blockers.append("sector_exposure_limit")
        if (
            snapshot.largest_correlated_exposure_pct
            > self.settings.portfolio_max_correlated_exposure_pct
        ):
            blockers.append("correlated_exposure_limit")
        if snapshot.open_positions > self.settings.max_open_positions:
            blockers.append("open_position_limit")
        if blockers and status == "ok":
            status = "blocked"
        return snapshot.model_copy(
            update={"drawdown_pct": drawdown_pct, "status": status, "blockers": blockers}
        )
