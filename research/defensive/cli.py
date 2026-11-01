"""Command line for the defensive trend study. Run from the repository root:

    python -I research/defensive/cli.py data --data-dir DATA
    python -I research/defensive/cli.py run  --data-dir DATA --out OUT

``-I`` keeps the downloaded (untrusted) data directory and the current directory off
``sys.path``; this package's own directory is added explicitly below. Nothing imports ``app``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="defensive-study")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("data", "check", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--data-dir", type=Path, required=True)
        cmd.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command == "data":
        import data

        data.download(args.data_dir)
        return 0
    if args.command == "check":
        import checks

        checks.run(args.data_dir)
        return 0
    if args.command == "run":
        import study

        study.run(args.data_dir, args.out)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
