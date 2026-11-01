"""Data acquisition for the pullback study: S&P 500 members and daily OHLCV.

Everything downloaded is untrusted data and lives outside the repository (``--data-dir``).
Nothing here is imported by ``app/``.

* Members: the Wikipedia "List of S&P 500 companies" constituents table (primary), cross-checked
  against the ``datasets/s-and-p-500-companies`` CSV on GitHub. Today's members only, so the
  universe carries survivorship bias (see the report).
* Prices: yfinance daily bars, ``auto_adjust=True`` (split- and dividend-adjusted OHLC), from
  2010-01-01, one parquet file per symbol with columns timestamp, open, high, low, close, volume.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
START = "2010-01-01"
# Benchmarks and filters: SPY (trend filter, buy-and-hold), ^VIX (fear filter), RSP (equal-weight
# S&P 500, used only to size the survivorship bias of a today's-members universe).
EXTRA_SYMBOLS = ["SPY", "^VIX", "^VIX3M", "HYG", "IEF", "RSP", "QQQ", "IWM"]


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs
        return response.read()


def parse_wikipedia_constituents(html: str) -> pd.DataFrame:
    """The ``id="constituents"`` table as Symbol / Security / GICS Sector / GICS Sub-Industry / Date added."""

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="constituents")
    if table is None:
        raise ValueError("constituents table not found")
    rows = table.find_all("tr")
    header = [cell.get_text(strip=True) for cell in rows[0].find_all(["th", "td"])]
    records = []
    for row in rows[1:]:
        cells = [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
        if len(cells) == len(header):
            records.append(dict(zip(header, cells, strict=True)))
    frame = pd.DataFrame.from_records(records)
    keep = ["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Date added"]
    return frame[[column for column in keep if column in frame.columns]]


def fetch_constituents(data_dir: Path) -> pd.DataFrame:
    """Download both member lists, keep the raw copies, return the Wikipedia table (CSV fallback)."""

    out = data_dir / "constituents"
    out.mkdir(parents=True, exist_ok=True)
    csv_frame = None
    try:
        raw_csv = _get(CSV_URL)
        (out / "constituents.csv").write_bytes(raw_csv)
        csv_frame = pd.read_csv(io.BytesIO(raw_csv))
    except Exception as exc:  # noqa: BLE001 - either source may be blocked
        print(f"csv source failed: {exc}")
    wiki_frame = None
    try:
        raw_html = _get(WIKI_URL)
        (out / "wiki.html").write_bytes(raw_html)
        wiki_frame = parse_wikipedia_constituents(raw_html.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"wikipedia source failed: {exc}")
    primary = wiki_frame if wiki_frame is not None and len(wiki_frame) >= 490 else csv_frame
    if primary is None:
        raise RuntimeError("no constituent source reachable")
    source = "wikipedia" if primary is wiki_frame else "github_csv"
    members = primary.rename(
        columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector",
                 "GICS Sub-Industry": "sub_industry", "Date added": "date_added"}
    )
    members["symbol"] = members["symbol"].str.strip().str.upper()
    members["yf_symbol"] = members["symbol"].str.replace(".", "-", regex=False)
    members.to_csv(out / "members.csv", index=False)
    meta: dict[str, object] = {"source": source, "count": int(len(members))}
    if wiki_frame is not None and csv_frame is not None:
        wiki_set = set(wiki_frame["Symbol"].str.upper())
        csv_set = set(csv_frame["Symbol"].str.upper())
        meta["only_in_wikipedia"] = sorted(wiki_set - csv_set)
        meta["only_in_csv"] = sorted(csv_set - wiki_set)
    (out / "members_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))
    return members


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.rename(columns=str.lower)
    frame = frame[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "high", "low", "close"])
    frame = frame[(frame["close"] > 0) & (frame["open"] > 0)]
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame.index.name = "timestamp"
    return frame.reset_index()


def download_prices(symbols: list[str], data_dir: Path, *, start: str = START, batch: int = 40) -> dict[str, int]:
    """Download adjusted daily bars for ``symbols`` (yfinance spelling) into ``data_dir/prices``."""

    import yfinance as yf

    out = data_dir / "prices"
    out.mkdir(parents=True, exist_ok=True)
    rows: dict[str, int] = {}
    pending = [symbol for symbol in symbols if not (out / f"{symbol}.parquet").exists()]
    for offset in range(0, len(pending), batch):
        chunk = pending[offset : offset + batch]
        for attempt in range(4):
            try:
                raw = yf.download(
                    chunk, start=start, interval="1d", auto_adjust=True, actions=False,
                    group_by="ticker", threads=True, progress=False,
                )
                break
            except Exception as exc:  # noqa: BLE001 - network hiccups
                print(f"batch {offset} attempt {attempt}: {exc}")
                time.sleep(2 ** (attempt + 1))
        else:
            continue
        for symbol in chunk:
            try:
                frame = raw[symbol] if isinstance(raw.columns, pd.MultiIndex) else raw
                frame = _normalise(frame)
            except KeyError:
                continue
            if len(frame) == 0:
                continue
            frame.to_parquet(out / f"{symbol}.parquet", index=False)
            rows[symbol] = len(frame)
        print(f"downloaded {offset + len(chunk)}/{len(pending)}")
    return rows


def load_prices(data_dir: Path, symbol: str) -> pd.DataFrame | None:
    path = data_dir / "prices" / f"{symbol}.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def load_members(data_dir: Path) -> pd.DataFrame:
    return pd.read_csv(data_dir / "constituents" / "members.csv")


# --- Cleaning rules (fixed before any signal or result was computed) -------------------------
STALE_ZERO_VOLUME_BARS = 10
"""A symbol with more than this many zero-volume bars has a stale pre-listing history (OTC or
foreign-line quotes: AMCR, FERG, SW, HWM, CHTR, VRT); its history starts after the last one."""
BREAK_FACTOR_JUMP = 0.15
"""A day on which yfinance's dividend-adjustment factor (adjusted close / split-adjusted close)
moves by more than 15% is a large corporate action (spin-off, mega special dividend) that
yfinance adjusts unreliably (DHR 2016-07-05 shows a fake +61% day). No trade may be held across
such a break and no signal is taken in the 120 bars from it (indicator warm-up)."""


def clean_symbol(data_dir: Path, symbol: str) -> tuple[pd.DataFrame, list[pd.Timestamp]] | None:
    """Cleaned adjusted bars for ``symbol`` and its corporate-action break dates."""

    frame = load_prices(data_dir, symbol)
    if frame is None or frame.empty:
        return None
    zero = frame["volume"] <= 0
    if not symbol.startswith("^"):
        if int(zero.sum()) > STALE_ZERO_VOLUME_BARS:
            last_zero = frame.index[zero].max()
            frame = frame.loc[last_zero + 1 :]
        frame = frame[frame["volume"] > 0]
    frame = frame.copy()
    # One bar in the whole set (HUBB 2021-05-05) has its low above its open; clip OHLC.
    frame["high"] = frame[["high", "open", "close"]].max(axis=1)
    frame["low"] = frame[["low", "open", "close"]].min(axis=1)
    frame = frame.reset_index(drop=True)

    breaks: list[pd.Timestamp] = []
    raw_path = data_dir / "raw" / f"{symbol}.parquet"
    if raw_path.exists() and not symbol.startswith("^"):
        raw = pd.read_parquet(raw_path)[["timestamp", "Close"]]
        raw["timestamp"] = pd.to_datetime(raw["timestamp"])
        merged = frame[["timestamp", "close"]].merge(raw, on="timestamp", how="inner")
        factor = merged["close"] / merged["Close"]
        jump = (factor / factor.shift(1) - 1.0).abs()
        breaks = [pd.Timestamp(ts) for ts in merged.loc[jump > BREAK_FACTOR_JUMP, "timestamp"]]
    return frame, breaks
