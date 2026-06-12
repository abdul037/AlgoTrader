"""Logging configuration."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.config import dictConfig


class JsonFormatter(logging.Formatter):
    """Emit one structured JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "event_type",
            "proposal_id",
            "queue_id",
            "execution_id",
            "client_order_id",
            "broker_order_id",
            "strategy",
            "symbol",
            "status",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Configure application logging."""

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "app.logging_config.JsonFormatter",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["console"], "level": "INFO"},
        }
    )
    logging.getLogger(__name__).debug("Logging configured")
