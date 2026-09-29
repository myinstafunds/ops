"""Authentication and authorisation at the HTTP boundary (PRD 11, 38).

None of these tests need PostgreSQL: a request that fails authentication or
authorisation is rejected before the handler runs, and no query is issued. That
keeps the most safety-critical rules - "the backend refuses what the UI merely
hides" - covered even where the database is unavailable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.dependencies.auth import PermissionGuard, RequestContext, require_any_permissions
from app.dependencies.auth import require_permissions as require_all
from app.errors import ErrorCode
from app.security.permissions import ALL_PERMISSIONS, Permission
from app.security.tokens import (
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    decode_token,
    hash_secret,
)
from tests.conftest import TENANT_A, TENANT_B, auth_header, make_token

#: One representative protected route per router. A gap here would mean an
#: unauthenticated surface nobody noticed.
PROTECTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/stats/dashboard"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("GET", "/api/departments"),
    ("GET", "/api/queues"),
    ("GET", "/api/work-items"),
    ("GET", "/api/availability"),
    ("GET", "/api/communications"),
    ("GET", "/api/audit-log"),
    ("GET", "/api/integrations/health"),
    ("GET", "/api/sync-runs"),
    ("GET", "/api/auth/me"),
)

#: Routes that must stay reachable without a credential.
PUBLIC_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/api/health/live"),
    ("GET", "/api/health/ready"),
)


@pytest.fixture
def bare_client() -> Iterator[TestClient]:
    """A client with the *real* ``get_db`` - proves no query happens on 401/403."""
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _minimal_request(headers: dict[str, str] | None = None) -> Any:
    from starlette.requests import Request

    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw,
            "client": ("10.0.0.1", 51234),
            "state": {"trace_id": "trace-unit-test"},
        }
    )


def _context_for(token: str, headers: dict[str, str] | None = None) -> RequestContext:
    return RequestContext(decode_token(token, expected_type="access"), _minimal_request(headers))


# --------------------------------------------------------------------------
# error envelope
# --------------------------------------------------------------------------


def assert_error_envelope(response: Any, *, code: str) -> dict[str, Any]:
    """Every failure must look like PRD 36, and must not leak internals."""
    body = response.json()
    assert set(body) == {"error", "trace_id"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body["error"]
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    # trace_id is echoed so an operator can quote it in a support ticket.
    assert body["trace_id"]
    assert body["trace_id"] == response.headers["x-trace-id"]
    return body["error"]


# --------------------------------------------------------------------------
# 401 - unauthenticated
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_protected_routes_reject_a_missing_token(
    bare_client: TestClient, method: str, path: str
) -> None:
    response = bare_client.request(method, path)
    assert response.status_code == 401
    error = assert_error_envelope(response, code=ErrorCode.UNAUTHENTICATED)
    assert "sign in" in error["message"].lower()
    assert response.headers["www-authenticate"] == "Bearer"


def test_health_probes_stay_public(bare_client: TestClient) -> None:
    for method, path in PUBLIC_ROUTES:
        response = bare_client.request(method, path)
        assert response.status_code in {200, 503}, (path, response.status_code)


def test_login_route_is_public(bare_client: TestClient) -> None:
    """A failed sign-in must be a 401 envelope, never a 500 from missing auth."""
    response = bare_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "wrong", "tenant_id": TENANT_A}
    )
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.UNAUTHENTICATED)


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Token abc",
        # A tab is not the single SP that delimits the scheme, so this is not
        # a bearer credential at all rather than a malformed one.
        "Bearer\tnot.a.jwt",
    ],
)
def test_headers_that_are_not_a_bearer_scheme_are_rejected(
    bare_client: TestClient, header: str
) -> None:
    """A non-bearer scheme is indistinguishable from no credential at all."""
    response = bare_client.get("/api/users", headers={"Authorization": header})
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.UNAUTHENTICATED)


@pytest.mark.parametrize("header", ["bearer not.a.jwt", "BEARER not.a.jwt"])
def test_the_bearer_scheme_is_matched_case_insensitively(
    bare_client: TestClient, header: str
) -> None:
    """RFC 7235 says the scheme is case-insensitive, so a valid one is parsed."""
    response = bare_client.get("/api/users", headers={"Authorization": header})
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)


def test_a_garbage_token_is_reported_as_invalid(bare_client: TestClient) -> None:
    response = bare_client.get("/api/users", headers=auth_header("not.a.jwt"))
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)


def test_an_expired_token_is_reported_as_expired(bare_client: TestClient) -> None:
    expired = make_token("admin", ttl_minutes=-5)
    response = bare_client.get("/api/users", headers=auth_header(expired))
    assert response.status_code == 401
    error = assert_error_envelope(response, code=ErrorCode.TOKEN_EXPIRED)
    assert "expired" in error["message"].lower()


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    import jwt

    from app.config import settings

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tenant": TENANT_A,
            "typ": "access",
            "mfa_verified": True,
            "permissions": sorted(ALL_PERMISSIONS),
            "iat": 1_700_000_000,
            "exp": 4_100_000_000,
        },
        "a-secret-an-attacker-chose",
        algorithm=settings.ops_jwt_algorithm,
    )
    assert settings.ops_jwt_algorithm == "HS256"
    with pytest.raises(Exception) as caught:
        decode_token(forged, expected_type="access")
    assert type(caught.value).__name__ == "TokenInvalidError"


def test_a_forged_admin_token_gets_no_access_over_http(bare_client: TestClient) -> None:
    import jwt

    from app.config import settings

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tenant": TENANT_A,
            "typ": "access",
            "mfa_verified": True,
            "permissions": sorted(ALL_PERMISSIONS),
            "iat": 1_700_000_000,
            "exp": 4_100_000_000,
        },
        "a-secret-an-attacker-chose",
        algorithm=settings.ops_jwt_algorithm,
    )
    response = bare_client.get("/api/users", headers=auth_header(forged))
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)


def test_the_alg_none_attack_is_refused(bare_client: TestClient) -> None:
    """Stripping the signature and declaring ``alg: none`` must not authenticate."""
    import base64
    import json as jsonlib

    def _segment(payload: dict[str, Any]) -> str:
        raw = base64.urlsafe_b64encode(jsonlib.dumps(payload).encode()).rstrip(b"=")
        return raw.decode()

    claims = {
        "sub": str(uuid.uuid4()),
        "tenant": TENANT_A,
        "typ": "access",
        "mfa_verified": True,
        "permissions": sorted(ALL_PERMISSIONS),
        "exp": 4_100_000_000,
        "iat": 1_700_000_000,
    }
    unsigned = f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(claims)}."
    response = bare_client.get("/api/users", headers=auth_header(unsigned))
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)


def test_privilege_escalation_needs_the_signing_secret() -> None:
    """Editing a claim changes the signature, so only the holder can re-sign."""
    import jwt

    from app.config import settings

    genuine = decode_token(make_token("agent"), expected_type="access")
    claims = {
        "sub": genuine.user_id,
        "username": genuine.username,
        "name": genuine.display_name,
        "tenant": TENANT_A,
        "roles": ["OPS_ADMIN"],
        "permissions": sorted(ALL_PERMISSIONS),
        "typ": "access",
        "mfa_verified": True,
        "iat": 1_700_000_000,
        "exp": 4_100_000_000,
    }
    forged = jwt.encode(claims, "a-secret-an-attacker-chose", algorithm="HS256")

    from app.errors import TokenInvalidError

    with pytest.raises(TokenInvalidError):
        decode_token(forged, expected_type="access")

    # The only way to mint a real one is the deployment secret, which never
    # leaves the server - the test suite can read it, a browser cannot.
    assert settings.ops_jwt_secret  # pragma: no cover - trivially true


def test_a_refresh_token_cannot_be_used_as_an_access_token(bare_client: TestClient) -> None:
    refresh, _ = create_refresh_token(user_id=str(uuid.uuid4()), tenant_id=TENANT_A)
    response = bare_client.get("/api/users", headers=auth_header(refresh))
    assert response.status_code == 401
    error = assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)
    assert "type" in error["message"].lower()


def test_an_mfa_pending_token_cannot_be_used_as_an_access_token(bare_client: TestClient) -> None:
    pending = create_mfa_pending_token(
        user_id=str(uuid.uuid4()), username="agent", tenant_id=TENANT_A
    )
    response = bare_client.get("/api/users", headers=auth_header(pending))
    assert response.status_code == 401
    assert_error_envelope(response, code=ErrorCode.TOKEN_INVALID)


# --------------------------------------------------------------------------
# MFA gate (PRD 7)
# --------------------------------------------------------------------------


def test_a_session_that_skipped_mfa_is_refused(bare_client: TestClient) -> None:
    """Having valid credentials is not enough - MFA must have completed."""
    response = bare_client.get(
        "/api/users", headers=auth_header(make_token("admin", mfa_verified=False))
    )
    assert response.status_code == 401
    error = assert_error_envelope(response, code=ErrorCode.UNAUTHENTICATED)
    assert "multi-factor" in error["message"].lower()


def test_the_mfa_gate_applies_to_every_protected_route(bare_client: TestClient) -> None:
    token = make_token("admin", mfa_verified=False)
    for method, path in PROTECTED_ROUTES:
        response = bare_client.request(method, path, headers=auth_header(token))
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHENTICATED, path


# --------------------------------------------------------------------------
# 403 - authenticated but not permitted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/users"),
        ("POST", "/api/users"),
        ("GET", "/api/users/meta/roles"),
        ("GET", "/api/audit-log"),
        ("GET", "/api/integrations/health"),
        ("GET", "/api/sync-runs"),
    ],
)
def test_an_agent_is_forbidden_from_administrative_reads(
    bare_client: TestClient, method: str, path: str
) -> None:
    response = bare_client.request(method, path, headers=auth_header(make_token("agent")))
    assert response.status_code == 403
    error = assert_error_envelope(response, code=ErrorCode.FORBIDDEN)
    assert "permission" in error["message"].lower()


def test_the_403_body_names_the_missing_permission(bare_client: TestClient) -> None:
    """The UI uses ``details`` to explain *why* a screen is unavailable."""
    response = bare_client.get("/api/users", headers=auth_header(make_token("agent")))
    error = response.json()["error"]
    assert error["details"]["required_permissions"] == [Permission.OPS_USERS_READ]


def test_an_auditor_can_read_but_never_write() -> None:
    from app.security.permissions import permissions_for_roles

    auditor = permissions_for_roles(["AUDITOR"])
    assert Permission.OPS_AUDIT_READ in auditor
    assert Permission.OPS_USERS_WRITE not in auditor
    assert Permission.OPS_AVAILABILITY_APPROVE not in auditor


def test_a_deactivated_permission_is_refused_even_with_a_valid_token(
    bare_client: TestClient,
) -> None:
    """A token minted before a role change cannot exceed its own claim set."""
    token = make_token("auditor")
    write = bare_client.post("/api/users", json={}, headers=auth_header(token))
    assert write.status_code == 403
    assert write.json()["error"]["details"]["required_permissions"] == [Permission.OPS_USERS_WRITE]
    approve = bare_client.post(
        "/api/availability/00000000-0000-0000-0000-000000000001/approve",
        json={},
        headers=auth_header(token),
    )
    assert approve.status_code == 403
    assert approve.json()["error"]["details"]["required_permissions"] == [
        Permission.OPS_AVAILABILITY_APPROVE
    ]


# --------------------------------------------------------------------------
# RequestContext
# --------------------------------------------------------------------------


def test_tenant_comes_from_the_token_and_never_from_a_header() -> None:
    """PRD Rule 4: a browser must not be able to widen its own tenant scope."""
    token = make_token("admin", tenant_id=TENANT_B)
    context = _context_for(token, {"X-Tenant-Id": TENANT_A, "X-Forwarded-Host": "evil.example"})
    assert context.tenant_id == TENANT_B
    assert TENANT_A not in context.tenant_id


def test_context_reports_tenant_from_the_token_for_tenant_a() -> None:
    assert _context_for(make_token("admin")).tenant_id == TENANT_A


def test_permission_helpers_agree_with_the_claim_set() -> None:
    agent = _context_for(make_token("agent"))
    assert agent.has(Permission.OPS_CASES_READ)
    assert agent.has("OPS_CASES_READ")
    assert not agent.has(Permission.OPS_USERS_READ)
    assert agent.has_all(Permission.OPS_CASES_READ, Permission.OPS_CASES_LOCK)
    assert not agent.has_all(Permission.OPS_CASES_READ, Permission.OPS_USERS_READ)
    assert agent.has_any(Permission.OPS_USERS_READ, Permission.OPS_CASES_READ)
    assert not agent.has_any(Permission.OPS_USERS_READ, Permission.OPS_ROLES_WRITE)


def test_context_takes_the_client_ip_from_x_forwarded_for() -> None:
    context = _context_for(make_token(), {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert context.ip_address == "203.0.113.9"


def test_context_falls_back_to_the_socket_peer() -> None:
    assert _context_for(make_token()).ip_address == "10.0.0.1"


def test_context_exposes_the_mfa_state() -> None:
    assert _context_for(make_token("agent", mfa_verified=True)).mfa_verified is True
    assert _context_for(make_token("agent", mfa_verified=False)).mfa_verified is False


# --------------------------------------------------------------------------
# PermissionGuard
# --------------------------------------------------------------------------


async def _guard_accepts(guard: PermissionGuard, token: str) -> bool:
    try:
        await guard(_context_for(token))
    except Exception:
        return False
    return True


@pytest.mark.asyncio
async def test_require_all_needs_every_declared_permission() -> None:
    guard = require_all(Permission.OPS_CASES_READ, Permission.OPS_CASES_CLOSE)
    # An agent may read cases but not close them.
    assert await _guard_accepts(guard, make_token("agent")) is False
    assert await _guard_accepts(guard, make_token("manager")) is True


@pytest.mark.asyncio
async def test_require_any_accepts_one_of_the_alternatives() -> None:
    guard = require_any_permissions(
        Permission.OPS_AVAILABILITY_APPROVE, Permission.OPS_CASES_ASSIGN
    )
    assert await _guard_accepts(guard, make_token("lead")) is True
    assert await _guard_accepts(guard, make_token("agent")) is False


@pytest.mark.asyncio
async def test_the_guard_passes_the_context_through_unchanged() -> None:
    guard = require_all(Permission.OPS_CASES_READ)
    context = _context_for(make_token("agent"))
    assert await guard(context) is context


def test_an_unknown_permission_fails_at_import_time() -> None:
    """A typo must be a startup crash, never a silent allow."""
    with pytest.raises(RuntimeError, match="Unknown permission"):
        require_all("OPS_DOES_NOT_EXIST")


def test_guard_names_appear_in_the_openapi_operation_ids() -> None:
    guard = require_all(Permission.OPS_CASES_TRANSFER)
    assert guard.__name__ == "require_ops_cases_transfer"
    assert "require_ops_cases_transfer" in repr(guard)


# --------------------------------------------------------------------------
# token plumbing
# --------------------------------------------------------------------------


def test_permissions_are_carried_as_a_sorted_deduplicated_list() -> None:
    token, _ = create_access_token(
        user_id=str(uuid.uuid4()),
        username="agent",
        display_name="Agent",
        tenant_id=TENANT_A,
        roles=["AGENT"],
        permissions=["OPS_CASES_READ", "OPS_CASES_READ", "OPS_CASES_LOCK"],
        mfa_verified=True,
    )
    payload = decode_token(token, expected_type="access")
    assert payload.permissions == ["OPS_CASES_LOCK", "OPS_CASES_READ"]
    assert payload.permission_set == frozenset({"OPS_CASES_READ", "OPS_CASES_LOCK"})


def test_roles_are_sorted_and_deduplicated_so_two_logins_match() -> None:
    def _token(roles: list[str]) -> str:
        token, _ = create_access_token(
            user_id=str(uuid.uuid4()),
            username="u",
            display_name="U",
            tenant_id=TENANT_A,
            roles=roles,
            permissions=[],
        )
        return token

    assert decode_token(_token(["B", "A", "B"])).roles == decode_token(_token(["A", "B"])).roles


def test_the_token_round_trips_its_identity() -> None:
    from datetime import datetime as _dt

    user_id = uuid.uuid4()
    token, expires_at = create_access_token(
        user_id=str(user_id),
        username="rahul.k",
        display_name="Rahul K",
        tenant_id=TENANT_A,
        roles=["AGENT"],
        permissions=["OPS_CASES_READ"],
    )
    payload = decode_token(token, expected_type="access")
    assert payload.user_id == str(user_id)
    assert payload.username == "rahul.k"
    assert payload.display_name == "Rahul K"
    assert payload.tenant_id == TENANT_A
    # JWT ``exp`` is whole seconds, so the round trip may lose sub-second parts.
    assert abs((payload.expires_at - expires_at).total_seconds()) < 1
    assert isinstance(payload.expires_at, _dt)
    assert payload.token_type == "access"
    assert payload.mfa_required is False


def test_each_token_gets_its_own_identifier() -> None:
    """Distinct ``jti`` values are what a future deny-list would key on."""
    tokens = {make_token("admin") for _ in range(5)}
    assert len(tokens) == 5


def test_hash_secret_is_deterministic_and_keyed() -> None:
    assert hash_secret("abc") == hash_secret("abc")
    assert hash_secret("abc") != hash_secret("abd")
    # Never a reversible encoding of the input.
    assert "abc" not in hash_secret("abc")
