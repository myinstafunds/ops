"""Database engine, session factory and the FastAPI session dependency.

Every query in this application is tenant scoped. Tenant filtering is enforced
in the repository/service layer, never from browser supplied values
(PRD 40 / Rule 4).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for every Ops table."""


def _build_engine() -> Engine:
    kwargs: dict[str, object] = {
        "echo": settings.db_echo or settings.ops_log_sql,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.database_url.startswith("postgresql"):
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
    return create_engine(settings.database_url, **kwargs)


engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session]:
    """FastAPI dependency yielding a request scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session]:
    """Context manager for background jobs, scripts and tests."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_engine() -> Engine:
    return engine
