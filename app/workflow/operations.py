"""Workflow execution helpers for scans, alerts, and ledger interactions."""

from __future__ import annotations

import inspect
from typing import Any

from app.models.workflow import WorkflowTaskResponse
from app.universe import resolve_universe
from app.utils.time import utc_now


def run_scan_task(
    service: Any,
    *,
    task: str,
    state_key: str,
    origin: str,
    timeframes: list[str],
    notify: bool,
    force_refresh: bool,
    symbols: list[str] | None = None,
) -> WorkflowTaskResponse:
    kwargs = {
        "symbols": symbols
        or resolve_universe(
            service.settings,
            limit=int(getattr(service.settings, "workflow_scan_default_universe_limit", 25) or 25),
        ),
        "timeframes": timeframes,
        "limit": service.settings.screener_top_k,
        "validated_only": service.settings.require_backtest_validation_for_alerts,
        "notify": False,
        "force_refresh": force_refresh,
    }
    if "scan_task" in inspect.signature(service.market_screener.scan_universe).parameters:
        kwargs["scan_task"] = task
    response = service.market_screener.scan_universe(**kwargs)
    alerts_sent = service._send_scan_alerts(task=task, response=response, notify=notify)
    service._track_candidates(response, origin=origin)
    proposals_created = service._auto_propose_candidates(response, origin=origin, notify=notify)
    service.runtime_state.set(state_key, utc_now().isoformat())
    service.run_logs.log(
        f"workflow_{task}_completed",
        {
            "started_at": utc_now().isoformat(),
            "candidates": len(response.candidates),
            "symbols_scanned": response.evaluated_symbols,
            "symbols_passed": [item.symbol for item in response.candidates],
            "alerts_sent": alerts_sent,
            "proposals_created": proposals_created,
            "timeframes": timeframes,
            "errors": response.errors,
        },
    )
    return WorkflowTaskResponse(
        task=task,
        status="ok",
        detail=f"{task.replace('_', ' ').title()} completed.",
        alerts_sent=alerts_sent,
        candidates=len(response.candidates),
        open_signals=len(service.tracked_signals.list(status="open", limit=500)),
        errors=list(response.errors),
    )


def track_candidates(service: Any, response: Any, *, origin: str) -> None:
    if not service.settings.track_alerted_signals:
        return
    for candidate in response.candidates:
        if candidate.state.value == "none":
            continue
        alert_eligible = bool(
            candidate.metadata.get("alert_eligible", (candidate.direction_label or candidate.state.value) != "watchlist")
        )
        classification = str(candidate.metadata.get("signal_classification") or "").lower()
        should_track_watchlist = bool(getattr(service.settings, "track_watchlist_signals", False)) and classification in {
            "watchlist",
            "trigger_ready",
            "execution_ready",
        }
        if not alert_eligible and not should_track_watchlist:
            continue
        service.tracked_signals.upsert_open(candidate, origin=origin)


def close_status(snapshot: Any, price: float) -> str | None:
    role = str(snapshot.signal_role or snapshot.metadata.get("signal_role") or "entry_long")
    is_short = role == "entry_short"
    stop = snapshot.stop_loss
    target = snapshot.take_profit or (snapshot.targets[0] if snapshot.targets else None)
    if stop is not None:
        if is_short and price >= stop:
            return "stop_hit"
        if not is_short and price <= stop:
            return "stop_hit"
    if target is not None:
        if is_short and price <= target:
            return "target_hit"
        if not is_short and price >= target:
            return "target_hit"
    return None


def check_open_signals_impl(service: Any, *, notify: bool, force_refresh: bool) -> WorkflowTaskResponse:
    records = service.tracked_signals.list(status="open", limit=500)
    closed_signals = 0
    alerts_sent = 0

    for record in records:
        quote = service.market_data.get_quote(
            record.symbol,
            timeframe=record.timeframe,
            force_refresh=force_refresh,
        )
        price = float(quote.last_execution or quote.ask or quote.bid or record.last_price or 0.0)
        snapshot = record.snapshot.model_copy(
            update={
                "current_price": price,
                "current_bid": quote.bid,
                "current_ask": quote.ask,
                "rate_timestamp": quote.timestamp,
                "generated_at": utc_now().isoformat(),
            }
        )
        service.tracked_signals.update_price(record.id, last_price=price, snapshot=snapshot)

        close_status_value = service._close_status(snapshot, price)
        if close_status_value is None:
            continue

        closed = service.tracked_signals.close(
            record.id,
            status=close_status_value,
            last_price=price,
            snapshot=snapshot,
        )
        closed_signals += 1
        message = service.notifier.format_tracked_signal_update(closed, event_type=close_status_value)
        if notify and service.notifier.send_text(message):
            alerts_sent += 1
        service.alert_history.create(
            category="tracked_signal_update",
            status=close_status_value,
            message_text=message,
            symbol=closed.symbol,
            strategy_name=closed.strategy_name,
            timeframe=closed.timeframe,
            payload=closed.model_dump(),
        )

    service.runtime_state.set("workflow:last_open_signal_check_at", utc_now().isoformat())
    service.run_logs.log(
        "workflow_open_signal_check_completed",
        {"open_signals": len(records), "closed_signals": closed_signals, "alerts_sent": alerts_sent},
    )
    return WorkflowTaskResponse(
        task="open_signal_check",
        status="ok",
        detail="Open signal check completed.",
        alerts_sent=alerts_sent,
        open_signals=len(records),
        closed_signals=closed_signals,
    )


