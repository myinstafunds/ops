"""Department and queue services."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import Any, NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record
from app.errors import ConflictError, NotFoundError
from app.models.audit import AuditAction, EntityType
from app.models.availability import (
    BLOCKING_STATUSES,
    BLOCKING_TYPES,
    UserAvailability,
)
from app.models.org import (
    Department,
    DepartmentMembership,
    OrgStatus,
    Queue,
    QueueMembership,
    SourceApp,
)
from app.models.sla import QueueSlaPolicy, SlaPolicy
from app.models.user import OperationalStatus, User
from app.models.work import OPEN_STATUSES, WorkItem
from app.schemas.org import (
    DepartmentCreate,
    DepartmentUpdate,
    QueueCreate,
    QueueUpdate,
)


class WriteContext:
    """Audit metadata for a write operation.

    Also carries the actor's resolved permission set so a service can apply an
    ownership rule (e.g. "only an approver may edit an approved window") without
    re-reading the token.
    """

    __slots__ = (
        "actor_name",
        "actor_user_id",
        "ip_address",
        "permissions",
        "tenant_id",
        "user_agent",
    )

    def __init__(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID,
        actor_name: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        permissions: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.actor_user_id = actor_user_id
        self.actor_name = actor_name
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.permissions = frozenset(permissions or ())

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def audit(
        self,
        session: Session,
        *,
        action: AuditAction,
        entity_type: EntityType,
        entity_id: str | uuid.UUID | None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        record(
            session,
            tenant_id=self.tenant_id,
            actor_user_id=self.actor_user_id,
            actor_name=self.actor_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            reason=reason,
            ip_address=self.ip_address,
            user_agent=self.user_agent,
        )


# ==========================================================================
# departments
# ==========================================================================
def list_departments(
    session: Session,
    *,
    tenant_id: str,
    search: str | None = None,
    status: OrgStatus | None = None,
    source_app: SourceApp | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str = "name",
    descending: bool = False,
) -> tuple[list[Department], int, dict[uuid.UUID, int]]:
    query: Select[tuple[Department]] = select(Department).where(Department.tenant_id == tenant_id)
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(Department.name).like(pattern) | func.lower(Department.code).like(pattern)
        )
    if status is not None:
        query = query.where(Department.status == status)
    if source_app is not None:
        query = query.where(Department.source_app == source_app)

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = {
        "name": Department.name,
        "code": Department.code,
        "created_at": Department.created_at,
    }.get(sort, Department.name)
    order = column.desc() if descending else column.asc()
    departments = list(session.scalars(query.order_by(order).offset(offset).limit(limit)))

    member_counts: dict[uuid.UUID, int] = {}
    if departments:
        rows = session.execute(
            select(DepartmentMembership.department_id, func.count(DepartmentMembership.id))
            .where(
                DepartmentMembership.tenant_id == tenant_id,
                DepartmentMembership.department_id.in_([d.id for d in departments]),
                DepartmentMembership.is_active.is_(True),
            )
            .group_by(DepartmentMembership.department_id)
        )
        member_counts = {row[0]: row[1] for row in rows}
    return departments, total, member_counts


def get_department(session: Session, *, tenant_id: str, department_id: uuid.UUID) -> Department:
    department = session.scalar(
        select(Department).where(Department.id == department_id, Department.tenant_id == tenant_id)
    )
    if department is None:
        raise NotFoundError("That department was not found.")
    return department


def department_member_count(session: Session, *, tenant_id: str, department_id: uuid.UUID) -> int:
    """Active membership count for a single department."""
    return (
        session.scalar(
            select(func.count(DepartmentMembership.id)).where(
                DepartmentMembership.tenant_id == tenant_id,
                DepartmentMembership.department_id == department_id,
                DepartmentMembership.is_active.is_(True),
            )
        )
        or 0
    )


def department_member_counts(
    session: Session, *, tenant_id: str, department_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Active membership counts keyed by department id."""
    if not department_ids:
        return {}
    rows = session.execute(
        select(DepartmentMembership.department_id, func.count(DepartmentMembership.id))
        .where(
            DepartmentMembership.tenant_id == tenant_id,
            DepartmentMembership.department_id.in_(list(department_ids)),
            DepartmentMembership.is_active.is_(True),
        )
        .group_by(DepartmentMembership.department_id)
    )
    return {row[0]: row[1] for row in rows}


