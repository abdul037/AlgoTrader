"""Live-vs-backtest scorecard for eToro LIVE trades (operator request 2026-10-06).

The operator's aim for the live account is to test whether the backtests hold up with
real fills, not only to make money. Each mirrored entry is recorded with what its
strategy's walk-forward backtest expected, then followed to its close:

* ``expected_r``: the strategy's pooled out-of-sample expectancy in R. The batch backtest
  risks 1% of a $10,000 account per trade, so R = expectancy_usd / ``BACKTEST_RISK_USD``.
* the eToro fill (``open_rate``) against the signal price (entry slippage);
* ``realized_r``: (close - open) / (open - strategy stop), so a trade that hits its stop
  is about -1 R whatever its size, and live trades compare directly with the backtest;
* the exit reason (target, stop, the bot's backup stop, or other).

eToro's API offers no closed-trade history here, so a close is detected when a position
that was seen disappears; its price is the bot's own close price when the backup stop
closed it, otherwise a fresh eToro rate read at detection (within one guard tick, 60 s).
"""

from __future__ import annotations

import json
import math
from contextlib import nullcontext
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

SCORECARD_KEY = "etoro_live:scorecard"
NOT_FILLED_AFTER_HOURS = 24
MIN_TRADES_FOR_VERDICT = 10
LEVEL_TOLERANCE = 0.003  # a close within 0.3% of the stop/target counts as hitting it


def record_entry(mirror: Any, record: dict[str, Any], *, timeframe: Any, entry_price: Any) -> None:
    """Remember a freshly submitted mirrored entry and what its backtest expected."""

    key = str(record.get("etoro_order_id") or record.get("proposal_id") or "")
    if not key:
        return
    verdict = _verdict(mirror.state, record.get("strategy_name"), timeframe)
    entry = _float(entry_price)
    stop = _float(record.get("stop_loss"))
    target = _float(record.get("take_profit"))
    amount = _float(record.get("amount_usd")) or 0.0
    leverage = int(record.get("leverage") or 1)
    trade = {
        "symbol": str(record.get("symbol") or "").upper(),
        "strategy_name": record.get("strategy_name"),
        "timeframe": str(timeframe or "").lower() or None,
        "proposal_id": record.get("proposal_id"),
        "paper_order_id": record.get("primary_order_id"),
        "opened_at": utc_now().isoformat(),
        "amount_usd": amount,
        "leverage": leverage,
        "notional_usd": round(amount * leverage, 2),
        "signal_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "planned_reward_r": _ratio(target, entry, stop),
        "expected_r": _r(verdict.get("oos_expectancy_usd")),
        "holdout_expected_r": _r(verdict.get("holdout_expectancy_usd")),
        "backtest_oos_trades": verdict.get("oos_trades"),
        "status": "pending_fill",
    }
    with _lock(mirror):
        cards = _load(mirror)
        cards[key] = trade
        _save(mirror, cards)


def update_scorecard(mirror: Any, bot_closes: list[dict[str, Any]] | None = None) -> int:
    """Attach eToro fills to new entries and close out trades whose position is gone.

    Returns how many trades changed. A malformed portfolio read changes nothing.
    """

    from app.broker.etoro_live_backup_stop import fresh_rate, portfolio_positions
    from app.broker.etoro_live_mirror import client_problem

    with _lock(mirror):
        cards = _load(mirror)
        active = {k: t for k, t in cards.items() if t.get("status") in {"pending_fill", "open"}}
        if not active or client_problem(mirror.client):
            return 0
        client = mirror.client
        positions = portfolio_positions(client.fetch_raw_portfolio())
        if positions is None:
            return 0
        closed_by_bot = {str(c.get("symbol") or "").upper(): c for c in bot_closes or []}
        stale = (utc_now() - timedelta(hours=NOT_FILLED_AFTER_HOURS)).isoformat()
        changed = 0
        for trade in active.values():
            instrument_id = trade.get("instrument_id") or _instrument_id(client, trade["symbol"])
            if not instrument_id:
                continue
            trade["instrument_id"] = instrument_id
            held = _held(positions, trade, int(instrument_id))
            if held:
                if trade["status"] == "pending_fill":
                    _mark_filled(trade, held)
                    changed += 1
                continue
            if trade["status"] == "pending_fill":
                if str(trade.get("opened_at") or "") < stale:
                    trade["status"] = "not_filled"
                    changed += 1
                continue
            bot = closed_by_bot.get(trade["symbol"])
            if bot is not None:
                rate, source = _float(bot.get("rate")), "bot_backup_stop"
            else:
                try:
                    rate, source = fresh_rate(client, trade["symbol"]), "rate_when_seen_closed"
                except Exception:  # noqa: BLE001 - retry next tick
                    rate, source = None, ""
            if rate is None:
                continue
            _mark_closed(trade, rate, source)
            changed += 1
            mirror.logs.log("etoro_live_scorecard_closed", {**trade})
        if changed:
            current = _load(mirror)
            current.update({k: t for k, t in active.items() if k in current})
            _save(mirror, current)
        return changed


