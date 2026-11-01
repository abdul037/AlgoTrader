"""Command line for the momentum study.

    python -I research/momentum/cli.py data --extra-dir EXTRA
    python -I research/momentum/cli.py run  --price-dir PRICES --extra-dir EXTRA --out OUT
    python -I research/momentum/report.py OUT > OUT/tables.md
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.momentum import data, study  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("data", help="download ^IRX and BIL into --extra-dir")
    fetch.add_argument("--extra-dir", type=Path, required=True)
    run = sub.add_parser("run", help="run the pre-registered study")
    run.add_argument("--price-dir", type=Path, required=True, help="the pullback study's cache (read-only)")
    run.add_argument("--extra-dir", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "data":
        print(data.download_extra(args.extra_dir))
    else:
        started = time.time()
        results = study.run(args.price_dir, args.extra_dir, args.out)
        print({name: v for name, v in results["verdicts"].items()})
        print(f"done in {time.time() - started:.0f}s; wrote {args.out / 'results.json'}")


if __name__ == "__main__":
    main()
