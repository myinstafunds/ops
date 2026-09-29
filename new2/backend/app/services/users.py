"""User service: reads, writes and the per-request actor context."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.audit import record
from app.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.models.audit import AuditAction, EntityType
from app.models.availability import UserAvailability
from app.models.org import DepartmentMembership, QueueMembership
from app.models.user import OperationalStatus, User, UserRole
from app.models.work import OPEN_STATUSES, WorkItem
from app.schemas.org import UserCreate, UserUpdate
from app.security.permissions import RoleKey, permissions_for_roles
from app.security.tokens import TokenPayload

#: Roles that may be granted. Anything else is rejected at the boundary so a
#: typo cannot silently create an account with no permissions.
VALID_ROLES: frozenset[str] = frozenset(role.value for role in RoleKey)


class UserContext:
    """The live operational actor behind a valid token."""

    __slots__ = (
        "display_name",
        "email",
        "id",
        "is_active",
        "permissions",
        "roles",
        "status",
        "tenant_id",
        "username",
    )

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        username: str,
        display_name: str,
        email: str | None,
        tenant_id: str,
        status: OperationalStatus,
        roles: list[str],
        permissions: frozenset[str],
    ) -> None:
        self.id = user_id
        self.username = username
        self.display_name = display_name
        self.email = email
        self.tenant_id = tenant_id
        self.status = status
        self.roles = roles
        self.permissions = permissions
        self.is_active = status is OperationalStatus.ACTIVE

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def build_user_context(session: Session, *, tenant_id: str, user_id: uuid.UUID) -> UserContext:
    """Load the actor, refusing access when the user is not operational.

    A token stays cryptographically valid until it expires, so operational
    deactivation must be re-checked on every request.
    """
    user = session.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id, User.tenant_id == tenant_id)
    )
    if user is None:
        raise PermissionDeniedError(
            "Your account is no longer available. Contact an administrator."
        )
    if user.status is not OperationalStatus.ACTIVE:
        raise PermissionDeniedError("Your account has been deactivated. Contact an administrator.")

    roles = user.role_keys
    return UserContext(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        tenant_id=user.tenant_id,
        status=user.status,
        roles=roles,
        permissions=permissions_for_roles(roles),
    )


def permissions_for_token(session: Session, tenant_id: str, user_id: uuid.UUID) -> list[str]:
    user = session.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id, User.tenant_id == tenant_id)
    )
    if user is None:
        return []
    return sorted(permissions_for_roles(user.role_keys))


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def _base_user_query(tenant_id: str) -> Select[tuple[User]]:
    return select(User).options(selectinload(User.roles)).where(User.tenant_id == tenant_id)


def list_users(
    session: Session,
    *,
    tenant_id: str,
    search: str | None = None,
    status: OperationalStatus | None = None,
    department_id: uuid.UUID | None = None,
    queue_id: uuid.UUID | None = None,
    is_available: bool | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str = "display_name",
    descending: bool = False,
) -> tuple[list[User], int]:
    query = _base_user_query(tenant_id)

    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(User.display_name).like(pattern)
            | func.lower(User.username).like(pattern)
            | func.coalesce(func.lower(User.email), "").like(pattern)
        )
    if status is not None:
        query = query.where(User.status == status)
    if is_available is not None:
        query = query.where(User.is_available.is_(is_available))
    if department_id is not None:
        query = query.join(
            DepartmentMembership,
            (DepartmentMembership.user_id == User.id)
            & (DepartmentMembership.tenant_id == tenant_id),
        ).where(DepartmentMembership.department_id == department_id)
    if queue_id is not None:
        query = query.join(
            QueueMembership,
            (QueueMembership.user_id == User.id) & (QueueMembership.tenant_id == tenant_id),
        ).where(QueueMembership.queue_id == queue_id)

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0

    sort_column = {
        "display_name": User.display_name,
        "username": User.username,
        "capacity": User.capacity,
        "status": User.status,
        "created_at": User.created_at,
    }.get(sort, User.display_name)
    order = sort_column.desc() if descending else sort_column.asc()

    users = list(session.scalars(query.order_by(order).offset(offset).limit(limit)).unique())
    return users, total


def get_user(session: Session, *, tenant_id: str, user_id: uuid.UUID) -> User:
    user = session.scalar(_base_user_query(tenant_id).where(User.id == user_id))
    if user is None:
        raise NotFoundError("That user was not found.")
    return user


# --------------------------------------------------------------------------
# workload / availability projections
# --------------------------------------------------------------------------
def open_workload_counts(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    query = select(WorkItem.assigned_user_id, func.count(WorkItem.id)).where(
        WorkItem.tenant_id == tenant_id,
        WorkItem.status.in_(tuple(OPEN_STATUSES)),
        WorkItem.assigned_user_id.isnot(None),
    )
    if user_ids is not None:
        if not user_ids:
            return {}
        query = query.where(WorkItem.assigned_user_id.in_(user_ids))
    # GROUP BY is required: assigned_user_id is in the projection, so without
    # it Postgres rejects the whole query with "must appear in the GROUP BY".
    query = query.group_by(WorkItem.assigned_user_id)
    return {row[0]: row[1] for row in session.execute(query).all()}


def locked_case_counts(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    now = datetime.now(UTC)
    query = select(WorkItem.locked_by_user_id, func.count(WorkItem.id)).where(
        WorkItem.tenant_id == tenant_id,
        WorkItem.locked_by_user_id.isnot(None),
        WorkItem.locked_until > now,
    )
    if user_ids is not None:
        if not user_ids:
            return {}
        query = query.where(WorkItem.locked_by_user_id.in_(user_ids))
    query = query.group_by(WorkItem.locked_by_user_id)
    return {row[0]: row[1] for row in session.execute(query).all()}


def active_unavailability(
    session: Session, *, tenant_id: str, moment: datetime | None = None
) -> dict[uuid.UUID, UserAvailability]:
    """The blocking window covering ``moment`` for each user, if any."""
    now = moment or datetime.now(UTC)
    rows = session.scalars(
        select(UserAvailability)
        .where(
            UserAvailability.tenant_id == tenant_id,
            UserAvailability.status == "APPROVED",
            UserAvailability.start_at <= now,
            UserAvailability.end_at > now,
        )
        .order_by(UserAvailability.end_at.asc())
    )
    result: dict[uuid.UUID, UserAvailability] = {}
    for window in rows:
        result.setdefault(window.user_id, window)
    return result


def department_codes_by_user(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[str]]:
    from app.models.org import Department

    query = (
        select(DepartmentMembership.user_id, Department.code)
        .join(Department, Department.id == DepartmentMembership.department_id)
        .where(
            DepartmentMembership.tenant_id == tenant_id,
            DepartmentMembership.is_active.is_(True),
        )
    )
    if user_ids is not None:
        if not user_ids:
            return {}
        query = query.where(DepartmentMembership.user_id.in_(user_ids))
    result: dict[uuid.UUID, list[str]] = {}
    for user_id, code in session.execute(query).all():
        result.setdefault(user_id, []).append(code)
    return result


def queue_codes_by_user(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[str]]:
    from app.models.org import Queue

    query = (
        select(QueueMembership.user_id, Queue.code)
        .join(Queue, Queue.id == QueueMembership.queue_id)
        .where(QueueMembership.tenant_id == tenant_id, QueueMembership.is_active.is_(True))
    )
    if user_ids is not None:
        if not user_ids:
            return {}
        query = query.where(QueueMembership.user_id.in_(user_ids))
    result: dict[uuid.UUID, list[str]] = {}
    for user_id, code in session.execute(query).all():
        result.setdefault(user_id, []).append(code)
    return result


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------
def _validate_roles(roles: list[str]) -> None:
    invalid = [role for role in roles if role not in VALID_ROLES]
    if invalid:
        raise ConflictError(
            f"Unknown operational role: {', '.join(sorted(invalid))}.",
            code="UNKNOWN_ROLE",
            details={"invalid_roles": invalid, "valid_roles": sorted(VALID_ROLES)},
        )


def create_user(
    session: Session,
    *,
    tenant_id: str,
    data: UserCreate,
    actor_user_id: uuid.UUID,
    actor_name: str,
    ip_address: str | None,
    user_agent: str | None,
) -> User:
    _validate_roles(data.roles)

    existing = session.scalar(
        select(User).where(
            User.tenant_id == tenant_id, func.lower(User.username) == data.username.lower()
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A user with the username '{data.username}' already exists.",
            code="USERNAME_TAKEN",
        )

    user = User(
        tenant_id=tenant_id,
        username=data.username,
        display_name=data.display_name,
        email=data.email,
        bre_user_id=data.bre_user_id,
        job_title=data.job_title,
        capacity=data.capacity,
        priority_weight=data.priority_weight,
        daily_allocation_limit=data.daily_allocation_limit,
        specialisations=",".join(data.specialisations) or None,
        is_available=data.is_available,
        status=OperationalStatus.ACTIVE,
    )
    session.add(user)
    session.flush()

    for role in dict.fromkeys(data.roles):
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role))

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        action=AuditAction.USER_CREATED,
        entity_type=EntityType.USER,
        entity_id=user.id,
        after=_snapshot(user),
        reason="User created",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    return user


def update_user(
    session: Session,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    data: UserUpdate,
    actor_user_id: uuid.UUID,
    actor_name: str,
    ip_address: str | None,
    user_agent: str | None,
    reason: str | None = None,
) -> User:
    user = get_user(session, tenant_id=tenant_id, user_id=user_id)
    before = _snapshot(user)

    changes = data.model_dump(exclude_unset=True)
    if "specialisations" in changes:
        specials = changes["specialisations"] or []
        user.specialisations = ",".join(specials) or None
        changes.pop("specialisations")
    for field, value in changes.items():
        setattr(user, field, value)

    if user.email:
        duplicate = session.scalar(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == user.email,
                User.id != user.id,
            )
        )
        if duplicate is not None:
            raise ConflictError("Another user already uses that email address.", code="EMAIL_TAKEN")

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        action=AuditAction.USER_UPDATED,
        entity_type=EntityType.USER,
        entity_id=user.id,
        before=before,
        after=_snapshot(user),
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    return user


def set_user_status(
    session: Session,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    status: OperationalStatus,
    reason: str,
    actor_user_id: uuid.UUID,
    actor_name: str,
    ip_address: str | None,
    user_agent: str | None,
) -> User:
    user = get_user(session, tenant_id=tenant_id, user_id=user_id)
    if user.id == actor_user_id and status is not OperationalStatus.ACTIVE:
        raise ConflictError("You cannot deactivate your own account.", code="SELF_DEACTIVATION")
    if user.status is status:
        return user

    before = _snapshot(user)
    user.status = status
    if status is OperationalStatus.ACTIVE:
        user.deactivated_at = None
        user.deactivated_reason = None
    else:
        user.deactivated_at = datetime.now(UTC)
        user.deactivated_reason = reason

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        action=(
            AuditAction.USER_ACTIVATED
            if status is OperationalStatus.ACTIVE
            else AuditAction.USER_DEACTIVATED
        ),
        entity_type=EntityType.USER,
        entity_id=user.id,
        before=before,
        after=_snapshot(user),
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.flush()
    return user


def assign_roles(
    session: Session,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    roles: list[str],
    reason: str,
    actor_user_id: uuid.UUID,
    actor_name: str,
    ip_address: str | None,
    user_agent: str | None,
) -> User:
    _validate_roles(roles)
    user = get_user(session, tenant_id=tenant_id, user_id=user_id)
    before_roles = user.role_keys

    desired = list(dict.fromkeys(roles))
    if desired == before_roles:
        return user

    for role_row in list(user.roles):
        session.delete(role_row)
    session.flush()
    for role in desired:
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role))
    session.flush()
    session.refresh(user)

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        action=AuditAction.USER_ROLE_CHANGED,
        entity_type=EntityType.USER,
        entity_id=user.id,
        before={"roles": before_roles},
        after={"roles": desired},
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return user


def sync_last_login(session: Session, *, tenant_id: str, user_id: uuid.UUID) -> None:
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    if user is not None:
        user.last_login_at = datetime.now(UTC)


def token_for_user(user: User) -> TokenPayload:
    """Build a token payload for the supplied user."""
    roles = user.role_keys
    return TokenPayload(
        user_id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        tenant_id=user.tenant_id,
        roles=roles,
        permissions=sorted(permissions_for_roles(roles)),
        token_type="access",
        expires_at=datetime.now(UTC),
    )


def _snapshot(user: User) -> dict[str, Any]:
    """Operational snapshot for the audit trail - never raw PII."""
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "status": user.status.value,
        "is_available": user.is_available,
        "capacity": user.capacity,
        "priority_weight": user.priority_weight,
        "daily_allocation_limit": user.daily_allocation_limit,
        "specialisations": user.specialisation_list,
        "roles": user.role_keys,
        "job_title": user.job_title,
    }
