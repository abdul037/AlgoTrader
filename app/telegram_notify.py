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
        """Format a simple trade-ready screener alert."""

        metadata = getattr(snapshot, "metadata", {}) or {}
        trade_plan = dict(metadata.get("trade_plan") or {})
        direction = TelegramNotifier._candidate_side(snapshot)
        header = f"{rank or snapshot.rank or '-'}. {snapshot.symbol} {snapshot.timeframe} {direction}"
        outcome_line = TelegramNotifier._format_ledger_outcome_line(metadata)
        classification = str(metadata.get("signal_classification") or "unclassified").replace("_", " ")
        entry = TelegramNotifier._format_entry_zone(trade_plan, snapshot)
        current = TelegramNotifier._fmt_number(snapshot.current_price)
        stop = TelegramNotifier._fmt_number(snapshot.stop_loss)
        rr = TelegramNotifier._fmt_number(snapshot.risk_reward_ratio)
        targets = TelegramNotifier._format_targets(snapshot)
        reasons = TelegramNotifier._format_reason_summary(snapshot)
        lines = [
            header,
            f"Action: review for manual approval. Bot has not placed an order.",
            f"Entry: {entry} | current {current}",
            f"Stop: {stop} | target {targets} | RR {rr}R",
            f"Score: {snapshot.score:.1f}/100 | status: {classification}",
            TelegramNotifier._format_snapshot_data_source(snapshot),
        ]
        if outcome_line:
            lines.append(outcome_line)
        if reasons != "n/a":
            lines.append(f"Why: {reasons}")
        return "\n".join(lines)

    @staticmethod
    def _format_ledger_outcome_line(metadata: dict[str, Any]) -> str | None:
        outcome_id = metadata.get("ledger_outcome_id")
        if outcome_id in (None, ""):
            return None
        return f"Ledger outcome: #{outcome_id}"

    @staticmethod
    def format_screener_summary(
        response: "ScreenerRunResponse",
        *,
        task_label: str | None = None,
        include_other_watches: bool = False,
    ) -> str:
        """Format a ranked screener summary for Telegram."""

        if not response.candidates:
            closest = list(getattr(response, "closest_rejections", []) or [])
            signal_label = "WAIT" if closest else "NO SETUP"
            lines = [
                "US market screener",
                f"TRADE SIGNAL: {signal_label}",
                "Action: do not open a trade now.",
            ]
            diagnostics = TelegramNotifier._format_scan_diagnostics(
                response,
                include_other_watches=include_other_watches,
            )
            if diagnostics:
                lines.append("")
                lines.extend(diagnostics)
            else:
                lines.append("Reason: no nearby setup passed enough checks.")
            if response.errors:
                lines.append("")
                first_error = str(response.errors[0]).replace("\n", " ")
                if len(first_error) > 180:
                    first_error = f"{first_error[:180]}..."
                lines.append(f"First issue: {first_error}")
                lines.append(f"Errors: {len(response.errors)}")
            lines.append("")
            lines.append(
                f"Scanned: {response.evaluated_symbols} symbol(s) | "
                f"Checks: {response.evaluated_strategy_runs} | "
                f"Timeframes: {', '.join(response.timeframes)}"
            )
            lines.append(
                "Safety: no order created. Manual approval is required for any future order."
            )
            return "\n".join(lines)

        lines = [
            "US market screener",
            "TRADE SIGNAL: READY FOR REVIEW",
            "Action: review the setup. Manual approval is still required.",
            (
                f"Run: {(task_label or 'manual_scan').replace('_', ' ')} | "
                f"Universe: {response.universe_name}"
            ),
            f"Timeframes: {', '.join(response.timeframes)}",
            (
                f"Scanned: {response.evaluated_symbols} symbols | "
                f"Strategy runs: {response.evaluated_strategy_runs} | "
                f"Passed: {len(response.candidates)} | Suppressed: {response.suppressed}"
            ),
        ]
        for index, item in enumerate(response.candidates, start=1):
            lines.append("")
            lines.append(TelegramNotifier.format_screener_candidate(item, rank=item.rank or index))
        diagnostics = TelegramNotifier._format_scan_diagnostics(
            response,
            include_other_watches=include_other_watches,
        )
        if diagnostics:
            lines.append("")
            lines.extend(diagnostics)
        if response.errors:
            lines.append(f"Errors: {len(response.errors)}")
        return "\n".join(lines)

    @staticmethod
    def _format_scan_diagnostics(
        response: "ScreenerRunResponse",
        *,
        include_other_watches: bool = False,
    ) -> list[str]:
        summary = dict(getattr(response, "rejection_summary", {}) or {})
        closest = list(getattr(response, "closest_rejections", []) or [])
        if not summary and not closest:
            return []
        lines = []
        if closest:
            lines.append("Best setup to watch:")
            lines.extend(TelegramNotifier._format_watch_item(1, closest[0], detailed=True))
            if include_other_watches and len(closest) > 1:
                lines.append("Other watches:")
                for item in closest[1:3]:
                    lines.append(TelegramNotifier._format_compact_watch_item(item))
        elif summary:
            top_reasons = sorted(summary.items(), key=lambda item: (-int(item[1]), item[0]))[:3]
            lines.append(
                "Reason: "
                + "; ".join(f"{TelegramNotifier._friendly_reason(reason)} ({count})" for reason, count in top_reasons)
            )
        return lines

    @staticmethod
    def _format_watch_item(index: int, item: dict[str, Any], *, detailed: bool = False) -> list[str]:
        measurements = dict(item.get("measurements") or {})
        score = item.get("score")
        score_text = f"{float(score):.1f}" if score is not None else "n/a"
        side = TelegramNotifier._watch_side(measurements)
        symbol = item.get("symbol") or "n/a"
        timeframe = item.get("timeframe") or "n/a"
        lines = [f"{index}. {symbol} {timeframe} {side} | score {score_text}"]

        watchlist_plan = TelegramNotifier._format_watchlist_plan(measurements)
        if watchlist_plan:
            lines.extend(watchlist_plan)

        reasons = "; ".join(
            TelegramNotifier._friendly_reason(reason)
            for reason in list(item.get("rejection_reasons") or [])[:3]
        )
        prefix = "Why wait" if detailed else "Not ready"
        lines.append(f"   {prefix}: {reasons or 'setup is not confirmed yet'}")
        return lines

    @staticmethod
    def _format_compact_watch_item(item: dict[str, Any]) -> str:
        measurements = dict(item.get("measurements") or {})
        symbol = item.get("symbol") or "n/a"
        timeframe = item.get("timeframe") or "n/a"
        side = TelegramNotifier._watch_side(measurements)
        current = TelegramNotifier._fmt_scan_num(measurements.get("current_price"))
        trigger = TelegramNotifier._compact_trigger_instruction(
            measurements.get("watchlist_trigger"),
            measurements.get("indicative_entry"),
        )
        return f"- {symbol} {timeframe} {side}: {trigger} | current {current}"

    @staticmethod
    def _format_watchlist_plan(measurements: dict[str, Any]) -> list[str]:
        if not measurements:
            return []
        entry = measurements.get("indicative_entry")
        stop = measurements.get("indicative_stop")
        target = measurements.get("indicative_target")
        if entry in (None, "") or stop in (None, "") or target in (None, ""):
            return []
        current = measurements.get("current_price")
        trigger = measurements.get("watchlist_trigger") or "watch"
        gap_atr = measurements.get("breakout_gap_atr")
        rr = measurements.get("indicative_rr")
        move_pct = measurements.get("indicative_target_move_pct")
        rvol = measurements.get("relative_volume")
        relaxed_rvol = measurements.get("minimum_relative_volume_relaxed")
        strict_rvol = measurements.get("minimum_relative_volume")
        volume_mode = measurements.get("volume_check_mode")
        volume_need = TelegramNotifier._format_volume_need(
            current_rvol=rvol,
            relaxed_rvol=relaxed_rvol,
            strict_rvol=strict_rvol,
            volume_mode=volume_mode,
        )
        trigger_text = TelegramNotifier._entry_instruction(trigger, entry)
        line1 = f"   {trigger_text}"
        line2 = (
            f"   Current: {TelegramNotifier._fmt_scan_num(current)} | "
            f"gap: {TelegramNotifier._fmt_scan_num(gap_atr)} ATR"
        )
        line3 = (
            f"   Stop: {TelegramNotifier._fmt_scan_num(stop)} | "
            f"target: {TelegramNotifier._fmt_scan_num(target)} | "
            f"RR {TelegramNotifier._fmt_scan_num(rr)}R"
        )
        line4 = f"   Volume: {volume_need}"
        line5 = f"   {TelegramNotifier._format_measurement_data_source(measurements)}"
        line6 = f"   Target move: {TelegramNotifier._fmt_scan_num(move_pct)}%"
        return [line1, line2, line3, line4, line5, line6]

    @staticmethod
    def _trigger_instruction(trigger: Any, entry: Any) -> str:
        entry_text = TelegramNotifier._fmt_scan_num(entry)
        trigger_value = str(trigger or "")
        if trigger_value in {"breakout_above", "breakout_confirmed"}:
            return f"enter only above {entry_text}"
        if trigger_value in {"breakdown_below", "breakdown_confirmed"}:
            return f"enter only below {entry_text}"
        return f"watch {entry_text}"

    @staticmethod
    def _entry_instruction(trigger: Any, entry: Any) -> str:
        entry_text = TelegramNotifier._fmt_scan_num(entry)
        trigger_value = str(trigger or "")
        if trigger_value in {"breakout_above", "breakout_confirmed"}:
            return f"Enter only if price goes above {entry_text}"
        if trigger_value in {"breakdown_below", "breakdown_confirmed"}:
            return f"Enter only if price goes below {entry_text}"
        return f"Watch price near {entry_text}"

    @staticmethod
    def _compact_trigger_instruction(trigger: Any, entry: Any) -> str:
        entry_text = TelegramNotifier._fmt_scan_num(entry)
        trigger_value = str(trigger or "")
        if trigger_value in {"breakout_above", "breakout_confirmed"}:
            return f"above {entry_text}"
        if trigger_value in {"breakdown_below", "breakdown_confirmed"}:
            return f"below {entry_text}"
        return f"near {entry_text}"

    @staticmethod
    def _watch_side(measurements: dict[str, Any]) -> str:
        raw_side = str(
            measurements.get("near_miss_side")
            or measurements.get("side")
            or ""
        ).lower()
        if raw_side in {"long", "short"}:
            return raw_side.upper()
        trigger = str(measurements.get("watchlist_trigger") or "")
        if "breakdown" in trigger:
            return "SHORT"
        if "breakout" in trigger:
            return "LONG"
        return "SETUP"

    @staticmethod
    def _format_volume_need(
        *,
        current_rvol: Any,
        relaxed_rvol: Any,
        strict_rvol: Any,
        volume_mode: Any,
    ) -> str:
        current_text = TelegramNotifier._fmt_scan_num(current_rvol)
        relaxed_text = TelegramNotifier._fmt_scan_num(relaxed_rvol)
        strict_text = TelegramNotifier._fmt_scan_num(strict_rvol)
        threshold = strict_rvol if strict_rvol not in (None, "") else relaxed_rvol
        try:
            volume_ok = threshold in (None, "") or float(current_rvol) >= float(threshold)
        except (TypeError, ValueError):
            volume_ok = False
        status = "OK" if volume_ok else "LOW"
        if relaxed_rvol not in (None, "") and strict_rvol not in (None, "") and relaxed_rvol != strict_rvol:
            return f"{status} (RVOL {current_text}, need {relaxed_text}-{strict_text})"
        return f"{status} (RVOL {current_text}, need {strict_text})"

    @staticmethod
    def _format_measurement_data_source(measurements: dict[str, Any]) -> str:
        quote_provider = str(measurements.get("quote_provider") or "unknown")
        history_provider = str(measurements.get("history_provider") or "unknown")
        suffixes: list[str] = []
        if bool(measurements.get("quote_used_fallback")):
            suffixes.append("quote fallback")
        if bool(measurements.get("history_used_fallback")):
            suffixes.append("candle fallback")
        if bool(measurements.get("history_from_cache")):
            suffixes.append("cached candles")
        suffix = f" ({', '.join(suffixes)})" if suffixes else ""
        return f"Data: {quote_provider} quote + {history_provider} candles{suffix}"

    @staticmethod
    def _format_snapshot_data_source(snapshot: "LiveSignalSnapshot") -> str:
        metadata = getattr(snapshot, "metadata", {}) or {}
        quote_provider = str(metadata.get("data_source_quote") or metadata.get("data_source") or "unknown")
        history_provider = str(metadata.get("data_source_history") or "unknown")
        suffixes: list[str] = []
        if bool(metadata.get("data_source_used_fallback")):
            suffixes.append("quote fallback")
        if bool(metadata.get("history_used_fallback")):
            suffixes.append("candle fallback")
        if bool(metadata.get("data_source_from_cache")):
            suffixes.append("cached candles")
        suffix = f" ({', '.join(suffixes)})" if suffixes else ""
        return f"Data: {quote_provider} quote + {history_provider} candles{suffix}"

    @staticmethod
    def _fmt_scan_num(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if abs(number) >= 100:
            return f"{number:.2f}"
        if abs(number) >= 10:
            return f"{number:.2f}"
        return f"{number:.2f}"

    @staticmethod
    def _humanize_reason(reason: str) -> str:
        return str(reason or "unknown").replace("_", " ")

    @staticmethod
    def _friendly_reason(reason: str) -> str:
        reason_text = str(reason or "unknown")
        labels = {
            "adx_too_low": "trend strength too low",
            "breakdown_level_not_cleared": "price has not broken below trigger",
            "breakout_level_not_cleared": "price has not broken above trigger",
            "candle_body_too_small": "candle is not strong enough",
            "close_location_short_too_low": "close is weak for short entry",
            "close_location_too_low": "close is weak for long entry",
            "confluence_score_too_low": "setup quality below threshold",
            "confirmation_too_weak": "confirmation is too weak",
            "false_positive_risk_too_high": "false-positive risk too high",
            "final_score_below_keep_threshold": "setup quality below threshold",
            "macd_hist_not_negative": "MACD has not turned bearish",
            "macd_hist_not_positive": "MACD has not turned bullish",
            "market_data_error": "market data error",
            "relative_volume_too_low": "volume is too low",
            "rsi_not_in_long_band": "RSI is not in long zone",
            "rsi_not_in_short_band": "RSI is not in short zone",
            "ema_9_slope_not_negative": "EMA slope has not turned bearish",
            "ema_9_slope_not_positive": "EMA slope has not turned bullish",
        }
        return labels.get(reason_text, TelegramNotifier._humanize_reason(reason_text))

    @staticmethod
    def _candidate_side(snapshot: "LiveSignalSnapshot") -> str:
        raw = str(
            snapshot.signal_role
            or getattr(snapshot, "direction_label", None)
            or snapshot.metadata.get("signal_role")
            or snapshot.state.value
            or ""
        ).lower()
        if "short" in raw or raw == "sell":
            return "SHORT"
        if "long" in raw or raw == "buy":
            return "LONG"
        return raw.upper() or "SETUP"

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
