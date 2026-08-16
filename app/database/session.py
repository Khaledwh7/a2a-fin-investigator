"""Async database engine + session factory.

SQLite via aiosqlite by default. We hand out an ``async_sessionmaker`` that the
stores use; table creation is explicit (``init_models``) so tests can control
when it happens.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base


def make_engine(database_url: str) -> AsyncEngine:
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        # A generous busy timeout keeps concurrent writers from erroring while
        # they wait for the write lock.
        connect_args["timeout"] = 30
        _ensure_sqlite_dir(database_url)
    return create_async_engine(database_url, future=True, connect_args=connect_args)


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-based SQLite DB if it's missing."""
    import pathlib

    path = database_url.split(":///", 1)[-1]
    if path and path != ":memory:":
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_models(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
