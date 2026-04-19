#!/usr/bin/env python3
"""
eToro API probe — verifies what your credentials can actually do end-to-end.

This script is READ-ONLY. It never places, modifies, or cancels any order.
It calls each endpoint the bot depends on and saves the raw response so you
can compare the real payloads against what the client code expects.

Run from the project root:

    python3 scripts/verify_etoro_demo.py

Output:
    - Console summary: pass/fail per endpoint
    - reports/etoro_probe/<UTC-timestamp>/*.json   raw payloads
    - reports/etoro_probe/<UTC-timestamp>/*.txt    raw text (for error bodies)
    - reports/etoro_probe/<UTC-timestamp>/summary.md  one-page findings
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent.parent

# Load .env without requiring python-dotenv
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(ROOT / ".env")
except Exception:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

API_KEY = os.environ.get("ETORO_API_KEY", "")
USER_KEY = (
    os.environ.get("ETORO_USER_KEY")
    or os.environ.get("ETORO_GENERATED_KEY")
    or ""
)
BASE_URL = os.environ.get("ETORO_BASE_URL", "https://public-api.etoro.com").rstrip("/")

SYMBOL = os.environ.get("PROBE_SYMBOL", "NVDA")


def redact(key: str) -> str:
    if not key:
        return "<missing>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def headers() -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "x-user-key": USER_KEY,
        "x-request-id": str(uuid4()),
        "Content-Type": "application/json",
    }


def probe(
    name: str,
    method: str,
    path: str,
    out_dir: Path,
    params: dict | None = None,
) -> dict:
    url = f"{BASE_URL}{path}" if path.startswith("/api/v1") else f"{BASE_URL}/api/v1{path}"
    print(f"\n[{name}] {method} {url}")
    if params:
        print(f"  params={params}")

    record: dict = {"name": name, "method": method, "url": url, "params": params}
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.request(method, url, headers=headers(), params=params)
        status = resp.status_code
        size = len(resp.content)
        ctype = resp.headers.get("content-type", "")
        print(f"  status={status} size={size}B content-type={ctype}")
        preview = resp.text[:500].replace("\n", " ")
        print(f"  body_preview={preview}")

        # Save raw text for every call (error bodies, etc.)
        (out_dir / f"{name}.txt").write_text(
            f"URL: {url}\nSTATUS: {status}\nHEADERS: {dict(resp.headers)}\n\nBODY:\n{resp.text}\n",
            encoding="utf-8",
        )
        # Save JSON if parseable
        try:
            parsed = resp.json()
            (out_dir / f"{name}.json").write_text(
                json.dumps(parsed, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            parsed = None

        record.update({"status": status, "ok": 200 <= status < 300, "size": size})
        return record
    except Exception as exc:
        print(f"  ERROR: {exc}")
        (out_dir / f"{name}.txt").write_text(
            f"URL: {url}\nSTATUS: exception\nERROR: {exc}\n", encoding="utf-8"
        )
        record.update({"status": 0, "ok": False, "error": str(exc)})
        return record


def main() -> int:
    print("=" * 60)
    print("eToro API Probe")
    print("=" * 60)
    print(f"Base URL      : {BASE_URL}")
    print(f"x-api-key     : {redact(API_KEY)}")
    print(f"x-user-key    : {redact(USER_KEY)}")
    print(f"Symbol        : {SYMBOL}")

    if not API_KEY or not USER_KEY:
        print("\nERROR: ETORO_API_KEY or ETORO_USER_KEY missing in .env. Aborting.")
        return 2

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "reports" / "etoro_probe" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing raw  : {out_dir}\n")

    results: list[dict] = []

    # 1. Symbol search (signal-side foundation)
    results.append(
        probe(
            "01_market_data_search",
            "GET",
            "/market-data/search",
            out_dir,
            params={"internalSymbolFull": SYMBOL},
        )
    )

    # Resolve instrument id from search response
    instrument_id: int | None = None
    search_json = out_dir / "01_market_data_search.json"
    if search_json.exists():
        try:
            payload = json.loads(search_json.read_text())
            items = payload.get("items", []) or []
            for item in items:
                if str(item.get("internalSymbolFull", "")).upper() == SYMBOL.upper():
                    instrument_id = int(item.get("internalInstrumentId", 0)) or None
                    break
            if instrument_id is None and items:
                instrument_id = int(items[0].get("internalInstrumentId", 0)) or None
        except Exception as exc:
            print(f"  (could not parse instrument id: {exc})")

    print(f"\nResolved instrument_id for {SYMBOL}: {instrument_id}")

    if instrument_id is not None:
        # 2. Live rates
        results.append(
            probe(
                "02_market_data_rates",
                "GET",
                "/market-data/instruments/rates",
                out_dir,
                params={"instrumentIds": str(instrument_id)},
            )
        )
        # 3. Daily candles
        results.append(
            probe(
                "03_candles_OneDay_30",
                "GET",
                f"/market-data/instruments/{instrument_id}/history/candles/desc/OneDay/30",
                out_dir,
            )
        )
        # 4. Hourly candles
        results.append(
            probe(
                "04_candles_OneHour_50",
                "GET",
                f"/market-data/instruments/{instrument_id}/history/candles/desc/OneHour/50",
                out_dir,
            )
        )
        # 5. 15-minute candles
        results.append(
            probe(
                "05_candles_FifteenMinutes_50",
                "GET",
                f"/market-data/instruments/{instrument_id}/history/candles/desc/FifteenMinutes/50",
                out_dir,
            )
        )
        # 6. 1-minute candles
        results.append(
            probe(
                "06_candles_OneMinute_50",
                "GET",
                f"/market-data/instruments/{instrument_id}/history/candles/desc/OneMinute/50",
                out_dir,
            )
        )

    # 7-10. Account/portfolio — read-only on both demo and real
    results.append(
        probe("07_demo_portfolio", "GET", "/trading/info/demo/portfolio", out_dir)
    )
    results.append(
        probe("08_demo_pnl", "GET", "/trading/info/demo/pnl", out_dir)
    )
    results.append(
        probe("09_real_portfolio", "GET", "/trading/info/portfolio", out_dir)
    )
    results.append(
        probe("10_real_pnl", "GET", "/trading/info/pnl", out_dir)
    )

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        status = "OK  " if r.get("ok") else "FAIL"
        print(f"  [{status}] {r['name']:<32} HTTP {r.get('status', 0)}")

    # Markdown report
    lines = [
        f"# eToro API Probe — {ts}",
        "",
        f"- Base URL: `{BASE_URL}`",
        f"- API key: `{redact(API_KEY)}`",
        f"- User key: `{redact(USER_KEY)}`",
        f"- Probe symbol: `{SYMBOL}`",
        f"- Resolved instrument_id: `{instrument_id}`",
        "",
        "## Endpoint results",
        "",
        "| # | Endpoint | Method | Status | OK |",
        "|---|----------|--------|--------|----|",
    ]
    for r in results:
        ok_mark = "yes" if r.get("ok") else "**NO**"
        url_short = r["url"].replace(BASE_URL, "")
        lines.append(
            f"| {r['name']} | `{url_short}` | {r['method']} | {r.get('status', 0)} | {ok_mark} |"
        )

    lines += [
        "",
        "## What this tells us",
        "",
        "- `01`–`06` are the **signal-side foundation**. If all pass, the Telegram signal-bot",
        "  product (Path A) has verified end-to-end market data. That is the core dependency.",
        "- `07`–`08` test **demo execution/read access**. They confirm whether demo portfolio",
        "  state can be read. Demo order placement is *not* tested here (this script is read-only).",
        "- `09`–`10` test **real-mode execution/read access**. If these return 401/403, your",
        "  keys are *not* actually authorized for real-mode trading, regardless of what the",
        "  `.env` flags say — confirm before trusting real-mode.",
        "",
        "## Next steps based on outcome",
        "",
        "- If 01–06 all pass: proceed with signal-only descope (Phase 2 of the plan).",
        "- If any of 01–06 fail: open the matching `.txt` file in this folder and inspect the",
        "  response body. The fix is almost always either an auth header issue, a path rewrite",
        "  (`/api/v1/` prefix), or a param name mismatch (`internalSymbolFull` vs `symbol`).",
        "- If 09–10 fail (401/403) but 07–08 pass: real-mode is demo-only; plan accordingly.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\nFull payloads : {out_dir}")
    print(f"Markdown summary: {out_dir / 'summary.md'}")

    # Exit non-zero if signal-side calls failed — those are the blocker for Path A
    signal_side = [r for r in results if r["name"].startswith(("01_", "02_", "03_"))]
    if not all(r.get("ok") for r in signal_side):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
