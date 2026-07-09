"""Strategy enhancement diagnostics and paper-only tuning recommendations."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.screener.profiles import (
    effective_auto_execution_min_score,
    paper_exploration_profile_enabled,
)
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
        diagnostics = [self._near_miss_diagnostics(row) for row in rows]
        return {
            "rows_analyzed": len(rows),
            "status_counts": dict(Counter(row.status for row in rows)),
            "top_reasons": self._top_reasons(rows),
            "near_miss_promotable_count": sum(1 for item in diagnostics if item["promotable"]),
            "near_miss_top_blocked_reasons": self._near_miss_blockers(diagnostics),
            "top_strategy_timeframes": self._top_strategy_timeframes(rows),
            "top_symbols": dict(Counter(row.symbol for row in rows).most_common(20)),
            "examples": [self._example(row, diagnostic) for row, diagnostic in zip(rows[:25], diagnostics[:25], strict=False)],
            "near_miss_promotion_examples": [
                self._example(row, diagnostic)
                for row, diagnostic in zip(rows, diagnostics, strict=False)
                if diagnostic["attempted"]
            ][:25],
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
                "screener_near_miss_min_relative_volume": self.settings.paper_exploration_near_miss_min_relative_volume,
                "screener_min_reward_to_risk": self.settings.paper_exploration_min_reward_to_risk,
                "screener_min_indicator_confluence": self.settings.paper_exploration_min_indicator_confluence,
                "auto_execution_min_score": effective_auto_execution_min_score(self.settings),
                "paper_near_miss_promotion_enabled": self.settings.paper_near_miss_promotion_enabled,
                "paper_near_miss_max_score_gap": self.settings.paper_near_miss_max_score_gap,
                "paper_near_miss_allowed_reasons": self.settings.paper_near_miss_allowed_reasons,
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
    def _example(row: Any, diagnostic: dict[str, Any] | None = None) -> dict[str, Any]:
        diagnostic = diagnostic or {}
        return {
            "created_at": row.created_at,
            "symbol": row.symbol,
            "strategy_name": row.strategy_name,
            "timeframe": row.timeframe,
            "status": row.status,
            "final_score": row.final_score,
            "rejection_reasons": row.rejection_reasons,
            "promoted_to_candidate": diagnostic.get("promoted_to_candidate", False),
            "near_miss_promotable": diagnostic.get("promotable", False),
            "promotion_blockers": diagnostic.get("promotion_blockers", []),
        }

    def _is_promotable_near_miss(self, row: Any) -> bool:
        return bool(self._near_miss_diagnostics(row)["promotable"])

    def _near_miss_diagnostics(self, row: Any) -> dict[str, Any]:
        payload = dict(getattr(row, "payload", {}) or {})
        metadata = dict(payload.get("metadata") or {})
        if metadata.get("signal_classification") == "paper_near_miss":
            blockers = [
                str(item)
                for item in (metadata.get("paper_near_miss_promotion_blockers") or [])
                if str(item).strip()
            ]
            return {
                "attempted": True,
                "promoted_to_candidate": bool(metadata.get("paper_near_miss_promoted_to_candidate", True)),
                "promotable": not blockers,
                "promotion_blockers": blockers,
            }

        blockers: list[str] = []
        if not bool(getattr(self.settings, "paper_near_miss_promotion_enabled", False)):
            blockers.append("paper_near_miss_disabled")
        if not paper_exploration_profile_enabled(self.settings):
            blockers.append("paper_exploration_profile_inactive")

        reasons = {
            str(item).strip().lower()
            for item in (getattr(row, "rejection_reasons", []) or getattr(row, "reason_codes", []) or [])
            if str(item).strip()
        }
        allowed = {
            str(item).strip().lower()
            for item in (getattr(self.settings, "paper_near_miss_allowed_reasons", []) or [])
            if str(item).strip()
        }
        if not reasons:
            blockers.append("missing_rejection_reasons")
        unsupported = reasons - allowed
        blockers.extend(f"unsupported_reason:{reason}" for reason in sorted(unsupported))

        measurements = dict(payload.get("measurements") or {})
        indicators = dict(payload.get("indicators") or {})
        relative_volume = self._safe_float(measurements.get("relative_volume"), indicators.get("relative_volume"))
        if relative_volume is None:
            blockers.append("relative_volume_unavailable")
        elif relative_volume < float(getattr(self.settings, "paper_exploration_near_miss_min_relative_volume", 0.75)):
            blockers.append("paper_near_miss_relative_volume_too_low")

        score = float(getattr(row, "final_score", None) or 0.0)
        minimum = effective_auto_execution_min_score(self.settings) - float(
            getattr(self.settings, "paper_near_miss_max_score_gap", 5.0) or 0.0
        )
        if score < minimum:
            blockers.append("paper_near_miss_score_gap_too_large")

        entry = self._safe_float(payload.get("entry_price"), payload.get("current_price"))
        stop = self._safe_float(payload.get("stop_loss"))
        target = self._safe_float(payload.get("take_profit"))
        if entry is None or stop is None or target is None:
            blockers.append("paper_near_miss_bracket_missing")
        elif not (stop < entry < target):
            blockers.append("paper_near_miss_invalid_bracket")

        spread_bps = self._safe_float(measurements.get("spread_bps"), indicators.get("spread_bps"))
        if spread_bps is None:
            blockers.append("paper_near_miss_spread_unavailable")
        elif spread_bps > float(getattr(self.settings, "screener_max_spread_bps", 50.0)):
            blockers.append("paper_near_miss_spread_too_wide")

        risk_reward = self._safe_float(payload.get("risk_reward_ratio"), metadata.get("risk_reward_ratio"))
        if risk_reward is None and entry is not None and stop is not None and target is not None and entry > stop:
            risk_reward = (target - entry) / (entry - stop)
        if risk_reward is None or risk_reward < float(getattr(self.settings, "paper_exploration_min_reward_to_risk", 1.20)):
            blockers.append("paper_near_miss_reward_to_risk_too_low")

        verified = bool(metadata.get("market_data_verified") or measurements.get("verified") or payload.get("market_data_verified"))
        data_blocked = bool(metadata.get("data_gate_blocked"))
        if not verified or data_blocked:
            blockers.append("market_data_unverified")

        if str(metadata.get("signal_role") or payload.get("signal_role") or "entry_long").lower() == "entry_short":
            blockers.append("paper_near_miss_short_blocked")
        if str(payload.get("direction_label") or "buy").lower() not in {"buy", "long"}:
            blockers.append("paper_near_miss_long_only")

        unique_blockers = list(dict.fromkeys(blockers))
        return {
            "attempted": bool(reasons or metadata),
            "promoted_to_candidate": False,
            "promotable": not unique_blockers,
            "promotion_blockers": unique_blockers,
        }

    def _near_miss_blockers(self, diagnostics: list[dict[str, Any]]) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for diagnostic in diagnostics:
            if diagnostic["promotable"]:
                continue
            counter.update(diagnostic["promotion_blockers"])
        return dict(counter.most_common(20))

    @staticmethod
    def _safe_float(*values: Any) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _specs_by_timeframe() -> dict[str, int]:
        counts: Counter[str] = Counter(spec.timeframe for spec in STRATEGY_SPECS)
        return dict(sorted(counts.items()))
