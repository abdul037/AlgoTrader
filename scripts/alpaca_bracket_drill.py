"""Controlled real Alpaca Paper bracket-order and idempotency drill."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.broker.alpaca_client import AlpacaClient
from app.runtime_settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("Pass --confirm to submit a real Alpaca Paper bracket drill.")

    settings = get_settings()
    if settings.execution_mode != "paper" or settings.enable_real_trading:
        raise SystemExit("Drill requires paper mode with real trading disabled.")
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_base_url,
        data_url=settings.alpaca_data_url,
        paper=True,
        data_feed=settings.alpaca_data_feed,
    )
    identity = client.get_account_identity()
    expected = settings.alpaca_expected_account_number
    if not expected or identity["account_number"] != expected:
        raise SystemExit(
            f"Account verification failed: expected={expected!r} actual={identity['account_number']!r}"
        )
    if not client.is_regular_market_open():
        raise SystemExit("US regular market is closed.")
    if client.get_portfolio().positions:
        raise SystemExit("Drill requires a flat dedicated paper account.")

    quote = client.get_quote(args.symbol, force_refresh=True)
    price = float(quote.ask or quote.last_execution or 0.0)
    if price <= 0:
        raise SystemExit("No usable Alpaca quote.")
    client_order_id = "bracket_drill_" + hashlib.sha256(
        datetime.now(UTC).isoformat().encode()
    ).hexdigest()[:16]
    first = client.submit_bracket_order(
        symbol=args.symbol,
        side="buy",
        qty=1,
        stop_loss_price=round(price * 0.98, 2),
        take_profit_price=round(price * 1.02, 2),
        client_order_id=client_order_id,
    )
    print(f"submitted={first.broker_order_id} client_order_id={client_order_id}")
    time.sleep(3)
    duplicate = client.submit_bracket_order(
        symbol=args.symbol,
        side="buy",
        qty=1,
        stop_loss_price=round(price * 0.98, 2),
        take_profit_price=round(price * 1.02, 2),
        client_order_id=client_order_id,
    )
    print(f"idempotency_first={first.broker_order_id} duplicate={duplicate.broker_order_id}")
    if duplicate.broker_order_id != first.broker_order_id:
        raise RuntimeError("Idempotency failure: duplicate bracket order was created")

    nested = client.get_order(first.broker_order_id)
    legs = list(nested.response_payload.get("legs") or [])
    print(f"parent_status={nested.response_payload.get('status')} protective_legs={len(legs)}")
    if len(legs) < 2:
        raise RuntimeError("Bracket protection legs were not present")

    client.cancel_all_orders()
    client.close_all_positions()
    time.sleep(3)
    open_orders = client.trading_client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
    )
    positions = client.trading_client.get_all_positions()
    print(f"final_open_orders={len(open_orders)} final_positions={len(positions)}")
    if open_orders or positions:
        raise RuntimeError("Kill-switch cleanup did not return the account to flat")
    print("ALPACA PAPER BRACKET DRILL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
