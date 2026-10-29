"""Daily eToro LIVE Telegram report (operator request 2026-10-06: "start the Telegram report").

Once per US trading day, 10 minutes after the close (16:10 New York time, so it follows
daylight-saving changes), the guard thread sends one plain-text message:

* balance at cost, free cash and the day's change;
* trades opened and closed since the last report, with R, $ and the operator's copy share;
* open positions with R now (fresh eToro price) and their stop/target;
* live-vs-backtest verdicts per strategy (from the scorecard);
* events: the 2x test, paper exits copied, detached trades, backup-stop closes, a halt.

It sends at most once per New York date and never raises into the guard. A failed send is
retried every ``RETRY_MINUTES``; with Telegram disabled it skips before building the report
(2026-10-06: production had Telegram off, so the guard rebuilt the report and read eToro
prices every minute), logging the skip once per date.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

REPORT_KEY = "etoro_live:daily_report"
ATTEMPT_KEY = "etoro_live:daily_report_attempt"
RETRY_MINUTES = 15
NEW_YORK = ZoneInfo("America/New_York")
DUBAI = ZoneInfo("Asia/Dubai")
SEND_AFTER_NY = time(16, 10)
COPY_INVESTED_USD = 500.0  # the operator's real money copying the AlgoBot account


def maybe_send_daily_report(mirror: Any, *, now: datetime | None = None) -> bool:
    """Send today's report if it is due. True when a report went out this call."""

    now = now or utc_now()
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() >= 5 or ny.time() < SEND_AFTER_NY:
        return False
    today = ny.date().isoformat()
    sent = _load(mirror.state, REPORT_KEY)
    if sent.get("ny_date") == today:
        return False
    attempt = _load(mirror.state, ATTEMPT_KEY)
    if attempt.get("ny_date") != today:
        attempt = {}
    if not _notifier_enabled(mirror):
        if attempt.get("reason") != "telegram_disabled":
            _record_attempt(mirror, today, now, "telegram_disabled")
            mirror.logs.log(
                "etoro_live_daily_report_skipped", {"ny_date": today, "reason": "telegram_disabled"}
            )
        return False
    last = _parse(attempt.get("attempted_at")) if attempt.get("reason") == "send_failed" else None
    if last is not None and now - last < timedelta(minutes=RETRY_MINUTES):
        return False
    since = sent.get("sent_at") or (now - timedelta(hours=24)).isoformat()
    text = build_report(mirror, since=since, now=now)
    if not _send(mirror, text):
        _record_attempt(mirror, today, now, "send_failed")
        return False  # retried after RETRY_MINUTES
    mirror.state.set(REPORT_KEY, json.dumps({"ny_date": today, "sent_at": now.isoformat()}))
    mirror.logs.log("etoro_live_daily_report_sent", {"ny_date": today})
    return True


