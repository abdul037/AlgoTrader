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

A close is detected when a position that was seen disappears. Its price is eToro's own fill
from the trade history (``GET /trading/info/trade/history``) when the position is there;
otherwise the bot's own close price when the backup stop closed it, or a fresh eToro rate
read at detection (within one guard tick, 60 s). Those estimates are re-read from the history
every ``REFINE_EVERY_MINUTES`` for ``REFINE_DAYS`` (2026-10-16: AMD was booked at the 539.00
seen a minute after eToro's stop filled at 541.53, and COST as "other" at 966.82 after its
target filled at 972.08).
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
HISTORY_SOURCE = "etoro_history"
REFINE_DAYS = 7
REFINE_EVERY_MINUTES = 15
HISTORY_PAGE_SIZE = 100
HISTORY_MAX_PAGES = 5
_BOT_EXITS = {"bot_backup_stop", "paper_exit"}  # the reason is known even when the price is not
_ESTIMATED_SOURCES = {"rate_when_seen_closed", *_BOT_EXITS}


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
        refinable = {k: t for k, t in cards.items() if _refinable(t)}
        if not (active or refinable) or client_problem(mirror.client):
            return 0
        client = mirror.client
        history = _History(client, [*active.values(), *refinable.values()])
        changed = 0
        for trade in refinable.values():
            if _refine(trade, history.close(trade.get("position_ids"))):
                changed += 1
                mirror.logs.log("etoro_live_scorecard_refined", {**trade})
        positions = portfolio_positions(client.fetch_raw_portfolio()) if active else None
        if positions is None:
            active, positions = {}, []  # a malformed read changes no open trade
        closed_by_bot = {str(c.get("symbol") or "").upper(): c for c in bot_closes or []}
        stale = (utc_now() - timedelta(hours=NOT_FILLED_AFTER_HOURS)).isoformat()
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
            filled = None if bot is not None else history.close(trade.get("position_ids"))
            if bot is not None:
                rate, source = _float(bot.get("rate")), "bot_backup_stop"
            elif filled is not None:
                rate, source = filled["rate"], HISTORY_SOURCE
            else:
                try:
                    rate, source = fresh_rate(client, trade["symbol"]), "rate_when_seen_closed"
                except Exception:  # noqa: BLE001 - retry next tick
                    rate, source = None, ""
            if rate is None:
                continue
            _mark_closed(trade, rate, source)
            trade.update(_etoro_fields(filled))
            if source != HISTORY_SOURCE:  # re-read once eToro has had time to list it
                trade["history_checked_at"] = trade["closed_seen_at"]
            changed += 1
            mirror.logs.log("etoro_live_scorecard_closed", {**trade})
        if changed or refinable:  # a refinable card also carries its new check time
            current = _load(mirror)
            current.update({k: t for k, t in {**refinable, **active}.items() if k in current})
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
    trade.update(status="closed", closed_seen_at=utc_now().isoformat(), close_rate=rate)
    trade["close_rate_source"] = source
    trade["exit_reason"] = source if source in _BOT_EXITS else _level_reason(trade, rate)
    _book(trade, rate)


def _refine(trade: dict[str, Any], filled: dict[str, Any] | None) -> bool:
    """Re-book an estimated close at eToro's own fill; True when the trade changed."""

    trade["history_checked_at"] = utc_now().isoformat()
    if filled is None:
        return False
    trade["seen_close_rate"] = trade.get("close_rate")
    trade["close_rate"] = filled["rate"]
    if trade.get("exit_reason") not in _BOT_EXITS:
        trade["exit_reason"] = _level_reason(trade, filled["rate"])
    trade["close_rate_source"] = HISTORY_SOURCE
    trade.update(_etoro_fields(filled))
    _book(trade, filled["rate"])
    return True


def _level_reason(trade: dict[str, Any], rate: float) -> str:
    stop, target = trade.get("stop_loss"), trade.get("take_profit")
    if stop and rate <= stop * (1 + LEVEL_TOLERANCE):
        return "stop"
    if target and rate >= target * (1 - LEVEL_TOLERANCE):
        return "target"
    return "other"


