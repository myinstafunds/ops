"""Work item service: lifecycle, locking, assignment and transfer.

Concurrency notes (PRD 18):

* Every mutating operation starts by taking a ``SELECT ... FOR UPDATE`` row lock
  on the work item row. That serialises two simultaneous lock/assign/transfer
  requests on the same case, so exactly one wins and the other gets a 409.
* The caller is responsible for committing the session; the router does it
  after the audit row has been written, so state and audit land together.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.audit import record
from app.config import settings
from app.errors import (
    CaseLockedError,
    ConflictError,
    NoEligibleUserError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.models.audit import AuditAction, EntityType
from app.models.availability import UserAvailability
from app.models.org import DepartmentMembership, Queue, QueueMembership
from app.models.sla import SlaPolicy, SlaStatus
from app.models.user import OperationalStatus, User
from app.models.work import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    CaseComment,
    EventType,
    Priority,
    WorkItem,
    WorkItemEvent,
    WorkStatus,
)
from app.security.masking import mask_mobile
from app.services import allocation as alloc
from app.services import sla as sla_service
from app.services.org import WriteContext


def utcnow() -> datetime:
    return datetime.now(UTC)


# ==========================================================================
# reads
# ==========================================================================
def _apply_sort(query: Select[Any], *, sort: str | None, descending: bool) -> Select[Any]:
    sortable: dict[str, InstrumentedAttribute[Any]] = {
        "created_at": WorkItem.created_at,
        "updated_at": WorkItem.updated_at,
        "due_at": WorkItem.due_at,
        "priority": WorkItem.priority,
        "status": WorkItem.status,
        "queue_code": WorkItem.queue_code,
        "last_activity_at": WorkItem.last_activity_at,
        "title": WorkItem.title,
    }
    column = sortable.get(sort or "created_at", WorkItem.created_at)
    return query.order_by(column.desc() if descending else column.asc())


def list_work_items(
    session: Session,
    *,
    tenant_id: str,
    source_app: str | None = None,
    queue_code: str | None = None,
    department_code: str | None = None,
    status: WorkStatus | None = None,
    statuses: Sequence[WorkStatus] | None = None,
    assigned_user_id: uuid.UUID | None = None,
    assigned_to_me: uuid.UUID | None = None,
    unassigned: bool = False,
    priority: Priority | None = None,
    sla_state: str | None = None,
    source_key: str | None = None,
    external_ref: str | None = None,
    search: str | None = None,
    overdue_only: bool = False,
    due_today: bool = False,
    locked_by: uuid.UUID | None = None,
    locked_by_me: uuid.UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    age_min_hours: int | None = None,
    offset: int = 0,
    limit: int = 25,
    sort: str | None = "created_at",
    descending: bool = True,
) -> tuple[list[WorkItem], int]:
    query: Select[tuple[WorkItem]] = select(WorkItem).where(WorkItem.tenant_id == tenant_id)

    if source_app:
        query = query.where(WorkItem.source_app == source_app)
    if queue_code:
        query = query.where(WorkItem.queue_code == queue_code.upper())
    if department_code:
        query = query.where(WorkItem.department_code == department_code.upper())
    if status is not None:
        query = query.where(WorkItem.status == status)
    if statuses:
        query = query.where(WorkItem.status.in_(tuple(statuses)))
    if assigned_user_id is not None:
        query = query.where(WorkItem.assigned_user_id == assigned_user_id)
    if assigned_to_me is not None:
        query = query.where(WorkItem.assigned_user_id == assigned_to_me)
    if unassigned:
        query = query.where(WorkItem.assigned_user_id.is_(None))
    if priority is not None:
        query = query.where(WorkItem.priority == priority)
    if sla_state:
        query = query.where(WorkItem.sla_status == sla_state)
    if source_key:
        query = query.where(WorkItem.source_key == source_key)
    if external_ref:
        query = query.where(func.lower(WorkItem.external_ref) == external_ref.strip().lower())
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(WorkItem.title).like(pattern)
            | func.lower(func.coalesce(WorkItem.customer_name, "")).like(pattern)
            | func.lower(func.coalesce(WorkItem.external_ref, "")).like(pattern)
            | func.lower(WorkItem.queue_code).like(pattern)
        )
    now = utcnow()
    if overdue_only:
        query = query.where(
            WorkItem.due_at.isnot(None),
            WorkItem.due_at < now,
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        )
    if due_today:
        end_of_day = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        query = query.where(
            WorkItem.due_at.isnot(None),
            WorkItem.due_at >= now,
            WorkItem.due_at < end_of_day,
        )
    if locked_by is not None:
        query = query.where(WorkItem.locked_by_user_id == locked_by, WorkItem.locked_until > now)
    if locked_by_me is not None:
        query = query.where(WorkItem.locked_by_user_id == locked_by_me, WorkItem.locked_until > now)
    if created_from is not None:
        query = query.where(WorkItem.created_at >= created_from)
    if created_to is not None:
        query = query.where(WorkItem.created_at <= created_to)
    if due_before is not None:
        query = query.where(WorkItem.due_at.isnot(None), WorkItem.due_at <= due_before)
    if due_after is not None:
        query = query.where(WorkItem.due_at.isnot(None), WorkItem.due_at >= due_after)
    if age_min_hours is not None:
        query = query.where(WorkItem.created_at <= now - timedelta(hours=age_min_hours))

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    items = list(
        session.scalars(
            _apply_sort(query, sort=sort, descending=descending).offset(offset).limit(limit)
        )
    )
    return items, total


def get_work_item(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    for_update: bool = False,
) -> WorkItem:
    """Load a work item, optionally holding a row lock for the transaction."""
    query = select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update()
    item = session.scalar(query)
    if item is None:
        # Deliberately a 404: a cross-tenant id must be indistinguishable from
        # a missing one (PRD 48 - wrong tenant -> not found/forbidden).
        raise NotFoundError("Work item was not found.")
    return item


def get_by_source_key(session: Session, *, tenant_id: str, source_key: str) -> WorkItem | None:
    return session.scalar(
        select(WorkItem).where(WorkItem.tenant_id == tenant_id, WorkItem.source_key == source_key)
    )


def search_work_items(
    session: Session,
    *,
    tenant_id: str,
    query: str,
    limit: int = 25,
    viewer_user_id: uuid.UUID | None = None,
) -> tuple[list[WorkItem], int]:
    """Global case search (PRD 31).

    Searching is always tenant scoped, and never restricted to the searcher's
    own work - read permission on cases is the gate, applied by the route.
    """
    term = query.strip()
    if len(term) < 2:
        return [], 0
    pattern = f"%{term.lower()}%"
    statement: Select[tuple[WorkItem]] = select(WorkItem).where(
        WorkItem.tenant_id == tenant_id,
        or_(
            func.lower(WorkItem.title).like(pattern),
            func.lower(func.coalesce(WorkItem.external_ref, "")).like(pattern),
            func.lower(func.coalesce(WorkItem.customer_name, "")).like(pattern),
            func.lower(func.coalesce(WorkItem.customer_mobile_masked, "")).like(pattern),
            func.lower(WorkItem.queue_code).like(pattern),
            func.lower(func.coalesce(WorkItem.assigned_user_name, "")).like(pattern),
            cast(WorkItem.id, String).like(f"%{term}%"),
        ),
    )
    total = (
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )
    items = list(
        session.scalars(
            statement.order_by(WorkItem.last_activity_at.desc().nullslast()).limit(min(limit, 100))
        )
    )
    return items, total


# ==========================================================================
# events
# ==========================================================================
def add_event(
    session: Session,
    *,
    tenant_id: str,
    work_item: WorkItem,
    event_type: EventType,
    actor_user_id: uuid.UUID | None,
    actor_name: str,
    from_value: str | None = None,
    to_value: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> WorkItemEvent:
    from app.logging_config import trace_id_var

    event = WorkItemEvent(
        tenant_id=tenant_id,
        work_item_id=work_item.id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        from_value=from_value,
        to_value=to_value,
        reason=reason,
        metadata_json=metadata,
        trace_id=trace_id_var.get(),
    )
    session.add(event)
    work_item.last_activity_at = utcnow()
    return event


def list_events(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[WorkItemEvent], int]:
    base = select(WorkItemEvent).where(
        WorkItemEvent.tenant_id == tenant_id, WorkItemEvent.work_item_id == work_item_id
    )
    total = session.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    events = list(
        session.scalars(base.order_by(WorkItemEvent.created_at.desc()).offset(offset).limit(limit))
    )
    return events, total


# ==========================================================================
# creation
# ==========================================================================
def create_work_item(
    session: Session,
    *,
    tenant_id: str,
    data: dict[str, Any],
    ctx: WriteContext,
) -> WorkItem:
    queue = session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == data["queue_code"].upper())
    )
    if queue is None:
        raise NotFoundError(f"Queue '{data['queue_code']}' was not found.")
    if queue.status.value != "ACTIVE":
        raise ConflictError(
            f"Queue '{queue.code}' is not accepting new work.", code="QUEUE_INACTIVE"
        )

    source_key = data.get("source_key")
    if source_key:
        existing = get_by_source_key(session, tenant_id=tenant_id, source_key=source_key)
        if existing is not None:
            raise ConflictError(
                "A work item already exists for this source record.",
                code="DUPLICATE_SOURCE_KEY",
                details={"work_item_id": str(existing.id)},
            )

    now = utcnow()
    policy = _sla_policy_for(session, tenant_id=tenant_id, queue=queue)
    due_at = data.get("due_at") or sla_service.compute_due_at(created_at=now, policy=policy)
    mobile = data.get("customer_mobile")

    item = WorkItem(
        tenant_id=tenant_id,
        source_app=data.get("source_app", "OPS"),
        source_key=source_key,
        external_ref=data.get("external_ref"),
        deep_link=data.get("deep_link"),
        title=data["title"],
        customer_name=data.get("customer_name"),
        customer_mobile=mobile,
        customer_mobile_masked=mask_mobile(mobile),
        queue_code=queue.code,
        department_code=_department_code_for(session, tenant_id=tenant_id, queue=queue),
        status=WorkStatus.OPEN,
        priority=data.get("priority", Priority.MEDIUM),
        due_at=due_at,
        sla_policy_id=policy.id if policy else None,
        sla_status=SlaStatus.NOT_STARTED,
        payload_json=data.get("payload") or None,
        last_activity_at=now,
    )
    session.add(item)
    session.flush()

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.CREATED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        to_value=item.queue_code,
        reason="Work item created",
        metadata={
            "source_app": item.source_app.value,
            "due_at": due_at.isoformat() if due_at else None,
        },
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_CREATED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        after=_audit_snapshot(item),
        reason="Work item created",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    return item


# ==========================================================================
# locking (PRD 18)
# ==========================================================================
def acquire_lock(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    ctx: WriteContext,
) -> WorkItem:
    """Claim a work item.

    The row is locked for the duration of the transaction, so two concurrent
    callers cannot both observe "no active lock" and both succeed.
    """
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    now = utcnow()

    if item.status in TERMINAL_STATUSES:
        raise ConflictError("This case is already closed and cannot be locked.", code="CASE_CLOSED")
    lock_expiry = item.active_lock_expiry(now)
    if lock_expiry is not None:
        if item.locked_by_user_id == ctx.actor_user_id:
            # Re-entrant claim by the owner simply extends the lease.
            return _renew_lock(session, tenant_id=tenant_id, item=item, ctx=ctx, now=now)
        raise CaseLockedError(item.locked_by_name or "another user", lock_expiry.isoformat())

    ttl = timedelta(minutes=settings.ops_lock_ttl_minutes)
    item.locked_by_user_id = ctx.actor_user_id
    item.locked_by_name = ctx.actor_name
    item.locked_at = now
    item.locked_until = now + ttl
    item.last_heartbeat_at = now
    item.lock_count += 1
    if item.status is WorkStatus.OPEN and item.assigned_user_id is None:
        # Claiming an unowned case also assigns it to the claimer.
        _apply_assignment(
            session,
            tenant_id=tenant_id,
            item=item,
            user_id=ctx.actor_user_id,
            user_name=ctx.actor_name,
            ctx=ctx,
            manual=False,
            event_type=EventType.AUTO_ASSIGNED,
            reason="Claimed via lock on an unassigned case",
            explanation=None,
        )

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.LOCKED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        to_value=item.locked_until.isoformat(),
        reason="Case locked",
        metadata={"ttl_minutes": settings.ops_lock_ttl_minutes},
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_LOCKED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        after={
            "locked_by_user_id": str(ctx.actor_user_id),
            "locked_until": item.locked_until.isoformat(),
        },
        reason="Case locked",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


def heartbeat(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    ctx: WriteContext,
) -> WorkItem:
    """Extend the lease held by the caller."""
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    now = utcnow()

    if not item.is_locked:
        raise ConflictError(
            "This case is not locked. Lock it before sending heartbeats.",
            code="NOT_LOCKED",
        )
    if item.locked_by_user_id != ctx.actor_user_id:
        raise CaseLockedError(
            item.locked_by_name or "another user",
            item.locked_until.isoformat() if item.locked_until else "",
        )
    if item.locked_until is not None and item.locked_until <= now:
        raise ConflictError(
            "Your lock has expired. Lock the case again to continue.",
            code="LOCK_EXPIRED",
        )

    return _renew_lock(session, tenant_id=tenant_id, item=item, ctx=ctx, now=now)


def _renew_lock(
    session: Session, *, tenant_id: str, item: WorkItem, ctx: WriteContext, now: datetime
) -> WorkItem:
    item.locked_until = now + timedelta(minutes=settings.ops_lock_ttl_minutes)
    item.last_heartbeat_at = now
    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.LOCK_RENEWED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        to_value=item.locked_until.isoformat(),
        reason="Lock heartbeat",
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_HEARTBEAT,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        after={"locked_until": item.locked_until.isoformat(), "last_heartbeat_at": now.isoformat()},
        reason="Lock heartbeat",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


def release_lock(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
    force: bool = False,
) -> WorkItem:
    """Release a lock. ``force`` lets a manager release somebody else's lock."""
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    now = utcnow()

    if not item.is_locked:
        raise ConflictError("This case is not currently locked.", code="NOT_LOCKED")

    is_owner = item.locked_by_user_id == ctx.actor_user_id
    if not is_owner and not (force or ctx.has_permission("OPS_CASES_ASSIGN")):
        raise PermissionDeniedError(
            "Only the lock holder or a manager can release this case.",
            details={"locked_by_name": item.locked_by_name},
        )
    if item.locked_until is not None and item.locked_until <= now:
        raise ConflictError("This lock has already expired.", code="LOCK_EXPIRED")

    previous_holder = item.locked_by_name
    item.locked_by_user_id = None
    item.locked_by_name = None
    item.locked_at = None
    item.locked_until = None

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.RELEASED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=previous_holder,
        reason=reason,
        metadata={"released_by_manager": not is_owner},
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_RELEASED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"locked_by_name": previous_holder},
        after={"locked_by_name": None},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


