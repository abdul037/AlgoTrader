from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.schemas import ExecutionOrder, ExecutionResult

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"


def get_execution_status() -> dict:
    settings = get_settings()
    ready = bool(settings.alpaca_api_key and settings.alpaca_api_secret)
    return {
        "provider": "alpaca",
        "ready": ready,
        "liveEnabled": settings.enable_live_execution,
        "paper": settings.alpaca_paper,
        "note": (
            f"Execution is armed for {'paper' if settings.alpaca_paper else 'live'} Alpaca orders."
            if settings.enable_live_execution and ready
            else (
                "Execution is enabled but Alpaca credentials are missing."
                if settings.enable_live_execution
                else "Execution defaults to simulated orders until ENABLE_LIVE_EXECUTION=true."
            )
        ),
    }


def _parse_command(command_text: str) -> dict:
    match = re.match(r"^(BUY|SELL)\s+(\d+(?:\.\d+)?)\s+([A-Z.]+)(?:\s+MARKET)?$", command_text.strip().upper())
    if not match:
        raise RuntimeError("Use commands like BUY 10 AAPL or SELL 5 MSFT.")
    side, quantity, symbol = match.groups()
    return {
        "symbol": symbol,
        "side": side.lower(),
        "quantity": float(quantity),
    }


async def execute_command(payload: dict) -> ExecutionResult:
    settings = get_settings()
    parsed = _parse_command(payload["command_text"]) if payload.get("command_text") else {}

    symbol = (payload.get("symbol") or parsed.get("symbol") or "").strip().upper()
    side = payload.get("side") or parsed.get("side")
    quantity = float(payload.get("quantity") or parsed.get("quantity") or 0)
    dry_run = payload.get("dry_run", True)

    if not symbol:
        raise RuntimeError("symbol is required.")
    if side not in {"buy", "sell"}:
        raise RuntimeError("side must be buy or sell.")
    if quantity <= 0:
        raise RuntimeError("quantity must be greater than zero.")

    submitted_at = datetime.now(UTC).isoformat()
    live_ready = settings.enable_live_execution and bool(settings.alpaca_api_key and settings.alpaca_api_secret)

    if dry_run or not live_ready:
        return ExecutionResult(
            accepted=True,
            simulated=True,
            broker="simulated",
            message=(
                "Live execution is enabled but credentials are missing, so this order was simulated."
                if settings.enable_live_execution and not live_ready
                else "Order simulated. Disable dry run and enable Alpaca to place a real order."
            ),
            submittedAt=submitted_at,
            order=ExecutionOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                orderType="market",
                timeInForce="day",
                status="simulated",
            ),
        )

    base_url = ALPACA_PAPER_URL if settings.alpaca_paper else ALPACA_LIVE_URL
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{base_url}/v2/orders",
            headers={
                "APCA-API-KEY-ID": settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
                "Content-Type": "application/json",
            },
            json={
                "symbol": symbol,
                "qty": str(quantity),
                "side": side,
                "type": "market",
                "time_in_force": "day",
            },
        )
        payload = response.json()
        if not response.is_success:
            raise RuntimeError(payload.get("message", "Broker rejected the order."))

    return ExecutionResult(
        accepted=True,
        simulated=False,
        broker="alpaca-paper" if settings.alpaca_paper else "alpaca-live",
        message="Order submitted to Alpaca.",
        submittedAt=submitted_at,
        order=ExecutionOrder(
            symbol=symbol,
            side=side,
            quantity=quantity,
            orderType="market",
            timeInForce="day",
            externalId=payload.get("id"),
            status=payload.get("status", "accepted"),
        ),
    )
