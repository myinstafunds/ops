"""Request dependencies: identity resolution and permission enforcement.

This module is the only place a request becomes an authenticated actor
(PRD 11.1). Routers never read the Authorization header themselves and never
perform their own permission logic - they declare what they need and the guard
answers. That keeps the "backend is authoritative" rule enforceable by review.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import AuthenticationError, PermissionDeniedError
from app.logging_config import actor_id_var, tenant_id_var, trace_id_var
from app.security.permissions import ALL_PERMISSIONS, Permission
from app.security.tokens import TokenPayload, decode_token
from app.services.users import UserContext, build_user_context

# ``auto_error=False`` so a missing header raises our envelope, not Starlette's.
_bearer = HTTPBearer(auto_error=False, description="BRE-issued Ops session token")

DbSession = Annotated[Session, Depends(get_db)]


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


class RequestContext:
    """Everything a handler needs about who is calling."""

    __slots__ = (
        "correlation_id",
        "display_name",
        "ip_address",
        "mfa_verified",
        "permissions",
        "roles",
        "tenant_id",
        "trace_id",
        "user_agent",
        "user_id",
        "username",
    )

    def __init__(self, token: TokenPayload, request: Request) -> None:
        self.user_id = uuid.UUID(token.user_id)
        self.username = token.username
        self.display_name = token.display_name
        #: Tenant is taken from the signed token only. A browser supplied
        #: tenant header is never consulted (PRD 40 / Rule 4).
        self.tenant_id = token.tenant_id
        self.roles = list(token.roles)
        self.permissions = token.permission_set
        self.mfa_verified = not token.mfa_required
        self.ip_address = _client_ip(request)
        self.user_agent = request.headers.get("user-agent")
        self.trace_id = getattr(request.state, "trace_id", None)
        self.correlation_id = getattr(request.state, "correlation_id", None)

    def has(self, permission: Permission | str) -> bool:
        value = permission.value if isinstance(permission, Permission) else permission
        return value in self.permissions

    def has_all(self, *permissions: Permission | str) -> bool:
        return all(self.has(item) for item in permissions)

    def has_any(self, *permissions: Permission | str) -> bool:
        return any(self.has(item) for item in permissions)


def get_request_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> RequestContext:
    """Resolve the caller from the bearer token or fail with 401."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Sign in to continue.")

    token = decode_token(credentials.credentials, expected_type="access")
    context = RequestContext(token, request)

    # Publish into the logging context so log lines carry the actor.
    actor_id_var.set(str(context.user_id))
    tenant_id_var.set(context.tenant_id)
    request.state.actor_user_id = str(context.user_id)
    request.state.tenant_id = context.tenant_id

    if not context.mfa_verified:
        raise AuthenticationError("Complete multi-factor verification to continue.")
    return context


CurrentUserCtx = Annotated[RequestContext, Depends(get_request_context)]


class PermissionGuard:
    """Callable dependency produced by the ``require_*`` factories.

    A route names its requirement once at module level and then references that
    name in every signature it applies to::

        CanClose = require_permissions(Permission.OPS_CASES_CLOSE)

        def close_case(
            work_item_id: uuid.UUID,
            context: CurrentUserCtx,
            session: DbSession,
            _: Annotated[RequestContext, Depends(CanClose)],
        ) -> WorkItemDetail: ...

    The check therefore cannot be forgotten, cannot be inlined into handler
    logic, and the declared name shows up in the generated OpenAPI requirement
    list. Routers never call :meth:`__call__` themselves and never branch on
    ``context.has(...)`` - if a route needs a permission it declares it here.
    """

    __slots__ = ("__name__", "any_of", "permissions")

    def __init__(self, wanted: tuple[str, ...], *, any_of: bool, name: str) -> None:
        self.permissions = wanted
        self.any_of = any_of
        self.__name__ = name

    async def __call__(
        self, context: RequestContext = Depends(get_request_context)
    ) -> RequestContext:
        # NOTE: the dependency is declared as the *default value*, not inside
        # ``Annotated``. FastAPI resolves string annotations with
        # ``call.__globals__``, which a guard *instance* does not have, so an
        # ``Annotated[..., Depends(...)]`` parameter here would silently
        # degrade into a required query parameter named ``context``.
        if self.any_of:
            allowed = context.has_any(*self.permissions)
        else:
            allowed = all(context.has(item) for item in self.permissions)
        if not allowed:
            key = "required_any_of" if self.any_of else "required_permissions"
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={key: list(self.permissions)},
            )
        return context

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PermissionGuard {self.__name__}>"


def require_permissions(*permissions: Permission | str) -> PermissionGuard:
    """Dependency factory enforcing *all* of the given permissions.

    ``require_any_permissions`` is available for read endpoints that accept
    either a manager permission or an ownership scoped permission.
    """
    wanted = _validate(permissions)
    return PermissionGuard(
        wanted,
        any_of=False,
        # Keeps the generated OpenAPI requirement list readable.
        name="require_" + "_and_".join(item.lower() for item in wanted),
    )


def require_any_permissions(*permissions: Permission | str) -> PermissionGuard:
    """Dependency factory requiring *at least one* of the given permissions."""
    wanted = _validate(permissions)
    return PermissionGuard(
        wanted,
        any_of=True,
        name="require_any_of_" + "_or_".join(item.lower() for item in wanted),
    )


def _validate(permissions: tuple[Permission | str, ...]) -> tuple[str, ...]:
    wanted = tuple(item.value if isinstance(item, Permission) else item for item in permissions)
    unknown = [item for item in wanted if item not in ALL_PERMISSIONS]
    if unknown:  # pragma: no cover - guards against typos at import time
        raise RuntimeError(f"Unknown permission(s) declared on a route: {unknown}")
    return wanted


def get_actor(session: Session, context: RequestContext) -> UserContext:
    """Load the live operational user behind the token.

    Authorization decisions that depend on operational state (a user who has
    been deactivated must lose access immediately) cannot rely on the token
    alone, so this is resolved per request.
    """
    return build_user_context(session, tenant_id=context.tenant_id, user_id=context.user_id)


ActorCtx = Annotated[UserContext, Depends(get_actor)]


def trace_of(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None) or trace_id_var.get()
