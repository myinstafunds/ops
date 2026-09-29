"""The permission registry and the route guards that consume it (PRD 11, 51).

Two invariants are enforced here, and both are structural rather than
behavioural, which is the point - they fail at *test* time for a mistake that
would otherwise fail silently in production:

1. Every permission a route declares exists in the central registry, so the
   OpenAPI surface and the token claims can never drift apart.
2. Every ``/api`` route other than health and sign-in is actually guarded.

Invariant 2 has a subtle failure mode worth naming. A guard is a *callable
object* with no ``__globals__``, so if it declares its context parameter with
``Annotated[..., Depends(...)]`` FastAPI cannot resolve the annotation and
silently turns it into a required query parameter. Every guarded route then
returns 422 and the guard is never reached. ``test_no_dependency_leaked_into_query_params``
pins that down.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

from app.dependencies.auth import PermissionGuard, RequestContext
from app.main import app as fastapi_app
from app.security.permissions import (
    ALL_PERMISSIONS,
    DASHBOARD_PERMISSIONS,
    READ_ONLY,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    Permission,
    RoleKey,
    is_known_permission,
    permissions_for_roles,
)

#: Routes reachable without a bearer token. Everything else must be guarded.
UNGUARDED_BY_DESIGN: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/health/live",
        "/health/ready",
        "/api/health",
        "/api/health/live",
        "/api/health/ready",
        "/api/auth/login",
        "/api/auth/mfa/verify",
        "/api/auth/refresh",
        # Self-service: any signed-in user may read their own profile and end
        # their own session. No role gate applies.
        "/api/auth/logout",
        "/api/auth/me",
    }
)

#: Parameter names that can only ever come from a mis-wired dependency.
INJECTED_PARAM_NAMES: frozenset[str] = frozenset({"context", "session", "_", "credentials"})


def _api_routes(application: FastAPI = fastapi_app) -> list[APIRoute]:
    return [route for route in application.routes if isinstance(route, APIRoute)]


def _guards(dependant: Dependant) -> list[PermissionGuard]:
    """Every :class:`PermissionGuard` anywhere in the dependency tree."""
    found: list[PermissionGuard] = []
    for sub in dependant.dependencies:
        if isinstance(sub.call, PermissionGuard):
            found.append(sub.call)
        found.extend(_guards(sub))
    return found


def _sub_dependants(dependant: Dependant) -> list[Dependant]:
    out: list[Dependant] = []
    for sub in dependant.dependencies:
        out.append(sub)
        out.extend(_sub_dependants(sub))
    return out


# --------------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------------


def test_every_permission_is_namespaced_to_ops() -> None:
    """A missing prefix would let an Ops grant collide with another system's."""
    for permission in Permission:
        assert permission.value.startswith("OPS_"), permission
        assert permission.value == permission.value.upper(), permission


def test_all_permissions_matches_the_enum() -> None:
    assert {p.value for p in Permission} == ALL_PERMISSIONS
    assert len(ALL_PERMISSIONS) == len(Permission)


def test_is_known_permission_rejects_typos() -> None:
    assert is_known_permission("OPS_CASES_READ")
    assert not is_known_permission("ops_cases_read")
    assert not is_known_permission("OPS_CASE_READ")
    assert not is_known_permission("")


def test_every_role_has_a_human_label() -> None:
    assert set(ROLE_LABELS) == set(RoleKey)
    for key, label in ROLE_LABELS.items():
        assert label.strip(), key
        assert not label.islower(), label


def test_every_role_grants_only_known_permissions() -> None:
    for role, granted in ROLE_PERMISSIONS.items():
        unknown = set(granted) - ALL_PERMISSIONS
        assert not unknown, f"{role} grants unknown permissions: {unknown}"


def test_every_role_can_reach_the_dashboard() -> None:
    """Otherwise the SPA would sign someone in to a permanently empty shell."""
    for role, granted in ROLE_PERMISSIONS.items():
        missing = set(DASHBOARD_PERMISSIONS) - set(granted)
        assert not missing, f"{role} cannot open the dashboard (missing {missing})"


def test_the_admin_role_can_do_everything() -> None:
    assert ROLE_PERMISSIONS[RoleKey.OPS_ADMIN] == ALL_PERMISSIONS


def test_read_only_means_read_only() -> None:
    """No write, approval, export or write-back permission may sneak in."""
    forbidden = {
        p for p in ALL_PERMISSIONS if p.endswith(("_WRITE", "_APPROVE", "_APPROVED", "_WRITEBACK"))
    }
    assert READ_ONLY.isdisjoint(forbidden)
    assert ROLE_PERMISSIONS[RoleKey.AUDITOR] == READ_ONLY


