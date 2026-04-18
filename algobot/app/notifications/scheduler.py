"""Background scheduler for Telegram alert delivery."""

from __future__ import annotations

import logging
from threading import Event, Thread

from app.notifications.telegram_bot import TelegramBotService

logger = logging.getLogger(__name__)


class TelegramAlertScheduler:
    """Run periodic Telegram alert checks inside the FastAPI process."""

    def __init__(self, service: TelegramBotService, *, check_interval_seconds: int = 60):
        self.service = service
        self.check_interval_seconds = max(check_interval_seconds, 5)
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """Start the background alert loop once."""

        if self._thread and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, name="telegram-alert-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background alert loop."""

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if hasattr(self.service, "run_scheduled_tasks"):
                    self.service.run_scheduled_tasks()
                else:
                    self.service.send_due_alerts()
            except Exception as exc:
                logger.exception("Telegram alert scheduler error: %s", exc)
            self._stop_event.wait(self.check_interval_seconds)