def sweep_expired_locks(
    session: Session, *, tenant_id: str, now: datetime | None = None, limit: int = 500
) -> list[WorkItem]:
    """Background cleanup: annotate locks that have lapsed (PRD 43).

    The lock columns are cleared so the case becomes claimable again, and an
    event records why. Idempotent - running it twice changes nothing.
    """
    moment = now or utcnow()
    expired = list(
        session.scalars(
            select(WorkItem)
            .where(
                WorkItem.tenant_id == tenant_id,
                WorkItem.locked_by_user_id.isnot(None),
                WorkItem.locked_until.isnot(None),
                WorkItem.locked_until <= moment,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    for item in expired:
        previous_holder = item.locked_by_name
        item.locked_by_user_id = None
        item.locked_by_name = None
        item.locked_at = None
        item.locked_until = None
        add_event(
            session,
            tenant_id=tenant_id,
            work_item=item,
            event_type=EventType.LOCK_EXPIRED,
            actor_user_id=None,
            actor_name="system",
            from_value=previous_holder,
            reason="Lock expired without a heartbeat",
        )
    if expired:
        session.flush()
    return expired


# ==========================================================================
# assignment
# ==========================================================================
def _apply_assignment(
    session: Session,
    *,
    tenant_id: str,
    item: WorkItem,
    user_id: uuid.UUID,
    user_name: str,
    ctx: WriteContext,
    manual: bool,
    event_type: EventType,
    reason: str,
    explanation: str | None,
    previous_user_id: uuid.UUID | None = None,
    previous_user_name: str | None = None,
) -> None:
    now = utcnow()
    item.assigned_user_id = user_id
    item.assigned_user_name = user_name
    item.assigned_at = now
    item.assigned_by_user_id = ctx.actor_user_id
    #: Sync must never overwrite a human decision (PRD Rule 6).
    item.assignment_is_manual = manual
    if explanation is not None:
        item.allocation_explanation = explanation
    _transition(
        session,
        tenant_id=tenant_id,
        item=item,
        new_status=WorkStatus.ASSIGNED,
        ctx=ctx,
        reason=reason,
    )

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=event_type,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=previous_user_name,
        to_value=user_name,
        reason=reason,
        metadata={"explanation": explanation} if explanation else None,
    )


def assign_manually(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> WorkItem:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.status in TERMINAL_STATUSES:
        raise ConflictError("This case is closed and cannot be reassigned.", code="CASE_CLOSED")

    user = _require_assignable_user(
        session, tenant_id=tenant_id, queue_code=item.queue_code, user_id=user_id
    )
    previous_id, previous_name = item.assigned_user_id, item.assigned_user_name

    _apply_assignment(
        session,
        tenant_id=tenant_id,
        item=item,
        user_id=user.id,
        user_name=user.display_name,
        ctx=ctx,
        manual=True,
        event_type=EventType.REASSIGNED if previous_id else EventType.ASSIGNED,
        reason=reason,
        explanation=f"Manually assigned by {ctx.actor_name}: {reason}",
        previous_user_id=previous_id,
        previous_user_name=previous_name,
    )
    # Reassignment invalidates the previous holder's claim.
    if item.locked_by_user_id is not None and item.locked_by_user_id != user.id:
        item.locked_by_user_id = None
        item.locked_by_name = None
        item.locked_at = None
        item.locked_until = None
        add_event(
            session,
            tenant_id=tenant_id,
            work_item=item,
            event_type=EventType.RELEASED,
            actor_user_id=ctx.actor_user_id,
            actor_name=ctx.actor_name,
            reason="Lock released because the case was reassigned",
        )

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_ASSIGNED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"assigned_user_name": previous_name},
        after={"assigned_user_name": user.display_name},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


def unassign(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> WorkItem:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.assigned_user_id is None:
        raise ConflictError("This case is not assigned to anybody.", code="NOT_ASSIGNED")

    previous_name = item.assigned_user_name
    item.assigned_user_id = None
    item.assigned_user_name = None
    item.assigned_at = None
    item.assigned_by_user_id = None
    _transition(
        session, tenant_id=tenant_id, item=item, new_status=WorkStatus.OPEN, ctx=ctx, reason=reason
    )

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.UNASSIGNED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=previous_name,
        reason=reason,
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_UNASSIGNED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"assigned_user_name": previous_name},
        after={"assigned_user_name": None},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


# ==========================================================================
# transfer (PRD 20)
# ==========================================================================
def transfer(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    destination_queue_code: str | None,
    destination_user_id: uuid.UUID | None,
    reason: str,
    ctx: WriteContext,
) -> WorkItem:
    if not reason or not reason.strip():
        from app.errors import ReasonRequiredError

        raise ReasonRequiredError()

    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.status in TERMINAL_STATUSES:
        raise ConflictError("This case is closed and cannot be transferred.", code="CASE_CLOSED")

    now = utcnow()
    lock_expiry = item.active_lock_expiry(now)
    if lock_expiry is not None and not ctx.has_permission("OPS_CASES_ASSIGN"):
        raise CaseLockedError(item.locked_by_name or "another user", lock_expiry.isoformat())

    from_queue = item.queue_code
    from_user_id, from_user_name = item.assigned_user_id, item.assigned_user_name
    before = {"queue_code": from_queue, "assigned_user_name": from_user_name}

    to_queue_code = from_queue
    if destination_queue_code:
        target_queue = session.scalar(
            select(Queue).where(
                Queue.tenant_id == tenant_id, Queue.code == destination_queue_code.upper()
            )
        )
        if target_queue is None:
            raise NotFoundError(f"Queue '{destination_queue_code}' was not found.")
        if target_queue.status.value != "ACTIVE":
            raise ConflictError(
                f"Queue '{target_queue.code}' is not accepting work.", code="QUEUE_INACTIVE"
            )
        to_queue_code = target_queue.code
        item.queue_code = to_queue_code
        item.department_code = _department_code_for(
            session, tenant_id=tenant_id, queue=target_queue
        )

    if destination_user_id is not None:
        user = _require_assignable_user(
            session, tenant_id=tenant_id, queue_code=to_queue_code, user_id=destination_user_id
        )
        _apply_assignment(
            session,
            tenant_id=tenant_id,
            item=item,
            user_id=user.id,
            user_name=user.display_name,
            ctx=ctx,
            manual=True,
            event_type=EventType.ASSIGNED,
            reason=reason,
            explanation=f"Transferred to {user.display_name} by {ctx.actor_name}: {reason}",
            previous_user_id=from_user_id,
            previous_user_name=from_user_name,
        )
    elif destination_queue_code:
        # Queue-only transfer: the work goes back to the pool unassigned.
        item.assigned_user_id = None
        item.assigned_user_name = None
        item.assigned_at = None
        item.assigned_by_user_id = None
        item.assignment_is_manual = True
        _transition(
            session,
            tenant_id=tenant_id,
            item=item,
            new_status=WorkStatus.OPEN,
            ctx=ctx,
            reason=reason,
        )

    # A transfer always releases the previous holder's lock.
    released_holder = item.locked_by_name
    if item.locked_by_user_id is not None:
        item.locked_by_user_id = None
        item.locked_by_name = None
        item.locked_at = None
        item.locked_until = None

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.TRANSFERRED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=f"{from_queue} / {from_user_name or 'unassigned'}",
        to_value=f"{to_queue_code} / {item.assigned_user_name or 'unassigned'}",
        reason=reason,
        metadata={"released_lock_from": released_holder} if released_holder else None,
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_TRANSFERRED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before=before,
        after={"queue_code": item.queue_code, "assigned_user_name": item.assigned_user_name},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


# ==========================================================================
# status / close
# ==========================================================================
def _transition(
    session: Session,
    *,
    tenant_id: str,
    item: WorkItem,
    new_status: WorkStatus,
    ctx: WriteContext,
    reason: str,
) -> None:
    """Move a work item to ``new_status`` if the lifecycle allows it."""
    if item.status is new_status:
        return
    allowed = ALLOWED_TRANSITIONS.get(item.status, frozenset())
    if new_status not in allowed:
        raise ConflictError(
            f"A case cannot move from {item.status.value} to {new_status.value}.",
            code="INVALID_STATE_TRANSITION",
            details={
                "current_status": item.status.value,
                "requested_status": new_status.value,
                "allowed": sorted(item.value for item in allowed),
            },
        )

    previous = item.status
    item.status = new_status

    event_type = {
        WorkStatus.IN_PROGRESS: EventType.STARTED,
        WorkStatus.ON_HOLD: EventType.PLACED_ON_HOLD,
        WorkStatus.CLOSED: EventType.CLOSED,
    }.get(new_status, EventType.STATUS_CHANGED)

    if new_status is WorkStatus.IN_PROGRESS and item.first_response_at is None:
        item.first_response_at = utcnow()

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=event_type,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=previous.value,
        to_value=new_status.value,
        reason=reason,
    )


def change_status(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    new_status: WorkStatus,
    reason: str,
    ctx: WriteContext,
) -> WorkItem:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    before_status = item.status
    _transition(
        session, tenant_id=tenant_id, item=item, new_status=new_status, ctx=ctx, reason=reason
    )
    _refresh_sla(session, tenant_id=tenant_id, item=item)

    if before_status is not new_status:
        record(
            session,
            tenant_id=tenant_id,
            actor_user_id=ctx.actor_user_id,
            actor_name=ctx.actor_name,
            action=AuditAction.WORK_ITEM_STATUS_CHANGED,
            entity_type=EntityType.WORK_ITEM,
            entity_id=item.id,
            before={"status": before_status.value},
            after={"status": item.status.value},
            reason=reason,
            ip_address=ctx.ip_address,
            user_agent=ctx.user_agent,
        )
    session.flush()
    return item


def close_work_item(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    reason: str,
    outcome: str | None,
    notes: str | None,
    ctx: WriteContext,
) -> WorkItem:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.status is WorkStatus.CLOSED:
        raise ConflictError("This case is already closed.", code="ALREADY_CLOSED")

    now = utcnow()
    before_status = item.status
    _transition(
        session,
        tenant_id=tenant_id,
        item=item,
        new_status=WorkStatus.CLOSED,
        ctx=ctx,
        reason=reason,
    )
    item.closed_at = now
    item.closed_by_user_id = ctx.actor_user_id
    item.closure_outcome = outcome
    item.closure_notes = notes
    # Closing releases the lock.
    item.locked_by_user_id = None
    item.locked_by_name = None
    item.locked_at = None
    item.locked_until = None
    _refresh_sla(session, tenant_id=tenant_id, item=item)

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_CLOSED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"status": before_status.value, "locked_by_name": None},
        after={"status": item.status.value, "outcome": outcome},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


