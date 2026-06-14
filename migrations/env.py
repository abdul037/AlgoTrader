from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.runtime_settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# Alembic stores options in ConfigParser, where percent signs trigger
# interpolation. Database URLs commonly contain percent-encoded passwords.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