def create_department(session: Session, *, data: DepartmentCreate, ctx: WriteContext) -> Department:
    department = Department(
        tenant_id=ctx.tenant_id,
        code=data.code.upper(),
        name=data.name,
        source_app=data.source_app,
        description=data.description,
        status=OrgStatus.ACTIVE,
    )
    session.add(department)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            f"A department with the code '{data.code.upper()}' already exists.",
            code="DEPARTMENT_CODE_TAKEN",
        ) from exc

    ctx.audit(
        session,
        action=AuditAction.DEPARTMENT_CREATED,
        entity_type=EntityType.DEPARTMENT,
        entity_id=department.id,
        after=_department_snapshot(department),
        reason="Department created",
    )
    session.flush()
    return department


def update_department(
    session: Session,
    *,
    tenant_id: str,
    department_id: uuid.UUID,
    data: DepartmentUpdate,
    ctx: WriteContext,
) -> Department:
    department = get_department(session, tenant_id=tenant_id, department_id=department_id)
    before = _department_snapshot(department)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(department, field, value)
    ctx.audit(
        session,
        action=AuditAction.DEPARTMENT_UPDATED,
        entity_type=EntityType.DEPARTMENT,
        entity_id=department.id,
        before=before,
        after=_department_snapshot(department),
        reason="Department updated",
    )
    session.flush()
    return department


def _department_snapshot(department: Department) -> dict[str, Any]:
    return {
        "code": department.code,
        "name": department.name,
        "source_app": department.source_app.value,
        "status": department.status.value,
        "description": department.description,
    }


# --------------------------------------------------------------------------
# department memberships
# --------------------------------------------------------------------------
def list_department_memberships(
    session: Session,
    *,
    tenant_id: str,
    department_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[DepartmentMembership], int]:
    query = (
        select(DepartmentMembership)
        .join(Department, Department.id == DepartmentMembership.department_id)
        .where(DepartmentMembership.tenant_id == tenant_id)
    )
    if department_id is not None:
        query = query.where(DepartmentMembership.department_id == department_id)
    if user_id is not None:
        query = query.where(DepartmentMembership.user_id == user_id)
    if is_active is not None:
        query = query.where(DepartmentMembership.is_active.is_(is_active))

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(Department.code, DepartmentMembership.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total


def upsert_department_membership(
    session: Session,
    *,
    department_id: uuid.UUID,
    user_id: uuid.UUID,
    is_primary: bool,
    ctx: WriteContext,
) -> DepartmentMembership:
    from app.models.user import User

    get_department(session, tenant_id=ctx.tenant_id, department_id=department_id)
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == ctx.tenant_id))
    if user is None:
        raise NotFoundError("That user was not found.")

    existing = session.scalar(
        select(DepartmentMembership).where(
            DepartmentMembership.tenant_id == ctx.tenant_id,
            DepartmentMembership.department_id == department_id,
            DepartmentMembership.user_id == user_id,
        )
    )
    if existing is not None:
        if existing.is_active and existing.is_primary == is_primary:
            return existing
        existing.is_active = True
        existing.is_primary = is_primary
        ctx.audit(
            session,
            action=AuditAction.DEPARTMENT_MEMBERSHIP_UPDATED,
            entity_type=EntityType.DEPARTMENT_MEMBERSHIP,
            entity_id=existing.id,
            after={
                "department_id": str(department_id),
                "user_id": str(user_id),
                "is_primary": is_primary,
                "is_active": True,
            },
            reason="Department membership reactivated",
        )
        session.flush()
        return existing

    membership = DepartmentMembership(
        tenant_id=ctx.tenant_id,
        department_id=department_id,
        user_id=user_id,
        is_primary=is_primary,
        is_active=True,
    )
    session.add(membership)
    session.flush()
    ctx.audit(
        session,
        action=AuditAction.DEPARTMENT_MEMBERSHIP_CREATED,
        entity_type=EntityType.DEPARTMENT_MEMBERSHIP,
        entity_id=membership.id,
        after={
            "department_id": str(department_id),
            "user_id": str(user_id),
            "is_primary": is_primary,
            "is_active": True,
        },
        reason="Department membership created",
    )
    return membership


