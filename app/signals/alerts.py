"""Alerting and ledger helpers for the live signal service."""

from __future__ import annotations

from typing import Any

from app.live_signal_schema import LiveSignalSnapshot, SignalState, TelegramAlertResponse
from app.models.signal import Signal


def send_signal_alert_with_label(
    service: Any,
    symbol: str,
    *,
    previous_state: str,
) -> TelegramAlertResponse:
    snapshot = service.get_latest_signal(symbol, commit=True, notify=False)
    if service.settings.require_verified_market_data_for_alerts and not bool(snapshot.metadata.get("data_source_verified", False)):
        reason = str(snapshot.metadata.get("data_source_verification_reason") or "market_data_unverified")
        service.logs.log(
            "telegram_signal_suppressed_market_data_gate",
            {
                "symbol": snapshot.symbol,
                "strategy_name": snapshot.strategy_name,
                "state": snapshot.state.value,
                "label": previous_state,
                "reason": reason,
            },
        )
        return TelegramAlertResponse(
            sent=False,
            detail=f"Telegram signal suppressed by market-data gate: {reason}",
            symbol=snapshot.symbol,
            chat_id=service.settings.telegram_chat_id or None,
        )
    validation = service._backtest_validation(snapshot)
    if service.settings.require_backtest_validation_for_alerts and not validation["passes"]:
        service.logs.log(
            "telegram_signal_suppressed_backtest_gate",
            {
                "symbol": snapshot.symbol,
                "strategy_name": snapshot.strategy_name,
                "state": snapshot.state.value,
                "label": previous_state,
                "reason": validation["reason"],
            },
        )
        return TelegramAlertResponse(
            sent=False,
            detail=f"Telegram signal suppressed by backtest gate: {validation['reason']}",
            symbol=snapshot.symbol,
            chat_id=service.settings.telegram_chat_id or None,
        )
    if not service.notifier or not hasattr(service.notifier, "send_signal_change"):
        return TelegramAlertResponse(
            sent=False,
            detail="Telegram notifier is not configured on the app.",
            symbol=snapshot.symbol,
        )

    try:
        snapshot = service._snapshot_with_ledger_outcome(
            snapshot,
            alert_source=f"telegram_{previous_state}",
        )
    except service.LedgerRecordingError as exc:
        service.logs.log(
            "telegram_signal_suppressed_ledger_gate",
            {
                "symbol": snapshot.symbol,
                "strategy_name": snapshot.strategy_name,
                "state": snapshot.state.value,
                "label": previous_state,
                "reason": str(exc),
            },
        )
        return TelegramAlertResponse(
            sent=False,
            detail=f"Telegram signal suppressed by ledger gate: {exc}",
            symbol=snapshot.symbol,
            chat_id=service.settings.telegram_chat_id or None,
        )
    sent = bool(service.notifier.send_signal_change(snapshot, previous_state=previous_state))
    if sent:
        service.logs.log(
            "telegram_signal_sent",
            {
                "symbol": snapshot.symbol,
                "strategy_name": snapshot.strategy_name,
                "state": snapshot.state.value,
                "label": previous_state,
            },
        )
    return TelegramAlertResponse(
        sent=sent,
        detail="Telegram signal alert sent." if sent else "Telegram signal alert was not sent.",
        symbol=snapshot.symbol,
        chat_id=service.settings.telegram_chat_id or None,
    )


def commit_snapshot(service: Any, snapshot: LiveSignalSnapshot, *, notify: bool) -> bool:
    previous = service.signal_states.get(
        snapshot.symbol,
        snapshot.strategy_name,
        snapshot.timeframe,
    )
    changed = previous is None or previous.state != snapshot.state
    service.signal_states.upsert(snapshot)

    if changed and snapshot.state != SignalState.NONE:
        service.signals.create(service._signal_from_snapshot(snapshot))

    if changed and notify:
        if snapshot.state != SignalState.NONE or service.settings.notify_on_none_signal_change:
            if service.settings.require_verified_market_data_for_alerts and not bool(snapshot.metadata.get("data_source_verified", False)):
                service.logs.log(
                    "signal_notification_suppressed_market_data_gate",
                    {
                        "symbol": snapshot.symbol,
                        "strategy_name": snapshot.strategy_name,
                        "state": snapshot.state.value,
                        "reason": snapshot.metadata.get("data_source_verification_reason", "market_data_unverified"),
                    },
                )
                return False
            validation = service._backtest_validation(snapshot)
            if service.settings.require_backtest_validation_for_alerts and not validation["passes"]:
                service.logs.log(
                    "signal_notification_suppressed_backtest_gate",
                    {
                        "symbol": snapshot.symbol,
                        "strategy_name": snapshot.strategy_name,
                        "state": snapshot.state.value,
                        "reason": validation["reason"],
                    },
                )
                return False
            if service.notifier and hasattr(service.notifier, "send_signal_change"):
                try:
                    snapshot_to_send = service._snapshot_with_ledger_outcome(
                        snapshot,
                        alert_source="signal_notification",
                    )
                except service.LedgerRecordingError as exc:
                    service.logs.log(
                        "signal_notification_suppressed_ledger_gate",
                        {
                            "symbol": snapshot.symbol,
                            "strategy_name": snapshot.strategy_name,
                            "state": snapshot.state.value,
                            "reason": str(exc),
                        },
                    )
                    return False
                sent = bool(
                    service.notifier.send_signal_change(
                        snapshot_to_send,
                        previous_state=previous.state.value if previous else None,
                    )
                )
                if sent:
                    service.logs.log(
                        "signal_notification_sent",
                        {
                            "symbol": snapshot.symbol,
                            "strategy_name": snapshot.strategy_name,
                            "state": snapshot.state.value,
                        },
                    )
                return sent
    return False


