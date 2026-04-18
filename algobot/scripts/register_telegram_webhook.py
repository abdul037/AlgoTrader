"""Register the Telegram webhook against the FastAPI endpoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime_settings import get_settings
from app.telegram_notify import TelegramNotifier


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Telegram webhook URL.")
    parser.add_argument("--url", help="Public HTTPS webhook URL. Defaults to TELEGRAM_WEBHOOK_URL.")
    parser.add_argument(
        "--drop-pending-updates",
        action="store_true",
        help="Drop queued updates while switching to webhook mode.",
    )
    args = parser.parse_args()

    settings = get_settings()
    webhook_url = (args.url or settings.telegram_webhook_url).strip()
    if not webhook_url:
        raise SystemExit("Provide --url or set TELEGRAM_WEBHOOK_URL in the environment.")

    notifier = TelegramNotifier(settings)
    result = notifier.set_webhook(
        webhook_url,
        secret_token=settings.telegram_webhook_secret or None,
        drop_pending_updates=args.drop_pending_updates,
    )
    print(result)
    print(notifier.get_webhook_info())


if __name__ == "__main__":
    main()
