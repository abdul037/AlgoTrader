"""FastAPI routes for Telegram webhook delivery and configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _telegram_service(request: Request) -> Any:
    return request.app.state.telegram_command_service


@router.post("/webhook")
def telegram_webhook(
    payload: dict[str, Any],
    request: Request,
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, Any]:
    settings = request.app.state.settings
    if settings.telegram_webhook_secret and secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram webhook secret.")

    processed = bool(_telegram_service(request).handle_update(payload))
    return {
        "ok": True,
        "processed": processed,
        "detail": "processed" if processed else "ignored",
    }


@router.post("/webhook/register")
def register_telegram_webhook(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    webhook_url = str(payload.get("webhook_url") or settings.telegram_webhook_url or "").strip()
    if not webhook_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide webhook_url or set TELEGRAM_WEBHOOK_URL.",
        )

    drop_pending_updates = bool(payload.get("drop_pending_updates", False))
    try:
        result = request.app.state.telegram_notifier.set_webhook(
            webhook_url,
            secret_token=settings.telegram_webhook_secret or None,
            drop_pending_updates=drop_pending_updates,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if not result.get("ok", False):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(result))

    return {
        "ok": True,
        "description": str(result.get("description") or "Webhook registered."),
        "webhook_url": webhook_url,
        "pending_update_count": 0,
        "has_custom_certificate": False,
    }


@router.get("/webhook/status")
def telegram_webhook_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    try:
        result = request.app.state.telegram_notifier.get_webhook_info()
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if not result.get("ok", False):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(result))

    payload = result.get("result") or {}
    return {
        "ok": True,
        "description": "Webhook info loaded.",
        "mode": settings.telegram_mode,
        "polling_enabled": bool(settings.telegram_polling_enabled),
        "webhook_auto_register": bool(settings.telegram_webhook_auto_register),
        "configured_webhook_url": str(settings.telegram_webhook_url or ""),
        "webhook_url": str(payload.get("url") or ""),
        "pending_update_count": int(payload.get("pending_update_count") or 0),
        "has_custom_certificate": bool(payload.get("has_custom_certificate", False)),
    }