def scorecard_report(runtime_state: Any, paper_fills: dict[str, float] | None = None) -> dict:
    """Per-trade rows and a per-strategy verdict: is live performance in line with the
    backtest? ``paper_fills`` maps proposal_id -> Alpaca paper fill (for fill slippage)."""

    try:
        cards = json.loads(runtime_state.get(SCORECARD_KEY) or "{}")
    except (TypeError, ValueError):
        cards = {}
    trades = []
    for key, trade in (cards if isinstance(cards, dict) else {}).items():
        row = {"etoro_order_id": key, **trade}
        paper = (paper_fills or {}).get(str(trade.get("proposal_id") or ""))
        if paper and trade.get("open_rate"):
            row["paper_fill"] = paper
            row["fill_vs_paper_bps"] = round((trade["open_rate"] / paper - 1.0) * 10_000, 1)
        trades.append(row)
    trades.sort(key=lambda t: str(t.get("opened_at") or ""), reverse=True)
    return {"trades": trades, "strategies": _by_strategy(trades)}


def _by_strategy(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        key = f"{trade.get('strategy_name')}:{trade.get('timeframe')}"
        groups.setdefault(key, []).append(trade)
    out: dict[str, dict[str, Any]] = {}
    for key, rows in groups.items():
        closed = [
            t for t in rows if t.get("status") == "closed" and t.get("realized_r") is not None
        ]
        rs = [float(t["realized_r"]) for t in closed]
        expected = next(
            (t.get("expected_r") for t in rows if t.get("expected_r") is not None), None
        )
        slips = [
            float(t["entry_slippage_bps"]) for t in rows if t.get("entry_slippage_bps") is not None
        ]
        summary: dict[str, Any] = {
            "trades": len(rows),
            "open": sum(1 for t in rows if t.get("status") in {"pending_fill", "open"}),
            "closed": len(closed),
            "expected_r": expected,
            "avg_realized_r": round(sum(rs) / len(rs), 3) if rs else None,
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 3) if rs else None,
            "pnl_usd": round(sum(float(t.get("pnl_usd") or 0.0) for t in closed), 2),
            "avg_entry_slippage_bps": round(sum(slips) / len(slips), 1) if slips else None,
            "exits": _count(t.get("exit_reason") for t in closed),
        }
        summary["verdict"] = _verdict_text(rs, expected)
        out[key] = summary
    return out


def _verdict_text(rs: list[float], expected: Any) -> str:
    """Within noise of the backtest, or clearly below it (mean + 2 standard errors < expected)."""

    if len(rs) < MIN_TRADES_FOR_VERDICT:
        return f"collecting ({len(rs)}/{MIN_TRADES_FOR_VERDICT} closed)"
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    se = math.sqrt(var / len(rs))
    if expected is not None and mean + 2 * se < float(expected):
        return "below_backtest"
    if mean - 2 * se > 0:
        return "profitable_live"
    return "in_line_with_backtest"


