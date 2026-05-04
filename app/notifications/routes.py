"""FastAPI routes for Telegram webhook delivery and configuration."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

router = APIRouter(prefix="/telegram", tags=["telegram"])

_rate_limit_lock = Lock()
_rate_limit_windows: dict[str, deque[datetime]] = defaultdict(deque)
_rate_limit_notified_until: dict[str, datetime] = {}


def _telegram_service(request: Request) -> Any:
    return request.app.state.telegram_command_service


@router.post("/webhook")
def telegram_webhook(
    payload: dict[str, Any],
    request: Request,
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> Any:
    settings = request.app.state.settings
    if settings.telegram_webhook_secret and secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_secret")

    chat_id = _extract_sender_chat_id(payload)
    if chat_id is not None and not _chat_allowed(settings, chat_id):
        _log_unauthorized_chat(request, chat_id)
        return Response(status_code=status.HTTP_200_OK)

    if chat_id is not None and _rate_limited(settings, chat_id):
        _send_rate_limit_notice_once(request, chat_id)
        return {
            "ok": True,
            "processed": False,
            "detail": "rate_limited",
        }

    processed = bool(_telegram_service(request).handle_update(payload))
    return {
        "ok": True,
        "processed": processed,
        "detail": "processed" if processed else "ignored",
    }


def _extract_sender_chat_id(payload: dict[str, Any]) -> str | None:
    message = payload.get("message") or payload.get("edited_message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    raw_id = sender.get("id") if sender.get("id") is not None else chat.get("id")
    if raw_id in (None, ""):
        return None
    return str(raw_id)


def _chat_allowed(settings: Any, chat_id: str) -> bool:
    allowed = [str(item) for item in (settings.telegram_allowed_chat_ids or []) if str(item)]
    return not allowed or chat_id in allowed


def _log_unauthorized_chat(request: Request, chat_id: str) -> None:
    service = _telegram_service(request)
    logs = getattr(service, "logs", None)
    if logs is not None and hasattr(logs, "log"):
        logs.log("telegram_unauthorized_chat", {"chat_id": chat_id})


def _rate_limited(settings: Any, chat_id: str) -> bool:
    limit = max(1, int(getattr(settings, "telegram_rate_limit_per_minute", 30) or 30))
    now = datetime.now(UTC)
    window_start = now - timedelta(seconds=60)
    with _rate_limit_lock:
        window = _rate_limit_windows[chat_id]
        while window and window[0] <= window_start:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False


def _send_rate_limit_notice_once(request: Request, chat_id: str) -> None:
    now = datetime.now(UTC)
    with _rate_limit_lock:
        notified_until = _rate_limit_notified_until.get(chat_id)
        if notified_until is not None and notified_until > now:
            return
        _rate_limit_notified_until[chat_id] = now + timedelta(seconds=60)
    request.app.state.telegram_notifier.send_text("rate_limit_exceeded", chat_id=chat_id)


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
