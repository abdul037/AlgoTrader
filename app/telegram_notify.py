"""Telegram notification integration."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from app.runtime_settings import AppSettings

if TYPE_CHECKING:
    from app.live_signal_schema import LiveSignalSnapshot, SignalScanResponse
    from app.models.screener import ScreenerRunResponse
    from app.models.workflow import AlertHistoryRecord, TrackedSignalRecord

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send signal updates to Telegram and manage Telegram webhooks."""

    def __init__(self, settings: AppSettings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.telegram_enabled
            and self.settings.telegram_bot_token
            and self.settings.telegram_chat_id
        )

    def send_signal_change(
        self,
        snapshot: "LiveSignalSnapshot",
        *,
        previous_state: str | None = None,
        chat_id: str | None = None,
    ) -> bool:
        message = self.format_signal_message(snapshot, previous_state=previous_state)
        return self.send_text(message, chat_id=chat_id)

    def send_text(self, message: str, *, chat_id: str | None = None) -> bool:
        if not self.enabled:
            return False
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        try:
            result = self._call(
                self._bot_url("sendMessage"),
                method="POST",
                json_payload=payload,
                timeout_seconds=15,
            )
        except RuntimeError as exc:
            logger.exception("Telegram notification failed: %s", exc)
            return False
        return bool(result.get("ok", False))

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        params: dict[str, Any] = {
            "timeout": max(timeout, 0),
            "limit": max(1, min(limit, 100)),
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            result = self._call(
                self._bot_url("getUpdates"),
                method="GET",
                params=params,
                timeout_seconds=max(timeout + 10, 15),
            )
        except RuntimeError as exc:
            logger.exception("Telegram getUpdates failed: %s", exc)
            return []
        return list(result.get("result", []))

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        if not self.enabled:
            return False
        try:
            result = self._call(
                self._bot_url("deleteWebhook"),
                method="POST",
                json_payload={"drop_pending_updates": drop_pending_updates},
                timeout_seconds=10,
            )
        except RuntimeError as exc:
            logger.exception("Telegram deleteWebhook failed: %s", exc)
            return False
        return bool(result.get("ok", False))

    def set_webhook(
        self,
        webhook_url: str,
        *,
        secret_token: str | None = None,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")
        payload: dict[str, Any] = {
            "url": webhook_url,
            "drop_pending_updates": drop_pending_updates,
            "allowed_updates": ["message"],
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return self._call(
            self._bot_url("setWebhook"),
            method="POST",
            json_payload=payload,
            timeout_seconds=15,
        )

    def get_webhook_info(self) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")
        return self._call(self._bot_url("getWebhookInfo"), method="GET", timeout_seconds=10)

    def ensure_webhook(
        self,
        webhook_url: str,
        *,
        secret_token: str | None = None,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        """Register the webhook only when Telegram is not already pointing at the target URL."""

        if not self.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")

        info = self.get_webhook_info()
        current_url = str((info.get("result") or {}).get("url") or "")
        if current_url == webhook_url:
            return {
                "ok": True,
                "description": "Webhook already configured.",
                "result": info.get("result") or {},
            }
        return self.set_webhook(
            webhook_url,
            secret_token=secret_token,
            drop_pending_updates=drop_pending_updates,
        )

    def _bot_url(self, method_name: str) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method_name}"

    def _call(
        self,
        url: str,
        *,
        method: str,
        timeout_seconds: int,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(timeout_seconds, 1)),
            "--request",
            method.upper(),
        ]
        if params:
            url = f"{url}?{urlencode(params)}"
        if json_payload is not None:
            command.extend(
                [
                    "--header",
                    "Content-Type: application/json",
                    "--data",
                    json.dumps(json_payload),
                ]
            )
        command.append(url)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=max(timeout_seconds + 2, 3),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid Telegram response: {result.stdout[:200]}") from exc

    @staticmethod
    def _source_label(snapshot: "LiveSignalSnapshot") -> tuple[str, str]:
        metadata = getattr(snapshot, "metadata", {}) or {}
        source = str(metadata.get("data_source") or "unknown")
        verified = "yes" if bool(metadata.get("data_source_verified", False)) else "no"
        return source, verified

    @staticmethod
    def format_signal_message(snapshot: "LiveSignalSnapshot", previous_state: str | None = None) -> str:
        source, verified = TelegramNotifier._source_label(snapshot)
        metadata = getattr(snapshot, "metadata", {}) or {}
        trade_plan = dict(metadata.get("trade_plan") or {})
        verdict = str(metadata.get("verdict") or trade_plan.get("verdict") or snapshot.direction_label or snapshot.state.value).upper()
        timing_label = str(metadata.get("timing_label") or trade_plan.get("timing_label") or "n/a").replace("_", " ")
        market_context = str(metadata.get("market_context_summary") or "n/a")
        provider_status = TelegramNotifier._format_provider_status(snapshot)
        entry_zone = TelegramNotifier._format_entry_zone(trade_plan, snapshot)
        rr = snapshot.risk_reward_ratio if snapshot.risk_reward_ratio is not None else metadata.get("risk_reward_ratio")
        lines = [
            f"{snapshot.symbol} | {verdict}",
            (
                f"Setup: {snapshot.strategy_name} | {snapshot.timeframe} | "
                f"Score {snapshot.score:.1f}/100 | {snapshot.confidence_label or 'n/a'}"
            ),
            f"Market: {market_context}",
            f"Timing: {timing_label} | Source: {source} | Verified: {verified}",
            provider_status,
        ]
        outcome_line = TelegramNotifier._format_ledger_outcome_line(metadata)
        if outcome_line:
            lines.append(outcome_line)
        if verdict == "NO_TRADE":
            if bool(metadata.get("data_gate_blocked")):
                lines.append("Gate: blocked by live market-data verification")
                lines.append(
                    f"Reason: {metadata.get('data_source_verification_reason') or 'market_data_unverified'}"
                )
            near_miss = TelegramNotifier._format_near_miss_setup(snapshot)
            if near_miss:
                lines.append(near_miss)
            strategy_checks = TelegramNotifier._format_strategy_checks(snapshot)
            if strategy_checks:
                lines.append(strategy_checks)
            blockers = TelegramNotifier._format_blocker_summary(snapshot)
            if blockers:
                lines.append(blockers)
            lines.extend(
                [
                    f"Why not now: {snapshot.rationale}",
                    f"Wait for: {trade_plan.get('confirmation_trigger') or 'a cleaner aligned setup'}",
                    f"Indicators: {TelegramNotifier._format_indicator_summary(snapshot)}",
                    f"Backtest: {TelegramNotifier._format_backtest_snapshot(snapshot)}",
                    "Execution: no trade. Manual approval remains required for any future broker action.",
                ]
            )
            return "\n".join(lines)

        lines.extend(
            [
                f"Entry zone: {entry_zone}",
                f"Trigger: {trade_plan.get('confirmation_trigger') or 'n/a'}",
                (
                    f"Stop: {TelegramNotifier._fmt_number(snapshot.stop_loss)} | "
                    f"Targets: {TelegramNotifier._format_targets(snapshot)} | "
                    f"RR: {TelegramNotifier._fmt_number(rr)}"
                ),
                f"Indicators: {TelegramNotifier._format_indicator_summary(snapshot)}",
                (
                    f"Plan: {trade_plan.get('preferred_entry_method', 'n/a').replace('_', ' ')} | "
                    f"Hold: {trade_plan.get('hold_style', 'n/a')} | "
                    f"Quality: {trade_plan.get('position_quality_label', 'n/a')}"
                ),
                f"Backtest: {TelegramNotifier._format_backtest_snapshot(snapshot)}",
                f"Rationale: {snapshot.rationale}",
                f"Invalidation: {trade_plan.get('invalidation_condition') or 'n/a'}",
            ]
        )
        if snapshot.pass_reasons:
            lines.append(f"Why this passed: {', '.join(snapshot.pass_reasons[:5])}")
        lines.append("Execution: manual approval required before any broker action.")
        return "\n".join(lines)

    @staticmethod
    def format_price_message(snapshot: "LiveSignalSnapshot") -> str:
        source, verified = TelegramNotifier._source_label(snapshot)
        metadata = getattr(snapshot, "metadata", {}) or {}
        lines = [
            f"Price snapshot for {snapshot.symbol}",
            f"Source: {source}",
            f"Verified: {verified}",
            f"Current: {snapshot.current_price if snapshot.current_price is not None else 'n/a'}",
            f"Bid: {snapshot.current_bid if snapshot.current_bid is not None else 'n/a'}",
            f"Ask: {snapshot.current_ask if snapshot.current_ask is not None else 'n/a'}",
            f"Signal state: {snapshot.state.value}",
            f"Entry watch: {snapshot.entry_price if snapshot.entry_price is not None else 'n/a'}",
            f"Exit watch: {snapshot.exit_price if snapshot.exit_price is not None else 'n/a'}",
            f"Stop: {snapshot.stop_loss if snapshot.stop_loss is not None else 'n/a'}",
            f"Target: {snapshot.take_profit if snapshot.take_profit is not None else 'n/a'}",
        ]
        if "backtest_validated" in metadata:
            lines.append(f"Backtest validated: {'yes' if bool(metadata.get('backtest_validated')) else 'no'}")
            lines.append(f"Backtest reason: {metadata.get('backtest_validation_reason') or 'n/a'}")
        return "\n".join(lines)

    @staticmethod
    def format_scan_message(response: "SignalScanResponse") -> str:
        if not response.candidates:
            return "No scan candidates were returned."

        lines = [f"Top {len(response.candidates)} live setups"]
        for item in response.candidates:
            source, verified = TelegramNotifier._source_label(item)
            lines.append(
                f"{item.symbol}: {item.state.value} | "
                f"price {item.current_price if item.current_price is not None else 'n/a'} | "
                f"entry {item.entry_price if item.entry_price is not None else 'n/a'} | "
                f"score {item.score:.2f} | "
                f"source {source} | "
                f"verified {verified}"
            )
        if response.errors:
            lines.append(f"Errors: {len(response.errors)}")
        return "\n".join(lines)

    @staticmethod
    def format_screener_candidate(snapshot: "LiveSignalSnapshot", *, rank: int | None = None) -> str:
        """Format a premium single-candidate screener alert."""

        direction = str(snapshot.direction_label or snapshot.state.value).upper()
        metadata = getattr(snapshot, "metadata", {}) or {}
        trade_plan = dict(metadata.get("trade_plan") or {})
        header = f"#{rank or snapshot.rank or '-'} {snapshot.symbol} | {direction}"
        setup_line = (
            f"Setup: {snapshot.strategy_name} | {snapshot.timeframe} | "
            f"Score {snapshot.score:.1f}/100 | {snapshot.confidence_label or 'n/a'}"
        )
        outcome_line = TelegramNotifier._format_ledger_outcome_line(metadata)
        freshness = f"Freshness: {snapshot.freshness or 'fresh'} | Verdict: {trade_plan.get('timing_label', 'n/a')}"
        entry = TelegramNotifier._format_entry_zone(trade_plan, snapshot)
        stop = TelegramNotifier._fmt_number(snapshot.stop_loss)
        rr = TelegramNotifier._fmt_number(snapshot.risk_reward_ratio)
        targets = TelegramNotifier._format_targets(snapshot)
        backtest = TelegramNotifier._format_backtest_snapshot(snapshot)
        reasons = TelegramNotifier._format_reason_summary(snapshot)
        timestamp = snapshot.generated_at or snapshot.signal_generated_at or "n/a"
        provider_status = TelegramNotifier._format_provider_status(snapshot)
        return "\n".join(
            [
                header,
                setup_line,
                *([outcome_line] if outcome_line else []),
                f"Context: {metadata.get('market_context_summary') or 'n/a'}",
                freshness,
                provider_status,
                f"Entry: {entry} | Stop: {stop} | Targets: {targets}",
                f"RR: {rr} | Price: {snapshot.current_price if snapshot.current_price is not None else 'n/a'} | Time: {timestamp}",
                f"Indicators: {TelegramNotifier._format_indicator_summary(snapshot)}",
                f"Backtest: {backtest}",
                f"Why: {reasons}",
                "Execution: manual approval required before any broker action.",
            ]
        )

    @staticmethod
    def _format_ledger_outcome_line(metadata: dict[str, Any]) -> str | None:
        outcome_id = metadata.get("ledger_outcome_id")
        if outcome_id in (None, ""):
            return None
        return f"Ledger outcome: #{outcome_id}"

    @staticmethod
    def format_screener_summary(response: "ScreenerRunResponse", *, task_label: str | None = None) -> str:
        """Format a ranked screener summary for Telegram."""

        if not response.candidates:
            lines = [
                "US market screener",
                f"Run: {(task_label or 'manual_scan').replace('_', ' ')}",
                f"Universe: {response.universe_name}",
                f"Timeframes: {', '.join(response.timeframes)}",
                (
                    f"Scanned: {response.evaluated_symbols} symbols | "
                    f"Strategy checks: {response.evaluated_strategy_runs} | "
                    f"Suppressed: {response.suppressed}"
                ),
                "No actionable candidates passed the current filters.",
            ]
            diagnostics = TelegramNotifier._format_scan_diagnostics(response)
            if diagnostics:
                lines.extend(diagnostics)
            if response.errors:
                first_error = str(response.errors[0]).replace("\n", " ")
                if len(first_error) > 180:
                    first_error = f"{first_error[:180]}..."
                lines.append(f"First issue: {first_error}")
                lines.append(f"Errors: {len(response.errors)}")
            lines.append(
                "Meaning: no trade proposal was created; manual approval is still required for any future order."
            )
            return "\n".join(lines)

        lines = [
            "US market screener",
            f"Run: {(task_label or 'manual_scan').replace('_', ' ')}",
            f"Universe: {response.universe_name}",
            f"Timeframes: {', '.join(response.timeframes)}",
            (
                f"Scanned: {response.evaluated_symbols} symbols | "
                f"Strategy runs: {response.evaluated_strategy_runs} | "
                f"Passed: {len(response.candidates)} | Suppressed: {response.suppressed}"
            ),
        ]
        for item in response.candidates:
            lines.append("")
            lines.append(TelegramNotifier.format_screener_candidate(item, rank=item.rank))
        diagnostics = TelegramNotifier._format_scan_diagnostics(response)
        if diagnostics:
            lines.append("")
            lines.extend(diagnostics)
        if response.errors:
            lines.append(f"Errors: {len(response.errors)}")
        return "\n".join(lines)

    @staticmethod
    def _format_scan_diagnostics(response: "ScreenerRunResponse") -> list[str]:
        summary = dict(getattr(response, "rejection_summary", {}) or {})
        closest = list(getattr(response, "closest_rejections", []) or [])
        if not summary and not closest:
            return []
        lines = ["Diagnostics:"]
        if summary:
            top_reasons = sorted(summary.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
            lines.append(
                "Top blockers: "
                + ", ".join(f"{TelegramNotifier._humanize_reason(reason)} ({count})" for reason, count in top_reasons)
            )
        if closest:
            lines.append("Closest rejected:")
            for item in closest[:3]:
                score = item.get("score")
                score_text = f"{float(score):.1f}" if score is not None else "n/a"
                reasons = ", ".join(
                    TelegramNotifier._humanize_reason(reason)
                    for reason in list(item.get("rejection_reasons") or [])[:3]
                )
                lines.append(
                    f"- {item.get('symbol')} {item.get('timeframe')} "
                    f"{item.get('strategy_name')} | score {score_text} | {reasons or 'rejected'}"
                )
        return lines

    @staticmethod
    def _humanize_reason(reason: str) -> str:
        return str(reason or "unknown").replace("_", " ")

    @staticmethod
    def format_tracked_signal_update(record: "TrackedSignalRecord", *, event_type: str) -> str:
        """Format a tracked-signal lifecycle update."""

        snapshot = record.snapshot
        direction = "LONG" if (snapshot.signal_role or snapshot.metadata.get("signal_role")) != "entry_short" else "SHORT"
        event_label = event_type.replace("_", " ").upper()
        return "\n".join(
            [
                f"Tracked signal update: {record.symbol}",
                f"Event: {event_label}",
                f"Strategy: {record.strategy_name}",
                f"Timeframe: {record.timeframe}",
                f"Direction: {direction}",
                f"Entry: {record.entry_price if record.entry_price is not None else 'n/a'}",
                f"Last price: {record.last_price if record.last_price is not None else 'n/a'}",
                f"Stop: {record.stop_loss if record.stop_loss is not None else 'n/a'}",
                f"Target: {record.take_profit if record.take_profit is not None else 'n/a'}",
                f"Opened: {record.opened_at}",
                f"Updated: {record.updated_at}",
            ]
        )

    @staticmethod
    def format_daily_summary(
        *,
        open_signals: list["TrackedSignalRecord"],
        recent_alerts: list["AlertHistoryRecord"],
    ) -> str:
        """Format an end-of-day style summary."""

        lines = [
            "Daily workflow summary",
            f"Open signals: {len(open_signals)}",
            f"Recent alerts: {len(recent_alerts)}",
        ]
        if open_signals:
            lines.append("Open signal watchlist:")
            for item in open_signals[:10]:
                direction = str(item.snapshot.direction_label or item.snapshot.state.value).upper()
                lines.append(
                    f"{item.symbol} | {direction} | {item.strategy_name} | {item.timeframe} | "
                    f"entry {item.entry_price if item.entry_price is not None else 'n/a'} | "
                    f"last {item.last_price if item.last_price is not None else 'n/a'} | "
                    f"stop {item.stop_loss if item.stop_loss is not None else 'n/a'} | "
                    f"target {item.take_profit if item.take_profit is not None else 'n/a'}"
                )
        if recent_alerts:
            lines.append("Latest alerts:")
            for alert in recent_alerts[:5]:
                label = f"{alert.category}:{alert.status}"
                symbol = alert.symbol or "-"
                lines.append(f"{symbol} | {label} | {alert.created_at}")
        return "\n".join(lines)

    @staticmethod
    def _format_targets(snapshot: "LiveSignalSnapshot") -> str:
        targets = snapshot.targets or ([snapshot.take_profit] if snapshot.take_profit is not None else [])
        if not targets:
            return "n/a"
        return ", ".join(f"{float(target):.2f}" for target in targets)

    @staticmethod
    def _format_entry_zone(trade_plan: dict[str, Any], snapshot: "LiveSignalSnapshot") -> str:
        low = trade_plan.get("entry_zone_low")
        high = trade_plan.get("entry_zone_high")
        if low is None and high is None:
            return TelegramNotifier._fmt_number(snapshot.entry_price)
        if low is None:
            return TelegramNotifier._fmt_number(high)
        if high is None:
            return TelegramNotifier._fmt_number(low)
        return f"{float(low):.2f} - {float(high):.2f}"

    @staticmethod
    def _format_backtest_snapshot(snapshot: "LiveSignalSnapshot") -> str:
        backtest = getattr(snapshot, "backtest_snapshot", {}) or {}
        if not backtest:
            return "n/a"
        return (
            f"WR {float(backtest.get('win_rate', 0.0) or 0.0):.1f}% | "
            f"PF {float(backtest.get('profit_factor', 0.0) or 0.0):.2f} | "
            f"Trades {int(backtest.get('total_trades', 0) or 0)} | "
            f"Cred {float(backtest.get('credibility_score', 0.0) or 0.0):.2f} | "
            f"Exp {float(backtest.get('expectancy_pct', 0.0) or 0.0):.2f}% | "
            f"DD {float(backtest.get('max_drawdown_pct', 0.0) or 0.0):.1f}%"
        )

    @staticmethod
    def _format_reason_summary(snapshot: "LiveSignalSnapshot") -> str:
        reasons = list(snapshot.pass_reasons or [])
        if not reasons:
            return "n/a"
        return ", ".join(reasons[:5])

    @staticmethod
    def _format_indicator_summary(snapshot: "LiveSignalSnapshot") -> str:
        metadata = getattr(snapshot, "metadata", {}) or {}
        values = {
            "RSI": metadata.get("rsi_14"),
            "VWAP": metadata.get("vwap"),
            "EMA9": metadata.get("ema_9"),
            "EMA20": metadata.get("ema_20"),
            "RVOL": metadata.get("relative_volume"),
            "ADX": metadata.get("adx_14"),
        }
        parts = []
        for label, value in values.items():
            if value is None:
                continue
            parts.append(f"{label} {TelegramNotifier._fmt_number(value)}")
        confluence = metadata.get("indicator_confluence_score")
        if confluence is not None:
            parts.append(f"Confluence {float(confluence):.2f}")
        accuracy = metadata.get("accuracy_score")
        if accuracy is not None:
            parts.append(f"Accuracy {float(accuracy):.2f}")
        confirmation = metadata.get("confirmation_score")
        if confirmation is not None:
            parts.append(f"Confirm {float(confirmation):.2f}")
        false_positive_risk = metadata.get("false_positive_risk_score")
        if false_positive_risk is not None:
            parts.append(f"FP-risk {float(false_positive_risk):.2f}")
        execution_ready = metadata.get("execution_ready")
        if execution_ready is not None:
            parts.append(f"Exec {'ready' if execution_ready else 'blocked'}")
        return " | ".join(parts) if parts else "n/a"

    @staticmethod
    def _format_near_miss_setup(snapshot: "LiveSignalSnapshot") -> str | None:
        metadata = getattr(snapshot, "metadata", {}) or {}
        setup = metadata.get("near_miss_setup")
        if not isinstance(setup, dict) or not setup:
            return None
        strategy = setup.get("strategy_name") or "n/a"
        timeframe = setup.get("timeframe") or "n/a"
        score = setup.get("score")
        status = str(setup.get("status") or "rejected").replace("_", " ")
        return (
            f"Nearest setup: {strategy} | {timeframe} | "
            f"{status} | near-score {TelegramNotifier._fmt_number(score)}"
        )

    @staticmethod
    def _format_strategy_checks(snapshot: "LiveSignalSnapshot") -> str | None:
        metadata = getattr(snapshot, "metadata", {}) or {}
        evaluated = metadata.get("analysis_strategy_runs_evaluated")
        if evaluated is None:
            return None
        if metadata.get("near_miss_setup"):
            return f"Strategy checks: {evaluated} evaluated"
        if bool(metadata.get("no_strategy_setup_triggered")):
            return f"Strategy checks: {evaluated} evaluated | no setup trigger fired"
        return f"Strategy checks: {evaluated} evaluated"

    @staticmethod
    def _format_blocker_summary(snapshot: "LiveSignalSnapshot") -> str | None:
        metadata = getattr(snapshot, "metadata", {}) or {}
        setup = metadata.get("near_miss_setup")
        if isinstance(setup, dict) and setup.get("rejection_reasons"):
            reasons = list(setup.get("rejection_reasons") or [])
        else:
            reasons = list(metadata.get("top_rejection_reasons") or snapshot.reject_reasons or [])
        if not reasons:
            return None
        return f"Blockers: {', '.join(str(reason) for reason in reasons[:5])}"

    @staticmethod
    def _format_provider_status(snapshot: "LiveSignalSnapshot") -> str:
        metadata = getattr(snapshot, "metadata", {}) or {}
        quote_provider = metadata.get("data_source_quote") or metadata.get("data_source") or "unknown"
        history_provider = metadata.get("data_source_history") or metadata.get("data_source") or "unknown"
        quote_verified = "yes" if bool(metadata.get("quote_live_verified", metadata.get("data_source_verified", False))) else "no"
        history_fresh = "yes" if bool(metadata.get("bars_fresh", False)) else "no"
        freshness_status = str(metadata.get("freshness_status") or "n/a")
        return (
            f"Quote: {quote_provider} ({quote_verified}) | "
            f"Bars: {history_provider} ({history_fresh}) | "
            f"Freshness: {freshness_status}"
        )

    @staticmethod
    def _fmt_number(value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)
