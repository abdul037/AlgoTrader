"""The AlgoBot Live Scorecard, served by the bot itself (operator 2026-11-07: "I need the
AlgoBot Live Scorecard to be deployed somewhere for Live").

The scorecard page (``scorecard_page.html``, the same file published as the claude.ai artifact)
reads its data through the operator's claude.ai Supabase connector, so it only worked inside
claude.ai. Served here, it polls ``GET /scorecard/data`` instead, which runs the page's own SQL
(extracted from the page, so there is one copy of it) on the bot's Postgres database in a
read-only transaction and returns ``{"d": ...}``; results are cached for ``CACHE_SECONDS``.

Real-money figures, so both routes need ``?key=`` equal to the ``SCORECARD_TOKEN`` setting
(constant-time compare). Without a token configured both routes answer 404: off by default.
Read-only: no route here changes anything.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["scorecard"])

PAGE_PATH = Path(__file__).with_name("scorecard_page.html")
CACHE_SECONDS = 10.0
STATEMENT_TIMEOUT_MS = 20_000
_HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="robots" content="noindex, nofollow">\n'
)
_SQL_EXPR = re.compile(r'const SQL = ((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+);')
_JS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "body": None}


@lru_cache(maxsize=1)
def page_html() -> str:
    """The scorecard page with a standard document head (the artifact host adds its own)."""

    return _HEAD + PAGE_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def scorecard_sql() -> str:
    """The page's own query: its ``const SQL = "..." + "...";`` literals joined and unescaped."""

    match = _SQL_EXPR.search(PAGE_PATH.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError("scorecard page has no SQL")
    return "".join(json.loads(f'"{part}"') for part in _JS_STRING.findall(match.group(1)))


def _token(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "scorecard_token", "") or "")


def _authorized(request: Request, key: str) -> bool | None:
    """None: the scorecard is off (no token configured); else whether ``key`` matches."""

    token = _token(request)
    if not token:
        return None
    return hmac.compare_digest(key.encode("utf-8"), token.encode("utf-8"))


@router.get("/scorecard", response_class=HTMLResponse, include_in_schema=False)
def scorecard_page(request: Request, key: str = "") -> HTMLResponse:
    if not _authorized(request, key):
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(page_html(), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/scorecard/data", include_in_schema=False)
def scorecard_data(request: Request, key: str = "") -> JSONResponse:
    allowed = _authorized(request, key)
    if allowed is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db = request.app.state.db
    if getattr(db, "is_sqlite", True) or getattr(db, "_engine", None) is None:
        return JSONResponse({"error": "the scorecard needs the Postgres database"}, status_code=503)
    with _cache_lock:
        if _cache["body"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return JSONResponse(_cache["body"], headers={"Cache-Control": "no-store"})
        try:
            body = {"d": _run(db)}
        except Exception as exc:  # noqa: BLE001 - the page shows "couldn't refresh" and retries
            return JSONResponse({"error": str(exc)[:200]}, status_code=502)
        _cache.update(at=time.monotonic(), body=body)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _run(db: Any) -> Any:
    """The page's query in a read-only transaction with a statement timeout."""

    with db._engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        row = connection.exec_driver_sql(scorecard_sql()).fetchone()
        connection.rollback()
    value = row[0] if row is not None else None
    return json.loads(value) if isinstance(value, str) else value