def snapshot_with_ledger_outcome(service: Any, snapshot: LiveSignalSnapshot, *, alert_source: str) -> LiveSignalSnapshot:
    if service.ledger_service is None:
        return snapshot
    if not bool(getattr(service.settings, "ledger_enabled", False)):
        return snapshot
    if not bool(getattr(service.settings, "ledger_record_alerts_enabled", False)):
        return snapshot
    try:
        generated_at = snapshot.generated_at or snapshot.signal_generated_at
        if not generated_at:
            service.logs.log(
                "ledger_alert_record_error",
                {
                    "alert_source": alert_source,
                    "symbol": snapshot.symbol,
                    "error": "missing generated_at on snapshot",
                },
            )
            raise service.LedgerRecordingError(
                f"snapshot {snapshot.symbol} lacks generated_at"
            )
        target = snapshot.take_profit or (snapshot.targets[0] if snapshot.targets else None)
        alert_id = (
            f"{alert_source}:{snapshot.symbol}:{snapshot.strategy_name}:"
            f"{snapshot.timeframe}:{generated_at}"
        )
        payload = snapshot.model_dump()
        payload.update(
            {
                "alert_source": alert_source,
                "direction": str(snapshot.direction_label or snapshot.state.value),
                "timestamp_utc": generated_at,
                "score": snapshot.score,
                "target": target,
                "stop": snapshot.stop_loss,
                "confluence_vector": {
                    "score_breakdown": dict(snapshot.score_breakdown or {}),
                    "indicators": dict(snapshot.indicators or {}),
                    "pass_reasons": list(snapshot.pass_reasons or []),
                    "reject_reasons": list(snapshot.reject_reasons or []),
                    "metadata": dict(snapshot.metadata or {}),
                },
            }
        )
        outcome_id = service.ledger_service.record_alert(
            alert_source=alert_source,
            alert_id=alert_id,
            symbol=snapshot.symbol,
            strategy_name=snapshot.strategy_name,
            timeframe=snapshot.timeframe,
            alert_created_at=generated_at,
            alert_entry_price=snapshot.entry_price or snapshot.current_price,
            alert_stop=snapshot.stop_loss,
            alert_target=target,
            alert_score=snapshot.score,
            alert_payload=payload,
        )
        metadata = dict(snapshot.metadata or {})
        metadata.update({"ledger_outcome_id": outcome_id, "ledger_alert_id": alert_id})
        service.logs.log(
            "ledger_alert_recorded",
            {
                "outcome_id": outcome_id,
                "alert_source": alert_source,
                "symbol": snapshot.symbol,
                "strategy_name": snapshot.strategy_name,
                "timeframe": snapshot.timeframe,
                "alert_id": alert_id,
            },
        )
        return snapshot.model_copy(update={"metadata": metadata})
    except Exception as exc:  # noqa: BLE001
        service.logs.log(
            "ledger_alert_record_error",
            {
                "alert_source": alert_source,
                "symbol": snapshot.symbol,
                "error": str(exc),
            },
        )
        if isinstance(exc, service.LedgerRecordingError):
            raise
        raise service.LedgerRecordingError(str(exc)) from exc


def signal_from_snapshot(snapshot: LiveSignalSnapshot) -> Signal:
    return Signal(
        symbol=snapshot.symbol,
        strategy_name=snapshot.strategy_name,
        action=snapshot.state.value,
        rationale=snapshot.rationale,
        confidence=snapshot.confidence,
        price=snapshot.entry_price or snapshot.current_price,
        stop_loss=snapshot.stop_loss,
        take_profit=snapshot.take_profit,
        metadata=snapshot.metadata,
    )
