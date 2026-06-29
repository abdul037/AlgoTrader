"""Paper-exploration threshold profiles for screener evaluation."""

from __future__ import annotations

from typing import Any


PAPER_EXPLORATION_BALANCED_LOOSE_OVERRIDES = {
    "screener_min_final_score_to_alert": "paper_exploration_min_final_score_to_alert",
    "screener_min_final_score_to_keep": "paper_exploration_min_final_score_to_keep",
    "screener_min_relative_volume": "paper_exploration_min_relative_volume",
    "screener_min_reward_to_risk": "paper_exploration_min_reward_to_risk",
    "screener_min_indicator_confluence": "paper_exploration_min_indicator_confluence",
}


def paper_exploration_profile_enabled(settings: Any) -> bool:
    """Return whether the paper-only loose screener profile should apply."""

    return (
        bool(getattr(settings, "paper_scanner_exploration_enabled", False))
        and str(getattr(settings, "execution_mode", "paper")) == "paper"
        and not bool(getattr(settings, "enable_real_trading", False))
        and str(getattr(settings, "paper_exploration_signal_profile", "off")) == "balanced_loose"
    )


def effective_auto_execution_min_score(settings: Any) -> float:
    """Return the paper-aware auto-execution score floor."""

    if paper_exploration_profile_enabled(settings):
        return float(getattr(settings, "paper_exploration_auto_execution_min_score", 60.0))
    return float(getattr(settings, "auto_execution_min_score", 65.0))


class EffectiveScreenerSettings:
    """Proxy settings object that applies paper-only screener threshold overrides."""

    def __init__(self, base: Any):
        self._base = base

    def __getattr__(self, name: str) -> Any:
        if paper_exploration_profile_enabled(self._base):
            mapped = PAPER_EXPLORATION_BALANCED_LOOSE_OVERRIDES.get(name)
            if mapped is not None:
                return getattr(self._base, mapped)
        return getattr(self._base, name)


def effective_screener_settings(settings: Any) -> Any:
    """Build a settings proxy for paper-only screener thresholds."""

    return EffectiveScreenerSettings(settings)
