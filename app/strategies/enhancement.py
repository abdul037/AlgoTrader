"""Strategy enhancement diagnostics and paper-only tuning recommendations."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.screener.profiles import effective_auto_execution_min_score, paper_exploration_profile_enabled
from app.strategies import CORE_STRATEGY_NAMES, ENHANCED_RESEARCH_STRATEGY_NAMES, STRATEGY_SPECS


class StrategyEnhancementService:
    """Summarize strategy near-misses and recommend paper-only tuning."""

    def __init__(self, *, settings: Any, scan_decisions: Any, strategy_governance: Any):
        self.settings = settings
        self.scan_decisions = scan_decisions
        self.strategy_governance = strategy_governance

    def status(self) -> dict[str, Any]:
        approved = []
        if self.strategy_governance is not None:
            approved = list(self.strategy_governance.approved_paper_exploration_strategies())
        return {
            "paper_exploration_enabled": bool(getattr(self.settings, "paper_scanner_exploration_enabled", False)),
            "profile": str(getattr(self.settings, "paper_exploration_signal_profile", "off")),
            "profile_active": paper_exploration_profile_enabled(self.settings),
            "thresholds": self._thresholds(),
            "strategy_families": {
                "core": len(CORE_STRATEGY_NAMES),
                "enhanced_research": len(ENHANCED_RESEARCH_STRATEGY_NAMES),
                "total": len(CORE_STRATEGY_NAMES) + len(ENHANCED_RESEARCH_STRATEGY_NAMES),
            },
            "strategy_specs": {
                "total": len(STRATEGY_SPECS),
                "by_timeframe": self._specs_by_timeframe(),
            },
            "paper_approved_strategy_count": len(approved),
            "paper_approved_strategies": sorted(approved),
        }

    def near_misses(self, *, limit: int = 500) -> dict[str, Any]:
        rows = self._recent_rows(limit=limit)
        return {
            "rows_analyzed": len(rows),
            "status_counts": dict(Counter(row.status for row in rows)),
            "top_reasons": self._top_reasons(rows),
            "top_strategy_timeframes": self._top_strategy_timeframes(rows),
            "top_symbols": dict(Counter(row.symbol for row in rows).most_common(20)),
            "examples": [self._example(row) for row in rows[:25]],
        }

    def run_paper_tuning(self, *, limit: int = 1000) -> dict[str, Any]:
        rows = self._recent_rows(limit=limit)
        reason_counts = Counter()
        for row in rows:
            reason_counts.update(row.rejection_reasons or row.reason_codes or [])
        recommendations = []
        if reason_counts["relative_volume_too_low"] >= 5:
            current = float(getattr(self.settings, "paper_exploration_min_relative_volume", 0.90))
            recommendations.append(
                {
                    "setting": "PAPER_EXPLORATION_MIN_RELATIVE_VOLUME",
                    "current": current,
                    "recommended": max(0.80, round(current - 0.05, 2)),
                    "reason": "Repeated near-misses are failing relative-volume confirmation.",
                    "scope": "paper_exploration_only",
                }
            )
        if reason_counts["indicator_confluence_too_low"] + reason_counts["confluence_score_too_low"] >= 5:
            current = float(getattr(self.settings, "paper_exploration_min_indicator_confluence", 0.35))
            recommendations.append(
                {
                    "setting": "PAPER_EXPLORATION_MIN_INDICATOR_CONFLUENCE",
                    "current": current,
                    "recommended": max(0.30, round(current - 0.03, 2)),
                    "reason": "Many setups have partial indicator confirmation but miss the confluence floor.",
                    "scope": "paper_exploration_only",
                }
            )
        if reason_counts["reward_to_risk_too_low"] >= 3:
            current = float(getattr(self.settings, "paper_exploration_min_reward_to_risk", 1.20))
            recommendations.append(
                {
                    "setting": "PAPER_EXPLORATION_MIN_REWARD_TO_RISK",
                    "current": current,
                    "recommended": max(1.10, round(current - 0.05, 2)),
                    "reason": "Some otherwise valid paper setups fail only the reward/risk floor.",
                    "scope": "paper_exploration_only",
                }
            )
        if reason_counts["final_score_below_keep_threshold"] >= 3:
            current = float(getattr(self.settings, "paper_exploration_min_final_score_to_keep", 50.0))
            recommendations.append(
                {
                    "setting": "PAPER_EXPLORATION_MIN_FINAL_SCORE_TO_KEEP",
                    "current": current,
                    "recommended": max(45.0, round(current - 2.0, 2)),
                    "reason": "Near-miss scores are clustering below the keep threshold.",
                    "scope": "paper_exploration_only",
                }
            )
        return {
            "dry_run": True,
            "mutated": False,
            "rows_analyzed": len(rows),
            "reason_counts": dict(reason_counts.most_common(20)),
            "recommendations": recommendations,
            "blocked_changes": [
                "broker",
                "order_size",
                "risk_limits",
                "stop_loss",
                "take_profit",
                "live_trading",
                "kill_switch",
            ],
        }

    def _recent_rows(self, *, limit: int) -> list[Any]:
        if self.scan_decisions is None:
            return []
        return list(self.scan_decisions.list(limit=max(1, min(limit, 5000))))

    def _thresholds(self) -> dict[str, Any]:
        return {
            "base": {
                "screener_min_final_score_to_alert": self.settings.screener_min_final_score_to_alert,
                "screener_min_final_score_to_keep": self.settings.screener_min_final_score_to_keep,
                "screener_min_relative_volume": self.settings.screener_min_relative_volume,
                "screener_min_reward_to_risk": self.settings.screener_min_reward_to_risk,
                "screener_min_indicator_confluence": self.settings.screener_min_indicator_confluence,
                "auto_execution_min_score": self.settings.auto_execution_min_score,
            },
            "paper_exploration": {
                "screener_min_final_score_to_alert": self.settings.paper_exploration_min_final_score_to_alert,
                "screener_min_final_score_to_keep": self.settings.paper_exploration_min_final_score_to_keep,
                "screener_min_relative_volume": self.settings.paper_exploration_min_relative_volume,
                "screener_min_reward_to_risk": self.settings.paper_exploration_min_reward_to_risk,
                "screener_min_indicator_confluence": self.settings.paper_exploration_min_indicator_confluence,
                "auto_execution_min_score": effective_auto_execution_min_score(self.settings),
            },
        }

    @staticmethod
    def _top_reasons(rows: list[Any]) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for row in rows:
            counter.update(row.rejection_reasons or row.reason_codes or [])
        return dict(counter.most_common(25))

    @staticmethod
    def _top_strategy_timeframes(rows: list[Any]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        for row in rows:
            grouped[(row.strategy_name, row.timeframe)][row.status] += 1
        ranked = sorted(grouped.items(), key=lambda item: sum(item[1].values()), reverse=True)
        return [
            {
                "strategy_name": strategy,
                "timeframe": timeframe,
                "count": sum(counts.values()),
                "status_counts": dict(counts),
            }
            for (strategy, timeframe), counts in ranked[:25]
        ]

    @staticmethod
    def _example(row: Any) -> dict[str, Any]:
        return {
            "created_at": row.created_at,
            "symbol": row.symbol,
            "strategy_name": row.strategy_name,
            "timeframe": row.timeframe,
            "status": row.status,
            "final_score": row.final_score,
            "rejection_reasons": row.rejection_reasons,
        }

    @staticmethod
    def _specs_by_timeframe() -> dict[str, int]:
        counts: Counter[str] = Counter(spec.timeframe for spec in STRATEGY_SPECS)
        return dict(sorted(counts.items()))