def override_due_at(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    due_at: datetime,
    reason: str,
    ctx: WriteContext,
) -> WorkItem:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.status in TERMINAL_STATUSES:
        raise ConflictError("This case is closed; its SLA cannot be changed.", code="CASE_CLOSED")

    before = item.due_at
    item.due_at = due_at
    item.due_at_overridden = True
    item.sla_breached_at = None
    _refresh_sla(session, tenant_id=tenant_id, item=item)

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.DUE_AT_OVERRIDDEN,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=before.isoformat() if before else None,
        to_value=due_at.isoformat(),
        reason=reason,
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_DUE_AT_OVERRIDDEN,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"due_at": before.isoformat() if before else None},
        after={"due_at": due_at.isoformat()},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item


# ==========================================================================
# eligibility and auto allocation
# ==========================================================================
def evaluate_queue(
    session: Session,
    *,
    tenant_id: str,
    queue_code: str,
    required_permission: str | None = None,
    now: datetime | None = None,
) -> tuple[Queue, list[alloc.CandidateResult]]:
    """Score every active member of a queue and explain each outcome."""
    moment = now or utcnow()
    queue = session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == queue_code.upper())
    )
    if queue is None:
        raise NotFoundError(f"Queue '{queue_code}' was not found.")

    memberships = list(
        session.scalars(
            select(QueueMembership)
            .options(
                selectinload(QueueMembership.user).selectinload(User.roles),
            )
            .where(QueueMembership.tenant_id == tenant_id, QueueMembership.queue_id == queue.id)
        )
    )
    if not memberships:
        return queue, []

    user_ids = [m.user_id for m in memberships]
    workloads = _open_workload_counts(session, tenant_id=tenant_id, user_ids=user_ids)
    today_allocations = _daily_allocation_counts(session, tenant_id=tenant_id, user_ids=user_ids)
    unavailable = _unavailable_until(session, tenant_id=tenant_id, user_ids=user_ids, moment=moment)
    departments = _department_ids_by_user(session, tenant_id=tenant_id, user_ids=user_ids)

    results: list[alloc.CandidateResult] = []
    for membership in memberships:
        user = membership.user
        candidate = alloc.CandidateInput(
            user_id=membership.user_id,
            display_name=user.display_name,
            username=user.username,
            membership=membership,
            user=user,
            department_ids=frozenset(departments.get(membership.user_id, set())),
            open_workload=workloads.get(membership.user_id, 0),
            daily_allocations_today=today_allocations.get(membership.user_id, 0),
            unavailable_until=unavailable.get(membership.user_id),
        )
        results.append(
            alloc.evaluate_candidate(
                candidate, queue=queue, required_permission=required_permission, now=moment
            )
        )
    return queue, results