def test_the_auditor_role_grants_nothing_extra() -> None:
    """Read-only is the entire point of the role; widening it is a decision."""
    assert ROLE_PERMISSIONS[RoleKey.AUDITOR] <= READ_ONLY


def test_permission_grants_only_grow_up_the_hierarchy() -> None:
    order = [
        RoleKey.AGENT,
        RoleKey.TEAM_LEAD,
        RoleKey.OPS_MANAGER,
        RoleKey.OPS_ADMIN,
    ]
    for lower, higher in itertools.pairwise(order):
        missing = ROLE_PERMISSIONS[lower] - ROLE_PERMISSIONS[higher]
        assert not missing, f"{lower} has permissions {higher} lacks: {missing}"


def test_agents_cannot_approve_their_own_availability() -> None:
    """Separation of duties: the requester must not be the approver."""
    assert Permission.OPS_AVAILABILITY_APPROVE not in ROLE_PERMISSIONS[RoleKey.AGENT]
    assert Permission.OPS_AVAILABILITY_APPROVE in ROLE_PERMISSIONS[RoleKey.TEAM_LEAD]
    assert Permission.OPS_AVAILABILITY_APPROVE in ROLE_PERMISSIONS[RoleKey.OPS_MANAGER]


def test_agents_cannot_close_assign_or_create_cases() -> None:
    agent = ROLE_PERMISSIONS[RoleKey.AGENT]
    for forbidden in (
        Permission.OPS_CASES_CREATE,
        Permission.OPS_CASES_ASSIGN,
        Permission.OPS_CASES_CLOSE,
        Permission.OPS_USERS_WRITE,
        Permission.OPS_ROLES_WRITE,
        Permission.OPS_SETTINGS_WRITE,
    ):
        assert forbidden not in agent, forbidden


def test_approving_an_export_is_separate_from_requesting_one() -> None:
    assert Permission.OPS_EXPORT_REQUEST in ROLE_PERMISSIONS[RoleKey.HEAD_OF_OPERATIONS]
    assert Permission.OPS_EXPORT_APPROVE not in ROLE_PERMISSIONS[RoleKey.HEAD_OF_OPERATIONS]


def test_write_back_is_admin_only_and_off_by_default() -> None:
    from app.config import settings

    assert settings.ops_feature_los_writeback is False
    assert settings.ops_feature_lms_writeback is False
    assert settings.ops_feature_writeback_kill_switch is True
    for role in (RoleKey.AGENT, RoleKey.TEAM_LEAD, RoleKey.OPS_MANAGER, RoleKey.AUDITOR):
        granted = ROLE_PERMISSIONS[role]
        assert Permission.OPS_LOS_WRITEBACK not in granted
        assert Permission.OPS_LMS_WRITEBACK not in granted


def test_permissions_for_roles_unions_and_ignores_strangers() -> None:
    combined = permissions_for_roles([RoleKey.AGENT, RoleKey.TEAM_LEAD])
    assert combined == ROLE_PERMISSIONS[RoleKey.AGENT] | ROLE_PERMISSIONS[RoleKey.TEAM_LEAD]
    assert permissions_for_roles(["NOT_A_ROLE"]) == frozenset()
    assert permissions_for_roles([]) == frozenset()
    # Role keys are plain strings on the wire; a StrEnum member must still match.
    assert permissions_for_roles([RoleKey.AGENT]) == permissions_for_roles(["AGENT"])


# --------------------------------------------------------------------------
# route guards
# --------------------------------------------------------------------------


def test_there_are_routes_to_check() -> None:
    assert len(_api_routes()) > 50


def test_every_protected_route_declares_a_permission() -> None:
    unprotected: list[str] = []
    for route in _api_routes():
        if route.path in UNGUARDED_BY_DESIGN:
            continue
        if not _guards(route.dependant):
            unprotected.append(f"{sorted(route.methods)} {route.path}")
    assert not unprotected, "routes reachable without a permission check:\n" + "\n".join(
        unprotected
    )


def test_every_declared_permission_exists_in_the_registry() -> None:
    unknown: list[str] = []
    for route in _api_routes():
        for guard in _guards(route.dependant):
            for permission in guard.permissions:
                if not is_known_permission(permission):
                    unknown.append(f"{route.path}: {permission}")
    assert not unknown, "undeclared permissions:\n" + "\n".join(unknown)


def test_a_guard_never_requires_an_empty_permission_set() -> None:
    """An empty requirement would allow everyone - the opposite of a check."""
    for route in _api_routes():
        for guard in _guards(route.dependant):
            assert guard.permissions, route.path


