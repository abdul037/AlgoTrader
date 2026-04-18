"""Telegram notification integration."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any
from urllib.parse import urlencode

from app.config import AppSettings
from app.models.live_signal import LiveSignalSnapshot, SignalScanResponse

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
        snapshot: LiveSignalSnapshot,
        *,
        previous_state: str | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """Send a formatted signal-change message to Telegram."""

        message = self.format_signal_message(snapshot, previous_state=previous_state)
        return self.send_text(message, chat_id=chat_id)

    def send_text(self, message: str, *, chat_id: str | None = None) -> bool:
        """Send a plain text message to Telegram."""

        if not self.enabled:
            return False

        url = self._bot_url("sendMessage")
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        try:
            result = self._call(url, method="POST", json_payload=payload, timeout_seconds=15)
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
        """Fetch Telegram updates via long polling."""

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
        """Delete the configured Telegram webhook."""

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
        """Set the Telegram webhook URL."""

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
        """Return Telegram webhook configuration info."""

        if not self.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")

        return self._call(self._bot_url("getWebhookInfo"), method="GET", timeout_seconds=10)

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
    def format_signal_message(snapshot: LiveSignalSnapshot, previous_state: str | None = None) -> str:
        lines = [
            f"Signal change for {snapshot.symbol}",
            f"Strategy: {snapshot.strategy_name}",
            f"State: {previous_state or 'unknown'} -> {snapshot.state.value}",
            f"Price: {snapshot.current_price if snapshot.current_price is not None else 'n/a'}",
            f"Entry: {snapshot.entry_price if snapshot.entry_price is not None else 'n/a'}",
            f"Exit: {snapshot.exit_price if snapshot.exit_price is not None else 'n/a'}",
            f"Stop: {snapshot.stop_loss if snapshot.stop_loss is not None else 'n/a'}",
            f"Target: {snapshot.take_profit if snapshot.take_profit is not None else 'n/a'}",
            f"Score: {snapshot.score:.2f}",
            f"Rationale: {snapshot.rationale}",
        ]
        return "\n".join(lines)

    @staticmethod
    def format_price_message(snapshot: LiveSignalSnapshot) -> str:
        lines = [
            f"Price snapshot for {snapshot.symbol}",
            f"Current: {snapshot.current_price if snapshot.current_price is not None else 'n/a'}",
            f"Bid: {snapshot.current_bid if snapshot.current_bid is not None else 'n/a'}",
            f"Ask: {snapshot.current_ask if snapshot.current_ask is not None else 'n/a'}",
            f"Signal state: {snapshot.state.value}",
            f"Entry watch: {snapshot.entry_price if snapshot.entry_price is not None else 'n/a'}",
            f"Exit watch: {snapshot.exit_price if snapshot.exit_price is not None else 'n/a'}",
            f"Stop: {snapshot.stop_loss if snapshot.stop_loss is not None else 'n/a'}",
            f"Target: {snapshot.take_profit if snapshot.take_profit is not None else 'n/a'}",
        ]
        return "\n".join(lines)

    @staticmethod
    def format_scan_message(response: SignalScanResponse) -> str:
        if not response.candidates:
            return "No scan candidates were returned."

        lines = [f"Top {len(response.candidates)} live setups"]
        for item in response.candidates:
            lines.append(
                f"{item.symbol}: {item.state.value} | "
                f"price {item.current_price if item.current_price is not None else 'n/a'} | "
                f"entry {item.entry_price if item.entry_price is not None else 'n/a'} | "
                f"score {item.score:.2f}"
            )
        if response.errors:
            lines.append(f"Errors: {len(response.errors)}")
        return "\n".join(lines)
