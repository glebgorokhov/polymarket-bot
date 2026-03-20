"""
Async SQLAlchemy session factory.
Provides async_engine and AsyncSession for all DB operations.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    """Initialize and return the async engine (call once at startup)."""
    global _engine, _session_factory
    settings = get_settings()
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return _engine


def get_engine() -> AsyncEngine:
    """Return the initialized engine. Raises if not yet initialized."""
    if _engine is None:
        raise RuntimeError("DB engine not initialized. Call init_engine() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory."""
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized. Call init_engine() first.")
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding a database session with automatic commit/rollback."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