def send_daily_summary_impl(service: Any, *, notify: bool) -> WorkflowTaskResponse:
    open_signals = service.tracked_signals.list(status="open", limit=20)
    recent_alerts = service.alert_history.list(limit=20)
    message = service.notifier.format_daily_summary(
        open_signals=open_signals,
        recent_alerts=recent_alerts,
    )
    alerts_sent = 1 if (notify and service.notifier.send_text(message)) else 0
    service.alert_history.create(
        category="daily_summary",
        status="sent" if alerts_sent else "generated",
        message_text=message,
        payload={
            "open_signals": [item.model_dump() for item in open_signals],
            "recent_alerts": [item.model_dump() if hasattr(item, "model_dump") else item for item in recent_alerts],
        },
    )
    service.runtime_state.set("workflow:last_daily_summary_at", utc_now().isoformat())
    service.run_logs.log(
        "workflow_daily_summary_completed",
        {"open_signals": len(open_signals), "recent_alerts": len(recent_alerts), "alerts_sent": alerts_sent},
    )
    return WorkflowTaskResponse(
        task="daily_summary",
        status="ok",
        detail="Daily summary generated.",
        alerts_sent=alerts_sent,
        open_signals=len(open_signals),
    )


def send_scan_alerts(service: Any, *, task: str, response: Any, notify: bool) -> int:
    if not notify:
        return 0
    alert_candidates = [
        item for item in response.candidates
        if bool(
            item.metadata.get(
                "alert_eligible",
                (item.direction_label or item.state.value) != "watchlist" and item.state.value != "none",
            )
        )
    ][: max(1, int(service.settings.screener_top_alerts_per_run))]
    if not alert_candidates:
        return 0

    if service.settings.screener_alert_mode == "single":
        sent = 0
        for item in alert_candidates:
            try:
                item = service._candidate_with_ledger_outcome(task=task, candidate=item)
            except service.LedgerRecordingError as exc:
                service.alert_history.create(
                    category=task,
                    status="dropped_ledger_failure",
                    message_text=f"[ledger failure] {exc}",
                    symbol=item.symbol,
                    strategy_name=item.strategy_name,
                    timeframe=item.timeframe,
                    payload=item.model_dump(),
                )
                continue
            message = service.notifier.format_screener_candidate(item)
            delivered = service.notifier.send_text(message)
            if delivered:
                sent += 1
            service.alert_history.create(
                category=task,
                status="sent" if delivered else "generated",
                message_text=message,
                symbol=item.symbol,
                strategy_name=item.strategy_name,
                timeframe=item.timeframe,
                payload=item.model_dump(),
            )
        return sent

    recorded_candidates: list[Any] = []
    for item in alert_candidates:
        try:
            recorded_candidates.append(
                service._candidate_with_ledger_outcome(task=task, candidate=item)
            )
        except service.LedgerRecordingError as exc:
            service.alert_history.create(
                category=task,
                status="dropped_ledger_failure",
                message_text=f"[ledger failure] {exc}",
                symbol=item.symbol,
                strategy_name=item.strategy_name,
                timeframe=item.timeframe,
                payload=item.model_dump(),
            )
    if not recorded_candidates:
        return 0
    digest = response.model_copy(update={"candidates": recorded_candidates})
    try:
        message = service.notifier.format_screener_summary(digest, task_label=task)
    except TypeError:
        message = service.notifier.format_screener_summary(digest)
    alerts_sent = 1 if service.notifier.send_text(message) else 0
    service.alert_history.create(
        category=task,
        status="sent" if alerts_sent else "generated",
        message_text=message,
        payload=digest.model_dump(),
    )
    return alerts_sent


