"""Outbox SQLAlchemy engine — isolated from the main ``deerflow.db``.

Why a separate database file?

- **Independent backup cadence.** A checkpoint table corruption must
  not block replay, and vice versa.
- **No alembic dependency.** The outbox owns its own schema and
  ``create_all`` bootstrap. Operators can wipe ``channel_outbox.db``
  to reset replay state without touching LangGraph checkpoints.
- **Predictable location.** Always at
  ``<sqlite_dir>/channel_outbox.db`` relative to the gateway's CWD,
  even when the main ``database.backend`` is ``postgres`` (then the
  outbox uses a sibling SQLite file so a single-process gateway
  restart does not require a cross-store transaction).
- **No-op when disabled.** When ``channels.outbox.enabled`` is false
  the gateway never instantiates this engine and the file is never
  created. Operators can enable later without code changes.

The engine mirrors ``deerflow.persistence.engine`` on purpose: same
WAL + busy_timeout + foreign_keys pragmas, same session-factory shape,
so the repository code is boring SQLAlchemy and reads identically on
SQLite and Postgres. (Postgres support is intentionally not wired in
this commit; the dispatcher still only runs in single-process mode.)
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.channels.outbox.model import OutboxRow
from deerflow.persistence.base import Base

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_FILENAME = "channel_outbox.db"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def resolve_outbox_path(sqlite_dir: str | Path) -> Path:
    """Return the on-disk path for the outbox SQLite file.

    ``sqlite_dir`` is the operator-configured ``database.sqlite_dir``
    (typically ``.berkshire-agent/data``). The file is named
    ``channel_outbox.db`` so it sits alongside ``deerflow.db`` and is
    easy to grep / rsync / back up.
    """
    return Path(sqlite_dir).expanduser().resolve() / DEFAULT_OUTBOX_FILENAME


async def init_outbox_engine(sqlite_dir: str | Path, *, echo: bool = False) -> AsyncEngine:
    """Initialise the outbox engine. Idempotent.

    Returns the engine so callers can chain ``await init_outbox_engine(...)``
    into ``with outbox_session() as session:`` without re-fetching the
    global. Tests that want a fresh DB should call ``close_outbox_engine``
    first.
    """
    global _engine, _session_factory

    if _engine is not None and _session_factory is not None:
        return _engine

    path = resolve_outbox_path(sqlite_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    logger.info("Initialising channel outbox engine at %s", path)

    _engine = create_async_engine(url, echo=echo, future=True)
    _configure_sqlite_pragmas(_engine)

    # create_all is enough: the outbox has no alembic migrations (yet).
    # When it grows one, swap this for ``bootstrap_schema`` like the
    # main persistence engine does.
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[OutboxRow.__table__])

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _configure_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Mirror the main engine's WAL / busy_timeout pragmas.

    PRAGMA settings are per-connection so they must run on connect.
    See ``deerflow.persistence.engine`` for the long-form rationale.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_wal(dbapi_conn: object, _record: object) -> None:  # noqa: ARG001
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=30000;")
        finally:
            cursor.close()


def get_outbox_engine() -> AsyncEngine | None:
    """Return the global engine, or ``None`` if not initialised."""
    return _engine


def get_outbox_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """Return the global session factory, or ``None`` if not initialised."""
    return _session_factory


@asynccontextmanager
async def outbox_session() -> AsyncIterator[AsyncSession]:
    """Async context manager that yields a fresh session.

    Commits on clean exit, rolls back on exception. The caller never
    has to think about the commit boundary unless it explicitly needs
    a savepoint — for the outbox, a single transaction per logical
    operation (insert / mark_delivered / sweep) is correct.
    """
    if _session_factory is None:
        raise RuntimeError("Outbox session factory not initialised. Call init_outbox_engine() during gateway startup (only when channels.outbox.enabled=true) and ensure database.sqlite_dir is configured.")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_outbox_engine() -> None:
    """Dispose the engine and release the globals.

    Safe to call multiple times. Tests call this between cases; the
    gateway shutdown path calls it once during teardown.
    """
    global _engine, _session_factory

    engine = _engine
    if engine is None:
        _session_factory = None
        return

    _engine = None
    _session_factory = None
    await engine.dispose()


def is_outbox_disabled_by_env() -> bool:
    """Operator escape hatch.

    Set ``DEER_FLOW_DISABLE_OUTBOX=1`` to force the outbox off even
    when ``channels.outbox.enabled=true``. Useful when an operator
    wants to recover from a wedged replay loop without touching YAML.
    """
    return os.environ.get("DEER_FLOW_DISABLE_OUTBOX") == "1"
