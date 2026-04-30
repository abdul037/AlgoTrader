"""Live signal evaluation and market scanning."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.live_signal_schema import LiveSignalSnapshot, SignalScanResponse, TelegramAlertResponse
from app.runtime_settings import AppSettings
from app.signals.alerts import (
    commit_snapshot,
    send_signal_alert_with_label,
    signal_from_snapshot,
    snapshot_with_ledger_outcome,
)
from app.signals.backtests import (
    attach_backtest_context,
    backtest_strategy_candidates,
    backtest_validation,
    ranking_key,
)
from app.signals.evaluation import (
    evaluate_equity,
    evaluate_gold,
    evaluate_symbol,
    score_equity_setup,
    trade_support,
)
from app.telegram_notify import TelegramNotifier
from app.utils.time import utc_now

if TYPE_CHECKING:
    from app.broker.etoro_market_data import EtoroMarketDataClient
    from app.storage.repositories import BacktestRepository, RunLogRepository, SignalRepository, SignalStateRepository


class LedgerRecordingError(RuntimeError):
    """Raised when a Telegram-bound live signal cannot be persisted to the ledger."""


class LiveSignalService:
    """Evaluate live market-data signals and scan ranked candidates."""

    LedgerRecordingError = LedgerRecordingError

    def __init__(
        self,
        *,
        settings: AppSettings,
        market_data_client: "EtoroMarketDataClient" | Any,
        signal_repository: "SignalRepository" | Any,
        signal_state_repository: "SignalStateRepository" | Any,
        run_log_repository: "RunLogRepository" | Any,
        backtest_repository: "BacktestRepository" | Any | None = None,
        telegram_notifier: TelegramNotifier | Any | None = None,
        ledger_service: Any | None = None,
    ):
        self.settings = settings
        self.market_data = market_data_client
        self.signals = signal_repository
        self.signal_states = signal_state_repository
        self.logs = run_log_repository
        self.backtests = backtest_repository
        self.notifier = telegram_notifier
        self.ledger_service = ledger_service
        from app.broker.instrument_resolver import InstrumentResolver

        self.resolver = InstrumentResolver(settings)

    def get_latest_signal(
        self,
        symbol: str,
        *,
        commit: bool = False,
        notify: bool = False,
    ) -> LiveSignalSnapshot:
        normalized = symbol.upper().strip()
        snapshot = self._evaluate_symbol(normalized)
        if commit or notify:
            self._commit_snapshot(snapshot, notify=notify)
        return snapshot

    def scan_market(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        supported_only: bool = False,
        commit: bool = False,
        notify: bool = False,
    ) -> SignalScanResponse:
        universe = [symbol.upper().strip() for symbol in (symbols or self.settings.signal_scan_universe)]
        errors: list[str] = []
        candidates: list[LiveSignalSnapshot] = []
        alerts_sent = 0

        for symbol in universe:
            try:
                snapshot = self._evaluate_symbol(symbol)
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue

            if supported_only and not snapshot.supported:
                continue
            candidates.append(snapshot)

            if commit or notify:
                if self._commit_snapshot(snapshot, notify=notify):
                    alerts_sent += 1

        ranked = sorted(candidates, key=self._ranking_key, reverse=True)
        max_items = max(1, min(limit or self.settings.signal_scan_limit, 100))
        response = SignalScanResponse(
            generated_at=utc_now().isoformat(),
            timeframe=self.settings.live_signal_interval,
            evaluated_count=len(candidates),
            limit=max_items,
            alerts_sent=alerts_sent,
            candidates=ranked[:max_items],
            errors=errors,
        )
        self.logs.log(
            "live_signal_scan",
            {
                "evaluated_count": response.evaluated_count,
                "limit": response.limit,
                "alerts_sent": response.alerts_sent,
                "supported_only": supported_only,
            },
        )
        return response

    def send_test_alert(self, message: str) -> TelegramAlertResponse:
        if not self.notifier or not hasattr(self.notifier, "send_text"):
            return TelegramAlertResponse(
                sent=False,
                detail="Telegram notifier is not configured on the app.",
            )

        sent = bool(self.notifier.send_text(message))
        if sent:
            self.logs.log(
                "telegram_test_sent",
                {
                    "message": message,
                },
            )
        return TelegramAlertResponse(
            sent=sent,
            detail="Telegram test message sent." if sent else "Telegram test message was not sent.",
            chat_id=self.settings.telegram_chat_id or None,
        )

    def send_signal_alert(self, symbol: str) -> TelegramAlertResponse:
        return self.send_signal_alert_with_label(symbol, previous_state="manual")

    def send_signal_alert_with_label(
        self,
        symbol: str,
        *,
        previous_state: str,
    ) -> TelegramAlertResponse:
        return send_signal_alert_with_label(self, symbol, previous_state=previous_state)

    def _evaluate_symbol(self, symbol: str) -> LiveSignalSnapshot:
        return evaluate_symbol(self, symbol)

    def _evaluate_equity(self, symbol: str, candles: Any, quote: Any) -> LiveSignalSnapshot:
        return evaluate_equity(self, symbol, candles, quote)

    def _evaluate_gold(self, symbol: str, candles: Any, quote: Any) -> LiveSignalSnapshot:
        return evaluate_gold(self, symbol, candles, quote)

    def _commit_snapshot(self, snapshot: LiveSignalSnapshot, *, notify: bool) -> bool:
        return commit_snapshot(self, snapshot, notify=notify)

    def _snapshot_with_ledger_outcome(self, snapshot: LiveSignalSnapshot, *, alert_source: str) -> LiveSignalSnapshot:
        return snapshot_with_ledger_outcome(self, snapshot, alert_source=alert_source)

    @staticmethod
    def _signal_from_snapshot(snapshot: LiveSignalSnapshot):
        return signal_from_snapshot(snapshot)

    def _attach_backtest_context(self, snapshot: LiveSignalSnapshot) -> LiveSignalSnapshot:
        return attach_backtest_context(self, snapshot)

    def _backtest_validation(self, snapshot: LiveSignalSnapshot) -> dict[str, Any]:
        return backtest_validation(self, snapshot)

    @staticmethod
    def _backtest_strategy_candidates(strategy_name: str) -> list[str]:
        return backtest_strategy_candidates(strategy_name)

    def _trade_support(self, symbol: str) -> tuple[bool, str | None]:
        return trade_support(self, symbol)

    @staticmethod
    def _ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, float]:
        return ranking_key(snapshot)

    @staticmethod
    def _score_equity_setup(**kwargs: Any) -> float:
        return score_equity_setup(**kwargs)
