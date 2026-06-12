"""Copy the existing AlgoBot SQLite state into an initialized PostgreSQL database."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from app.runtime_settings import AppSettings
from app.storage.db import Database

AUTO_ID_TABLES = {
    "alert_history",
    "portfolio_snapshots",
    "reconciliation_runs",
    "run_logs",
    "scan_decisions",
    "signal_outcomes",
    "tracked_signals",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="./etoro_bot.db")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.exists():
        raise SystemExit(f"SQLite source does not exist: {source_path}")

    target_settings = AppSettings(database_url=args.target_url)
    Database(target_settings).initialize()
    source_engine = create_engine(f"sqlite:///{source_path}", future=True)
    target_engine = create_engine(args.target_url, future=True)
    source_metadata = MetaData()
    target_metadata = MetaData()
    source_metadata.reflect(bind=source_engine)
    target_metadata.reflect(bind=target_engine)

    copied: dict[str, int] = {}
    with source_engine.connect() as source, target_engine.begin() as target:
        for table_name in inspect(source_engine).get_table_names():
            if table_name == "sqlite_sequence" or table_name not in target_metadata.tables:
                continue
            source_table = Table(table_name, source_metadata, autoload_with=source_engine)
            target_table = Table(table_name, target_metadata, autoload_with=target_engine)
            rows = [dict(row) for row in source.execute(select(source_table)).mappings()]
            copied[table_name] = len(rows)
            if rows and not args.dry_run:
                existing = int(target.execute(select(target_table).limit(1)).fetchone() is not None)
                if existing:
                    raise RuntimeError(f"Target table {table_name} is not empty")
                target.execute(target_table.insert(), rows)
                if table_name in AUTO_ID_TABLES:
                    target.execute(
                        text(
                            f"""
                            SELECT setval(
                                pg_get_serial_sequence(:table_name, 'id'),
                                (SELECT MAX(id) FROM {table_name}),
                                true
                            )
                            """
                        ),
                        {"table_name": table_name},
                    )

    for table_name, count in sorted(copied.items()):
        print(f"{table_name}: {count}")
    print("DRY RUN COMPLETE" if args.dry_run else "MIGRATION COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
