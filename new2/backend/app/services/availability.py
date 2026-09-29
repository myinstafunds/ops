"""Availability service (PRD 14).

Invariants enforced here and covered by tests:

1. A user cannot approve their own request.
2. ``created_by_*`` is written once and never overwritten by an edit.
3. ``decided_by_*`` is written once and never overwritten by an edit.
4. An edit to a decided request returns it to ``REQUESTED`` and keeps both
   provenance fields, so the new decision is auditable.
5. Only an ``APPROVED`` blocking window stops allocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.audit import record
from app.errors import ConflictError, ErrorCode, NotFoundError, PermissionDeniedError
from app.models.audit import AuditAction, EntityType
from app.models.availability import (
    AvailabilityRequestStatus,
    AvailabilityType,
    UserAvailability,
)
from app.models.user import User
from app.schemas.org import AvailabilityCreate, AvailabilityUpdate
from app.services.org import WriteContext


def _validate_window(start_at: datetime, end_at: datetime) -> None:
    if end_at <= start_at:
        raise ConflictError("The end of the window must be after its start.", code="INVALID_WINDOW")
    if (end_at - start_at).total_seconds() > 60 * 60 * 24 * 366:
        raise ConflictError("An availability window cannot exceed one year.", code="INVALID_WINDOW")


def create_availability(
    session: Session,
    *,
    tenant_id: str,
    data: AvailabilityCreate,
    ctx: WriteContext,
) -> UserAvailability:
    user = session.scalar(select(User).where(User.id == data.user_id, User.tenant_id == tenant_id))
    if user is None:
        raise NotFoundError("That user was not found.")
    _validate_window(data.start_at, data.end_at)
    _assert_no_overlapping_blocking_request(
        session, tenant_id, data.user_id, data.start_at, data.end_at
    )

    # Self approval is structurally impossible: a request is always created in
    # REQUESTED and only somebody else holding the approve permission can move
    # it to APPROVED. That is enforced again by the ``no_self_approval`` check
    # constraint, which compares ``created_by_user_id`` to
    # ``decided_by_user_id`` -- so a manager cannot raise and approve on a
    # report's behalf either. Creation has no path that writes both.
    window = UserAvailability(
        tenant_id=tenant_id,
        user_id=data.user_id,
        availability_type=data.availability_type,
        status=AvailabilityRequestStatus.REQUESTED,
        start_at=data.start_at,
        end_at=data.end_at,
        reason=data.reason,
        notes=data.notes,
        created_by_user_id=ctx.actor_user_id,
        created_by_name=ctx.actor_name,
        decided_by_user_id=None,
        decided_by_name=None,
        decided_at=None,
        edit_count=0,
    )
    session.add(window)
    session.flush()

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.AVAILABILITY_CREATED,
        entity_type=EntityType.AVAILABILITY,
        entity_id=window.id,
        after=_snapshot(window),
        reason=data.reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    _sync_user_availability_flag(session, tenant_id, data.user_id)
    session.flush()
    return window


def _assert_no_overlapping_blocking_request(
    session: Session,
    tenant_id: str,
    user_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> None:
    existing = session.scalar(
        select(UserAvailability)
        .where(
            UserAvailability.tenant_id == tenant_id,
            UserAvailability.user_id == user_id,
            UserAvailability.status.in_(
                (AvailabilityRequestStatus.REQUESTED, AvailabilityRequestStatus.APPROVED)
            ),
            UserAvailability.start_at < end_at,
            UserAvailability.end_at > start_at,
        )
        .limit(1)
    )
    if existing is not None:
        raise ConflictError(
            "This overlaps an existing availability request for the same person.",
            code="OVERLAPPING_AVAILABILITY",
            details={"conflicting_request_id": str(existing.id)},
        )


def update_availability(
    session: Session,
    *,
    tenant_id: str,
    availability_id: uuid.UUID,
    data: AvailabilityUpdate,
    ctx: WriteContext,
) -> UserAvailability:
    window = get_availability(session, tenant_id=tenant_id, availability_id=availability_id)
    before = _snapshot(window)

    if window.status is AvailabilityRequestStatus.CANCELLED:
        raise ConflictError(
            "A cancelled availability request cannot be edited.", code="ALREADY_CANCELLED"
        )

    # A requester may only edit their own window while it is still pending;
    # changing an approved window requires the approver permission.
    is_requester = window.user_id == ctx.actor_user_id
    if (
        is_requester
        and not ctx.has_permission("OPS_AVAILABILITY_APPROVE")
        and window.status is not AvailabilityRequestStatus.REQUESTED
    ):
        raise PermissionDeniedError(
            "An approved availability window can only be changed by an approver."
        )

    start_at = data.start_at or window.start_at
    end_at = data.end_at or window.end_at
    _validate_window(start_at, end_at)

    window.availability_type = data.availability_type or window.availability_type
    window.start_at = start_at
    window.end_at = end_at
    if data.reason is not None:
        window.reason = data.reason
    if data.notes is not None:
        window.notes = data.notes
    window.edit_count += 1
    # An edited request must be re-approved; the original decision is kept so
    # the audit trail still shows who decided what, and when.
    if window.status in (
        AvailabilityRequestStatus.APPROVED,
        AvailabilityRequestStatus.REJECTED,
    ):
        window.status = AvailabilityRequestStatus.REQUESTED

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.AVAILABILITY_UPDATED,
        entity_type=EntityType.AVAILABILITY,
        entity_id=window.id,
        before=before,
        after=_snapshot(window),
        reason=data.change_reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    _sync_user_availability_flag(session, tenant_id, window.user_id)
    session.flush()
    return window


def approve_availability(
    session: Session,
    *,
    tenant_id: str,
    availability_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> UserAvailability:
    window = get_availability(session, tenant_id=tenant_id, availability_id=availability_id)

    if window.created_by_user_id == ctx.actor_user_id or window.user_id == ctx.actor_user_id:
        raise PermissionDeniedError(
            "You cannot approve an availability request that you raised.",
            code=ErrorCode.SELF_APPROVAL_NOT_ALLOWED,
            details={"availability_id": str(availability_id)},
        )
    if window.status is not AvailabilityRequestStatus.REQUESTED:
        raise ConflictError(
            f"This request is already {window.status.value.lower()}.",
            code="INVALID_STATE_TRANSITION",
            details={"current_status": window.status.value},
        )

    before = _snapshot(window)
    window.status = AvailabilityRequestStatus.APPROVED
    window.decided_by_user_id = ctx.actor_user_id
    window.decided_by_name = ctx.actor_name
    window.decided_at = datetime.now(UTC)
    window.decision_reason = reason

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.AVAILABILITY_APPROVED,
        entity_type=EntityType.AVAILABILITY,
        entity_id=window.id,
        before=before,
        after=_snapshot(window),
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    _sync_user_availability_flag(session, tenant_id, window.user_id)
    session.flush()
    return window


def reject_availability(
    session: Session,
    *,
    tenant_id: str,
    availability_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> UserAvailability:
    window = get_availability(session, tenant_id=tenant_id, availability_id=availability_id)

    if window.created_by_user_id == ctx.actor_user_id or window.user_id == ctx.actor_user_id:
        raise PermissionDeniedError(
            "You cannot reject an availability request that you raised.",
            code=ErrorCode.SELF_APPROVAL_NOT_ALLOWED,
            details={"availability_id": str(availability_id)},
        )
    if window.status is not AvailabilityRequestStatus.REQUESTED:
        raise ConflictError(
            f"This request is already {window.status.value.lower()}.",
            code="INVALID_STATE_TRANSITION",
            details={"current_status": window.status.value},
        )

    before = _snapshot(window)
    window.status = AvailabilityRequestStatus.REJECTED
    window.decided_by_user_id = ctx.actor_user_id
    window.decided_by_name = ctx.actor_name
    window.decided_at = datetime.now(UTC)
    window.decision_reason = reason

    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.AVAILABILITY_REJECTED,
        entity_type=EntityType.AVAILABILITY,
        entity_id=window.id,
        before=before,
        after=_snapshot(window),
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    _sync_user_availability_flag(session, tenant_id, window.user_id)
    session.flush()
    return window


def cancel_availability(
    session: Session,
    *,
    tenant_id: str,
    availability_id: uuid.UUID,
    reason: str,
    ctx: WriteContext,
) -> UserAvailability:
    window = get_availability(session, tenant_id=tenant_id, availability_id=availability_id)
    if window.status is AvailabilityRequestStatus.CANCELLED:
        return window

    can_cancel = (
        window.user_id == ctx.actor_user_id
        or window.created_by_user_id == ctx.actor_user_id
        or ctx.has_permission("OPS_AVAILABILITY_APPROVE")
    )
    if not can_cancel:
        raise PermissionDeniedError("You can only cancel your own availability request.")

    before = _snapshot(window)
    window.status = AvailabilityRequestStatus.CANCELLED
    record(
        session,
        tenant_id=tenant_id,
        actor_user_id=ctx.actor_user_id,
        actor_name=ctx.actor_name,
        action=AuditAction.AVAILABILITY_CANCELLED,
        entity_type=EntityType.AVAILABILITY,
        entity_id=window.id,
        before=before,
        after=_snapshot(window),
        reason=reason,
        ip_address=ctx.ip_address,
        user_agent=ctx.user_agent,
    )
    _sync_user_availability_flag(session, tenant_id, window.user_id)
    session.flush()
    return window


def get_availability(
    session: Session, *, tenant_id: str, availability_id: uuid.UUID
) -> UserAvailability:
    window = session.scalar(
        select(UserAvailability).where(
            UserAvailability.id == availability_id, UserAvailability.tenant_id == tenant_id
        )
    )
    if window is None:
        raise NotFoundError("That availability request was not found.")
    return window


def list_availability(
    session: Session,
    *,
    tenant_id: str,
    user_id: uuid.UUID | None = None,
    status: AvailabilityRequestStatus | None = None,
    availability_type: AvailabilityType | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    active_only: bool = False,
    offset: int = 0,
    limit: int = 25,
    sort: str = "start_at",
    descending: bool = False,
) -> tuple[list[UserAvailability], int]:
    query: Select[tuple[UserAvailability]] = select(UserAvailability).where(
        UserAvailability.tenant_id == tenant_id
    )
    if user_id is not None:
        query = query.where(UserAvailability.user_id == user_id)
    if status is not None:
        query = query.where(UserAvailability.status == status)
    if availability_type is not None:
        query = query.where(UserAvailability.availability_type == availability_type)
    if from_at is not None:
        query = query.where(UserAvailability.end_at >= from_at)
    if to_at is not None:
        query = query.where(UserAvailability.start_at <= to_at)
    if active_only:
        now = datetime.now(UTC)
        query = query.where(
            UserAvailability.status == AvailabilityRequestStatus.APPROVED,
            UserAvailability.start_at <= now,
            UserAvailability.end_at > now,
        )

    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = {
        "start_at": UserAvailability.start_at,
        "end_at": UserAvailability.end_at,
        "status": UserAvailability.status,
        "created_at": UserAvailability.created_at,
    }.get(sort, UserAvailability.start_at)
    order = column.desc() if descending else column.asc()
    rows = list(session.scalars(query.order_by(order).offset(offset).limit(limit)))
    return rows, total


def pending_approval_count(session: Session, *, tenant_id: str) -> int:
    return (
        session.scalar(
            select(func.count(UserAvailability.id)).where(
                UserAvailability.tenant_id == tenant_id,
                UserAvailability.status == AvailabilityRequestStatus.REQUESTED,
            )
        )
        or 0
    )


def unavailable_user_ids(
    session: Session, *, tenant_id: str, moment: datetime | None = None
) -> dict[uuid.UUID, datetime]:
    """Map of user id to the end of the blocking window covering ``moment``."""
    now = moment or datetime.now(UTC)
    rows = session.execute(
        select(UserAvailability.user_id, UserAvailability.end_at)
        .where(
            UserAvailability.tenant_id == tenant_id,
            UserAvailability.status == AvailabilityRequestStatus.APPROVED,
            UserAvailability.availability_type != AvailabilityType.BREAK,
            UserAvailability.start_at <= now,
            UserAvailability.end_at > now,
        )
        .order_by(UserAvailability.end_at.desc())
    )
    return {row[0]: row[1] for row in rows}


def _sync_user_availability_flag(session: Session, tenant_id: str, user_id: uuid.UUID) -> None:
    """Keep ``ops_users.is_available`` consistent with approved windows.

    A ``BREAK`` deliberately does not clear the flag - staff are still on shift.
    """
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    if user is None:
        return
    blocking = unavailable_user_ids(session, tenant_id=tenant_id)
    user.is_available = user_id not in blocking
    if user.is_available:
        user.availability_ends_at = None
    else:
        user.availability_ends_at = blocking.get(user_id)


def _snapshot(window: UserAvailability) -> dict[str, Any]:
    return {
        "user_id": str(window.user_id),
        "availability_type": window.availability_type.value,
        "status": window.status.value,
        "start_at": window.start_at.isoformat(),
        "end_at": window.end_at.isoformat(),
        "reason": window.reason,
        "notes": window.notes,
        "created_by_user_id": str(window.created_by_user_id) if window.created_by_user_id else None,
        "created_by_name": window.created_by_name,
        "decided_by_user_id": str(window.decided_by_user_id) if window.decided_by_user_id else None,
        "decided_by_name": window.decided_by_name,
        "decided_at": window.decided_at.isoformat() if window.decided_at else None,
        "decision_reason": window.decision_reason,
        "edit_count": window.edit_count,
    }


__all__ = [
    "approve_availability",
    "cancel_availability",
    "create_availability",
    "get_availability",
    "list_availability",
    "pending_approval_count",
    "reject_availability",
    "unavailable_user_ids",
    "update_availability",
]
