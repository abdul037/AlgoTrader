"""Telegram polling bot for commands and scheduled alerts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import inspect
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.runtime_settings import AppSettings
from app.telegram_notify import TelegramNotifier
from app.signals.service import LiveSignalService
from app.utils.time import utc_now

if TYPE_CHECKING:
    from app.storage.repositories import RunLogRepository, RuntimeStateRepository

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Poll Telegram commands and send scheduled alerts."""

    HELP_TEXT = (
        "Commands:\n"
        "/start or /help - show help\n"
        "/signal SYMBOL - full signal snapshot\n"
        "/price SYMBOL - quick price and watch levels\n"
        "/scan [limit] - ranked multi-stock screener\n"
        "/intraday_scan [limit] - ranked intraday screener\n"
        "/supported_scan [limit] - ranked screener for supported symbols\n"
        "/validated_scan [limit] - ranked screener filtered by validated backtests\n"
        "/open_signals - tracked active signals\n"
        "/daily_summary - latest workflow summary\n"
        "/notify SYMBOL - force-send the current signal snapshot\n"
    )

    def __init__(
        self,
        *,
        settings: AppSettings,
        notifier: TelegramNotifier,
        live_signals: LiveSignalService,
        market_screener: Any | None = None,
        workflow_service: Any | None = None,
        runtime_state_repository: "RuntimeStateRepository" | Any,
        run_log_repository: "RunLogRepository" | Any,
    ):
        self.settings = settings
        self.notifier = notifier
        self.live_signals = live_signals
        self.market_screener = market_screener
        self.workflow_service = workflow_service
        self.state = runtime_state_repository
        self.logs = run_log_repository

    def run_forever(self) -> None:
        """Run the long-polling command bot and scheduled alert loop."""

        if not self.notifier.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")

        self.state.set("telegram_bot_started_at", utc_now().isoformat())
        try:
            self.notifier.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            self._log_loop_error("telegram_bot_delete_webhook_error", exc)
        logger.info("Telegram bot loop started")
        while True:
            try:
                self.poll_once(timeout_seconds=self.settings.telegram_poll_interval_seconds)
            except Exception as exc:
                self._log_loop_error("telegram_bot_poll_error", exc)

            try:
                self.run_scheduled_tasks()
            except Exception as exc:
                self._log_loop_error("telegram_bot_alert_error", exc)

            time.sleep(1)

    def poll_once(self, *, timeout_seconds: int = 0) -> int:
        """Process one batch of Telegram updates."""

        offset = self._next_update_offset()
        updates = self.notifier.get_updates(
            offset=offset,
            timeout=max(timeout_seconds, 0),
            limit=20,
        )
        processed = 0
        for update in updates:
            processed += int(self.handle_update(update))
        self.state.set("telegram_last_poll_at", utc_now().isoformat())
        return processed

    def handle_update(self, update: dict) -> bool:
        """Handle a single Telegram update for polling or webhook delivery."""

        update_id = update.get("update_id")
        if update_id is not None:
            self.state.set("telegram_last_update_id", str(update_id))

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str(chat.get("id") or "")
        if not text or not chat_id:
            return False
        if not self._chat_allowed(chat_id):
            return False

        self.handle_text(chat_id, text)
        return True

    def send_due_alerts(self) -> int:
        """Send hourly alerts when configured and due."""

        if not self.settings.telegram_hourly_alerts_enabled:
            return 0

        sent = 0
        for symbol in self.settings.telegram_alert_symbols:
            state_key = f"telegram_hourly_alert:{symbol}"
            last_sent_raw = self.state.get(state_key)
            if last_sent_raw and not self._is_due(last_sent_raw):
                continue

            response = self._run_with_timeout(
                self.live_signals.send_signal_alert_with_label,
                symbol,
                previous_state="scheduled",
            )
            if response.sent:
                self.state.set(state_key, utc_now().isoformat())
                self.logs.log("telegram_hourly_alert_sent", {"symbol": symbol})
                sent += 1
        return sent

    def run_scheduled_tasks(self) -> int:
        """Run hourly compatibility alerts plus the workflow scheduler if configured."""

        sent = self.send_due_alerts()
        if self.workflow_service is not None:
            result = self.workflow_service.run_scheduled_tasks()
            sent += int(result.get("alerts_sent", 0))
        return sent

    def handle_text(self, chat_id: str, text: str) -> None:
        """Handle one Telegram command message."""

        try:
            self._handle_text_impl(chat_id, text)
        except Exception as exc:
            logger.exception("Telegram command handling failed: %s", exc)
            self.logs.log(
                "telegram_command_error",
                {"chat_id": chat_id, "text": text, "error": str(exc)},
            )
            self.notifier.send_text(
                f"Command failed for `{text}`.\n{exc}",
                chat_id=chat_id,
            )

    def _handle_text_impl(self, chat_id: str, text: str) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        if command in {"/start", "/help"}:
            self.notifier.send_text(self.HELP_TEXT, chat_id=chat_id)
            return

        if command == "/signal":
            if not args:
                self.notifier.send_text("Usage: /signal SYMBOL", chat_id=chat_id)
                return
            if self.market_screener is not None and hasattr(self.market_screener, "analyze_symbol"):
                snapshot = self._run_with_timeout(
                    self.market_screener.analyze_symbol,
                    args[0],
                    force_refresh=False,
                )
            else:
                snapshot = self._run_with_timeout(
                    self.live_signals.get_latest_signal,
                    args[0],
                    commit=False,
                    notify=False,
                )
            self.notifier.send_text(
                self.notifier.format_signal_message(snapshot, previous_state="query"),
                chat_id=chat_id,
            )
            return

        if command == "/price":
            if not args:
                self.notifier.send_text("Usage: /price SYMBOL", chat_id=chat_id)
                return
            snapshot = self._run_with_timeout(
                self.live_signals.get_latest_signal,
                args[0],
                commit=False,
                notify=False,
            )
            self.notifier.send_text(
                self.notifier.format_price_message(snapshot),
                chat_id=chat_id,
            )
            return

        if command in {"/scan", "/screener", "/supported_scan", "/intraday_scan", "/validated_scan"}:
            limit = self._parse_limit(args)
            supported_only = command == "/supported_scan"
            validated_only = command == "/validated_scan"
            intraday = command == "/intraday_scan"
            self.notifier.send_text(
                self._scan_message(
                    limit=limit,
                    supported_only=supported_only,
                    validated_only=validated_only,
                    intraday=intraday,
                ),
                chat_id=chat_id,
            )
            return

        if command == "/notify":
            if not args:
                self.notifier.send_text("Usage: /notify SYMBOL", chat_id=chat_id)
                return
            response = self._run_with_timeout(
                self.live_signals.send_signal_alert_with_label,
                args[0],
                previous_state="telegram",
            )
            detail = response.detail if response.sent else f"Failed: {response.detail}"
            self.notifier.send_text(detail, chat_id=chat_id)
            return

        if command == "/open_signals":
            if self.workflow_service is None:
                self.notifier.send_text("Workflow service is not configured.", chat_id=chat_id)
                return
            status = self.workflow_service.status()
            records = self.workflow_service.tracked_signals.list(status="open", limit=10)
            message = self.notifier.format_daily_summary(open_signals=records, recent_alerts=[])
            message = f"{message}\nScheduler enabled: {'yes' if status.scheduler_enabled else 'no'}"
            self.notifier.send_text(message, chat_id=chat_id)
            return

        if command == "/daily_summary":
            if self.workflow_service is None:
                self.notifier.send_text("Workflow service is not configured.", chat_id=chat_id)
                return
            result = self._run_with_timeout(self.workflow_service.send_daily_summary, notify=False)
            self.notifier.send_text(result.detail, chat_id=chat_id)
            summary = self.notifier.format_daily_summary(
                open_signals=self.workflow_service.tracked_signals.list(status="open", limit=10),
                recent_alerts=self.workflow_service.alert_history.list(limit=10),
            )
            self.notifier.send_text(summary, chat_id=chat_id)
            return

        self.notifier.send_text(self.HELP_TEXT, chat_id=chat_id)

    def _next_update_offset(self) -> int | None:
        last_update_id = self.state.get("telegram_last_update_id")
        if last_update_id is None:
            return None
        try:
            return int(last_update_id) + 1
        except ValueError:
            return None

    def _chat_allowed(self, chat_id: str) -> bool:
        allowed = self.settings.telegram_allowed_chat_ids or [self.settings.telegram_chat_id]
        return chat_id in [str(item) for item in allowed if str(item)]

    def _log_loop_error(self, event_type: str, exc: Exception) -> None:
        logger.exception("Telegram bot loop error: %s", exc)
        self.logs.log(event_type, {"error": str(exc)})

    def _scan_message(
        self,
        *,
        limit: int,
        supported_only: bool,
        validated_only: bool,
        intraday: bool,
    ) -> str:
        if self.market_screener is None:
            response = self._run_with_timeout(
                self.live_signals.scan_market,
                limit=limit,
                supported_only=supported_only,
                commit=False,
                notify=False,
            )
            return self.notifier.format_scan_message(response)

        symbols = None
        if supported_only:
            symbols = list(self.settings.allowed_instruments)
        timeframes = (
            list(self.settings.screener_intraday_timeframes)
            if intraday
            else list(self.settings.screener_default_timeframes)
        )
        kwargs = {
            "symbols": symbols,
            "timeframes": timeframes,
            "limit": limit,
            "validated_only": validated_only,
            "notify": False,
            "force_refresh": False,
        }
        if "scan_task" in inspect.signature(self.market_screener.scan_universe).parameters:
            kwargs["scan_task"] = (
                "manual_intraday_scan"
                if intraday
                else "manual_validated_scan"
                if validated_only
                else "manual_supported_scan"
                if supported_only
                else "manual_scan"
            )
        response = self._run_with_timeout(
            self.market_screener.scan_universe,
            **kwargs,
        )
        task_label = (
            "intraday_scan"
            if intraday
            else "validated_scan"
            if validated_only
            else "supported_scan"
            if supported_only
            else "scan"
        )
        try:
            return self.notifier.format_screener_summary(response, task_label=task_label)
        except TypeError:
            return self.notifier.format_screener_summary(response)

    def _run_with_timeout(self, func, *args, **kwargs):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=self.settings.telegram_command_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                f"Operation timed out after {self.settings.telegram_command_timeout_seconds}s"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _is_due(self, last_sent_raw: str) -> bool:
        try:
            last_sent = datetime.fromisoformat(last_sent_raw)
        except ValueError:
            return True
        return utc_now() - last_sent >= timedelta(minutes=self.settings.telegram_alert_interval_minutes)

    @staticmethod
    def _parse_limit(args: list[str]) -> int:
        if not args:
            return 5
        try:
            return max(1, min(int(args[0]), 20))
        except ValueError:
            return 5