def test_read_and_write_routes_declare_different_permissions() -> None:
    """A typo that points a write route at its own read permission is caught."""
    same_name: list[str] = []
    for route in _api_routes():
        for method in route.methods - {"HEAD", "OPTIONS"}:
            guards = _guards(get_dependant(path=route.path_format, call=route.endpoint))
            for guard in guards:
                if method in {"POST", "PUT", "PATCH", "DELETE"} and any(
                    permission.endswith("_READ") for permission in guard.permissions
                ):
                    same_name.append(f"{method} {route.path} -> {list(guard.permissions)}")
    assert not same_name, "write routes guarded only by a read permission:\n" + "\n".join(same_name)


def test_delete_routes_are_guarded_by_something_other_than_read() -> None:
    for route in _api_routes():
        if "DELETE" not in route.methods:
            continue
        for guard in _guards(route.dependant):
            assert not all(p.endswith("_READ") for p in guard.permissions), route.path


def test_no_dependency_leaked_into_query_params() -> None:
    """Regression guard for the ``Annotated``-on-a-callable-object trap.

    A ``PermissionGuard`` instance has no ``__globals__``, so FastAPI cannot
    resolve a stringified ``Depends`` annotation on it. It then reads the
    parameter as a required query field and every guarded route 422s before the
    guard ever runs. This asserts the symptom at the schema level.
    """
    leaked: list[str] = []
    for route in _api_routes():
        names = {param.name for param in route.dependant.query_params}
        leaked += [f"{route.path}: {name}" for name in sorted(names & INJECTED_PARAM_NAMES)]
        for sub in _sub_dependants(route.dependant):
            sub_names = {param.name for param in sub.query_params}
            leaked += [
                f"{route.path} (via {getattr(sub.call, '__qualname__', sub.call)}): {name}"
                for name in sorted(sub_names & INJECTED_PARAM_NAMES)
            ]
    assert not leaked, "dependency parameters leaked into the query string:\n" + "\n".join(leaked)


def test_the_openapi_schema_exposes_no_injected_parameters() -> None:
    schema = fastapi_app.openapi()
    offenders: list[str] = []
    for path, operations in schema.get("paths", {}).items():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter.get("name") in INJECTED_PARAM_NAMES:
                    offenders.append(f"{path}: {parameter['name']}")
    assert not offenders, offenders


def test_the_openapi_schema_documents_the_error_envelope() -> None:
    schema = fastapi_app.openapi()
    responses = schema["paths"]["/api/users"]["get"]["responses"]
    for code in ("401", "403", "404", "422", "500"):
        assert code in responses, f"/api/users GET is missing a documented {code}"
    envelope = schema["components"]["schemas"]["ErrorResponse"]
    assert set(envelope["properties"]) == {"error", "trace_id"}


def test_the_health_probes_are_the_only_unguarded_non_auth_surface() -> None:
    """``/api/integrations/health`` is tenant data and must stay guarded."""
    public_health = {
        "/health",
        "/health/live",
        "/health/ready",
        "/api/health",
        "/api/health/live",
        "/api/health/ready",
    }
    for route in _api_routes():
        if route.path in public_health:
            assert not _guards(route.dependant), route.path
        elif route.path not in UNGUARDED_BY_DESIGN:
            assert _guards(route.dependant), route.path


# --------------------------------------------------------------------------
# guard behaviour, independent of any database
# --------------------------------------------------------------------------


def _minimal_request() -> Any:
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.mark.parametrize(
    "permission",
    sorted(ALL_PERMISSIONS),
    ids=sorted(ALL_PERMISSIONS),
)
def test_every_permission_is_usable_as_a_guard(permission: str) -> None:
    """Each registry entry must survive the ``require_permissions`` factory."""
    from app.dependencies.auth import require_any_permissions
    from app.dependencies.auth import require_permissions as require_all

    for factory in (require_all, require_any_permissions):
        guard = factory(permission)
        assert guard.permissions == (permission,)
        assert guard.any_of is (factory is require_any_permissions)
        assert guard.__name__.startswith("require_")


@pytest.mark.asyncio
async def test_a_guard_allows_a_permitted_call_and_returns_the_context() -> None:
    from starlette.requests import Request

    from app.dependencies.auth import require_permissions as require_all
    from app.security.tokens import create_access_token, decode_token

    token, _ = create_access_token(
        user_id=str(uuid.uuid4()),
        username="a",
        display_name="A",
        tenant_id="BPSEC",
        roles=["AGENT"],
        permissions=[Permission.OPS_CASES_READ],
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    resolved = RequestContext(decode_token(token), request)
    guard = require_all(Permission.OPS_CASES_READ)
    assert await guard(resolved) is resolved


def test_guard_repr_names_the_requirement() -> None:
    from app.dependencies.auth import require_permissions as require_all

    text = repr(require_all(Permission.OPS_CASES_READ, Permission.OPS_CASES_CLOSE))
    assert "require_ops_cases_read_and_ops_cases_close" in text
