"""Check eToro broker auth and basic read access."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.broker.health import EToroBrokerHealthChecker
from app.runtime_settings import get_settings


def main() -> None:
    settings = get_settings()
    checker = EToroBrokerHealthChecker(settings)
    print(
        json.dumps(
            {
                "stage": "starting",
                "base_url": settings.etoro_base_url,
                "account_mode": settings.etoro_account_mode,
            }
        ),
        flush=True,
    )

    print(json.dumps({"stage": "checking_market_data", "symbol": "NVDA"}), flush=True)
    market_data = checker.check_market_data_symbol("NVDA")
    print(
        json.dumps(
            {
                "stage": "market_data_result",
                "ok": market_data.ok,
                "status_code": market_data.status_code,
                "detail": market_data.detail,
            }
        ),
        flush=True,
    )

    print(json.dumps({"stage": "checking_demo_pnl"}), flush=True)
    demo_pnl = checker.check_demo_pnl()
    print(
        json.dumps(
            {
                "stage": "demo_pnl_result",
                "ok": demo_pnl.ok,
                "status_code": demo_pnl.status_code,
                "detail": demo_pnl.detail,
            }
        ),
        flush=True,
    )

    result = {
        "base_url": settings.etoro_base_url,
        "account_mode": settings.etoro_account_mode,
        "market_data": {
            "ok": market_data.ok,
            "stage": market_data.stage,
            "status_code": market_data.status_code,
            "detail": market_data.detail,
        },
        "demo_pnl": {
            "ok": demo_pnl.ok,
            "stage": demo_pnl.stage,
            "status_code": demo_pnl.status_code,
            "detail": demo_pnl.detail,
        },
    }
    print(json.dumps({"stage": "complete", "result": result}, indent=2), flush=True)


if __name__ == "__main__":
    main()