def candidate_with_ledger_outcome(service: Any, *, task: str, candidate: Any) -> Any:
    if service.ledger_service is None:
        return candidate
    if not bool(getattr(service.settings, "ledger_enabled", False)):
        return candidate
    if not bool(getattr(service.settings, "ledger_record_alerts_enabled", False)):
        return candidate

    generated_at = candidate.generated_at or candidate.signal_generated_at
    if not generated_at:
        service.run_logs.log(
            "ledger_alert_record_error",
            {
                "task": task,
                "symbol": getattr(candidate, "symbol", None),
                "error": "missing generated_at on candidate",
            },
        )
        raise service.LedgerRecordingError(
            f"candidate {getattr(candidate, 'symbol', '?')} lacks generated_at"
        )

    alert_id = (
        f"{task}:{candidate.symbol}:{candidate.strategy_name}:"
        f"{candidate.timeframe}:{generated_at}"
    )
    target = candidate.take_profit
    if target is None and candidate.targets:
        target = candidate.targets[0]
    payload = service._ledger_alert_payload(
        task=task,
        candidate=candidate,
        generated_at=generated_at,
        target=target,
    )
    try:
        outcome_id = service.ledger_service.record_alert(
            alert_source=task,
            alert_id=alert_id,
            symbol=candidate.symbol,
            strategy_name=candidate.strategy_name,
            timeframe=candidate.timeframe,
            alert_created_at=generated_at,
            alert_entry_price=candidate.entry_price or candidate.current_price,
            alert_stop=candidate.stop_loss,
            alert_target=target,
            alert_score=candidate.score,
            alert_payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        service.run_logs.log(
            "ledger_alert_record_error",
            {
                "task": task,
                "symbol": getattr(candidate, "symbol", None),
                "error": str(exc),
            },
        )
        raise service.LedgerRecordingError(str(exc)) from exc

    candidate = service._copy_candidate_with_metadata(
        candidate,
        {
            "ledger_outcome_id": outcome_id,
            "ledger_alert_id": alert_id,
        },
    )
    service.run_logs.log(
        "ledger_alert_recorded",
        {
            "outcome_id": outcome_id,
            "task": task,
            "symbol": candidate.symbol,
            "strategy_name": candidate.strategy_name,
            "timeframe": candidate.timeframe,
            "alert_id": alert_id,
        },
    )
    return candidate


def ledger_alert_payload(
    *,
    task: str,
    candidate: Any,
    generated_at: str,
    target: float | None,
) -> dict[str, Any]:
    payload = candidate.model_dump() if hasattr(candidate, "model_dump") else {}
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    payload.update(
        {
            "alert_source": task,
            "direction": str(
                getattr(candidate, "direction_label", None)
                or getattr(getattr(candidate, "state", None), "value", None)
                or ""
            ),
            "timestamp_utc": generated_at,
            "score": getattr(candidate, "score", None),
            "target": target,
            "stop": getattr(candidate, "stop_loss", None),
            "confluence_vector": {
                "score_breakdown": dict(getattr(candidate, "score_breakdown", {}) or {}),
                "indicators": dict(getattr(candidate, "indicators", {}) or {}),
                "strategy_checks": metadata.get("strategy_checks"),
                "strategy_diagnostics": metadata.get("strategy_diagnostics"),
                "pass_reasons": list(getattr(candidate, "pass_reasons", []) or []),
                "reject_reasons": list(getattr(candidate, "reject_reasons", []) or []),
                "metadata": metadata,
            },
        }
    )
    return payload


def copy_candidate_with_metadata(candidate: Any, metadata_updates: dict[str, Any]) -> Any:
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    metadata.update(metadata_updates)
    if hasattr(candidate, "model_copy"):
        return candidate.model_copy(update={"metadata": metadata})
    candidate.metadata = metadata
    return candidate


def run_ledger_cycle_impl(service: Any) -> WorkflowTaskResponse:
    if service.ledger_service is None:
        return WorkflowTaskResponse(
            task="ledger_cycle",
            status="skipped",
            detail="Ledger service is not configured.",
            skipped=True,
        )
    result = service.ledger_service.run_cycle()
    service.runtime_state.set("workflow:last_ledger_cycle_at", utc_now().isoformat())
    service.run_logs.log("workflow_ledger_cycle_completed", result)
    return WorkflowTaskResponse(
        task="ledger_cycle",
        status="ok",
        detail=(
            "Ledger cycle completed. "
            f"Positions seen: {result.get('positions_seen', 0)}, "
            f"matched: {result.get('matched_new', 0)}, "
            f"manual imported: {result.get('manual_imported', 0)}, "
            f"closed: {result.get('closed_new', 0)}, "
            f"expired: {result.get('expired_pending', 0)}."
        ),
        closed_signals=int(result.get("closed_new", 0) or 0),
    )
