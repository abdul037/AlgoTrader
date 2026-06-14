"""Securely configure Supabase PostgreSQL URLs in the local .env file."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from urllib.parse import quote


def replace_or_append(lines: list[str], name: str, value: str) -> list[str]:
    replacement = f"{name}={value}\n"
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = replacement
            return lines
    lines.append(replacement)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--pooler-host", required=True)
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        raise SystemExit(f"Environment file does not exist: {env_path}")

    password = getpass.getpass("Supabase database password: ")
    confirmation = getpass.getpass("Confirm database password: ")
    if not password or password != confirmation:
        raise SystemExit("Passwords are empty or do not match")

    encoded_password = quote(password, safe="")
    authority = f"postgres.{args.project_ref}:{encoded_password}@{args.pooler_host}:5432"
    database_url = f"postgresql+psycopg://{authority}/postgres?sslmode=require"
    backup_url = f"postgresql://{authority}/postgres?sslmode=require"

    lines = env_path.read_text().splitlines(keepends=True)
    lines = replace_or_append(lines, "DATABASE_URL", database_url)
    lines = replace_or_append(lines, "POSTGRES_BACKUP_URL", backup_url)
    env_path.write_text("".join(lines))
    print(f"Configured Supabase database URLs in {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