def _mark_filled(trade: dict[str, Any], held: list[dict[str, Any]]) -> None:
    units = sum(_float(p.get("units")) or 0.0 for p in held)
    cost = sum((_float(p.get("units")) or 0.0) * (_float(p.get("openRate")) or 0.0) for p in held)
    open_rate = cost / units if units else _float(held[0].get("openRate"))
    trade.update(
        status="open",
        filled_seen_at=utc_now().isoformat(),
        position_ids=[p.get("positionID") for p in held],
        units=round(units, 6) if units else None,
        open_rate=open_rate,
        broker_stop=_float(held[0].get("stopLossRate")),
        broker_target=_float(held[0].get("takeProfitRate")),
    )
    signal = trade.get("signal_price")
    if open_rate and signal:
        trade["entry_slippage_bps"] = round((open_rate / signal - 1.0) * 10_000, 1)
    # R is measured against the strategy stop from the actual fill.
    trade["fill_risk_pct"] = _pct(open_rate, trade.get("stop_loss"))


def _mark_closed(trade: dict[str, Any], rate: float, source: str) -> None:
    open_rate = trade.get("open_rate")
    stop, target = trade.get("stop_loss"), trade.get("take_profit")
    trade.update(status="closed", closed_seen_at=utc_now().isoformat(), close_rate=rate)
    trade["close_rate_source"] = source
    if source == "bot_backup_stop":
        reason = "bot_backup_stop"
    elif stop and rate <= stop * (1 + LEVEL_TOLERANCE):
        reason = "stop"
    elif target and rate >= target * (1 - LEVEL_TOLERANCE):
        reason = "target"
    else:
        reason = "other"
    trade["exit_reason"] = reason
    if open_rate:
        trade["return_pct"] = round((rate / open_rate - 1.0) * 100, 3)
        trade["pnl_usd"] = round(
            float(trade.get("notional_usd") or 0.0) * (rate / open_rate - 1.0), 2
        )
        if stop and open_rate > stop:
            trade["realized_r"] = round((rate - open_rate) / (open_rate - stop), 3)


def _held(positions: list[dict[str, Any]], trade: dict[str, Any], instrument_id: int) -> list:
    ids = trade.get("position_ids")
    if ids:  # once filled, follow those exact positions
        wanted = {str(i) for i in ids}
        return [p for p in positions if str(p.get("positionID")) in wanted]
    return [p for p in positions if int(p.get("instrumentID") or 0) == instrument_id]


def _instrument_id(client: Any, symbol: str) -> int | None:
    try:
        return int(client._search_instrument(symbol)["instrument_id"])
    except Exception:  # noqa: BLE001 - try again next tick
        return None


def _verdict(runtime_state: Any, strategy: Any, timeframe: Any) -> dict[str, Any]:
    from app.performance.strategy_evidence import VERDICTS_KEY

    try:
        cached = json.loads(runtime_state.get(VERDICTS_KEY) or "{}")
    except (TypeError, ValueError):
        return {}
    key = f"{strategy}:{str(timeframe or '').lower()}"
    return dict((cached.get("verdicts") or {}).get(key) or {})


def _r(expectancy_usd: Any) -> float | None:
    from app.performance.strategy_evidence import BACKTEST_RISK_USD

    value = _float(expectancy_usd)
    return None if value is None else round(value / BACKTEST_RISK_USD, 4)


def _ratio(target: Any, entry: Any, stop: Any) -> float | None:
    if not (target and entry and stop) or entry <= stop:
        return None
    return round((target - entry) / (entry - stop), 2)


def _pct(entry: Any, stop: Any) -> float | None:
    if not (entry and stop):
        return None
    return round((entry - stop) / entry * 100, 3)


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return out


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _lock(mirror: Any) -> Any:
    return getattr(mirror, "lock", None) or nullcontext()


def _load(mirror: Any) -> dict[str, Any]:
    try:
        value = json.loads(mirror.state.get(SCORECARD_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save(mirror: Any, cards: dict[str, Any]) -> None:
    mirror.state.set(SCORECARD_KEY, json.dumps(cards))
