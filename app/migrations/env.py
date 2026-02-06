import os
import sys
from pathlib import Path

# Ensure project root is on path so "app" is importable (e.g. on Render)
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from logging.config import fileConfig

from sqlalchemy import create_engine, engine_from_config
from sqlalchemy import pool

from alembic import context

from app.db import Base
from app import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


target_metadata = Base.metadata


def _get_url():
    """Use DATABASE_URL on Render/production; else alembic.ini."""
    url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url or "postgresql://postgres:postgres@localhost:5432/competitor_signals"


def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
