"""Leverage simulation of closed paper trades, and the exploration leaderboard (Phase 1, 2026-11-10).

Paper trades at 1x. The operator wants to see what each strategy's trades would have made with
eToro leverage before any real-money leveraged lane exists (that lane comes later, only for a
strategy proven here). For every closed paper trade, exploration and normal alike, this computes
the result at ``LEVERAGES`` (1x, 2x, 5x, 10x) of the SAME margin (the 1x trade's entry notional):

* price result: the paper trade's realized P/L x L (paper's own fills, so its spread and slippage
  scale with the exposure, as they would);
* overnight financing (L > 1 only: a leveraged eToro position is a CFD; a 1x real stock pays none):
  the cost model's daily rate (``CostModel.overnight_fee_daily_pct``, 0.015%) on the leveraged
  notional (L x margin) for every night held, a Friday night ``weekend_multiplier`` (3) times and
  Saturday and Sunday nights not again (eToro's weekend charge); New York dates, US holidays
  counted as ordinary nights;
* forced close: when the worst price during the hold moved 1/L or more against the entry (the
  loss reached the margin), the position is closed with the whole margin lost (plus financing up
  to the time of that worst mark, or the whole hold when that time is unknown).

The worst price comes from marks the paper refresh samples about every minute while a position is
open (``track_marks``: no extra market-data reads), with the entry and exit prices. It is
``approximate`` unless the marks cover the whole hold (``worst_basis`` "marks"): a move between two
marks is missed, a pre- or after-market print is counted, and a trade closed before the marks
existed (or opened before them) has only its entry and exit ("entry_exit", "partial_marks").

Not modelled (approximations, stated): eToro's own spread (paper's Alpaca fills are used), eToro's
$1 a leg on a real 1x stock, financing rates other than the cost model's, the margin call before a
full stop-out, and the order in which a stop and a forced close would have been hit within a bar.

R = the simulated result over the 1x trade's dollar risk at its stop (|entry - stop| x quantity):
at 2x the stop costs 2R. Results per trade ride in its closed trade's payload (``SIM_FIELD``,
written by the paper ledger when the trade closes; older trades are simulated on the fly).

The leaderboard (``LEADERBOARD_KEY``, refreshed hourly in maintenance) aggregates per lane
(exploration, OOS-only paper-only, normal) and ``strategy:timeframe``: trades, win rate, total and
mean $ and R, worst drawdown, forced closes and financing per leverage, and whether the
exploration rows meet the PROVISIONAL proof rule (``PROOF_MIN_TRADES`` closed exploration trades
with a positive total $ and a positive mean R after the simulated costs, at that leverage; the
operator confirms the exact rule later). Display only: nothing trades on it.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

LEVERAGES = (1, 2, 5, 10)
SIM_FIELD = "leverage_sim"
SIM_VERSION = 1
LEADERBOARD_KEY = "paper_exploration:leaderboard"
REFRESHED_KEY = "paper_exploration:leaderboard_refreshed_at"
LEADERBOARD_REFRESH_MINUTES = 60
TRADE_ROWS = 2000  # the newest closed paper trades, as the paper ledger's own summary reads them
PROOF_MIN_TRADES = 30
LANES = ("exploration", "oos_only", "normal")
# Marks the paper refresh keeps in an open position's payload (they ride into its closed trade).
WORST_PRICE_FIELD = "worst_price"
WORST_AT_FIELD = "worst_at"
BEST_PRICE_FIELD = "best_price"
MARKS_SINCE_FIELD = "marks_since"
MARKS_COVER_MINUTES = 10.0  # marks from at most this long after the open cover the whole hold
MAX_NIGHTS_SPAN_DAYS = 400
NEW_YORK = ZoneInfo("America/New_York")


def track_marks(position: Any, price: float, *, at: datetime | None = None) -> None:
    """Keep the most adverse and the best price seen while ``position`` is open in its payload
    (a long's lowest and highest mark; a short's highest and lowest). Never raises."""

    try:
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            return
        payload = position.payload if isinstance(position.payload, dict) else {}
        short = str(getattr(position, "side", "") or "").lower() == "sell"
        stamp = (at or utc_now()).isoformat()
        worst = _number(payload.get(WORST_PRICE_FIELD))
        best = _number(payload.get(BEST_PRICE_FIELD))
        if worst is None or (price > worst if short else price < worst):
            payload[WORST_PRICE_FIELD] = round(price, 6)
            payload[WORST_AT_FIELD] = stamp
        if best is None or (price < best if short else price > best):
            payload[BEST_PRICE_FIELD] = round(price, 6)
        payload.setdefault(MARKS_SINCE_FIELD, stamp)
        position.payload = payload
    except Exception:  # noqa: BLE001 - a mark must never disturb the paper refresh
        return


def financing_nights(opened_at: datetime, closed_at: datetime, *, weekend_multiplier: float) -> float:
    """eToro overnight charges between the two times, in nights: one for each Monday to Thursday
    night held (New York dates), ``weekend_multiplier`` for a Friday night, none for Saturday and
    Sunday (Friday's charge covers them)."""

    start = opened_at.astimezone(NEW_YORK).date()
    end = closed_at.astimezone(NEW_YORK).date()
    if end <= start:
        return 0.0
    end = min(end, start + timedelta(days=MAX_NIGHTS_SPAN_DAYS))
    nights = 0.0
    day = start
    while day < end:
        weekday = day.weekday()
        nights += 1.0 if weekday < 4 else float(weekend_multiplier) if weekday == 4 else 0.0
        day += timedelta(days=1)
    return nights


def simulate(
    *,
    side: str,
    entry_price: Any,
    exit_price: Any,
    quantity: Any,
    opened_at: Any,
    closed_at: Any,
    pnl_usd: Any = None,
    stop_loss: Any = None,
    risk_usd: Any = None,
    worst_price: Any = None,
    worst_at: Any = None,
    marks_since: Any = None,
) -> dict[str, Any] | None:
    """One closed trade at every leverage in ``LEVERAGES`` (see the module docstring); None when
    its entry, exit, quantity or times cannot be read."""

    from app.backtesting.cost_model import CostModel

    entry, exit_, qty = _number(entry_price), _number(exit_price), _number(quantity)
    opened, closed = _time(opened_at), _time(closed_at)
    if not entry or entry <= 0 or not exit_ or exit_ <= 0 or not qty or qty <= 0:
        return None
    if opened is None or closed is None or closed < opened:
        return None
    short = str(side or "").lower() == "sell"
    margin = entry * qty
    pnl = _number(pnl_usd)
    if pnl is None:
        pnl = (entry - exit_) * qty if short else (exit_ - entry) * qty
    risk = _number(risk_usd)
    stop = _number(stop_loss)
    if risk is None and stop is not None and stop > 0 and stop != entry:
        risk = abs(entry - stop) * qty
    risk = risk if risk is not None and risk > 0 else None
    worst, basis, worst_time = _worst(short, entry, exit_, opened, worst_price, worst_at, marks_since)
    adverse = max((worst - entry) / entry if short else (entry - worst) / entry, 0.0)
    model = CostModel()
    rate, weekend = float(model.overnight_fee_daily_pct), float(model.weekend_multiplier)
    nights = financing_nights(opened, closed, weekend_multiplier=weekend)
    by_leverage: dict[str, dict[str, Any]] = {}
    for leverage in LEVERAGES:
        forced = leverage > 1 and adverse >= 1.0 / leverage
        gross = -margin if forced else pnl * leverage
        held = nights
        if forced and worst_time is not None:
            held = financing_nights(opened, worst_time, weekend_multiplier=weekend)
        financing = margin * leverage * rate * held if leverage > 1 else 0.0
        net = gross - financing
        by_leverage[str(leverage)] = {
            "pnl_usd": round(net, 2),
            "gross_usd": round(gross, 2),
            "financing_usd": round(financing, 2),
            "r": round(net / risk, 4) if risk else None,
            "forced_close": forced,
        }
    return {
        "version": SIM_VERSION,
        "margin_usd": round(margin, 2),
        "pnl_1x_usd": round(pnl, 2),
        "risk_usd": round(risk, 2) if risk else None,
        "nights": nights,
        "worst_price": round(worst, 6),
        "worst_basis": basis,
        "adverse_move_pct": round(adverse * 100.0, 4),
        "approximate": basis != "marks",
        "by_leverage": by_leverage,
    }


def simulate_position_close(position: Any, *, exit_price: float, pnl_usd: float, closed_at: Any) -> dict[str, Any] | None:
    """``simulate`` for a paper position being closed (the paper ledger's close). Never raises."""

    try:
        payload = position.payload if isinstance(position.payload, dict) else {}
        return simulate(
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            opened_at=position.opened_at,
            closed_at=closed_at,
            pnl_usd=pnl_usd,
            stop_loss=position.stop_loss,
            worst_price=payload.get(WORST_PRICE_FIELD),
            worst_at=payload.get(WORST_AT_FIELD),
            marks_since=payload.get(MARKS_SINCE_FIELD),
        )
    except Exception:  # noqa: BLE001 - the leaderboard simulates it again later
        return None


def trade_simulation(trade: Any) -> dict[str, Any] | None:
    """A closed trade's stored simulation, else one computed now from its row (R from its
    ``realized_r_multiple`` when its stop is not stored)."""

    payload = getattr(trade, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    stored = payload.get(SIM_FIELD)
    if isinstance(stored, dict) and stored.get("version") == SIM_VERSION:
        return stored
    pnl = _number(getattr(trade, "realized_pnl_usd", None))
    r_multiple = _number(payload.get("realized_r_multiple"))
    risk = abs(pnl) / abs(r_multiple) if pnl is not None and r_multiple else None
    return simulate(
        side=getattr(trade, "side", "buy"),
        entry_price=getattr(trade, "entry_price", None),
        exit_price=getattr(trade, "exit_price", None),
        quantity=getattr(trade, "quantity", None),
        opened_at=getattr(trade, "opened_at", None),
        closed_at=getattr(trade, "closed_at", None),
        pnl_usd=pnl,
        risk_usd=risk,
        worst_price=payload.get(WORST_PRICE_FIELD),
        worst_at=payload.get(WORST_AT_FIELD),
        marks_since=payload.get(MARKS_SINCE_FIELD),
    )


def lane_of(payload: Any) -> str:
    from app.performance.paper_only import is_exploration, is_paper_only

    if is_exploration(payload):
        return "exploration"
    return "oos_only" if is_paper_only(payload) else "normal"


def compute_leaderboard(trades: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The leaderboard from closed paper trades (see the module docstring); JSON-safe."""

    from app.backtesting.cost_model import CostModel

    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    read = skipped = 0
    for trade in trades:
        read += 1
        strategy = str(getattr(trade, "strategy_name", "") or "").strip()
        if not strategy:
            skipped += 1
            continue
        sim = trade_simulation(trade)
        if sim is None:
            skipped += 1
            continue
        key = f"{strategy}:{str(getattr(trade, 'timeframe', '') or '').strip().lower()}"
        lane = lane_of(getattr(trade, "payload", None))
        groups.setdefault((lane, key), []).append((str(getattr(trade, "closed_at", "") or ""), sim))
    lanes: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
    for (lane, key), items in groups.items():
        items.sort(key=lambda item: item[0])
        lanes[lane].append(_row(lane, key, items))
    for rows in lanes.values():
        rows.sort(key=_rank)
    model = CostModel()
    return {
        "version": SIM_VERSION,
        "computed_at": (now or utc_now()).isoformat(),
        "leverages": list(LEVERAGES),
        "proof_rule": {
            "provisional": True,
            "lane": "exploration",
            "min_closed_trades": PROOF_MIN_TRADES,
            "rule": "at that leverage: total $ > 0 and mean R > 0 after simulated costs",
        },
        "cost_basis": {
            "financing_daily_pct": round(float(model.overnight_fee_daily_pct) * 100.0, 6),
            "weekend_multiplier": float(model.weekend_multiplier),
            "financing_at_1x": False,
            "forced_close": "adverse move of 1/L against the entry: the margin is lost",
            "worst_price": "marks sampled by the paper refresh (about every minute) plus entry and exit",
        },
        "trades_read": read,
        "trades_skipped": skipped,
        "lanes": lanes,
    }


def refresh_leaderboard(service: Any, *, force: bool = False) -> dict[str, Any] | None:
    """Recompute the leaderboard into ``LEADERBOARD_KEY`` when an hour has passed (maintenance);
    None when not due or without a paper ledger."""

    from app.workflow.schedule import is_due

    trades_repo = getattr(getattr(service, "paper_trading", None), "trades", None)
    if trades_repo is None or not hasattr(trades_repo, "list"):
        return None
    if not force and not is_due(service, REFRESHED_KEY, LEADERBOARD_REFRESH_MINUTES):
        return None
    now = utc_now()
    board = compute_leaderboard(trades_repo.list(limit=TRADE_ROWS), now=now)
    service.runtime_state.set(LEADERBOARD_KEY, json.dumps(board, allow_nan=False))
    service.runtime_state.set(REFRESHED_KEY, now.isoformat())
    return board


def _row(lane: str, key: str, items: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    strategy, _, timeframe = key.rpartition(":")
    by_leverage: dict[str, dict[str, Any]] = {}
    proof: dict[str, bool | None] = {}
    for leverage in LEVERAGES:
        results = [sim["by_leverage"].get(str(leverage)) or {} for _, sim in items]
        pnls = [float(result.get("pnl_usd") or 0.0) for result in results]
        rs = [float(result["r"]) for result in results if _number(result.get("r")) is not None]
        n = len(pnls)
        total, total_r = sum(pnls), sum(rs)
        mean_r = total_r / len(rs) if rs else None
        stats = {
            "trades": n,
            "wins": sum(1 for pnl in pnls if pnl > 0),
            "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / n * 100.0, 2) if n else None,
            "total_usd": round(total, 2),
            "mean_usd": round(total / n, 2) if n else None,
            "total_r": round(total_r, 4),
            "mean_r": round(mean_r, 4) if mean_r is not None else None,
            "r_trades": len(rs),
            "max_drawdown_usd": round(_max_drawdown(pnls), 2),
            "forced_closes": sum(1 for result in results if result.get("forced_close")),
            "financing_usd": round(sum(float(result.get("financing_usd") or 0.0) for result in results), 2),
        }
        meets = None
        if lane == "exploration":
            meets = n >= PROOF_MIN_TRADES and total > 0 and mean_r is not None and mean_r > 0
            stats["meets_proof_rule"] = meets
        by_leverage[str(leverage)] = stats
        proof[str(leverage)] = meets
    return {
        "key": key,
        "strategy": strategy,
        "timeframe": timeframe,
        "lane": lane,
        "trades": len(items),
        "approximate_trades": sum(1 for _, sim in items if sim.get("approximate")),
        "first_closed_at": items[0][0] if items else None,
        "last_closed_at": items[-1][0] if items else None,
        "meets_proof_rule": proof,
        "proven_leverages": [int(lev) for lev, ok in proof.items() if ok],
        "by_leverage": by_leverage,
    }


def _rank(row: dict[str, Any]) -> tuple[Any, ...]:
    one = row["by_leverage"].get("1") or {}
    mean_r = one.get("mean_r")
    return (-len(row["proven_leverages"]), -(mean_r if mean_r is not None else -math.inf), -row["trades"], row["key"])


def _max_drawdown(pnls: list[float]) -> float:
    equity = peak = worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def _worst(
    short: bool, entry: float, exit_: float, opened: datetime, worst_price: Any, worst_at: Any, marks_since: Any
) -> tuple[float, str, datetime | None]:
    """The most adverse price of the hold, how it is known, and when (None when unknown)."""

    mark = _number(worst_price)
    candidates = [entry, exit_] + ([mark] if mark is not None and mark > 0 else [])
    worst = max(candidates) if short else min(candidates)
    if mark is None or mark <= 0:
        return worst, "entry_exit", None
    since = _time(marks_since)
    covered = since is not None and (since - opened).total_seconds() <= MARKS_COVER_MINUTES * 60.0
    when = _time(worst_at) if worst == mark else None
    return worst, "marks" if covered else "partial_marks", when


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
