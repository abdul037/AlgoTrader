#!/usr/bin/env python3
"""Preflight check for Alpaca paper connectivity before enabling the bot.

Reads the app settings (env vars / .env on the host), connects READ-ONLY to
Alpaca, verifies the connected account matches ``ALPACA_EXPECTED_ACCOUNT_NUMBER``,
reports account status and the market clock, and prints a clear GO / NO-GO.

It places NO orders and changes nothing. Run it on your DEPLOYMENT HOST (where
the Alpaca env vars live) -- not in CI, and not in this build sandbox, which
cannot reach Alpaca:

    python scripts/verify_alpaca.py

Exit codes: 0 = GO, 1 = reachable but not safe (mismatch / blocked),
2 = config incomplete, 3 = could not connect / auth failed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime_settings import get_settings  # noqa: E402 - path bootstrap above


def config_blockers(settings: Any) -> list[str]:
    """Return reasons the Alpaca config is not ready (empty list = ready)."""

    blockers: list[str] = []
    if not bool(getattr(settings, "alpaca_enabled", False)):
        blockers.append("ALPACA_ENABLED is false")
    if not str(getattr(settings, "alpaca_api_key", "") or "").strip():
        blockers.append("ALPACA_API_KEY is not set")
    if not str(getattr(settings, "alpaca_secret_key", "") or "").strip():
        blockers.append("ALPACA_SECRET_KEY is not set")
    if not str(getattr(settings, "alpaca_expected_account_number", "") or "").strip():
        blockers.append("ALPACA_EXPECTED_ACCOUNT_NUMBER is not set")
    if str(getattr(settings, "execution_mode", "paper")) != "paper":
        blockers.append("EXECUTION_MODE is not 'paper'")
    if bool(getattr(settings, "enable_real_trading", False)):
        blockers.append("ENABLE_REAL_TRADING is true (this preflight is paper-only)")
    return blockers


def main() -> int:
    settings = get_settings()

    blockers = config_blockers(settings)
    if blockers:
        print("NO-GO: Alpaca configuration is incomplete:")
        for item in blockers:
            print(f"  - {item}")
        return 2

    # Import here so config-only checks work even without alpaca-py installed.
    from app.broker.alpaca_client import AlpacaClient

    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_base_url,
        paper=True,
        data_feed=settings.alpaca_data_feed,
        data_url=settings.alpaca_data_url,
    )

    try:
        identity = client.get_account_identity()
    except Exception as exc:  # noqa: BLE001 - report any transport/auth failure clearly
        print(f"NO-GO: could not reach Alpaca or authentication failed: {exc}")
        return 3

    expected = str(settings.alpaca_expected_account_number).strip()
    actual = str(identity.get("account_number") or "")
    account_matches = actual == expected
    trading_blocked = bool(identity.get("trading_blocked"))

    print("Alpaca paper account:")
    for key in ("account_number", "status", "equity", "cash", "trading_blocked"):
        print(f"  {key:16}: {identity.get(key)}")

    try:
        market_open = client.is_regular_market_open()
    except Exception:  # noqa: BLE001 - clock is informational only
        market_open = None
    print(f"  market_open     : {market_open}")

    print()
    print(f"Account matches ALPACA_EXPECTED_ACCOUNT_NUMBER ({expected}): {account_matches}")

    if account_matches and not trading_blocked:
        print("GO: Alpaca paper connectivity verified. Safe to run the bot in shadow/supervised mode.")
        return 0

    if not account_matches:
        print(
            "NO-GO: connected account does not match the expected number. "
            "The bot's reconciliation would trip the circuit breaker. "
            "Check ALPACA_EXPECTED_ACCOUNT_NUMBER and your API keys."
        )
    if trading_blocked:
        print("NO-GO: the account reports trading_blocked=true. Resolve this in the Alpaca dashboard.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