def update_department_membership(
    session: Session,
    *,
    tenant_id: str,
    membership_id: uuid.UUID,
    is_primary: bool | None,
    is_active: bool | None,
    ctx: WriteContext,
) -> DepartmentMembership:
    membership = session.scalar(
        select(DepartmentMembership).where(
            DepartmentMembership.id == membership_id,
            DepartmentMembership.tenant_id == tenant_id,
        )
    )
    if membership is None:
        raise NotFoundError("That department membership was not found.")
    before = {
        "is_primary": membership.is_primary,
        "is_active": membership.is_active,
    }
    if is_primary is not None:
        membership.is_primary = is_primary
    if is_active is not None:
        membership.is_active = is_active
    ctx.audit(
        session,
        action=AuditAction.DEPARTMENT_MEMBERSHIP_UPDATED,
        entity_type=EntityType.DEPARTMENT_MEMBERSHIP,
        entity_id=membership.id,
        before=before,
        after={"is_primary": membership.is_primary, "is_active": membership.is_active},
        reason="Department membership updated",
    )
    session.flush()
    return membership


# ==========================================================================
# queues
# ==========================================================================
def list_queues(
    session: Session,
    *,
    tenant_id: str,
    search: str | None = None,
    status: OrgStatus | None = None,
    source_app: SourceApp | None = None,
    department_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str = "name",
    descending: bool = False,
) -> tuple[list[Queue], int]:
    query: Select[tuple[Queue]] = select(Queue).where(Queue.tenant_id == tenant_id)
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(Queue.name).like(pattern) | func.lower(Queue.code).like(pattern)
        )
    if status is not None:
        query = query.where(Queue.status == status)
    if source_app is not None:
        query = query.where(Queue.source_app == source_app)
    if department_id is not None:
        query = query.where(Queue.department_id == department_id)

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = {
        "name": Queue.name,
        "code": Queue.code,
        "priority": Queue.priority,
        "created_at": Queue.created_at,
    }.get(sort, Queue.name)
    order = column.desc() if descending else column.asc()
    queues = list(session.scalars(query.order_by(order).offset(offset).limit(limit)))
    return queues, total


def get_queue(session: Session, *, tenant_id: str, queue_id: uuid.UUID) -> Queue:
    queue = session.scalar(select(Queue).where(Queue.id == queue_id, Queue.tenant_id == tenant_id))
    if queue is None:
        raise NotFoundError("That queue was not found.")
    return queue


class QueueCapacity(NamedTuple):
    """Headline numbers for one queue (PRD 12.4, 33.5)."""

    open_items: int
    member_count: int
    #: Active members who are not blocked and still have capacity headroom.
    available_members: int


class _Availability(NamedTuple):
    """The queue-independent half of the allocation eligibility check."""

    blocked: bool
    open_workload: int


def queue_capacity(
    session: Session,
    *,
    tenant_id: str,
    queue: Queue,
    now: datetime | None = None,
) -> QueueCapacity:
    return queue_capacities(session, tenant_id=tenant_id, queues=[queue], now=now)[queue.id]


def queue_capacities(
    session: Session,
    *,
    tenant_id: str,
    queues: Sequence[Queue],
    now: datetime | None = None,
) -> dict[uuid.UUID, QueueCapacity]:
    """Batched capacity summary for a page of queues.

    ``available_members`` is deliberately not a copy of ``member_count``: it
    counts active members who are not blocked by an approved availability
    window and still have capacity headroom - the number a manager actually
    needs when deciding whether work can be placed.

    Batched because the queue register renders a whole page and a per-row
    implementation would issue five queries per row.
    """
    moment = now or utcnow()
    if not queues:
        return {}

    queue_ids = [queue.id for queue in queues]

    rows = session.execute(
        select(WorkItem.queue_code, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.queue_code.in_([queue.code for queue in queues]),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        )
        .group_by(WorkItem.queue_code)
    )
    open_by_code = {row[0]: row[1] for row in rows}

    rows = session.execute(
        select(
            QueueMembership.queue_id,
            QueueMembership.user_id,
            QueueMembership.capacity,
        ).where(
            QueueMembership.tenant_id == tenant_id,
            QueueMembership.queue_id.in_(queue_ids),
            QueueMembership.is_active.is_(True),
        )
    )
    members = [(row[0], row[1], row[2]) for row in rows]

    member_counts: dict[uuid.UUID, int] = {}
    for queue_id, _, _ in members:
        member_counts[queue_id] = member_counts.get(queue_id, 0) + 1

    state = _availability_state(
        session,
        tenant_id=tenant_id,
        user_ids={user_id for _, user_id, _ in members},
        moment=moment,
    )

    result: dict[uuid.UUID, QueueCapacity] = {}
    for queue in queues:
        available = 0
        for queue_id, user_id, capacity in members:
            if queue_id != queue.id:
                continue
            user_state = state.get(user_id)
            if user_state is None or user_state.blocked:
                continue
            limit = capacity if capacity is not None else queue.max_workload_per_user
            if user_state.open_workload < limit:
                available += 1
        result[queue.id] = QueueCapacity(
            open_items=open_by_code.get(queue.code, 0),
            member_count=member_counts.get(queue.id, 0),
            available_members=available,
        )
    return result


