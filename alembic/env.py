"""
Alembic environment configuration.
Uses the async SQLAlchemy engine and reads DATABASE_URL from environment.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Alembic Config object for access to values in alembic.ini
config = context.config

# Setup Python logging from alembic.ini config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can detect schema changes
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.models import Base  # noqa: E402

target_metadata = Base.metadata


def get_url() -> str:
    """Return the database URL from environment or alembic.ini."""
    return os.environ.get(
        "DATABASE_URL",
        config.get_main_option("sqlalchemy.url", ""),
    )


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    so a DBAPI is not required.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations with a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create async engine and run migrations."""
    url = get_url()
    connectable = create_async_engine(url, echo=False)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using asyncio."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
