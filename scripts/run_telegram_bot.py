"""Run the Telegram polling bot for commands and scheduled alerts.

Fallback utility only. Production should prefer webhook mode via FastAPI.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def main() -> None:
    _bootstrap_path()

    from app.main import create_app

    app = create_app(enable_background_jobs=False)
    settings = app.state.settings

    if not settings.telegram_enabled:
        raise SystemExit("Telegram is disabled. Set TELEGRAM_ENABLED=true.")
    if not settings.telegram_polling_enabled:
        raise SystemExit(
            "Telegram polling is disabled. Webhook mode is primary; "
            "set TELEGRAM_POLLING_ENABLED=true only for fallback polling."
        )

    print("Starting Telegram polling bot.")
    print(f"Allowed chats: {settings.telegram_allowed_chat_ids or [settings.telegram_chat_id]}")
    print(f"Hourly alerts enabled: {settings.telegram_hourly_alerts_enabled}")
    print(f"Alert symbols: {settings.telegram_alert_symbols}")
    print("Press Ctrl+C to stop.")

    # This process runs the cadence in polling mode, so the separate paper learning account's
    # thread runs here too (app/learning_account); it stops when the bot does.
    from app.learning_account import run_learning_account_here

    stop_learning_account = run_learning_account_here(app)
    try:
        app.state.telegram_command_service.run_forever()
    except KeyboardInterrupt:
        print("\nTelegram polling bot stopped.")
    finally:
        stop_learning_account()


if __name__ == "__main__":
    main()
