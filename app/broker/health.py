"""Broker health and auth diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any

import httpx

from app.runtime_settings import AppSettings


@dataclass
class BrokerHealthResult:
    ok: bool
    stage: str
    status_code: int | None
    detail: str
    payload: dict[str, Any] | None = None


class EToroBrokerHealthChecker:
    """Small diagnostic wrapper for validating eToro auth and market-data reachability."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.base_url = settings.etoro_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        user_key = settings_user_key(self.settings)
        return {
            "x-api-key": self.settings.etoro_api_key,
            "x-user-key": user_key,
            "x-request-id": str(uuid.uuid4()),
        }

    def check_market_data_symbol(self, symbol: str = "NVDA") -> BrokerHealthResult:
        """Validate a simple market-data search request."""

        url = f"{self.base_url}/api/v1/market-data/search"
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(
                    url,
                    params={"internalSymbolFull": symbol},
                    headers=self._headers(),
                )
        except Exception as exc:
            return BrokerHealthResult(
                ok=False,
                stage="network",
                status_code=None,
                detail=f"Network error while querying eToro: {exc}",
            )

        return self._normalize_response(
            response,
            success_stage="market_data_search",
            error_stage="market_data_search",
        )

    def check_demo_pnl(self) -> BrokerHealthResult:
        """Validate demo-account read access."""

        url = f"{self.base_url}/api/v1/trading/info/demo/pnl"
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(url, headers=self._headers())
        except Exception as exc:
            return BrokerHealthResult(
                ok=False,
                stage="network",
                status_code=None,
                detail=f"Network error while querying demo pnl: {exc}",
            )

        return self._normalize_response(
            response,
            success_stage="demo_pnl",
            error_stage="demo_pnl",
        )

    def full_check(self) -> dict[str, Any]:
        """Run a compact broker health check."""

        market_data = self.check_market_data_symbol("NVDA")
        demo_pnl = self.check_demo_pnl()
        return {
            "base_url": self.base_url,
            "account_mode": self.settings.etoro_account_mode,
            "market_data": result_to_dict(market_data),
            "demo_pnl": result_to_dict(demo_pnl),
        }

    def _normalize_response(
        self,
        response: httpx.Response,
        *,
        success_stage: str,
        error_stage: str,
    ) -> BrokerHealthResult:
        payload: dict[str, Any] | None = None
        detail = response.text[:500]
        try:
            parsed = response.json()
            payload = parsed if isinstance(parsed, dict) else {"data": parsed}
            detail = compact_payload(parsed)
        except Exception:
            payload = None

        return BrokerHealthResult(
            ok=response.status_code == 200,
            stage=success_stage if response.status_code == 200 else error_stage,
            status_code=response.status_code,
            detail=detail,
            payload=payload,
        )


def settings_user_key(settings: AppSettings) -> str:
    """Resolve the configured generated key alias."""

    generated = getattr(settings, "etoro_generated_key", None)
    user_key = getattr(settings, "etoro_user_key", None)
    return str(user_key or generated or "")


def compact_payload(value: Any) -> str:
    """Create a short text summary without dumping full secrets or huge bodies."""

    try:
        text = json.dumps(value)
    except Exception:
        text = str(value)
    return text[:500]


def result_to_dict(result: BrokerHealthResult) -> dict[str, Any]:
    """Serialize the dataclass to a JSON-friendly dict."""

    return {
        "ok": result.ok,
        "stage": result.stage,
        "status_code": result.status_code,
        "detail": result.detail,
    }
