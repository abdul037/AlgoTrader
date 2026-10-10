"""Mirror real Alpaca-paper fills into the internal paper P&L ledger.

When ``paper_broker`` is ``alpaca`` the execution coordinator records an
``ExecutionRecord`` plus broker order snapshots, but never writes
``paper_positions`` / ``paper_trades`` — those tables are only fed by the
self-simulated path. Everything downstream (equity curve, EOD digest,
per-strategy scorecard, Stage-1 track record) reads the ledger, so real
broker-backed paper trades were invisible to the P&L story (first observed
with the GOOGL fill on 2026-09-08).

This module closes that gap without touching the broker path:

* a filled parent bracket order opens a ``PaperPositionRecord`` (entry = the
  broker's average fill, quantity = filled qty, stop/target from the bracket
  legs, ``opened_at`` = the broker fill time);
* a filled protective leg — or a separate close order matched by the existing
  lifecycle logic — closes it into a ``PaperTradeRecord`` at the broker's exit
  fill, with realized P&L computed from the two fills;
* broker-backed positions are only marked-to-market by the simulator's refresh;
  the *broker* decides exits, so the simulator must never "close" them itself.

Idempotent: keyed by ``execution_id`` stored in the position payload, so the
sync can run every refresh cycle and after restarts. Paper-only by
construction — it only reads executions the lifecycle view already classifies
as Alpaca paper executions.
"""

from __future__ import annotations

from typing import Any

from app.models.paper import PaperBrokerExecutionRecord, PaperPositionRecord
from app.utils.time import utc_now

LEDGER_SOURCE = "alpaca_paper"


def is_broker_backed(position: PaperPositionRecord) -> bool:
    """True for ledger rows mirrored from a real broker fill (exits owned by the broker)."""

    return str(dict(position.payload or {}).get("ledger_source") or "") == LEDGER_SOURCE


def sync_broker_backed_positions(service: Any, *, limit: int = 500) -> dict[str, int]:
    """Open/close ledger rows from broker-backed paper executions. Returns counts."""

    if service.executions is None:
        return {"scanned": 0, "opened": 0, "closed": 0}
    opened = closed = scanned = 0
    positions_by_execution = _positions_by_execution(service)
    for execution in service.broker_executions(limit=limit):
        scanned += 1
        if execution.filled_qty <= 0 or execution.entry_fill_price is None:
            continue
        position = positions_by_execution.get(execution.execution_id)
        if position is None:
            position = _open_position(service, execution)
            positions_by_execution[execution.execution_id] = position
            opened += 1
        if position.status != "open":
            continue
        if execution.exit_fill_price is None:
            continue
        service._close_position(
            position,
            exit_price=float(execution.exit_fill_price),
            outcome=_exit_outcome(execution),
            closed_at=_exit_filled_at(execution),
        )
        closed += 1
    return {"scanned": scanned, "opened": opened, "closed": closed}


def _positions_by_execution(service: Any) -> dict[str, PaperPositionRecord]:
    result: dict[str, PaperPositionRecord] = {}
    for position in service.positions.list(limit=2000):
        if not is_broker_backed(position):
            continue
        execution_id = str(dict(position.payload or {}).get("execution_id") or "")
        if execution_id:
            result[execution_id] = position
    return result


def _open_position(service: Any, execution: PaperBrokerExecutionRecord) -> PaperPositionRecord:
    request = dict(execution.payload.get("request") or {})
    metadata = dict(request.get("metadata") or {})
    trade_plan = dict(metadata.get("trade_plan") or {})
    stop_leg = next((leg for leg in execution.legs if leg.stop_price is not None), None)
    target_leg = next((leg for leg in execution.legs if leg.limit_price is not None), None)
    stop = stop_leg.stop_price if stop_leg is not None else _optional_float(request.get("stop_loss"))
    target = target_leg.limit_price if target_leg is not None else _optional_float(request.get("take_profit"))
    opened_at = execution.filled_at or execution.submitted_at or execution.created_at
    entry = float(execution.entry_fill_price or 0.0)
    record = PaperPositionRecord(
        proposal_id=execution.proposal_id,
        signal_id=str(metadata.get("signal_id") or "") or None,
        symbol=execution.symbol.upper(),
        strategy_name=execution.strategy_name or str(request.get("strategy_name") or "") or "unknown",
        timeframe=str(metadata.get("timeframe") or "1d"),
        side=str(execution.side or request.get("side") or "buy").lower(),
        regime_label=str(metadata.get("market_regime_label") or "") or None,
        hold_style=str(trade_plan.get("hold_style") or "") or None,
        quantity=float(execution.filled_qty),
        entry_price=entry,
        current_price=entry,
        stop_loss=stop,
        target_1=target,
        opened_at=str(opened_at),
        updated_at=utc_now().isoformat(),
        payload={
            "ledger_source": LEDGER_SOURCE,
            "execution_id": execution.execution_id,
            "broker_order_id": execution.broker_order_id,
            "client_order_id": execution.client_order_id,
            "source": execution.source,
            "risk_reward_ratio": metadata.get("risk_reward_ratio") or metadata.get("estimated_reward_to_risk"),
            "proposed_price": _optional_float(request.get("proposed_price")),
            "slippage_bps": None,
        },
    )
    service.positions.create(record)
    service.logs.log(
        "paper_position_opened",
        {
            "proposal_id": record.proposal_id,
            "execution_id": execution.execution_id,
            "symbol": record.symbol,
            "entry_price": record.entry_price,
            "quantity": record.quantity,
            "timeframe": record.timeframe,
            "ledger_source": LEDGER_SOURCE,
        },
    )
    return record


def _exit_outcome(execution: PaperBrokerExecutionRecord) -> str:
    exit_leg = next(
        (
            leg
            for leg in execution.legs
            if str(leg.status or "").lower() == "filled"
            and str(leg.side or "").lower() == "sell"
            and leg.filled_avg_price is not None
        ),
        None,
    )
    if exit_leg is None:
        return "broker_close"
    if exit_leg.stop_price is not None or str(exit_leg.order_type or "").lower() == "stop":
        return "stop_loss"
    if exit_leg.limit_price is not None or str(exit_leg.order_type or "").lower() == "limit":
        return "target_1"
    return "broker_close"


def _exit_filled_at(execution: PaperBrokerExecutionRecord) -> str | None:
    for leg in execution.legs:
        if str(leg.status or "").lower() == "filled" and str(leg.side or "").lower() == "sell" and leg.filled_at:
            return str(leg.filled_at)
    return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
