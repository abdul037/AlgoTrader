"""Manual Alpaca paper smoke test.

This script intentionally hits the live Alpaca paper API. Do not run it from
CI. Add ALPACA_API_KEY and ALPACA_SECRET_KEY to your local .env before running.
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.broker.alpaca_client import AlpacaClient
from app.runtime_settings import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        print("SMOKE TEST FAILED: ALPACA_API_KEY and ALPACA_SECRET_KEY are required.")
        return 1

    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_base_url,
        data_url=settings.alpaca_data_url,
        paper=True,
        data_feed=settings.alpaca_data_feed,
    )

    client_order_id = "smoke_" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
    broker_order_id: str | None = None
    submitted = False

    try:
        portfolio = client.get_portfolio()
        account = portfolio.account
        account_id = str(getattr(client.trading_client.get_account(), "id", "n/a"))
        print(
            "Account: "
            f"equity={account.equity} cash={account.cash_balance} "
            f"buying_power={getattr(account, 'buying_power', 'n/a')} "
            f"day_trade_count={getattr(account, 'day_trade_count', 'n/a')} "
            f"account_id_first_4_chars={account_id[:4]}"
        )

        quote = client.get_quote("AAPL", force_refresh=True)
        print(
            "AAPL quote: "
            f"bid={quote.bid} ask={quote.ask} last={quote.last_execution} "
            f"timestamp={quote.timestamp} source={quote.source}"
        )

        end = datetime.now(UTC)
        start = end - timedelta(days=30)
        bars = client.get_bars("AAPL", timeframe="1d", start=start, end=end)
        print(f"AAPL 1d bars rows={len(bars)}")
        if not bars.empty:
            print("AAPL last bar:")
            print(bars.tail(1).to_string(index=False))

        try:
            record = client.submit_order(
                symbol="AAPL",
                side="buy",
                qty=1,
                order_type="limit",
                limit_price=1.00,
                time_in_force="day",
                client_order_id=client_order_id,
            )
            broker_order_id = record.broker_order_id
            submitted = bool(broker_order_id)
            print(
                "Submitted test order: "
                f"client_order_id={client_order_id} broker_order_id={broker_order_id} "
                f"status={record.response_payload.get('status')}"
            )
        except Exception as exc:  # noqa: BLE001 - manual smoke output should be explicit.
            print(f"Test order rejected: {exc}")

        time.sleep(2)
        if submitted:
            open_orders = client.trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            matching = [
                item
                for item in open_orders
                if getattr(item, "client_order_id", None) == client_order_id
            ]
            print(f"Open order present for client_order_id={client_order_id}: {bool(matching)}")
            if not matching:
                raise RuntimeError("Submitted smoke order was not found in open orders.")

            duplicate = client.submit_order(
                symbol="AAPL",
                side="buy",
                qty=1,
                order_type="limit",
                limit_price=1.00,
                time_in_force="day",
                client_order_id=client_order_id,
            )
            print(
                "Idempotency check: "
                f"first={broker_order_id} duplicate={duplicate.broker_order_id}"
            )
            if duplicate.broker_order_id != broker_order_id:
                raise RuntimeError("Duplicate client_order_id returned a different broker order.")

            if broker_order_id:
                cancelled = client.cancel_order(broker_order_id)
                print(f"Cancelled test order: {cancelled}")

        final_portfolio = client.get_portfolio()
        print(
            "Final account: "
            f"equity={final_portfolio.account.equity} cash={final_portfolio.account.cash_balance} "
            f"buying_power={getattr(final_portfolio.account, 'buying_power', 'n/a')}"
        )
        print("SMOKE TEST PASSED")
        return 0
    except Exception as exc:  # noqa: BLE001 - manual smoke output should be explicit.
        if broker_order_id:
            try:
                client.cancel_order(broker_order_id)
            except Exception:
                pass
        print(f"SMOKE TEST FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