def open_workloads(
    session: Session, *, tenant_id: str, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Open (non-terminal) work item count per assignee."""
    if not user_ids:
        return {}
    rows = session.execute(
        select(WorkItem.assigned_user_id, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.assigned_user_id.in_(list(user_ids)),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        )
        .group_by(WorkItem.assigned_user_id)
    )
    return {row[0]: row[1] for row in rows if row[0] is not None}


def availability_state(
    session: Session, *, tenant_id: str, user_ids: Sequence[uuid.UUID], now: datetime | None = None
) -> dict[uuid.UUID, _Availability]:
    """Public wrapper around the queue-independent eligibility half."""
    return _availability_state(
        session, tenant_id=tenant_id, user_ids=set(user_ids), moment=now or utcnow()
    )


def _availability_state(
    session: Session, *, tenant_id: str, user_ids: set[uuid.UUID], moment: datetime
) -> dict[uuid.UUID, _Availability]:
    """Availability facts per user, ignoring any queue specific rule.

    Mirrors the checks in :func:`app.services.allocation.evaluate_candidate`
    that do not depend on the target queue: the account is operationally
    active, the user is not inside an approved blocking window, and how much
    open work they are already carrying.
    """
    if not user_ids:
        return {}
    ids = list(user_ids)
    workloads = open_workloads(session, tenant_id=tenant_id, user_ids=ids)
    blocked = _users_blocked_at(session, tenant_id=tenant_id, user_ids=ids, moment=moment)
    rows = session.execute(
        select(User.id, User.is_available, User.status).where(
            User.tenant_id == tenant_id, User.id.in_(ids)
        )
    )
    return {
        user_id: _Availability(
            blocked=(
                not is_available or status is not OperationalStatus.ACTIVE or user_id in blocked
            ),
            open_workload=workloads.get(user_id, 0),
        )
        for user_id, is_available, status in rows
    }


def _users_blocked_at(
    session: Session, *, tenant_id: str, user_ids: Sequence[uuid.UUID], moment: datetime
) -> set[uuid.UUID]:
    """Users inside an approved window that blocks allocation at ``moment``."""
    if not user_ids:
        return set()
    rows = session.execute(
        select(UserAvailability.user_id).where(
            UserAvailability.tenant_id == tenant_id,
            UserAvailability.user_id.in_(list(user_ids)),
            UserAvailability.availability_type.in_(tuple(BLOCKING_TYPES)),
            UserAvailability.status.in_(tuple(BLOCKING_STATUSES)),
            UserAvailability.start_at <= moment,
            UserAvailability.end_at > moment,
        )
    )
    return {row[0] for row in rows}


def get_queue_by_code(session: Session, *, tenant_id: str, code: str) -> Queue | None:
    return session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == code.upper())
    )


def create_queue(session: Session, *, data: QueueCreate, ctx: WriteContext) -> Queue:
    if data.sla_policy_id is not None:
        _assert_sla_policy(session, ctx.tenant_id, data.sla_policy_id)

    queue = Queue(
        tenant_id=ctx.tenant_id,
        code=data.code.upper(),
        name=data.name,
        source_app=data.source_app,
        department_id=data.department_id,
        description=data.description,
        max_workload_per_user=data.max_workload_per_user,
        priority=data.priority,
        eligible_roles=list(data.eligible_roles) or None,
        required_specialisations=list(data.required_specialisations) or None,
        sla_policy_id=data.sla_policy_id,
        sync_interval_minutes=data.sync_interval_minutes,
        status=OrgStatus.ACTIVE,
    )
    session.add(queue)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            f"A queue with the code '{data.code.upper()}' already exists.",
            code="QUEUE_CODE_TAKEN",
        ) from exc

    ctx.audit(
        session,
        action=AuditAction.QUEUE_CREATED,
        entity_type=EntityType.QUEUE,
        entity_id=queue.id,
        after=_queue_snapshot(queue),
        reason="Queue created",
    )
    session.flush()
    return queue


def update_queue(
    session: Session,
    *,
    tenant_id: str,
    queue_id: uuid.UUID,
    data: QueueUpdate,
    ctx: WriteContext,
) -> Queue:
    queue = get_queue(session, tenant_id=tenant_id, queue_id=queue_id)
    if data.sla_policy_id is not None:
        _assert_sla_policy(session, tenant_id, data.sla_policy_id)
    before = _queue_snapshot(queue)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(queue, field, value)
    ctx.audit(
        session,
        action=AuditAction.QUEUE_UPDATED,
        entity_type=EntityType.QUEUE,
        entity_id=queue.id,
        before=before,
        after=_queue_snapshot(queue),
        reason="Queue updated",
    )
    session.flush()
    return queue


def _assert_sla_policy(session: Session, tenant_id: str, sla_policy_id: uuid.UUID) -> SlaPolicy:
    policy = session.scalar(
        select(SlaPolicy).where(SlaPolicy.id == sla_policy_id, SlaPolicy.tenant_id == tenant_id)
    )
    if policy is None:
        raise NotFoundError("That SLA policy was not found.")
    return policy


def queue_sla_policy(session: Session, *, tenant_id: str, queue: Queue) -> SlaPolicy | None:
    """Resolve the policy for a queue: explicit override first, then default."""
    if queue.sla_policy_id is not None:
        policy = session.scalar(
            select(SlaPolicy).where(
                SlaPolicy.id == queue.sla_policy_id, SlaPolicy.tenant_id == tenant_id
            )
        )
        if policy is not None and policy.is_active:
            return policy
    override = session.scalar(
        select(QueueSlaPolicy)
        .where(
            QueueSlaPolicy.tenant_id == tenant_id,
            QueueSlaPolicy.queue_id == queue.id,
            QueueSlaPolicy.is_active.is_(True),
            QueueSlaPolicy.is_default_for_queue.is_(True),
        )
        .order_by(QueueSlaPolicy.created_at.desc())
    )
    if override is None:
        return None
    return session.scalar(
        select(SlaPolicy).where(
            SlaPolicy.id == override.sla_policy_id, SlaPolicy.tenant_id == tenant_id
        )
    )


def _queue_snapshot(queue: Queue) -> dict[str, Any]:
    return {
        "code": queue.code,
        "name": queue.name,
        "source_app": queue.source_app.value,
        "status": queue.status.value,
        "department_id": str(queue.department_id) if queue.department_id else None,
        "max_workload_per_user": queue.max_workload_per_user,
        "priority": queue.priority,
        "eligible_roles": queue.eligible_roles or [],
        "required_specialisations": queue.required_specialisations or [],
        "sla_policy_id": str(queue.sla_policy_id) if queue.sla_policy_id else None,
        "sync_interval_minutes": queue.sync_interval_minutes,
        "description": queue.description,
    }


# --------------------------------------------------------------------------
# queue memberships
# --------------------------------------------------------------------------
def list_queue_memberships(
    session: Session,
    *,
    tenant_id: str,
    queue_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[QueueMembership], int]:
    query = (
        select(QueueMembership)
        .join(Queue, Queue.id == QueueMembership.queue_id)
        .where(QueueMembership.tenant_id == tenant_id)
    )
    if queue_id is not None:
        query = query.where(QueueMembership.queue_id == queue_id)
    if user_id is not None:
        query = query.where(QueueMembership.user_id == user_id)
    if is_active is not None:
        query = query.where(QueueMembership.is_active.is_(is_active))

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(
        session.scalars(
            query.order_by(Queue.code, QueueMembership.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total


def add_queue_member(
    session: Session, *, data: dict[str, Any], ctx: WriteContext
) -> QueueMembership:
    from app.models.user import User

    get_queue(session, tenant_id=ctx.tenant_id, queue_id=data["queue_id"])
    user = session.scalar(
        select(User).where(User.id == data["user_id"], User.tenant_id == ctx.tenant_id)
    )
    if user is None:
        raise NotFoundError("That user was not found.")

    existing = session.scalar(
        select(QueueMembership).where(
            QueueMembership.tenant_id == ctx.tenant_id,
            QueueMembership.queue_id == data["queue_id"],
            QueueMembership.user_id == data["user_id"],
        )
    )
    if existing is not None and existing.is_active:
        raise ConflictError(
            "That user is already an active member of this queue.", code="ALREADY_QUEUE_MEMBER"
        )

    shift_start = _parse_time(data.get("shift_start"))
    shift_end = _parse_time(data.get("shift_end"))
    if (shift_start is None) != (shift_end is None):
        raise ConflictError(
            "Provide both a shift start and a shift end, or neither.", code="INVALID_SHIFT"
        )

    if existing is not None:
        existing.is_active = True
        existing.capacity = data.get("capacity")
        existing.priority_weight = data.get("priority_weight", 0)
        existing.daily_max_allocations = data.get("daily_max_allocations")
        existing.specialisation = data.get("specialisation")
        existing.shift_start = shift_start
        existing.shift_end = shift_end
        existing.shift_days = data.get("shift_days") or None
        session.flush()
        ctx.audit(
            session,
            action=AuditAction.QUEUE_MEMBERSHIP_UPDATED,
            entity_type=EntityType.QUEUE_MEMBERSHIP,
            entity_id=existing.id,
            after=_membership_snapshot(existing),
            reason="Queue membership reactivated",
        )
        return existing

    membership = QueueMembership(
        tenant_id=ctx.tenant_id,
        queue_id=data["queue_id"],
        user_id=data["user_id"],
        is_active=True,
        capacity=data.get("capacity"),
        priority_weight=data.get("priority_weight", 0),
        daily_max_allocations=data.get("daily_max_allocations"),
        specialisation=data.get("specialisation"),
        shift_start=shift_start,
        shift_end=shift_end,
        shift_days=data.get("shift_days") or None,
    )
    session.add(membership)
    session.flush()
    ctx.audit(
        session,
        action=AuditAction.QUEUE_MEMBERSHIP_CREATED,
        entity_type=EntityType.QUEUE_MEMBERSHIP,
        entity_id=membership.id,
        after=_membership_snapshot(membership),
        reason="Queue membership created",
    )
    return membership


def update_queue_member(
    session: Session,
    *,
    tenant_id: str,
    membership_id: uuid.UUID,
    changes: dict[str, Any],
    ctx: WriteContext,
) -> QueueMembership:
    membership = session.scalar(
        select(QueueMembership).where(
            QueueMembership.id == membership_id, QueueMembership.tenant_id == tenant_id
        )
    )
    if membership is None:
        raise NotFoundError("That queue membership was not found.")
    before = _membership_snapshot(membership)

    for field, value in changes.items():
        if field in {"shift_start", "shift_end"}:
            value = _parse_time(value)
        setattr(membership, field, value)

    ctx.audit(
        session,
        action=AuditAction.QUEUE_MEMBERSHIP_UPDATED,
        entity_type=EntityType.QUEUE_MEMBERSHIP,
        entity_id=membership.id,
        before=before,
        after=_membership_snapshot(membership),
        reason="Queue membership updated",
    )
    session.flush()
    return membership


def _membership_snapshot(membership: QueueMembership) -> dict[str, Any]:
    return {
        "queue_id": str(membership.queue_id),
        "user_id": str(membership.user_id),
        "is_active": membership.is_active,
        "capacity": membership.capacity,
        "priority_weight": membership.priority_weight,
        "daily_max_allocations": membership.daily_max_allocations,
        "specialisation": membership.specialisation,
        "shift_start": membership.shift_start.isoformat() if membership.shift_start else None,
        "shift_end": membership.shift_end.isoformat() if membership.shift_end else None,
        "shift_days": membership.shift_days or [],
    }


def _parse_time(value: str | None) -> dt_time | None:
    if value is None:
        return None
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return dt_time(hour=parts[0], minute=parts[1])
    if len(parts) == 3:
        return dt_time(hour=parts[0], minute=parts[1], second=parts[2])
    raise ConflictError("Shift times must be HH:MM or HH:MM:SS.", code="INVALID_SHIFT")


def utcnow() -> datetime:
    return datetime.now(UTC)
