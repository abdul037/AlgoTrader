"""Open-signal check: re-price tracked signals and close those that hit target or stop.

2026-10-06: in market hours prices change every run, so it wrote all ~90 open signals
back to Postgres and fetched a quote per symbol/timeframe each time, taking ~130 s and,
together with the ledger and reconciliation steps, pushing maintenance past its 240 s
limit. Now a price is written only when it moved at least ``PRICE_WRITE_MIN_MOVE``
(closing a signal is always written), and the check stops after
``OPEN_SIGNAL_BUDGET_SECONDS``, resuming the rest first on the next run.
"""

from __future__ import annotations

import time
from typing import Any

from app.models.workflow import WorkflowTaskResponse
from app.utils.time import utc_now

OPEN_SIGNAL_BUDGET_SECONDS = 60.0
PRICE_WRITE_MIN_MOVE = 0.001  # 0.1%
CURSOR_KEY = "workflow:open_signal_cursor"


def check_open_signals_impl(
    service: Any, *, notify: bool, force_refresh: bool
) -> WorkflowTaskResponse:
    records = service.tracked_signals.list(status="open", limit=500)
    closed_signals = 0
    alerts_sent = 0
    quotes: dict[tuple[str, str], Any] = {}  # one quote per symbol/timeframe per run
    deadline = time.monotonic() + OPEN_SIGNAL_BUDGET_SECONDS
    try:
        cursor = int(service.runtime_state.get(CURSOR_KEY) or 0) % max(len(records), 1)
    except (TypeError, ValueError):
        cursor = 0
    ordered = records[cursor:] + records[:cursor]  # resume where the last run stopped
    checked = 0

    for record in ordered:
        if time.monotonic() >= deadline:
            break
        checked += 1
        key = (record.symbol, record.timeframe)
        if key not in quotes:
            quotes[key] = service.market_data.get_quote(
                record.symbol, timeframe=record.timeframe, force_refresh=force_refresh
            )
        quote = quotes[key]
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
        last = float(record.last_price or 0.0)
        if not last or abs(price - last) >= last * PRICE_WRITE_MIN_MOVE:  # skip tiny moves
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
        message = service.notifier.format_tracked_signal_update(
            closed, event_type=close_status_value
        )
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

    if records:
        service.runtime_state.set(CURSOR_KEY, str((cursor + checked) % len(records)))
    service.runtime_state.set("workflow:last_open_signal_check_at", utc_now().isoformat())
    service.run_logs.log(
        "workflow_open_signal_check_completed",
        {
            "open_signals": len(records),
            "checked": checked,
            "closed_signals": closed_signals,
            "alerts_sent": alerts_sent,
        },
    )
    return WorkflowTaskResponse(
        task="open_signal_check",
        status="ok",
        detail="Open signal check completed.",
        alerts_sent=alerts_sent,
        open_signals=len(records),
        closed_signals=closed_signals,
    )
