"""Shared pytest fixtures.

Two tiers of test live in this suite:

* **Pure tests** need nothing but the application modules - masking, the
  permission registry, token handling and the allocation algorithm are all
  deterministic functions of their inputs.
* **Database tests** need PostgreSQL with the schema created from the models.
  When no database is reachable they are skipped rather than failed, so the
  suite stays runnable on a laptop without Docker. Start one with
  ``docker compose up -d db`` (see ``backend/README.md``).

Settings are pinned to ``test`` *before* ``app.config`` is imported so nothing
in the suite can accidentally reach a development database or the real BRE
identity provider.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

os.environ.setdefault("OPS_ENV", "test")
os.environ.setdefault("OPS_AUTH_MODE", "local")
os.environ.setdefault("OPS_LOCAL_AUTH_ENABLED", "true")
os.environ.setdefault("OPS_TENANT_ID", "BPSEC")
os.environ.setdefault("OPS_JWT_SECRET", "test-secret-not-used-anywhere-else")
os.environ.setdefault("OPS_LOG_FORMAT", "console")
os.environ.setdefault("OPS_LOG_LEVEL", "WARNING")

from app.config import settings
from app.security.permissions import (
    Permission,
    permissions_for_roles,
)
from app.security.tokens import create_access_token

TENANT_A = "BPSEC"
TENANT_B = "OTHERCO"

#: Personas used across the suite. ``admin`` can do everything, ``agent`` is a
#: deliberately narrow operational role used to prove 403 behaviour.
PERSONA_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(permission.value for permission in Permission),
    "manager": permissions_for_roles(["OPS_MANAGER"]),
    "lead": permissions_for_roles(["TEAM_LEAD"]),
    "agent": permissions_for_roles(["AGENT"]),
    "auditor": permissions_for_roles(["AUDITOR"]),
}


def make_token(
    persona: str = "admin",
    *,
    tenant_id: str = TENANT_A,
    user_id: uuid.UUID | None = None,
    mfa_verified: bool = True,
    ttl_minutes: int = 30,
) -> str:
    """Issue a signed access token for a persona.

    Tokens are minted directly rather than through ``/auth/login`` so a test can
    express "a user from another tenant" or "a user who never finished MFA"
    without faking a BRE response.
    """
    permissions = sorted(PERSONA_PERMISSIONS.get(persona, frozenset()))
    token, _ = create_access_token(
        user_id=str(user_id or uuid.uuid4()),
        username=persona,
        display_name=persona.title(),
        tenant_id=tenant_id,
        roles=[persona.upper()],
        permissions=permissions,
        mfa_verified=mfa_verified,
        ttl_minutes=ttl_minutes,
    )
    return token


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token() -> str:
    return make_token("admin")


@pytest.fixture
def agent_token() -> str:
    return make_token("agent")


@pytest.fixture
def other_tenant_token() -> str:
    return make_token("admin", tenant_id=TENANT_B)


@pytest.fixture
def unauthenticated_mfa_token() -> str:
    return make_token("admin", mfa_verified=False)


@pytest.fixture(scope="session")
def engine() -> Iterator[Any]:
    """A schema-backed engine, or a skip when PostgreSQL is unavailable."""
    from sqlalchemy import create_engine, text

    url = os.environ.get("TEST_DATABASE_URL") or settings.sync_database_url
    candidate = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with candidate.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        candidate.dispose()
        pytest.skip(f"PostgreSQL is not reachable for tests: {type(exc).__name__}")

    from app.models import Base  # registers every table on the shared metadata

    Base.metadata.drop_all(candidate)
    Base.metadata.create_all(candidate)
    try:
        yield candidate
    finally:
        Base.metadata.drop_all(candidate)
        candidate.dispose()


@pytest.fixture
def session(engine: Any) -> Iterator[Any]:
    """One transaction per test, always rolled back."""
    from sqlalchemy.orm import Session

    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(engine: Any) -> Iterator[Any]:
    """A ``TestClient`` wired to the transactional session fixture."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override() -> Iterator[Any]:
        from sqlalchemy.orm import Session

        connection = engine.connect()
        transaction = connection.begin()
        db_session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        try:
            yield db_session
            transaction.commit()
        finally:
            transaction.rollback()
            connection.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def write_context() -> Any:
    """A ``WriteContext`` for service level tests."""
    from app.services.org import WriteContext

    return WriteContext(
        tenant_id=TENANT_A,
        actor_user_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        actor_name="Test Admin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )


@pytest.fixture
def soon() -> datetime:
    return datetime.now(UTC) + timedelta(hours=2)
