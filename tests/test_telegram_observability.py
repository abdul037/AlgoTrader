from __future__ import annotations

from app.notifications.telegram_bot import TelegramBotService


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send_text(self, message: str, *, chat_id: str | None = None):
        self.sent.append((message, chat_id))
        return True


class FakeSettings:
    telegram_allowed_chat_ids = ["1"]
    telegram_chat_id = "1"
    telegram_hourly_alerts_enabled = False
    telegram_alert_interval_minutes = 60
    telegram_alert_symbols = []
    telegram_poll_interval_seconds = 0
    telegram_command_timeout_seconds = 5


class FakeState:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeLogs:
    def log(self, event_type, payload):
        pass


class FakeLiveSignals:
    pass


class FakeLedgerRepository:
    def summary_stats(self):
        return {
            "total_outcomes": 3,
            "by_status": {"pending_match": 1, "open": 1, "target_hit": 1},
            "closed_count": 1,
            "wins": 1,
            "losses": 0,
            "win_rate": 1.0,
            "profit_factor": None,
            "avg_r_multiple": 2.0,
            "avg_hold_hours": 3.5,
            "by_strategy": [
                {
                    "strategy_name": "rsi_vwap_ema_confluence",
                    "closed": 1,
                    "win_rate": 1.0,
                    "profit_factor": None,
                    "avg_r_multiple": 2.0,
                }
            ],
        }


class FakeLedgerService:
    repository = FakeLedgerRepository()


class FakeWorkflow:
    ledger_service = FakeLedgerService()

    def health_summary(self):
        return {
            "status": "ok",
            "reason": "healthy",
            "last_successful_screener_run_at": "2026-04-21T01:00:00+00:00",
            "last_successful_ledger_cycle_at": "2026-04-21T01:05:00+00:00",
            "pending_match_count": 1,
            "pending_match_older_than_24h_count": 0,
            "model_deployment_mode": "shadow",
            "active_meta_model_version": None,
            "current_regime_label": None,
            "last_etoro_api_error": None,
            "last_etoro_api_error_at": None,
        }


def test_outcomes_command_returns_ledger_summary() -> None:
    notifier = FakeNotifier()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        workflow_service=FakeWorkflow(),
        runtime_state_repository=FakeState(),
        run_log_repository=FakeLogs(),
    )

    bot.handle_text("1", "/outcomes")

    assert "Outcome ledger" in notifier.sent[0][0]
    assert "Total outcomes: 3" in notifier.sent[0][0]
    assert "rsi_vwap_ema_confluence" in notifier.sent[0][0]


def test_health_command_returns_phase_zero_fields() -> None:
    notifier = FakeNotifier()
    bot = TelegramBotService(
        settings=FakeSettings(),
        notifier=notifier,
        live_signals=FakeLiveSignals(),
        workflow_service=FakeWorkflow(),
        runtime_state_repository=FakeState(),
        run_log_repository=FakeLogs(),
    )

    bot.handle_text("1", "/health")

    assert "Bot health" in notifier.sent[0][0]
    assert "Last screener: 2026-04-21T01:00:00+00:00" in notifier.sent[0][0]
    assert "Pending matches: 1 | >24h: 0" in notifier.sent[0][0]