def auto_assign(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    ctx: WriteContext,
) -> tuple[WorkItem, alloc.CandidateResult, list[alloc.CandidateResult]]:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id, for_update=True)
    if item.status in TERMINAL_STATUSES:
        raise ConflictError("This case is closed and cannot be assigned.", code="CASE_CLOSED")

    _, results = evaluate_queue(session, tenant_id=tenant_id, queue_code=item.queue_code)
    chosen = alloc.select(results)
    if chosen is None:
        raise NoEligibleUserError(
            reasons=alloc.rejection_summary(results), queue_code=item.queue_code
        )

    queue = session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == item.queue_code)
    )
    assert queue is not None  # evaluate_queue already proved it exists
    lowest = min(item_.candidate.open_workload for item_ in results if item_.eligible)
    factors = alloc.build_factors(chosen, queue, lowest_workload=lowest)
    explanation = alloc.AllocationDecision(
        chosen=chosen,
        considered=len(results),
        eligible_count=sum(1 for item_ in results if item_.eligible),
        factors=factors,
        rejected_reasons=tuple(alloc.rejection_summary(results)),
    ).explanation()

    previous_id, previous_name = item.assigned_user_id, item.assigned_user_name
    _apply_assignment(
        session,
        tenant_id=tenant_id,
        item=item,
        user_id=chosen.candidate.user_id,
        user_name=chosen.candidate.display_name,
        ctx=ctx,
        manual=False,
        event_type=EventType.AUTO_ASSIGNED,
        reason="Automatic allocation",
        explanation=explanation,
        previous_user_id=previous_id,
        previous_user_name=previous_name,
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_AUTO_ASSIGNED,
        entity_type=EntityType.WORK_ITEM,
        entity_id=item.id,
        before={"assigned_user_name": previous_name},
        after={"assigned_user_name": chosen.candidate.display_name, "manual": False},
        reason=explanation,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return item, chosen, results


# ==========================================================================
# comments (PRD 21)
# ==========================================================================
def add_comment(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    comment: str,
    ctx: WriteContext,
) -> CaseComment:
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=work_item_id)
    _reject_if_comment_looks_sensitive(comment)

    row = CaseComment(
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        author_user_id=ctx.actor_user_id,
        author_name=ctx.actor_name,
        comment=comment.strip(),
    )
    session.add(row)
    session.flush()

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.COMMENTED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        reason="Comment added",
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_COMMENTED,
        entity_type=EntityType.CASE_COMMENT,
        entity_id=row.id,
        after={"work_item_id": str(work_item_id), "length": len(row.comment)},
        reason="Comment added",
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    return row


