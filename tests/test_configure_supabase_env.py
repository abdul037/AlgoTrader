from __future__ import annotations

import sys

from scripts import configure_supabase_env


def test_configure_supabase_env_encodes_password_and_updates_urls(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=sqlite:///./etoro_bot.db\nKEEP_ME=true\n")
    passwords = iter(["new p@ss%", "new p@ss%"])
    monkeypatch.setattr(configure_supabase_env.getpass, "getpass", lambda _prompt: next(passwords))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_supabase_env.py",
            "--env-file",
            str(env_path),
            "--project-ref",
            "project-ref",
            "--pooler-host",
            "pooler.example.com",
        ],
    )

    assert configure_supabase_env.main() == 0

    content = env_path.read_text()
    assert "KEEP_ME=true" in content
    assert "new p@ss%" not in content
    assert (
        "DATABASE_URL=postgresql+psycopg://postgres.project-ref:"
        "new%20p%40ss%25@pooler.example.com:5432/postgres?sslmode=require"
    ) in content
    assert (
        "POSTGRES_BACKUP_URL=postgresql://postgres.project-ref:"
        "new%20p%40ss%25@pooler.example.com:5432/postgres?sslmode=require"
    ) in content
