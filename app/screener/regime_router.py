"""Regime router: toggle whole strategy families on/off by market regime.

The per-strategy regime *filters* already in the codebase gate individual
signals. This is the layer above them the roadmap calls the single biggest lever
for a steady daily number: a portfolio-level switch that decides which strategy
*families* are even allowed to run given the market regime.

The playbook it encodes is deliberately conventional:

* **Healthy uptrend** (broad participation, contained vol) -> run momentum /
  trend / breakout; let winners trend.
* **Uptrend but narrow or high-vol** -> stop chasing breakouts (they whipsaw);
  favor mean-reversion (buy dips) and intraday.
* **Choppy / neutral tape** -> mean-reversion and intraday; breakouts fail in
  range-bound markets.
* **Downtrend** -> defensive: only mean-reversion (oversold bounces), no new
  momentum/breakout longs.

Pure and dependency-light: the router reasons over a normalized ``RegimeSignal``
(trend / volatility / breadth), and ``regime_signal_from_scores`` translates the
MarketIntelligenceService output into that signal. Nothing here has side effects;
the selection layer applies ``route_specs`` only when ``regime_router_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Strategy families the router switches between.
FAMILY_MOMENTUM = "momentum"
FAMILY_MEAN_REVERSION = "mean_reversion"
FAMILY_INTRADAY = "intraday"

# Map each StrategySpec.style to a family. Unknown styles default to momentum
# (treated as trend/breakout-like: on in uptrends, off in downtrends).
STYLE_TO_FAMILY: dict[str, str] = {
    "trend": FAMILY_MOMENTUM,
    "momentum": FAMILY_MOMENTUM,
    "position": FAMILY_MOMENTUM,
    "swing": FAMILY_MOMENTUM,
    "breakout": FAMILY_MOMENTUM,
    "trend_breakout": FAMILY_MOMENTUM,
    "opening_range": FAMILY_MOMENTUM,
    "pullback_continuation": FAMILY_MOMENTUM,
    "rotation": FAMILY_MOMENTUM,
    "confluence": FAMILY_MOMENTUM,
    "gap": FAMILY_MOMENTUM,
    "mean_reversion": FAMILY_MEAN_REVERSION,
    "reversal": FAMILY_MEAN_REVERSION,
    "intraday": FAMILY_INTRADAY,
    "scalp": FAMILY_INTRADAY,
}


def family_for_style(style: str) -> str:
    return STYLE_TO_FAMILY.get(str(style or "").strip().lower(), FAMILY_MOMENTUM)


@dataclass(frozen=True)
class RegimeSignal:
    """Normalized market regime the router reasons over."""

    trend: str = "neutral"  # "up" | "down" | "neutral"
    volatility: str = "normal"  # "low" | "normal" | "high"
    breadth: str = "neutral"  # "strong" | "weak" | "neutral"


def allowed_families(regime: RegimeSignal) -> set[str]:
    """Which strategy families may run in this regime (see module docstring)."""

    if regime.trend == "down":
        # Defensive: long-only momentum/breakout struggle in downtrends.
        return {FAMILY_MEAN_REVERSION}
    if regime.trend == "up":
        if regime.volatility == "high" or regime.breadth == "weak":
            # Narrow or high-vol advance: fade dips, don't chase breakouts.
            return {FAMILY_MEAN_REVERSION, FAMILY_INTRADAY}
        return {FAMILY_MOMENTUM, FAMILY_INTRADAY}
    # Neutral / choppy tape.
    return {FAMILY_MEAN_REVERSION, FAMILY_INTRADAY}


def route_specs(specs: Iterable[Any], regime: RegimeSignal) -> list[Any]:
    """Filter strategy specs to the families allowed in ``regime``."""

    families = allowed_families(regime)
    return [spec for spec in specs if family_for_style(getattr(spec, "style", "")) in families]


def regime_signal_from_scores(
    *,
    trend_score: float | None,
    breadth_score: float | None,
    volatility_environment: str | None,
    trend_up: float = 0.6,
    trend_down: float = 0.4,
    breadth_strong: float = 0.6,
    breadth_weak: float = 0.4,
) -> RegimeSignal:
    """Translate MarketIntelligenceService outputs into a normalized RegimeSignal.

    ``trend_score`` / ``breadth_score`` are the service's 0..1 scores;
    ``volatility_environment`` is its label ("compressed" / "healthy" /
    "elevated" / "volatile" / "unknown"). Missing inputs fall back to neutral.
    """

    trend = "neutral"
    if trend_score is not None:
        if trend_score >= trend_up:
            trend = "up"
        elif trend_score <= trend_down:
            trend = "down"

    breadth = "neutral"
    if breadth_score is not None:
        if breadth_score >= breadth_strong:
            breadth = "strong"
        elif breadth_score <= breadth_weak:
            breadth = "weak"

    volatility = {
        "compressed": "low",
        "healthy": "normal",
        "elevated": "high",
        "volatile": "high",
    }.get(str(volatility_environment or "").strip().lower(), "normal")

    return RegimeSignal(trend=trend, volatility=volatility, breadth=breadth)
