"""Run one outcome-ledger cycle from the command line.

This polls your real eToro portfolio, links newly-opened positions to any
pending bot alerts, marks closed positions as outcomes with realized PnL
and R-multiple, and expires old unmatched alerts.

Run:

    python3 scripts/run_ledger_cycle.py

Useful flags:

    --summary              Print aggregate ledger stats after the cycle.
    --recent N             Show the last N outcomes (default 10).
    --fake-alert SYMBOL    Inject a fake pending_match alert for the given
                           symbol. Handy for end-to-end testing: alert now,
                           open a tiny trade on eToro, rerun the script.
    --match-existing SYMBOL
                           Test-only: inject an alert and link it to an
                           already-open portfolio position. This does not
                           place or modify any trade.
    --position-id ID       With --match-existing, choose a specific position
                           when the symbol has multiple open positions.
    --entry / --stop / --target  (Only with --fake-alert) set price levels
                                 so the eventual R-multiple is meaningful.

Typical operator loop:

    python3 scripts/run_ledger_cycle.py --summary --recent 20

Cron it every 5-15 minutes so the ledger stays fresh.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.broker.etoro_client import EToroClient
from app.ledger.repository import LedgerRepository
from app.ledger.service import LedgerService
from app.runtime_settings import get_settings
from app.storage.db import Database
from app.utils.time import utc_now


def _fmt_currency(value: float | None) -> str:
    if value is None:
        return "--"
    return f"${value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:.1f}%"


def _fmt_r(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}R"


def _print_recent(repo: LedgerRepository, limit: int) -> None:
    rows = repo.recent_outcomes(limit=limit)
    if not rows:
        print("\n(no outcomes yet)")
        return
    print(f"\nRecent outcomes (last {len(rows)}):")
    print(f"{'id':>4}  {'symbol':<7} {'strategy':<22} {'status':<18} "
          f"{'entry':>9} {'close':>9} {'pnl':>10} {'R':>8}  alert_at")
    for row in rows:
        print(
            f"{row.get('id'):>4}  "
            f"{(row.get('symbol') or ''):<7} "
            f"{(row.get('strategy_name') or '-'):<22.22} "
            f"{(row.get('outcome_status') or ''):<18} "
            f"{_safe_num(row.get('alert_entry_price')):>9} "
            f"{_safe_num(row.get('close_rate')):>9} "
            f"{_fmt_currency(row.get('realized_pnl_usd')):>10} "
            f"{_fmt_r(row.get('realized_r_multiple')):>8}  "
            f"{row.get('alert_created_at')}"
        )


def _safe_num(value: object) -> str:
    if value in (None, ""):
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _print_summary(repo: LedgerRepository) -> None:
    s = repo.summary_stats()
    print("\nLedger summary")
    print(f"  total outcomes   : {s['total_outcomes']}")
    print(f"  by status        : {s['by_status']}")
    print(f"  closed           : {s['closed_count']}")
    print(f"  wins / losses    : {s['wins']} / {s['losses']}")
    print(f"  win rate         : {_fmt_pct(s['win_rate'])}")
    print(f"  total pnl (USD)  : {_fmt_currency(s['total_realized_pnl_usd'])}")
    print(f"  avg R-multiple   : {_fmt_r(s['avg_r_multiple'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one ledger cycle.")
    parser.add_argument("--summary", action="store_true", help="Print aggregate stats.")
    parser.add_argument("--recent", type=int, default=10, help="Show last N outcomes.")
    parser.add_argument("--fake-alert", metavar="SYMBOL", default=None,
                        help="Inject a fake pending alert for an end-to-end test.")
    parser.add_argument("--match-existing", metavar="SYMBOL", default=None,
                        help="Test-only: link a fake alert to an already-open position. Does not trade.")
    parser.add_argument("--position-id", type=int, default=None,
                        help="Specific eToro positionID to link when using --match-existing.")
    parser.add_argument("--entry", type=float, default=None)
    parser.add_argument("--stop", type=float, default=None)
    parser.add_argument("--target", type=float, default=None)
    parser.add_argument("--strategy", default="manual_test")
    parser.add_argument("--timeframe", default="1d")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()

    if settings.broker_simulation_enabled:
        print(
            "WARNING: broker_simulation_enabled is true — snapshot will return a "
            "fake empty portfolio. Check your .env (ETORO_API_KEY / ETORO_USER_KEY)."
        )

    broker = EToroClient(settings)
    repo = LedgerRepository(db)
    service = LedgerService(
        settings=settings,
        broker=broker,
        repository=repo,
        database=db,
    )

    if args.fake_alert:
        outcome_id = service.record_alert(
            symbol=args.fake_alert.upper(),
            strategy_name=args.strategy,
            timeframe=args.timeframe,
            alert_created_at=utc_now(),
            alert_entry_price=args.entry,
            alert_stop=args.stop,
            alert_target=args.target,
            alert_score=None,
            alert_source="manual_test",
        )
        print(f"Fake alert recorded as outcome #{outcome_id} "
              f"(symbol={args.fake_alert.upper()}, strategy={args.strategy})")

    if args.match_existing:
        outcome_id, matched = _record_and_match_existing(
            service=service,
            repo=repo,
            symbol=args.match_existing.upper(),
            position_id=args.position_id,
            strategy=args.strategy,
            timeframe=args.timeframe,
            entry=args.entry,
            stop=args.stop,
            target=args.target,
        )
        print(
            f"Existing-position test alert recorded as outcome #{outcome_id} "
            f"and linked to position #{matched['position_id']} "
            f"(symbol={args.match_existing.upper()}, strategy={args.strategy})."
        )

    result = service.run_cycle()
    print("\nCycle result:")
    print(json.dumps(result, indent=2, default=str))

    if args.summary:
        _print_summary(repo)

    if args.recent:
        _print_recent(repo, limit=args.recent)

    return 0


def _record_and_match_existing(
    *,
    service: LedgerService,
    repo: LedgerRepository,
    symbol: str,
    position_id: int | None,
    strategy: str,
    timeframe: str,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> tuple[int, dict]:
    """Create a test outcome and link it to an already-open position.

    This is deliberately limited to the ledger. It never places, closes, or
    modifies an eToro trade.
    """
    snapshot = service.snapshot_portfolio()
    matches = [
        position
        for position in (snapshot.get("positions") or [])
        if str(position.get("symbol") or "").upper() == symbol
    ]
    if position_id is not None:
        matches = [
            position
            for position in matches
            if int(position.get("position_id") or 0) == int(position_id)
        ]
    if not matches:
        raise SystemExit(
            f"No open position found for {symbol}"
            + (f" with position_id={position_id}" if position_id is not None else "")
            + "."
        )
    matches = sorted(matches, key=lambda item: str(item.get("open_datetime") or ""), reverse=True)
    position = matches[0]
    if repo.position_already_matched(int(position["position_id"])):
        raise SystemExit(f"Position {position['position_id']} is already linked to a ledger outcome.")

    alert_entry = entry if entry is not None else float(position.get("open_rate") or 0.0)
    alert_stop = stop if stop is not None else position.get("stop_loss_rate")
    alert_target = target if target is not None else position.get("take_profit_rate")
    outcome_id = service.record_alert(
        symbol=symbol,
        strategy_name=strategy,
        timeframe=timeframe,
        alert_created_at=utc_now(),
        alert_entry_price=alert_entry,
        alert_stop=alert_stop,
        alert_target=alert_target,
        alert_score=None,
        alert_source="manual_existing_test",
        alert_id=f"manual_existing_test:{symbol}:{position['position_id']}",
        alert_payload={
            "test_only": True,
            "matched_existing_position": True,
            "position_id": position["position_id"],
            "note": "Linked to an already-open eToro position for ledger plumbing validation only.",
        },
    )
    repo.mark_matched(
        outcome_id=outcome_id,
        position_id=int(position["position_id"]),
        position_open_at=position.get("open_datetime"),
        position_open_rate=float(position.get("open_rate") or 0.0),
        position_amount_usd=position.get("amount_usd"),
        position_units=float(position.get("units") or 0.0),
        position_is_buy=bool(position.get("is_buy", True)),
        position_leverage=int(position.get("leverage") or 1),
        position_stop_loss_rate=position.get("stop_loss_rate"),
        position_take_profit_rate=position.get("take_profit_rate"),
    )
    return outcome_id, position


if __name__ == "__main__":
    sys.exit(main())
