"""Authentication service.

Ops issues its own short lived API session token after BRE has authenticated
the operator. The token carries the tenant and permission set so the common
path needs no extra round trip, while the per-request guard still re-checks
operational status against the database.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.config import settings
from app.errors import AuthenticationError, RateLimitedError
from app.integrations.bre import BreUser, get_provider
from app.models.audit import AuditAction, EntityType
from app.models.user import OperationalStatus, User, UserRole
from app.security.permissions import permissions_for_roles
from app.security.tokens import (
    TokenPayload,
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    decode_token,
)

logger = logging.getLogger(__name__)


class LoginOutcome:
    __slots__ = ("access_token", "expires_at", "mfa_required", "mfa_token", "refresh_token", "user")

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        user: User | None = None,
        mfa_required: bool = False,
        mfa_token: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.user = user
        self.mfa_required = mfa_required
        self.mfa_token = mfa_token


def _rate_limit_key(username: str, tenant_id: str) -> str:
    return f"login:{tenant_id}:{username.lower()}"


class LoginRateLimiter:
    """In-process limiter.

    Adequate for a single API instance. A multi-instance deployment must swap
    this for the shared store behind ``app.workers`` - the interface is
    deliberately tiny so that is a one-file change.
    """

    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def check(self, key: str) -> None:
        now = datetime.now(UTC).timestamp()
        locked_until = self._locked_until.get(key)
        if locked_until is not None:
            if locked_until > now:
                minutes = max(1, int((locked_until - now) // 60) + 1)
                raise RateLimitedError(
                    f"Too many sign-in attempts. Try again in about {minutes} minute"
                    f"{'s' if minutes != 1 else ''}.",
                    details={"retry_after_seconds": int(locked_until - now)},
                )
            self._locked_until.pop(key, None)
            self._attempts.pop(key, None)

        recent = [ts for ts in self._attempts.get(key, []) if now - ts < self.window_seconds]
        if len(recent) >= self.max_attempts:
            self._locked_until[key] = now + self.lockout_seconds
            self._attempts.pop(key, None)
            raise RateLimitedError(
                "Too many sign-in attempts. This account is temporarily locked.",
                details={"retry_after_seconds": self.lockout_seconds},
            )
        self._attempts[key] = recent

    def succeed(self, key: str) -> None:
        self._attempts.pop(key, None)
        self._locked_until.pop(key, None)


login_limiter = LoginRateLimiter(
    settings.ops_login_rate_limit_per_minute,
    window_seconds=60,
    lockout_seconds=settings.ops_login_rate_limit_lockout_minutes * 60,
)


def _issue(user: User) -> LoginOutcome:
    roles = user.role_keys
    permissions = sorted(permissions_for_roles(roles))
    access_token, expires_at = create_access_token(
        user_id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        tenant_id=user.tenant_id,
        roles=roles,
        permissions=permissions,
        mfa_verified=True,
    )
    refresh_token, _ = create_refresh_token(user_id=str(user.id), tenant_id=user.tenant_id)
    return LoginOutcome(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        user=user,
    )


def _provision_if_absent(session: Session, bre_user: BreUser) -> User:
    """First sign-in projection of a BRE identity into Ops.

    Ops creates only the operational shell - no roles, no capacity changes. An
    administrator must grant roles before the person can do anything, which
    keeps a new BRE hire from appearing with unexpected access.
    """
    user = session.scalar(
        select(User).where(User.tenant_id == bre_user.tenant_id, User.username == bre_user.username)
    )
    if user is None:
        user = User(
            tenant_id=bre_user.tenant_id,
            username=bre_user.username,
            display_name=bre_user.display_name,
            email=bre_user.email,
            bre_user_id=bre_user.bre_user_id,
            bre_roles=",".join(bre_user.roles) or None,
            status=OperationalStatus.ACTIVE,
            is_available=True,
            capacity=25,
            priority_weight=0,
            daily_allocation_limit=50,
        )
        session.add(user)
        session.flush()
        record(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=None,
            actor_name="bre-identity",
            action=AuditAction.USER_CREATED,
            entity_type=EntityType.USER,
            entity_id=user.id,
            after={
                "username": user.username,
                "display_name": user.display_name,
                "source": "BRE first sign-in projection",
                "roles": [],
            },
            reason="User projected from BRE identity on first sign-in",
            is_system_action=True,
        )
    else:
        # Keep the operational projection aligned with BRE without touching
        # anything an administrator owns.
        user.display_name = bre_user.display_name
        user.email = bre_user.email or user.email
        user.bre_user_id = bre_user.bre_user_id
        user.bre_roles = ",".join(bre_user.roles) or user.bre_roles
    return user


def login(
    session: Session,
    *,
    username: str,
    password: str,
    tenant_id: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> LoginOutcome:
    provider = get_provider()
    key = _rate_limit_key(username, tenant_id or settings.ops_tenant_id)
    login_limiter.check(key)

    try:
        result = provider.authenticate(username, password)
    except AuthenticationError:
        record(
            session,
            tenant_id=tenant_id or settings.ops_tenant_id,
            actor_user_id=None,
            actor_name=username,
            action=AuditAction.LOGIN_FAILED,
            entity_type=EntityType.AUTH,
            entity_id=None,
            after={"reason": "invalid credentials"},
            reason="Sign-in failed",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise

    user = _provision_if_absent(session, result.user)

    if user.status is not OperationalStatus.ACTIVE:
        record(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            actor_name=user.display_name,
            action=AuditAction.LOGIN_FAILED,
            entity_type=EntityType.AUTH,
            entity_id=str(user.id),
            after={"reason": "account not active"},
            reason="Sign-in blocked - account not active",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise AuthenticationError("Your account has been deactivated. Contact an administrator.")

    login_limiter.succeed(key)

    if result.mfa_required:
        record(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            actor_name=user.display_name,
            action=AuditAction.LOGIN_SUCCEEDED,
            entity_type=EntityType.AUTH,
            entity_id=str(user.id),
            after={"stage": "password accepted, MFA pending"},
            reason="Sign-in step 1 of 2",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        mfa_token = result.mfa_token or create_mfa_pending_token(
            user_id=str(user.id), username=user.username, tenant_id=user.tenant_id
        )
        return LoginOutcome(mfa_required=True, mfa_token=mfa_token, user=user)

    return _complete_login(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        stage="credentials and MFA accepted",
    )


def verify_mfa(
    session: Session,
    *,
    mfa_token: str,
    code: str,
    ip_address: str | None,
    user_agent: str | None,
) -> LoginOutcome:
    provider = get_provider()
    try:
        result = provider.verify_mfa(mfa_token, code)
    except AuthenticationError:
        raise
    user = _provision_if_absent(session, result.user)
    return _complete_login(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        stage="MFA verified",
    )


def _complete_login(
    session: Session,
    *,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
    stage: str,
) -> LoginOutcome:
    user.last_login_at = datetime.now(UTC)
    session.flush()
    record(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        actor_name=user.display_name,
        action=AuditAction.LOGIN_SUCCEEDED,
        entity_type=EntityType.AUTH,
        entity_id=str(user.id),
        after={"stage": stage, "roles": user.role_keys},
        reason="Signed in",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return _issue(user)


def logout(
    session: Session,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    actor_name: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Tokens are stateless, so logout is recorded and the client discards them.

    A future deployment can add a deny-list keyed on ``jti`` without changing
    this contract.
    """
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        actor_name=actor_name,
        action=AuditAction.LOGOUT,
        entity_type=EntityType.AUTH,
        entity_id=user_id,
        after={"stage": "session ended"},
        reason="Signed out",
        ip_address=ip_address,
        user_agent=user_agent,
    )


def refresh(
    session: Session, *, refresh_token: str, ip_address: str | None, user_agent: str | None
) -> LoginOutcome:
    payload: TokenPayload = decode_token(refresh_token, expected_type="refresh")
    user = session.scalar(
        select(User).where(User.id == payload.user_id, User.tenant_id == payload.tenant_id)
    )
    if user is None or user.status is not OperationalStatus.ACTIVE:
        raise AuthenticationError("Your session is no longer valid. Sign in again.")
    return _complete_login(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        stage="session refreshed",
    )


def user_roles_granted(user: User) -> list[UserRole]:
    return list(user.roles)
