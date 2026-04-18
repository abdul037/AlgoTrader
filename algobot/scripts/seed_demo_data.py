"""Generate deterministic synthetic OHLCV data for local demos."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_series() -> pd.DataFrame:
    """Create a deterministic swing-trading-friendly NVDA sample series."""

    timestamps = pd.bdate_range("2025-01-02", periods=90, tz="UTC")
    price = 128.0
    rows: list[dict[str, float | str]] = []

    for index, timestamp in enumerate(timestamps):
        if index < 18:
            drift = 1.10
        elif index < 28:
            drift = -0.95
        elif index < 50:
            drift = 1.35
        elif index < 63:
            drift = -0.80
        else:
            drift = 1.05
        noise = ((index % 6) - 2.5) * 0.22
        previous_close = price
        close = round(max(previous_close + drift + noise, 40.0), 2)
        open_price = round(previous_close + (0.25 if index % 2 == 0 else -0.2), 2)
        high = round(max(open_price, close) + 1.35, 2)
        low = round(min(open_price, close) - 1.15, 2)
        volume = int(1_200_000 + index * 18_000 + (index % 7) * 11_000)
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
        price = close

    return pd.DataFrame(rows)


def main() -> None:
    """Write the sample CSV file."""

    sample_dir = ROOT / "sample_data"
    sample_dir.mkdir(parents=True, exist_ok=True)
    output_path = sample_dir / "nvda.csv"
    build_series().to_csv(output_path, index=False)
    print(output_path)


if __name__ == "__main__":
    main()
