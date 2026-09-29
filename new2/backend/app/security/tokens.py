"""Token issuance and verification for the Ops API.

Ops owns its own API session tokens (PRD Rule 1 - identity stays with BRE,
authentication is still verified by BRE). Tokens are short lived, carry the
tenant and the permission set, and are verified on every protected request.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.config import settings
from app.errors import TokenExpiredError, TokenInvalidError

TokenType = Literal["access", "refresh", "mfa_pending"]

_CLAIM_TENANT = "tenant"
_CLAIM_ROLES = "roles"
_CLAIM_PERMISSIONS = "permissions"
_CLAIM_TYPE = "typ"
_CLAIM_SUBJECT = "sub"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    user_id: str
    username: str
    display_name: str
    tenant_id: str
    roles: list[str]
    permissions: list[str]
    token_type: TokenType
    expires_at: datetime
    mfa_required: bool = False

    @property
    def permission_set(self) -> frozenset[str]:
        return frozenset(self.permissions)


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(claims: dict[str, Any]) -> str:
    token = jwt.encode(claims, settings.ops_jwt_secret, algorithm=settings.ops_jwt_algorithm)
    return token if isinstance(token, str) else str(token)


def create_access_token(
    *,
    user_id: str,
    username: str,
    display_name: str,
    tenant_id: str,
    roles: list[str],
    permissions: list[str],
    mfa_verified: bool = True,
    ttl_minutes: int | None = None,
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` for a fully authenticated session."""
    ttl = ttl_minutes or settings.ops_access_token_ttl_minutes
    issued_at = _now()
    expires_at = issued_at + timedelta(minutes=ttl)
    jti = uuid.uuid4().hex
    claims: dict[str, Any] = {
        _CLAIM_SUBJECT: user_id,
        "username": username,
        "name": display_name,
        _CLAIM_TENANT: tenant_id,
        _CLAIM_ROLES: sorted(set(roles)),
        _CLAIM_PERMISSIONS: sorted(set(permissions)),
        _CLAIM_TYPE: "access",
        "mfa_verified": mfa_verified,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return _encode(claims), expires_at


def create_refresh_token(*, user_id: str, tenant_id: str) -> tuple[str, datetime]:
    issued_at = _now()
    expires_at = issued_at + timedelta(minutes=settings.ops_refresh_token_ttl_minutes)
    claims: dict[str, Any] = {
        _CLAIM_SUBJECT: user_id,
        _CLAIM_TENANT: tenant_id,
        _CLAIM_TYPE: "refresh",
        "jti": uuid.uuid4().hex,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return _encode(claims), expires_at


def create_mfa_pending_token(*, user_id: str, username: str, tenant_id: str) -> str:
    """Short lived token proving credentials but not MFA (PRD 7)."""
    issued_at = _now()
    claims: dict[str, Any] = {
        _CLAIM_SUBJECT: user_id,
        "username": username,
        _CLAIM_TENANT: tenant_id,
        _CLAIM_TYPE: "mfa_pending",
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(minutes=5)).timestamp()),
    }
    return _encode(claims)


def decode_token(token: str, *, expected_type: TokenType = "access") -> TokenPayload:
    """Verify signature, expiry and type, returning a typed payload.

    Raises :class:`AuthenticationError` subclasses - never returns ``None`` -
    so no caller can forget to handle a bad token.
    """
    try:
        claims = jwt.decode(
            token,
            settings.ops_jwt_secret,
            algorithms=[settings.ops_jwt_algorithm],
            leeway=settings.ops_bre_token_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError() from exc

    token_type = claims.get(_CLAIM_TYPE)
    if token_type != expected_type:
        raise TokenInvalidError("Unexpected token type for this endpoint.")

    return TokenPayload(
        user_id=str(claims[_CLAIM_SUBJECT]),
        username=str(claims.get("username", "")),
        display_name=str(claims.get("name", claims.get("username", ""))),
        tenant_id=str(claims.get(_CLAIM_TENANT, settings.ops_tenant_id)),
        roles=list(claims.get(_CLAIM_ROLES) or []),
        permissions=list(claims.get(_CLAIM_PERMISSIONS) or []),
        token_type=expected_type,
        expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
        mfa_required=not bool(claims.get("mfa_verified", True)),
    )


def hash_secret(value: str) -> str:
    """Deterministic hash used for refresh token / dev credential comparison."""
    return hmac.new(
        settings.ops_jwt_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
