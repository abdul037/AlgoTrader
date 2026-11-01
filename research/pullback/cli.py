"""Command line for the pullback study. Run from the repository root with an isolated interpreter:

    python -I research/pullback/cli.py data   --data-dir DATA
    python -I research/pullback/cli.py parity --data-dir DATA --out OUT
    python -I research/pullback/cli.py run    --data-dir DATA --out OUT
    python -I research/pullback/cli.py calibrate --data-dir DATA --out OUT   # after run

``-I`` keeps the downloaded (untrusted) data directory and the current directory off
``sys.path``; the repository root is added explicitly below so ``app`` and ``research`` import.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pullback-study")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("data", "parity", "run", "calibrate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--data-dir", type=Path, required=True)
        cmd.add_argument("--out", type=Path, default=None)
    sub.choices["parity"].add_argument("--symbols", default="WMT,PYPL,DIS,TSLA,AAPL,JPM")
    sub.choices["parity"].add_argument("--window", type=int, default=400)
    args = parser.parse_args(argv)

    if args.command == "data":
        from research.pullback import data

        members = data.fetch_constituents(args.data_dir)
        symbols = sorted(set(members["yf_symbol"])) + data.EXTRA_SYMBOLS
        rows = data.download_prices(symbols, args.data_dir)
        print(f"downloaded {len(rows)} new symbols")
        return 0
    if args.command == "parity":
        from research.pullback import parity

        report = parity.run_parity(args.data_dir, args.symbols.split(","), window=args.window)
        parity.write_report(report, args.out)
        return 0 if report["all_match"] else 1
    if args.command == "calibrate":
        from research.pullback import calibration

        calibration.run(args.data_dir, args.out)
        return 0
    if args.command == "run":
        from research.pullback import study

        study.run(args.data_dir, args.out)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
