"""Conservative portfolio allocation across strategies and sectors."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field


class AllocationCandidate(BaseModel):
    symbol: str
    strategy_name: str
    sector: str = "unknown"
    score: float = 0.0
    annualized_volatility_pct: float = Field(gt=0.0)
    correlation_bucket: str = "default"
    requested_amount_usd: float = Field(gt=0.0)


class AllocationDecision(BaseModel):
    symbol: str
    strategy_name: str
    amount_usd: float
    weight_pct: float
    blockers: list[str] = Field(default_factory=list)


def allocate_candidates(
    candidates: list[AllocationCandidate],
    *,
    equity_usd: float,
    gross_exposure_limit_pct: float,
    symbol_limit_pct: float,
    sector_limit_pct: float,
    correlation_limit_pct: float,
    per_trade_cap_usd: float,
) -> list[AllocationDecision]:
    """Rank by score and inverse volatility while enforcing exposure limits."""

    if equity_usd <= 0:
        raise ValueError("equity_usd must be positive")
    gross_limit = equity_usd * gross_exposure_limit_pct / 100.0
    symbol_limit = equity_usd * symbol_limit_pct / 100.0
    sector_limit = equity_usd * sector_limit_pct / 100.0
    correlation_limit = equity_usd * correlation_limit_pct / 100.0
    gross_used = 0.0
    sector_used: dict[str, float] = defaultdict(float)
    correlation_used: dict[str, float] = defaultdict(float)
    decisions: list[AllocationDecision] = []
    ranked = sorted(
        candidates,
        key=lambda item: (item.score, 1.0 / item.annualized_volatility_pct),
        reverse=True,
    )
    for candidate in ranked:
        amount = min(candidate.requested_amount_usd, per_trade_cap_usd, symbol_limit)
        blockers: list[str] = []
        if gross_used + amount > gross_limit:
            blockers.append("gross_exposure_limit")
        if sector_used[candidate.sector] + amount > sector_limit:
            blockers.append("sector_exposure_limit")
        if correlation_used[candidate.correlation_bucket] + amount > correlation_limit:
            blockers.append("correlated_exposure_limit")
        accepted = 0.0 if blockers else amount
        if accepted:
            gross_used += accepted
            sector_used[candidate.sector] += accepted
            correlation_used[candidate.correlation_bucket] += accepted
        decisions.append(
            AllocationDecision(
                symbol=candidate.symbol.upper(),
                strategy_name=candidate.strategy_name,
                amount_usd=round(accepted, 2),
                weight_pct=round((accepted / equity_usd) * 100.0, 4),
                blockers=blockers,
            )
        )
    return decisions