def _book(trade: dict[str, Any], rate: float) -> None:
    open_rate, stop = trade.get("open_rate"), trade.get("stop_loss")
    if open_rate:
        trade["return_pct"] = round((rate / open_rate - 1.0) * 100, 3)
        trade["pnl_usd"] = round(
            float(trade.get("notional_usd") or 0.0) * (rate / open_rate - 1.0), 2
        )
        if stop and open_rate > stop:
            trade["realized_r"] = round((rate - open_rate) / (open_rate - stop), 3)


def _refinable(trade: dict[str, Any]) -> bool:
    """A recent close booked from an estimate, not checked against the history lately."""

    if trade.get("status") != "closed" or trade.get("close_rate_source") not in _ESTIMATED_SOURCES:
        return False
    if not trade.get("position_ids"):
        return False
    now, closed = utc_now(), _when(trade.get("closed_seen_at"))
    if closed is None or now - closed > timedelta(days=REFINE_DAYS):
        return False
    checked = _when(trade.get("history_checked_at"))
    return checked is None or now - checked >= timedelta(minutes=REFINE_EVERY_MINUTES)


class _History:
    """eToro's closed positions, read at most once per update and only when one is needed."""

    def __init__(self, client: Any, trades: list[dict[str, Any]]) -> None:
        self._client = client
        dates = sorted(str(t.get("opened_at") or "")[:10] for t in trades if t.get("opened_at"))
        floor = (utc_now() - timedelta(days=364)).date().isoformat()  # eToro: under a year
        self._since = max(dates[0], floor) if dates else None
        self._rows: dict[str, dict[str, Any]] | None = None

    def close(self, position_ids: Any) -> dict[str, Any] | None:
        """eToro's fill for every one of these positions (units-weighted), or None."""

        ids = [str(i) for i in position_ids or []]
        if not ids or self._since is None:
            return None
        rows = self._load()
        found = [rows[i] for i in ids if i in rows]
        rates = [_float(r.get("closeRate")) for r in found]
        if len(found) != len(ids) or any(rate is None for rate in rates):
            return None  # not (all) closed yet, or no usable rate: keep the estimate
        units = [_float(r.get("units")) or 0.0 for r in found]
        total = sum(units)
        weighted = sum(u * float(r or 0.0) for u, r in zip(units, rates, strict=True))
        rate = weighted / total if total else sum(float(r or 0.0) for r in rates) / len(rates)
        return {
            "rate": round(rate, 6),
            "closed_at": max(str(r.get("closeTimestamp") or "") for r in found) or None,
            "net_profit_usd": round(sum(_signed(r.get("netProfit")) for r in found), 2),
            "fees_usd": round(sum(_signed(r.get("fees")) for r in found), 2),
        }

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._rows is not None:
            return self._rows
        rows: dict[str, dict[str, Any]] = {}
        try:
            for page in range(1, HISTORY_MAX_PAGES + 1):
                batch = self._client.fetch_trade_history(
                    self._since, page=page, page_size=HISTORY_PAGE_SIZE
                )
                rows.update({str(r["positionId"]): r for r in batch if r.get("positionId")})
                if len(batch) < HISTORY_PAGE_SIZE:
                    break
        except Exception:  # noqa: BLE001 - no history this tick: estimates stand, retried later
            rows = {}
        self._rows = rows
        return rows


def _etoro_fields(filled: dict[str, Any] | None) -> dict[str, Any]:
    if filled is None:
        return {}
    return {
        "etoro_closed_at": filled["closed_at"],
        "etoro_net_profit_usd": filled["net_profit_usd"],
        "etoro_fees_usd": filled["fees_usd"],
    }


def _when(value: Any) -> Any:
    from datetime import UTC, datetime

    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=UTC) if moment and moment.tzinfo is None else moment


def _signed(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


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
