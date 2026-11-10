"""Command line for the bigger-moves study. Run from the repository root, isolated:

    python -I research/bigger_moves/cli.py parity    --data-dir DATA --out OUT
    python -I research/bigger_moves/cli.py run       --data-dir DATA --out OUT   # main period
    python -I research/bigger_moves/cli.py holdout   --data-dir DATA --out OUT   # sealed holdout
    python -I research/bigger_moves/cli.py calibrate --data-dir DATA --out OUT   # V0 vs production

``DATA`` is the pullback study's cache (``research/pullback/cli.py data``). ``-I`` keeps the
downloaded (untrusted) data and the current directory off ``sys.path``; the repository root is
added explicitly so ``app`` and ``research`` import.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigger-moves-study")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("parity", "run", "holdout", "calibrate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--data-dir", type=Path, required=True)
        cmd.add_argument("--out", type=Path, required=True)
    sub.choices["parity"].add_argument("--symbols", default=None)
    sub.choices["parity"].add_argument("--execution-symbols", default=None)
    for name in ("run", "holdout"):
        sub.choices[name].add_argument("--symbols", default=None, help="comma list (testing only)")
    args = parser.parse_args(argv)

    if args.command == "parity":
        from research.bigger_moves import parity

        ok = parity.run(args.data_dir, args.out,
                        symbols=args.symbols.split(",") if args.symbols else None,
                        execution=args.execution_symbols.split(",") if args.execution_symbols else None)
        return 0 if ok else 1
    if args.command in ("run", "holdout"):
        from research.bigger_moves import study

        study.run(args.data_dir, args.out, period="main" if args.command == "run" else "holdout",
                  only=args.symbols.split(",") if args.symbols else None)
        return 0
    if args.command == "calibrate":
        from research.bigger_moves import calibration

        calibration.run(args.data_dir, args.out)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