def build_report(mirror: Any, *, since: str, now: datetime | None = None) -> str:
    """The report text. Pure apart from fresh eToro price reads for open positions."""

    from app.broker.etoro_live_mirror import HALTED_KEY, STATE_KEY
    from app.broker.etoro_live_scorecard import SCORECARD_KEY, scorecard_report

    now = now or utc_now()
    live = _load(mirror.state, STATE_KEY)
    cards = _load(mirror.state, SCORECARD_KEY)
    eth = _load(mirror.state, "etoro_live:test_order")
    equity = _num(live.get("last_equity"))
    share = COPY_INVESTED_USD / equity if equity else 0.0

    ny, dubai = now.astimezone(NEW_YORK), now.astimezone(DUBAI)
    lines = [f"eToro LIVE daily report - US session {ny:%a %d %b} (sent {dubai:%H:%M} Dubai)"]
    start, cash = _num(live.get("day_start_equity")), _num(live.get("last_cash"))
    money = f"Balance at cost {_usd(equity)}"
    if cash is not None:
        money += f" | free cash {_usd(cash)}"
    if equity is not None and start is not None:
        money += f" | today {_signed(equity - start)}"
    lines.append(money)

    trades = list(cards.values())
    opened = [t for t in trades if str(t.get("opened_at") or "") >= since]
    closed = [t for t in trades if t.get("status") == "closed" and _closed_at(t) >= since]
    lines.append("")
    lines.append(f"Since the last report: {len(opened)} opened, {len(closed)} closed")
    for t in opened:
        lines.append(
            f"+ Opened {t.get('symbol')} {_usd(t.get('amount_usd'))} x{t.get('leverage') or 1}"
            f" ({t.get('strategy_name')}), stop {_px(t.get('stop_loss'))}"
            f" / target {_px(t.get('take_profit'))}"
        )
    for t in closed:
        pnl = _num(t.get("pnl_usd"))
        lines.append(
            f"- Closed {t.get('symbol')} {_r(t.get('realized_r'))} {_signed(pnl)}"
            f" ({_reason(t.get('exit_reason'))}), your copy {_signed(_mul(pnl, share))}"
        )

    open_rows = [t for t in trades if t.get("status") in {"open", "pending_fill"}]
    if eth.get("status") == "open":
        open_rows.append(
            {**eth, "strategy_name": "ETH test", "notional_usd": eth.get("amount_usd")}
        )
    lines.append("")
    lines.append(f"Open positions: {len(open_rows)}")
    for t in open_rows:
        rate = _fresh(mirror, str(t.get("symbol") or ""))
        r_now, pnl = _r_now(t, rate), _pnl_now(t, rate)
        note = " (before ~1% fee each way)" if t.get("strategy_name") == "ETH test" else ""
        detached = " [detached]" if t.get("exit_copy") == "detached" else ""
        lines.append(
            f"* {t.get('symbol')}{detached} {_r(r_now)} {_signed(pnl)}{note}"
            f" | stop {_px(t.get('stop_loss'))} / target {_px(t.get('take_profit'))}"
        )

    strategies = scorecard_report(mirror.state).get("strategies") or {}
    if strategies:
        lines.append("")
        lines.append("Live vs backtest:")
        for key, s in strategies.items():
            avg = s.get("avg_realized_r")
            live_part = f"live avg {_r(avg)}" if avg is not None else "no closed trades yet"
            lines.append(
                f"* {key}: {s.get('closed', 0)} closed, {live_part},"
                f" backtest {_r(s.get('expected_r'))} - {s.get('verdict')}"
            )

    events = []
    if any(int(t.get("leverage") or 1) > 1 for t in opened):
        events.append("2x leverage test placed")
    copies = sum(1 for t in closed if t.get("exit_reason") == "paper_exit")
    backups = sum(1 for t in closed if t.get("exit_reason") == "bot_backup_stop")
    detached_now = [t for t in trades if str(t.get("detached_at") or "") >= since]
    if copies:
        events.append(f"{copies} paper exit(s) copied")
    if backups:
        events.append(f"{backups} closed by the bot's backup stop")
    if detached_now:
        names = ", ".join(str(t.get("symbol")) for t in detached_now)
        events.append(f"detached after a paper safety flatten: {names}")
    halted = mirror.state.get(HALTED_KEY)
    if halted and str(halted).strip() not in {"", "null"}:
        events.append(f"LIVE TRADING HALTED: {_halt_reason(halted)}")
    lines.append("")
    lines.append("Events: " + ("; ".join(events) if events else "none"))
    lines.append(f"Your copy = {share * 100:.1f}% of each AlgoBot trade. P/L is the price move.")
    return "\n".join(lines)


def _notifier_enabled(mirror: Any) -> bool:
    notifier = getattr(mirror, "notifier", None)
    return notifier is not None and getattr(notifier, "enabled", True) is not False


def _record_attempt(mirror: Any, ny_date: str, now: datetime, reason: str) -> None:
    mirror.state.set(
        ATTEMPT_KEY,
        json.dumps({"ny_date": ny_date, "attempted_at": now.isoformat(), "reason": reason}),
    )


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _send(mirror: Any, text: str) -> bool:
    notifier = getattr(mirror, "notifier", None)
    send = getattr(notifier, "send_text", None) if notifier is not None else None
    if send is None:
        return False
    try:
        return send(text) is not False
    except Exception:  # noqa: BLE001 - retried next tick
        return False


def _fresh(mirror: Any, symbol: str) -> float | None:
    from app.broker.etoro_live_backup_stop import fresh_rate

    try:
        return fresh_rate(mirror.client, symbol)
    except Exception:  # noqa: BLE001 - shown as unknown
        return None


def _r_now(trade: dict[str, Any], rate: float | None) -> float | None:
    open_rate, stop = _num(trade.get("open_rate")), _num(trade.get("stop_loss"))
    if not (rate and open_rate and stop) or open_rate <= stop:
        return None
    return (rate - open_rate) / (open_rate - stop)


def _pnl_now(trade: dict[str, Any], rate: float | None) -> float | None:
    open_rate = _num(trade.get("open_rate"))
    notional = _num(trade.get("notional_usd")) or _num(trade.get("amount_usd"))
    if not (rate and open_rate and notional):
        return None
    return notional * (rate / open_rate - 1.0)


def _closed_at(trade: dict[str, Any]) -> str:
    return str(trade.get("closed_seen_at") or trade.get("exit_copied_at") or "")


def _halt_reason(value: Any) -> str:
    try:
        return str(json.loads(value).get("reason") or value)[:120]
    except (TypeError, ValueError, AttributeError):
        return str(value)[:120]


def _reason(value: Any) -> str:
    return str(value or "unknown").replace("_", " ")


def _load(state: Any, key: str) -> dict[str, Any]:
    try:
        value = json.loads(state.get(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mul(a: float | None, b: float) -> float | None:
    return None if a is None else a * b


def _usd(value: Any) -> str:
    n = _num(value)
    return "n/a" if n is None else f"${n:,.0f}" if abs(n) >= 100 else f"${n:,.2f}"


def _signed(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _px(value: Any) -> str:
    n = _num(value)
    return "n/a" if n is None else f"{n:,.2f}"


def _r(value: Any) -> str:
    n = _num(value)
    return "n/a" if n is None else f"{n:+.2f}R"