def edit_comment(
    session: Session,
    *,
    tenant_id: str,
    comment_id: uuid.UUID,
    comment: str,
    edit_reason: str,
    ctx: WriteContext,
) -> CaseComment:
    row = session.scalar(
        select(CaseComment).where(CaseComment.id == comment_id, CaseComment.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError("That comment was not found.")
    if row.is_deleted:
        raise ConflictError("A deleted comment cannot be edited.", code="COMMENT_DELETED")
    if row.author_user_id != ctx.actor_user_id and not ctx.has_permission("OPS_ROLES_WRITE"):
        raise PermissionDeniedError("You can only edit your own comments.")

    _reject_if_comment_looks_sensitive(comment)
    item = get_work_item(session, tenant_id=tenant_id, work_item_id=row.work_item_id)

    if row.original_comment is None:
        row.original_comment = row.comment
    before = row.comment
    row.comment = comment.strip()
    row.edit_count += 1

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.COMMENT_EDITED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        from_value=f"edit {row.edit_count - 1}",
        to_value=f"edit {row.edit_count}",
        reason=edit_reason,
        metadata={"original_preserved": True},
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_COMMENT_EDITED,
        entity_type=EntityType.CASE_COMMENT,
        entity_id=row.id,
        before={"comment": before},
        after={"comment": row.comment},
        reason=edit_reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return row


def delete_comment(
    session: Session,
    *,
    tenant_id: str,
    comment_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> CaseComment:
    """Soft delete - the row and its text remain for audit purposes."""
    if not ctx.has_permission("OPS_ROLES_WRITE"):
        raise PermissionDeniedError("Deleting a comment requires an administrator role.")
    row = session.scalar(
        select(CaseComment).where(CaseComment.id == comment_id, CaseComment.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError("That comment was not found.")
    if row.is_deleted:
        return row

    item = get_work_item(session, tenant_id=tenant_id, work_item_id=row.work_item_id)
    row.deleted_at = utcnow()
    row.deleted_by_user_id = ctx.actor_user_id
    row.delete_reason = reason

    add_event(
        session,
        tenant_id=tenant_id,
        work_item=item,
        event_type=EventType.COMMENT_DELETED,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        reason=reason,
    )
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.WORK_ITEM_COMMENT_DELETED,
        entity_type=EntityType.CASE_COMMENT,
        entity_id=row.id,
        before={"deleted": False},
        after={"deleted": True},
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    session.flush()
    return row


def list_comments(
    session: Session,
    *,
    tenant_id: str,
    work_item_id: uuid.UUID,
    include_deleted: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[CaseComment], int]:
    query = select(CaseComment).where(
        CaseComment.tenant_id == tenant_id, CaseComment.work_item_id == work_item_id
    )
    if not include_deleted:
        query = query.where(CaseComment.deleted_at.is_(None))
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = list(
        session.scalars(query.order_by(CaseComment.created_at.asc()).offset(offset).limit(limit))
    )
    return rows, total


_SENSITIVE_COMMENT_PATTERNS = (
    ("card number", "a full card number"),
    ("cvv", "a CVV"),
    ("otp", "an OTP"),
    ("password", "a password"),
    ("aadhaar", "a full Aadhaar number"),
    ("pin", "a PIN"),
)


def _reject_if_comment_looks_sensitive(comment: str) -> None:
    """Block obvious secret material before it reaches the database.

    This is a guardrail, not a guarantee - reviewers still matter. A comment is
    a permanent, readable record.
    """
    lowered = comment.lower()
    for needle, label in _SENSITIVE_COMMENT_PATTERNS:
        if needle in lowered:
            raise ValidationError(
                f"This comment appears to contain {label}. Remove it and try again.",
                code="COMMENT_CONTAINS_SENSITIVE_DATA",
            )


# ==========================================================================
# helpers
# ==========================================================================
def _require_assignable_user(
    session: Session, *, tenant_id: str, queue_code: str, user_id: uuid.UUID
) -> User:
    user = session.scalar(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id, User.tenant_id == tenant_id)
    )
    if user is None:
        raise NotFoundError("That user was not found.")
    if user.status is not OperationalStatus.ACTIVE:
        raise ConflictError(
            f"{user.display_name} is not an active operational user.", code="USER_INACTIVE"
        )

    queue = session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == queue_code)
    )
    if queue is None:
        raise NotFoundError(f"Queue '{queue_code}' was not found.")

    membership = session.scalar(
        select(QueueMembership).where(
            QueueMembership.tenant_id == tenant_id,
            QueueMembership.queue_id == queue.id,
            QueueMembership.user_id == user_id,
            QueueMembership.is_active.is_(True),
        )
    )
    if membership is None:
        raise ConflictError(
            f"{user.display_name} is not an active member of {queue.code}.",
            code="NOT_QUEUE_MEMBER",
            details={"queue_code": queue.code, "user_id": str(user_id)},
        )
    return user


def _sla_policy_for(session: Session, *, tenant_id: str, queue: Queue) -> SlaPolicy | None:
    from app.services.org import queue_sla_policy

    return queue_sla_policy(session, tenant_id=tenant_id, queue=queue)


def _department_code_for(session: Session, *, tenant_id: str, queue: Queue) -> str | None:
    if queue.department_id is None:
        return None
    from app.models.org import Department

    return session.scalar(
        select(Department.code).where(
            Department.id == queue.department_id, Department.tenant_id == tenant_id
        )
    )


def _refresh_sla(session: Session, *, tenant_id: str, item: WorkItem) -> None:
    queue = session.scalar(
        select(Queue).where(Queue.tenant_id == tenant_id, Queue.code == item.queue_code)
    )
    policy = _sla_policy_for(session, tenant_id=tenant_id, queue=queue) if queue else None
    resolution = sla_service.resolve_for_item(item, policy=policy)
    item.sla_status = resolution.status
    if resolution.breached_at is not None:
        item.sla_breached_at = resolution.breached_at


def _open_workload_counts(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not user_ids:
        return {}
    rows = session.execute(
        select(WorkItem.assigned_user_id, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.assigned_user_id.in_(user_ids),
            WorkItem.status.in_(tuple(OPEN_STATUSES)),
        )
        .group_by(WorkItem.assigned_user_id)
    )
    return {row[0]: row[1] for row in rows}


def _daily_allocation_counts(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Assignments made since midnight UTC."""
    if not user_ids:
        return {}
    midnight = datetime.combine(utcnow().date(), datetime.min.time(), tzinfo=UTC)
    rows = session.execute(
        select(WorkItem.assigned_by_user_id, func.count(WorkItem.id))
        .where(
            WorkItem.tenant_id == tenant_id,
            WorkItem.assigned_by_user_id.in_(user_ids),
            WorkItem.assigned_at >= midnight,
        )
        .group_by(WorkItem.assigned_by_user_id)
    )
    return {row[0]: row[1] for row in rows}


def _unavailable_until(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID], moment: datetime
) -> dict[uuid.UUID, datetime]:
    from app.models.availability import AvailabilityType

    if not user_ids:
        return {}
    rows = session.execute(
        select(UserAvailability.user_id, UserAvailability.end_at)
        .where(
            UserAvailability.tenant_id == tenant_id,
            UserAvailability.user_id.in_(user_ids),
            UserAvailability.status == "APPROVED",
            UserAvailability.availability_type != AvailabilityType.BREAK,
            UserAvailability.start_at <= moment,
            UserAvailability.end_at > moment,
        )
        .order_by(UserAvailability.end_at.desc())
    )
    return {row[0]: row[1] for row in rows}


def _department_ids_by_user(
    session: Session, *, tenant_id: str, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    if not user_ids:
        return {}
    rows = session.execute(
        select(DepartmentMembership.user_id, DepartmentMembership.department_id).where(
            DepartmentMembership.tenant_id == tenant_id,
            DepartmentMembership.user_id.in_(user_ids),
            DepartmentMembership.is_active.is_(True),
        )
    )
    result: dict[uuid.UUID, set[uuid.UUID]] = {}
    for user_id, department_id in rows:
        result.setdefault(user_id, set()).add(department_id)
    return result


def _audit_snapshot(item: WorkItem) -> dict[str, Any]:
    """Non-PII operational snapshot for the audit trail."""
    return {
        "queue_code": item.queue_code,
        "status": item.status.value,
        "priority": item.priority.value,
        "assigned_user_name": item.assigned_user_name,
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "sla_status": item.sla_status.value,
        "source_app": item.source_app.value,
        "external_ref": item.external_ref,
    }
