"""Intraday edge study (RESEARCH ONLY): run the candidates through the production walk-forward.

    python -I scripts/research_intraday_study.py run --data-dir DATA --out results.jsonl
    python -I scripts/research_intraday_study.py report --results results.jsonl --out table.md

``DATA`` holds ``5m/<SYMBOL>.parquet`` (regular-hours bars: timestamp UTC, open, high, low,
close, volume), the source of the 5m, 10m and 15m frames, and optionally ``1h/<SYMBOL>.parquet``.
The 2026-10-26 study fetched them keyless from yfinance (``period="60d"``, ``interval="5m"``;
``period="730d"``, ``interval="1h"``; ``prepost=False``, ``auto_adjust=False``). Nothing here
reads settings files, keys or a database, and nothing is written outside ``--out``.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.research.intraday import CANDIDATE_VARIANTS  # noqa: E402
from app.research.intraday.harness import resample_session_bars  # noqa: E402
from app.research.intraday.study import (  # noqa: E402
    UNIVERSES,
    gate_columns,
    run_grid,
    summarize,
    unit_supported,
)

TABLE_COLUMNS = (
    "variant", "tf", "dir", "universe", "oos_trades", "win_rate", "gross_r", "etoro_r",
    "alpaca_r", "stress_r", "holdout_trades", "holdout_alpaca_r", "holdout_stress_r",
    "stress_daily_t", "passed", "paper_full", "paper_oos_bar",
)


def _frames(data_dir: Path):
    @cache
    def load(symbol: str, source: str) -> pd.DataFrame:
        frame = pd.read_parquet(data_dir / source / f"{symbol}.parquet")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame

    def frame_for(symbol: str, timeframe: str) -> pd.DataFrame:
        source = "1h" if timeframe == "1h" else "5m"
        return resample_session_bars(load(symbol, source), timeframe)

    return frame_for


def run(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    symbols = sorted({s for name in args.universes.split(",") for s in UNIVERSES[name]})
    # "live:<registry name>" variants run a live strategy for calibration (see study.py).
    variants = args.variants.split(",") if args.variants else list(CANDIDATE_VARIANTS)
    units = [
        (variant, timeframe, direction, symbol)
        for variant in variants
        for timeframe in args.timeframes.split(",")
        for direction in args.directions.split(",")
        for symbol in symbols
        if unit_supported(variant, timeframe)
        and (data_dir / ("1h" if timeframe == "1h" else "5m") / f"{symbol}.parquet").exists()
    ]
    out = Path(args.out)
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            item = json.loads(line)
            done.add((item["variant"], item["timeframe"], item["direction"], item["symbol"]))
    todo = [unit for unit in units if unit not in done]
    print(f"{len(units)} units, {len(todo)} to run", flush=True)
    with out.open("a") as sink:
        def write(item: dict) -> None:
            sink.write(json.dumps(item) + "\n")
            sink.flush()

        run_grid(todo, _frames(data_dir), workers=args.workers, on_result=write)


def report(args: argparse.Namespace) -> None:
    results = [json.loads(line) for line in Path(args.results).read_text().splitlines() if line]
    rows = summarize(results)
    Path(args.out).with_suffix(".json").write_text(json.dumps(rows, indent=1, default=str))
    lines = ["| " + " | ".join(TABLE_COLUMNS) + " |", "|" + "---|" * len(TABLE_COLUMNS)]
    for row in rows:
        columns = gate_columns(row)
        lines.append("| " + " | ".join(_cell(columns[name]) for name in TABLE_COLUMNS) + " |")
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"{len(rows)} rows -> {args.out}")


def _cell(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:+.3f}" if abs(value) < 10 else f"{value:.1f}"
    return "" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--data-dir", required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--timeframes", default="5m,10m,15m")
    run_parser.add_argument("--directions", default="long,short")
    run_parser.add_argument("--universes", default="live24,volatile20")
    run_parser.add_argument("--variants", default="")
    run_parser.add_argument("--workers", type=int, default=1)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--results", required=True)
    report_parser.add_argument("--out", required=True)
    args = parser.parse_args()
    run(args) if args.command == "run" else report(args)


if __name__ == "__main__":
    main()
